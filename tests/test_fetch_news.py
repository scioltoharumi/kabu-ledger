"""fetch_news.py のパースと日付フィルタの回帰テスト。

ここは **ネットワークを使わない**。`parse_news()` と `filter_recent()` は純関数なので、
実ページ（2026-08-16 取得の kabutan ニュース一覧）から構造を写した HTML 断片を
直接渡して検証できる。fixture の日付は合成（実データ非依存）で、`filter_recent` には
固定の today を渡すため、実行日が変わっても結果は変わらない。
"""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import fetch_news as FN  # noqa: E402


def eq(actual, expected, label=""):
    assert actual == expected, f"{label}: expected {expected!r}, got {actual!r}"


_ENTRY = {
    "id": "kabutan_news",
    "table_selector": "table.s_news_list",
    "base_url": "https://kabutan.jp/",
}

_TODAY = date(2026, 1, 10)   # 実データ非依存の固定基準日

# 実ページの行構造を写した断片（日付・コード・見出しは合成）。
# 開示行: 絶対URL + td.td_kaiji + アンカー内に pdf アイコン画像
# 材料行: 相対URL + アンカー外に premium アイコン画像
_HTML = """
<div id="news_contents">
<table class="s_news_list mgbt0">
<tr>
<td class="news_time"><time datetime="2026-01-10T15:30:00+09:00">26/01/10&nbsp;15:30</time></td>
<td><div class="newslist_ctg newsctg_kaiji_b">開示</div></td>
<td class="td_kaiji"><a href="https://kabutan.jp/disclosures/pdf/20260110/140120260110000001/" target="pdf">剰余金の配当に関するお知らせ<img src="/images/cmn/pdf16.gif" alt="pdf" /></a></td>
</tr>
<tr>
<td class="news_time"><time datetime="2026-01-04T10:57:00+09:00">26/01/04&nbsp;10:57</time></td>
<td><div class="newslist_ctg newsctg2_b">材料</div></td>
<td><img src="/images/cmn/premium_short_expired.svg" class="vat pdr4" /><a href="/stock/news?code=9999&b=n202601040534">合成テスト銘柄が大幅反発、新作の販売本数が１０万本を突破</a></td>
</tr>
<tr>
<td class="news_time"><time datetime="2026-01-03T15:30:00+09:00">26/01/03&nbsp;15:30</time></td>
<td><div class="newslist_ctg newsctg3_kk_b" data-code="">決算</div></td>
<td><a href="/stock/news?code=9999&b=k202601030474">合成テスト銘柄、10-12月期(2Q)経常は赤字縮小で着地</a></td>
</tr>
</table>
</div>
"""


# =============================================================================
# 1. パース（項目の抽出）
# =============================================================================

def test_parse_extracts_date_title_url_category():
    items = FN.parse_news(_HTML, _ENTRY)
    eq(len(items), 3, "3行とも読める")
    eq(items[0]["date"], "2026-01-10", "datetime 属性から YYYY-MM-DD")
    eq(items[0]["title"], "剰余金の配当に関するお知らせ",
       "pdf アイコンの img は見出しに混ざらない")
    eq(items[0]["url"],
       "https://kabutan.jp/disclosures/pdf/20260110/140120260110000001/",
       "絶対URLはそのまま")
    eq(items[0]["category"], "開示", "区分を拾う")
    eq([i["category"] for i in items], ["開示", "材料", "決算"], "区分は行ごと")


def test_relative_url_is_absolutized():
    items = FN.parse_news(_HTML, _ENTRY)
    eq(items[1]["url"],
       "https://kabutan.jp/stock/news?code=9999&b=n202601040534",
       "相対 href を base_url で絶対化する")


def test_category_missing_is_null():
    html = """<table class="s_news_list"><tr>
    <td class="news_time"><time datetime="2026-01-05T09:00:00+09:00">26/01/05&nbsp;09:00</time></td>
    <td><a href="/stock/news?code=9999&b=n1">区分セルの無い行</a></td>
    </tr></table>"""
    items = FN.parse_news(html, _ENTRY)
    eq(len(items), 1, "行は読める")
    eq(items[0]["category"], None, "区分が無ければ null（推測で埋めない）")


def test_date_falls_back_to_display_text():
    html = """<table class="s_news_list"><tr>
    <td class="news_time"><time>26/01/05&nbsp;09:00</time></td>
    <td><a href="/stock/news?code=9999&b=n1">datetime 属性の無い行</a></td>
    </tr></table>"""
    items = FN.parse_news(html, _ENTRY)
    eq(len(items), 1, "表示テキストから読める")
    eq(items[0]["date"], "2026-01-05", "yy/mm/dd を YYYY-MM-DD に正規化")


# =============================================================================
# 2. 日付フィルタ（--days 境界）
# =============================================================================

def test_days_window_boundary():
    """days=7 は today を含む直近7暦日。today-6 は残り、today-7 は落ちる。"""
    items = FN.parse_news(_HTML, _ENTRY)
    kept = FN.filter_recent(items, 7, _TODAY)
    eq([i["date"] for i in kept], ["2026-01-10", "2026-01-04"],
       "today(01-10) と today-6(01-04) が残り、today-7(01-03) は落ちる")


def test_days_1_keeps_only_today():
    items = FN.parse_news(_HTML, _ENTRY)
    kept = FN.filter_recent(items, 1, _TODAY)
    eq([i["date"] for i in kept], ["2026-01-10"], "days=1 は当日のみ")


def test_future_dated_item_is_excluded():
    items = [{"date": "2026-01-11", "title": "未来日付", "url": "u", "category": None}]
    eq(FN.filter_recent(items, 7, _TODAY), [], "未来日付は直近に数えない")


def test_unparsable_date_is_dropped_not_fatal():
    items = [{"date": "not-a-date", "title": "壊れた日付", "url": "u", "category": None},
             {"date": "2026-01-10", "title": "正常", "url": "u", "category": None}]
    kept = FN.filter_recent(items, 7, _TODAY)
    eq([i["title"] for i in kept], ["正常"], "読めない日付の項目だけ落ちる")


# =============================================================================
# 3. 空ページ
# =============================================================================

def test_empty_table_returns_empty():
    html = '<table class="s_news_list"></table>'
    eq(FN.parse_news(html, _ENTRY), [], "行が無ければ空リスト")


def test_filter_on_empty_is_empty():
    eq(FN.filter_recent([], 7, _TODAY), [], "空入力は空出力")


# =============================================================================
# 4. 構造が壊れた HTML（例外にせず空を返す）
# =============================================================================

def test_missing_table_is_not_an_exception():
    eq(FN.parse_news("<html><body>リニューアルしました</body></html>", _ENTRY),
       [], "セレクタが外れたら空リスト（例外にしない）")


def test_broken_rows_are_skipped_not_fatal():
    """time 無し・アンカー無し・datetime も表示も壊れた行は、その行だけ落ちる。"""
    html = """<table class="s_news_list">
    <tr><td><a href="/stock/news?code=9999&b=n1">time の無い行</a></td></tr>
    <tr><td class="news_time"><time datetime="2026-01-09T09:00:00+09:00">26/01/09</time></td>
        <td>アンカーの無い行</td></tr>
    <tr><td class="news_time"><time datetime="garbage">こわれた表示</time></td>
        <td><a href="/stock/news?code=9999&b=n2">日付の読めない行</a></td></tr>
    <tr><td class="news_time"><time datetime="2026-01-08T09:00:00+09:00">26/01/08</time></td>
        <td><a href="/stock/news?code=9999&b=n3">正常な行</a></td></tr>
    </table>"""
    items = FN.parse_news(html, _ENTRY)
    eq([i["title"] for i in items], ["正常な行"], "壊れた行だけ落ちる")


def test_truncated_html_is_not_an_exception():
    """タグが閉じていない途切れ HTML でも例外にしない（読めた分だけ返す）。"""
    truncated = _HTML.split("</table>")[0]   # </table> 以降を切り落とす
    items = FN.parse_news(truncated, _ENTRY)
    assert isinstance(items, list), "リストが返る（例外にしない）"
    eq(len(items), 3, "読めた行は返す")


# =============================================================================
# ランナー
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
    if failed:
        for name, msg in failed:
            print(f"  - {name}: {msg}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
