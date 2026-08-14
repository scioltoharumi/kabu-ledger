"""公開の到達確認（クロスプラットフォーム版）。

`tools/published.ps1` と同じことを標準ライブラリだけで行う。
**クラウドのルーティンは Linux なので PowerShell 版が使えない。**
完了条件が実行できないと「push した＝公開された」に逆戻りするので、こちらを使う。

見るもの（2つとも満たして初めて PUBLISHED）:
  1. live の実バイトから計算した **git blob SHA-1** が、HEAD の blob SHA と一致するか
     → CDN が古いものを返していれば一致しない
  2. 今回入れた印（marker）が live に出ているか
     → 1 だけだと「docs/ を再生成せずに src/ だけ push した」を見逃す
        （その場合 live も HEAD も同じ古い内容なので blob は一致してしまう）

`gh` に依存しない。比較元は**手元の git checkout の HEAD**なので、
push 済みであることが前提（push 前に実行すると当然ずれる）。

使い方:
    python tools/published.py --marker 2026-W34
    python tools/published.py --marker prose-table --retry 8 --wait 60

終了コード: 0=PUBLISHED / 1=未到達 / 2=実行できなかった
"""
from __future__ import annotations

import argparse
import hashlib
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASE = "https://scioltoharumi.github.io/kabu-ledger/"
UA = "kabu-ledger/1.0 (personal research; contact via repo issues)"


def git(*args: str) -> str:
    r = subprocess.run(["git", *args], cwd=ROOT, capture_output=True,
                       text=True, encoding="utf-8", errors="replace")
    return r.stdout.strip() if r.returncode == 0 else ""


def blob_sha(data: bytes) -> str:
    """git が付けるのと同じ SHA-1（"blob <長さ>\\0" を前置してから取る）。"""
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def head_blobs() -> dict[str, str]:
    """HEAD の docs/**/*.html を {リポジトリからの相対パス: blob sha} で返す。"""
    out: dict[str, str] = {}
    for line in git("ls-tree", "-r", "HEAD", "docs").splitlines():
        # 例: 100644 blob 1a2b3c...\tdocs/index.html
        meta, _, path = line.partition("\t")
        parts = meta.split()
        if len(parts) == 3 and parts[1] == "blob" and path.endswith(".html"):
            out[path] = parts[2]
    return out


def fetch(url: str) -> bytes:
    req = urllib.request.Request(url, headers={
        "User-Agent": UA, "Cache-Control": "no-cache", "Pragma": "no-cache"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return r.read()


def probe(blobs: dict[str, str], marker: str) -> tuple[int, int, list[str]]:
    bad = 0
    hits = 0
    lines: list[str] = []
    for path in sorted(blobs):
        rel = path[len("docs/"):]
        try:
            body = fetch(BASE + rel)
        except (urllib.error.URLError, OSError) as e:      # noqa: PERF203
            bad += 1
            lines.append(f"{rel:<18} ERROR  {e}")
            continue
        live = blob_sha(body)
        ok = live == blobs[path]
        if not ok:
            bad += 1
        n = len(re.findall(re.escape(marker),
                           body.decode("utf-8", errors="replace")))
        hits += n
        lines.append(f"{rel:<18} {'OK' if ok else 'STALE':<6} "
                     f"live={live[:8]} head={blobs[path][:8]} {marker}={n}")
    return bad, hits, lines


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--marker", required=True,
                    help="今回入れた印。既定値は置かない（渡し忘れると素通りするため）")
    ap.add_argument("--min-hits", type=int, default=1)
    ap.add_argument("--retry", type=int, default=4)
    ap.add_argument("--wait", type=int, default=15)
    args = ap.parse_args()

    blobs = head_blobs()
    if not blobs:
        print("HEAD:docs に .html が1つも無い（git 管理下で実行しているか確認）")
        return 2

    bad, hits, lines = 0, 0, []
    for i in range(args.retry + 1):
        bad, hits, lines = probe(blobs, args.marker)
        if bad == 0:
            break
        if i < args.retry:
            print(f"STALE {bad}/{len(blobs)} ページ。CDN を待って再試行 "
                  f"({i + 1}/{args.retry})")
            time.sleep(args.wait)

    for line in lines:
        print(line)

    if bad:
        print(f"NOT PUBLISHED: {bad} / {len(blobs)} ページが HEAD と違う。"
              "公開がまだ走っていないか、push していない")
        return 1
    if hits < args.min_hits:
        print(f"MISSING: live は HEAD と一致しているが marker "
              f"'{args.marker}' が {hits} 件（要 {args.min_hits} 件以上）。"
              "docs/ を再生成せずに src/ だけ push した疑い")
        return 1
    print(f"PUBLISHED: 全 {len(blobs)} ページが HEAD と一致 / "
          f"marker '{args.marker}' {hits} 箇所")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
