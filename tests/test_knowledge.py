"""前提知識ページ（docs/knowledge.html）の回帰テスト。

見るもの:
  1. knowledge/*.html が読めて、カテゴリが最大3階層に収まっている
  2. ページ生成が決定論的（D8。2回生成してバイト一致）で、
     全記事が :target 用の一意な id を持つ
  3. 「記事を隠す/出す」CSS が存在する。この規則が消えると全記事が
     常時表示（または常時非表示）になるが、HTML 構造は壊れないので
     他のテストは反応しない
  4. 共通ナビに「前提知識」がある（リンクが無いと誰にも辿り着けない）

実行:
  python tests/test_knowledge.py
"""
from __future__ import annotations

import re
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import build as B  # noqa: E402
from style import CSS  # noqa: E402

_TMPDIRS: list[Path] = []


def _tmpdocs() -> Path:
    d = Path(tempfile.mkdtemp(prefix="kb-test-"))
    _TMPDIRS.append(d)
    return d


def test_articles_load_and_categories_are_bounded():
    arts = B.load_knowledge()
    assert arts, "knowledge/ に記事が1本も無い"
    slugs = [a["slug"] for a in arts]
    assert len(slugs) == len(set(slugs)), f"slug が重複している: {slugs}"
    for a in arts:
        assert a["title"], f"{a['slug']}: title が空"
        assert 1 <= len(a["category"]) <= 3, (
            f"{a['slug']}: カテゴリは1〜3階層（実際 {a['category']}）")
        assert a["body"].strip(), f"{a['slug']}: 本文が空"


def test_page_is_deterministic_and_has_all_articles():
    orig = B.DOCS
    try:
        B.DOCS = _tmpdocs()
        B.build_knowledge_page("2026-08-29")
        first = (B.DOCS / "knowledge.html").read_bytes()
        B.build_knowledge_page("2026-08-29")
        second = (B.DOCS / "knowledge.html").read_bytes()
    finally:
        B.DOCS = orig
    assert first == second, "同じ入力から違う knowledge.html が出た（D8違反）"

    text = first.decode("utf-8")
    for a in B.load_knowledge():
        anchor = f'id="kb-{a["slug"]}"'
        n = text.count(anchor)
        assert n == 1, f"{a['slug']}: 記事アンカーが {n} 個（1個であること）"
        link = f'href="#kb-{a["slug"]}"'
        assert link in text, f"{a['slug']}: 左カラムからのリンクが無い"


def test_css_can_toggle_articles():
    assert re.search(r"\.kb-article\s*\{[^}]*display\s*:\s*none", CSS), \
        "記事を隠す規則（.kb-article{display:none}）が CSS に無い"
    assert re.search(r"\.kb-article:target\s*\{[^}]*display\s*:\s*block", CSS), \
        "記事を出す規則（.kb-article:target）が CSS に無い"
    assert re.search(r"\.kb-main:has\(\.kb-article:target\)\s+\.kb-home", CSS), \
        "記事選択時に一覧（.kb-home）を隠す規則が CSS に無い"


def test_nav_links_to_knowledge():
    hrefs = [href for href, _label in B.NAV_ITEMS]
    assert "knowledge.html" in hrefs, "共通ナビに knowledge.html が無い"
    assert "前提知識" in B.site_header(), "ヘッダーに「前提知識」リンクが出ていない"


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
