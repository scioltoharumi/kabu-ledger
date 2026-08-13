"""改行コードの回帰を止める（CRLF の「幻の差分」を作らない）。

なぜ要るか
----------
Windows の `Path.write_text(..., encoding="utf-8")` は `\\n` を `\\r\\n` に変換する。
すると index の stat サイズと作業ツリーのサイズがズレ、`git status` が
docs/*.html を「全行変更」に見せる（`git diff` は空）。実際に原因調査で1往復した。
CLAUDE.md「生成」節の「git diff が『先週から何が変わったか』そのものになる」を
守るには、書き込み側で改行を固定するしかない。

見るもの
--------
1. src/*.py のテキスト書き込みに `newline=` が付いているか（AST で見る。
   文字列検索だと引数の途中改行を取りこぼす）
2. 生成済みの docs/**/*.html に CRLF が無いか
   → **これはローカル専用の網**。CI では .gitattributes の `eol=lf` により
     checkout 時点で必ず LF であり、かつ test ジョブは build の前に走るので
     実質デッドコード。scoring/*.yaml や *.json には広げない
     （現状 CRLF のまま残っているファイルがあり、広げると手元でいきなり赤くなる）。

実行:
  $env:PYTHONIOENCODING = "utf-8"; python tests/test_eol.py
"""
from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"


def _offenders() -> list[str]:
    bad: list[str] = []
    for py in sorted(SRC.glob("*.py")):
        tree = ast.parse(py.read_text(encoding="utf-8"), filename=str(py))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = node.func.attr if isinstance(node.func, ast.Attribute) else (
                node.func.id if isinstance(node.func, ast.Name) else "")
            has_nl = any(kw.arg == "newline" for kw in node.keywords)
            if name == "write_text":
                if not has_nl:
                    bad.append(f"{py.name}:{node.lineno} write_text")
                continue
            if name == "open":
                mode = ""
                for i, a in enumerate(node.args):
                    if isinstance(a, ast.Constant) and isinstance(a.value, str):
                        # Path.open(mode) は第1引数、組み込み open(path, mode) は第2引数
                        if (isinstance(node.func, ast.Attribute) and i == 0) or i == 1:
                            mode = a.value
                for kw in node.keywords:
                    if kw.arg == "mode" and isinstance(kw.value, ast.Constant):
                        mode = str(kw.value.value)
                if ("w" in mode or "a" in mode or "+" in mode) and "b" not in mode:
                    if not has_nl:
                        bad.append(f"{py.name}:{node.lineno} open({mode!r})")
    return bad


def test_write_calls_pin_newline() -> None:
    off = _offenders()
    assert not off, "newline= が無いテキスト書き込み: " + ", ".join(off)


def test_generated_files_are_lf() -> None:
    docs = ROOT / "docs"
    if not docs.exists():
        print("    (docs/ が無いのでスキップ)")
        return
    bad = [str(p.relative_to(ROOT)) for p in sorted(docs.rglob("*.html"))
           if b"\r\n" in p.read_bytes()]
    assert not bad, "docs/ に CRLF の生成物がある: " + ", ".join(bad)


def main() -> int:
    tests = [("write 系は newline を固定する", test_write_calls_pin_newline),
             ("生成済み docs/*.html は LF", test_generated_files_are_lf)]
    failed = []
    for name, fn in tests:
        try:
            fn()
            print(f"  PASS  {name}")
        except AssertionError as e:
            failed.append(name)
            print(f"  FAIL  {name}: {e}")
        except Exception as e:  # noqa: BLE001
            failed.append(name)
            print(f"  ERROR {name}: {type(e).__name__}: {e}")
    print(f"\n{len(tests) - len(failed)}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
