"""表示層（build.py）の純関数の回帰テスト。

なぜ要るか
----------
記号バッジ（✓※†）とURL短縮は **文字列置換** で実装している。置換の条件が
1文字ずれると、SVG の中まで書き換えて図を壊す・書き手が付けたラベルを
上書きする、といった崩れが黙って起きる。ここは置換の**前提条件**を
文字列だけで固定する（ブラウザもビルドも要らない）。

実行:
  python tests/test_display.py
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import build as B  # noqa: E402
import report as R  # noqa: E402


# =============================================================================
# 記号バッジ（mark_badges）
# =============================================================================

def test_mark_badges_wraps_each_mark():
    """✓※† それぞれが、意味の title を持つバッジになる。"""
    out = B.mark_badges("自己資本比率は17.8%✓（3Q時点は9.9%※）、売上†")
    assert '<span class="vm vm-ok" title="' in out and ">✓</span>" in out
    assert '<span class="vm vm-ref" title="' in out and ">※</span>" in out
    assert '<span class="vm vm-pri" title="' in out and ">†</span>" in out
    # title は表記規約の「意味」そのもの
    assert "2ソース照合済みの採用値（status OK）" in out
    assert "未照合・参考値" in out
    assert "決算短信（一次情報）から機械抽出" in out


def test_mark_badges_leaves_plain_text_alone():
    out = B.mark_badges("<p>記号の無い本文は変えない</p>")
    assert out == "<p>記号の無い本文は変えない</p>"


def test_badges_do_not_reach_inside_svg():
    """置換は expand_charts の**前**。あとから差し込まれる SVG は素通しになる。

    SVG の <text> に <span> が入ると描画が壊れる。to_html の適用順が
    入れ替わると、この不変条件が最初に破れる。
    """
    class _Res:  # chartdata.Resolved の必要最小限
        spec = {"type": "bar", "caption": "参考※",
                "data": [{"label": "照合✓", "value": 1.0}]}
        empty_reason = ""
    out = B.to_html("本文の記号✓は着せる。\n\n{{chart:x}}", {"x": _Res()})
    assert '<span class="vm vm-ok"' in out, "本文の記号がバッジになっていない"
    svg = out[out.index("<svg"):out.index("</figure>")]
    assert "<span" not in svg, "SVG・図キャプションの中まで置換している"
    assert "照合✓" in svg and "参考※" in svg, "図の中の文字は素のまま残す"


# =============================================================================
# URL短縮（short_link_label / shorten_autolinks）
# =============================================================================

def test_short_link_label_known_hosts():
    cases = {
        "https://kabutan.jp/stock/?code=4073": "株探",
        "https://s.kabutan.jp/stocks/4073/": "株探",
        "https://minkabu.jp/stock/4073": "みんかぶ",
        "https://irbank.net/E34934": "IR BANK",
        "https://release.tdnet.info/inbs/x.pdf": "TDnet",
        "https://www.release.tdnet.info/inbs/x.pdf": "TDnet",
        "https://www.nikkei.com/nkd/company/?scode=4073": "日経",
    }
    for url, want in cases.items():
        got = B.short_link_label(url)
        assert got == want, f"{url}: {got!r} != {want!r}"


def test_short_link_label_falls_back_to_host():
    """未知のホストは www を除いたホスト名（黙って「出典」等に丸めない）。"""
    assert B.short_link_label("https://www.example.co.jp/ir/") == "example.co.jp"
    assert B.short_link_label("https://prtimes.jp/main/html/rd/p/1.html") == "prtimes.jp"


def test_shorten_autolinks_only_bare_urls():
    """テキストが URL そのもののリンクだけ短縮する。href は変えない。"""
    url = "https://kabutan.jp/news/?b=k2026", "&amp;c=1"
    href = url[0] + url[1]
    src = f'<p><a href="{href}">{href}</a></p>'
    out = B.shorten_autolinks(src)
    assert f'href="{href}"' in out, "href を書き換えている"
    assert ">株探 ↗</a>" in out
    assert 'class="ext"' in out and 'target="_blank"' in out and "noopener" in out

    # 書き手がラベルを付けたリンクには触らない
    labeled = '<p><a href="https://kabutan.jp/">決算ページ</a></p>'
    assert B.shorten_autolinks(labeled) == labeled


# =============================================================================
# 週次アップデートの折りたたみ（render_updates）
# =============================================================================

def _rep_with_weeks(n: int) -> R.Report:
    body = "\n".join(f"### 2026-W{30 + i:02d}（8月）\n\n第{i}週の本文。\n"
                     for i in range(n))
    return R.Report(code="0000", meta={}, sections={"updates": body})


def test_render_updates_folds_older_weeks():
    """新しい3件は開いたまま。4件目以降は details に畳む（消さない）。"""
    out = B.render_updates(_rep_with_weeks(5), "", {})
    assert out.count('<div class="upd">') == 5, "畳んでも件数は減らない"
    assert '<details class="upd-old">' in out
    assert "それ以前の週次アップデート（2件）" in out
    # 新しい2週は details の外、古い2週は中にある
    head, _, tail = out.partition("<details")
    assert "2026-W34" in head and "2026-W33" in head and "2026-W32" in head
    assert "2026-W31" in tail and "2026-W30" in tail


def test_render_updates_few_weeks_no_fold():
    out = B.render_updates(_rep_with_weeks(3), "", {})
    assert "<details" not in out, "3件以下なら畳まない"


# =============================================================================
# 判定タイル（load_stamp）
# =============================================================================

def test_load_stamp_reads_json_and_tolerates_absence():
    saved = B.STAMPS
    try:
        with tempfile.TemporaryDirectory(prefix="kabu-display-") as td:
            path = Path(td) / "stamps.json"
            B.STAMPS = path
            assert B.load_stamp("4073") is None, "ファイルが無ければ None"
            path.write_text(json.dumps({"4073": "見送(トレンド)"}),
                            encoding="utf-8")
            assert B.load_stamp("4073") == "見送(トレンド)"
            assert B.load_stamp("9999") is None, "載っていない銘柄は None"
            path.write_text("{ broken", encoding="utf-8")
            assert B.load_stamp("4073") is None, "壊れた JSON でも落とさない"
    finally:
        B.STAMPS = saved


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
    print(f"\n{len(tests) - len(failed)}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
