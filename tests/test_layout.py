"""台帳の「表が横に伸びる」崩れを、ブラウザ無しで押さえる回帰テスト。

なぜ要るか
----------
`td:first-child{white-space:nowrap}` が、1列目に 200〜280 字の概要文が入る
一覧表にも当たり、表が数千px幅に伸びた。**432 本のテストは1本も反応しなかった**
（CSS も生成 HTML の構造も、どのテストも見ていなかった）。
崩れの確認は headless Edge のスクショに頼っていたが、CI（ubuntu-latest）には
ブラウザが無く、ローカルでも 5 回失敗した。

ここで見るのは「レンダリング結果」ではなく **崩れの前提条件**である。
  1. 折り返し禁止の規則を置いたなら、本文列を折り返し可に戻す規則も必ず置く
  2. 長い本文が入る表には、折り返し可のクラス（prose-table / list-table）が付く

どちらも文字列と HTML 構造だけで判定できる。ブラウザは要らない。

実行:
  $env:PYTHONIOENCODING = "utf-8"; python tests/test_layout.py
"""
from __future__ import annotations

import re
import shutil
import sys
import tempfile
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from style import CSS  # noqa: E402

# 1セルに入る本文の長さ。これを超えたら「短い見出し列」ではない。
LONG_CELL = 60



def wrapping_classes() -> set[str]:
    """CSS 側で「td を折り返す」と宣言しているクラス名を CSS から引く。

    クラス名をテストに書き写すと、CSS で改名した瞬間に検査が黙って
    無効になる（`.prose-table` を消しても素通りする）。CSS を正とする。
    """
    return {m.group(1) for m in
            re.finditer(r"\.([\w-]+)\s+td\s*\{[^}]*white-space\s*:\s*normal",
                        CSS)}

_TMPDIRS: list[Path] = []


# =============================================================================
# 1. CSS の不変条件（ビルド不要・0ms）
# =============================================================================

def test_nowrap_rule_has_a_matching_escape_hatch():
    """1列目の折り返し禁止を置くなら、本文列を戻す規則を必ず併置する。"""
    if not re.search(r"td:first-child\s*\{[^}]*white-space\s*:\s*nowrap", CSS):
        return  # 折り返し禁止そのものが無いなら、この不変条件は不要
    m = re.search(r"\.prose-table\s+td\s*\{([^}]*)\}", CSS)
    assert m, ("td:first-child に white-space:nowrap を置きながら、"
               ".prose-table td で折り返しを戻す規則が無い。"
               "本文が1列目に来る表が数千px幅に伸びる")
    assert "white-space" in m.group(1) and "normal" in m.group(1), \
        f".prose-table td に white-space:normal が無い: {m.group(1)!r}"


def test_fixed_column_widths_have_table_layout_fixed():
    """列幅指定は table-layout:fixed が無いと効かない（指定だけ残る事故を防ぐ）。"""
    if not re.search(r"\.list-table\s+(?:th|td):nth-child\(\d+\)[^}]*width", CSS):
        return
    assert re.search(r"\.list-table\s*\{[^}]*table-layout\s*:\s*fixed", CSS), \
        ".list-table に列幅を指定しているのに table-layout:fixed が無い"


# =============================================================================
# 2. 生成 HTML の不変条件（build を1回だけ回す）
# =============================================================================

class _Tables(HTMLParser):
    """<table> ごとに (class, 1列目セルの最長テキスト長) を集める。

    見るのは**1列目だけ**。`td:first-child{white-space:nowrap}` が当たるのは
    そこだけであり、2列目以降の長文は既定で折り返す。全列を見ると
    正常な表まで FAIL になり、毎週ビルドを止める検査になってしまう。
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tables: list[tuple[str, int]] = []
        self._stack: list[list] = []   # [class, 1列目の最長長, 現在の列番号]
        self._in_first_td = False
        self._cell = 0

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "table":
            self._stack.append([a.get("class") or "", 0, 0])
        elif tag == "tr" and self._stack:
            self._stack[-1][2] = 0
        elif tag == "td" and self._stack:
            self._stack[-1][2] += 1
            self._in_first_td = self._stack[-1][2] == 1
            self._cell = 0

    def handle_endtag(self, tag):
        if tag == "td" and self._stack and self._in_first_td:
            self._stack[-1][1] = max(self._stack[-1][1], self._cell)
            self._in_first_td = False
        elif tag == "table" and self._stack:
            cls, mx, _ = self._stack.pop()
            self.tables.append((cls, mx))

    def handle_data(self, data):
        if self._in_first_td:
            self._cell += len(data.strip())


def _built_docs() -> Path:
    """実リポジトリを一時ディレクトリに複製し、docs/ を1回だけ生成する。"""
    base = Path(tempfile.mkdtemp(prefix="kabu-layout-"))
    _TMPDIRS.append(base)
    shutil.copytree(ROOT / "data", base / "data")
    for name in ("reports", "predictions", "scoring", "theses", "bear", "src"):
        if (ROOT / name).exists():
            shutil.copytree(ROOT / name, base / name,
                            ignore=shutil.ignore_patterns("__pycache__"))
    (base / "docs").mkdir(exist_ok=True)

    import build as B
    import chartdata as CD
    import judge as J
    import report as R
    import verification as VF
    saved = []
    for mod, attrs in ((J, {"ROOT": ""}),
                       (B, {"ROOT": "", "DOCS": "docs",
                            "STAMPS": "scoring/stamps.json"}),
                       (R, {"ROOT": "", "REPORTS": "reports"}),
                       (CD, {"ROOT": "", "DATA": "data"}), (VF, {"ROOT": ""})):
        for attr, rel in attrs.items():
            if hasattr(mod, attr):
                saved.append((mod, attr, getattr(mod, attr)))
                setattr(mod, attr, base / rel if rel else base)
    try:
        assert B.main() == 0, "build.main() が 0 を返さない"
    finally:
        for mod, attr, old in saved:
            setattr(mod, attr, old)
    return base / "docs"


def test_tables_with_long_text_can_wrap():
    """長い本文を持つ表には、折り返しを許すクラスが必ず付く。"""
    wrapping = wrapping_classes()
    assert wrapping, "CSS に「td を折り返す」クラスが1つも無い"
    docs = _built_docs()
    bad: list[str] = []
    for page in sorted(docs.rglob("*.html")):
        p = _Tables()
        p.feed(page.read_text(encoding="utf-8"))
        for i, (cls, mx) in enumerate(p.tables):
            if mx <= LONG_CELL:
                continue
            if not (set(cls.split()) & wrapping):
                bad.append(f"{page.name} の {i+1} 番目の表: "
                           f"最長セル {mx}字 / class={cls!r}")
    assert not bad, ("折り返せない表に長い本文が入っている（横に伸びる）:\n  "
                     + "\n  ".join(bad))


# =============================================================================
# 実行
# =============================================================================

def main() -> int:
    tests = [(n, f) for n, f in sorted(globals().items())
             if n.startswith("test_") and callable(f)]
    failed = []
    for name, fn in tests:
        try:
            fn()
            print(f"  PASS  {name}")
        except AssertionError as e:
            failed.append((name, str(e)))
            print(f"  FAIL  {name}: {e}")
        except Exception as e:  # noqa: BLE001
            failed.append((name, f"{type(e).__name__}: {e}"))
            print(f"  ERROR {name}: {type(e).__name__}: {e}")
    for d in _TMPDIRS:
        shutil.rmtree(d, ignore_errors=True)
    print(f"\n{len(tests) - len(failed)}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
