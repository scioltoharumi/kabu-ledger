"""ローカル専用の並列テストランナー（案3の実装。weekly.yml は触らない）。

  python tools/run_tests.py            # 全部
  python tools/run_tests.py --shards 8 # test_checks.py の分割数

CI は従来どおり `for f in tests/test_*.py; do python "$f"; done` を直列で回す。
このファイルは tests/ の外に置くので CI の glob には拾われない。
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TESTS = ROOT / "tests"
SPLIT = "test_checks.py"          # 1本で全体の8割を占めるので分割する

_SHARD_SRC = r'''
import importlib.util, shutil, sys
from pathlib import Path
ROOT = Path(sys.argv[1]); k = int(sys.argv[2]); n = int(sys.argv[3])
sys.path.insert(0, str(ROOT / "src")); sys.path.insert(0, str(ROOT / "tests"))
spec = importlib.util.spec_from_file_location("tc", ROOT / "tests" / "%s")
tc = importlib.util.module_from_spec(spec); spec.loader.exec_module(tc)
tests = [(nm, fn) for nm, fn in sorted(vars(tc).items())
         if nm.startswith("test_") and callable(fn)]
mine = [t for i, t in enumerate(tests) if i %% n == k]
failed = []
for nm, fn in mine:
    try:
        fn()
    except BaseException as e:
        failed.append(nm); print(f"FAIL {nm}: {type(e).__name__}: {e}")
for d in getattr(tc, "_TMPDIRS", []):
    shutil.rmtree(d, ignore_errors=True)
print(f"shard {k}/{n}: {len(mine)-len(failed)}/{len(mine)} passed")
sys.exit(1 if failed else 0)
''' % SPLIT


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--shards", type=int, default=8)
    ap.add_argument("--jobs", type=int, default=os.cpu_count() or 4)
    args = ap.parse_args()

    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    jobs: list[tuple[str, list[str]]] = []
    for f in sorted(TESTS.glob("test_*.py")):
        if f.name == SPLIT:
            for k in range(args.shards):
                jobs.append((f"{f.name}[{k}]",
                             [sys.executable, "-c", _SHARD_SRC,
                              str(ROOT), str(k), str(args.shards)]))
        else:
            jobs.append((f.name, [sys.executable, str(f)]))

    # 重いジョブから投入する（全体所要は最後に走る重いジョブに律速される。
    # 軽いテストが先にワーカーを埋めると、重い test_checks のシャードが
    # 終盤に回って尻尾が伸びる）。sort は安定なので同順位内は辞書順のまま。
    order = {SPLIT: 0, "test_data_advance.py": 1}
    jobs.sort(key=lambda j: order.get(j[0].split("[")[0], 2))

    # 出力は **一時ファイル**に落とす。subprocess.PIPE のまま poll() で待つと、
    # 子がパイプバッファ（Windows は数KB）を埋めた時点で書き込みブロックし、
    # 親は poll() が None を返し続けて**両方止まる**（実測でデッドロックした）。
    tmp = Path(tempfile.mkdtemp(prefix="kabu-run-"))
    t0 = time.perf_counter()
    running: list[tuple[str, subprocess.Popen, object, Path]] = []
    pending = list(jobs)
    results: list[tuple[str, int, str]] = []
    try:
        while pending or running:
            while pending and len(running) < args.jobs:
                name, cmd = pending.pop(0)
                log = tmp / (name.replace("[", "_").replace("]", "") + ".log")
                fh = log.open("wb")
                running.append((name, subprocess.Popen(
                    cmd, cwd=ROOT, env=env, stdout=fh,
                    stderr=subprocess.STDOUT), fh, log))
            time.sleep(0.05)
            for item in list(running):
                name, p, fh, log = item
                if p.poll() is None:
                    continue
                running.remove(item)
                fh.close()
                results.append((name, p.returncode,
                                log.read_text("utf-8", errors="replace")))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    bad = [r for r in results if r[1] != 0]
    for name, code, out in sorted(results):
        print(f"{name:<24} exit={code}")
        if code != 0:
            print(out[-2000:])
    print(f"\n{len(results)-len(bad)}/{len(results)} プロセスが exit=0  "
          f"{time.perf_counter()-t0:.2f}s")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
