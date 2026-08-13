"""fetch_fundamentals.py の照合・単位正規化・期間解釈の回帰テスト。

ここは **ネットワークを使わない**。`parse_source()` / `reconcile()` は純関数なので、
合成 HTML と Obs を直接渡して検証できる。

なぜこのファイルが要るか:
  レポート（reports/{code}.md）の財務数値は、これまで人間が表を目で読んで転記した
  ものだった。株価の `close` は2ソース一致でしか採用値にならないのに（D7）、
  **レポートの本体である財務数値には同じ規律が無かった。** その規律をコードにした
  以上、規律のほうが壊れていないことを機械で確かめ続ける必要がある。

このファイルの禁じ手（過去に CI を落とした原因）:
  **実データの値・日付を期待値にべた書きしない。** 財務データは決算のたびに増える。
  実データを使う検証は「data/fundamentals/*.csv の不変条件」として書き、
  期待値は CSV 自身から引く。
"""
from __future__ import annotations

import csv
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import fetch_fundamentals as FF  # noqa: E402


def eq(actual, expected, label=""):
    assert actual == expected, "%s: expected %r, got %r" % (label, expected, actual)


def is_none(actual, label=""):
    assert actual is None, "%s: expected None, got %r" % (label, actual)


NA = [FF.squeeze(m) for m in
      ["-", "－", "―", "‐", "--", "---", "赤字", "黒字", "黒転", "赤転", "*"]]

METRICS = {
    "revenue": "JPY_million",
    "operating_income": "JPY_million",
    "eps": "JPY",
    "equity_ratio": "pct",
    "interest_bearing_debt_ratio": "x",
}


def obs(source, site, value, resolution, order=0, period="FY2025-06",
        metric="revenue", unit="JPY_million", flags=()):
    return FF.Obs(period=period, metric=metric, value=value,
                  resolution=resolution, unit=unit, raw=str(value),
                  source=source, site=site, url="https://example.invalid/",
                  order=order, flags=flags)


def rows_by_key(rows):
    return {(r["period"], r["metric"]): r for r in rows}


def flags_of(row):
    return set(str(row["status"]).split("|"))


# =============================================================================
# 数値の読み取りと単位の正規化
# =============================================================================

def test_plain_number_carries_display_resolution():
    eq(FF.parse_number("1,588", NA), (1588.0, 1.0, None), "整数は解像度1")
    eq(FF.parse_number("-39.2", NA), (-39.2, 0.1, None), "小数1桁は解像度0.1")
    eq(FF.parse_number("107.51", NA), (107.51, 0.01, None), "小数2桁は解像度0.01")


def test_suffixed_number_is_scaled_to_millions():
    got = FF.normalize_value("26.4億", "JPY_suffixed", "JPY_million", NA)
    eq(got, (2640.0, 10.0, False), "26.4億 = 2,640百万円・解像度は0.1億=10百万円")
    got = FF.normalize_value("136百万", "JPY_suffixed", "JPY_million", NA)
    eq(got, (136.0, 1.0, False), "百万表記はそのまま")
    got = FF.normalize_value("-0.43百万", "JPY_suffixed", "JPY_million", NA)
    eq(got[1], 0.01, "小数2桁の百万表記は解像度0.01百万円")


def test_suffixless_cell_is_rejected_when_suffix_is_expected():
    """単位を仮定しない。'28,000' を 28,000百万円 と読む事故を作らない。"""
    is_none(FF.normalize_value("28,000", "JPY_suffixed", "JPY_million", NA),
            "接尾辞が無いセルは読まない")


def test_trailing_yoy_in_the_same_cell_is_dropped():
    """IR BANK は '349百万 +4.9%' のように前年比を同じセルに書く。"""
    got = FF.normalize_value("349百万 +4.9%", "JPY_suffixed", "JPY_million", NA)
    eq(got, (349.0, 1.0, False), "先頭トークンだけを読む")


def test_na_marks_are_not_numbers():
    for mark in ("-", "－", "赤字", "黒転", ""):
        is_none(FF.normalize_value(mark, "JPY_million", "JPY_million", NA), mark)


def test_percent_is_converted_to_multiple_and_marked():
    """kabutan は「有利子負債倍率(倍)」、IR BANK は「有利子負債比率(％)」。"""
    got = FF.normalize_value("546.72", "pct", "x", NA)
    eq(round(got[0], 6), 5.4672, "％→倍は1/100")
    eq(round(got[1], 8), 0.0001, "解像度も一緒に換算する")
    eq(got[2], True, "換算したことを記録する")


def test_incompatible_units_are_dropped_not_guessed():
    is_none(FF.normalize_value("100", "JPY_million", "pct", NA),
            "百万円を％に読み替えない")


# =============================================================================
# 期間の解釈
# =============================================================================

def test_period_keys():
    eq(FF.parse_period("2022.06", "fy")[0], "FY2022-06", "kabutan の通期")
    eq(FF.parse_period("2022/06", "fy")[0], "FY2022-06", "IR BANK の通期（同じキー）")
    eq(FF.parse_period("24.04-06", "fy")[0], "Q2024-04_2024-06", "3か月")
    eq(FF.parse_period("23.01-06", "fy")[0], "H2023-01_2023-06", "6か月")
    eq(FF.parse_period("25.07-03", "fy")[0], "C2025-07_2026-03", "9か月累計（年跨ぎ）")
    eq(FF.parse_period("2026.03", "quarter")[0], "Q2026-01_2026-03",
       "四半期末だけの表記は3か月遡る")


def test_forecast_mark_is_detected_on_both_sides():
    eq(FF.parse_period("予 2026.06", "fy")[1], True, "kabutan は前置")
    eq(FF.parse_period("2026/06 予", "fy")[1], True, "IR BANK は後置")
    eq(FF.parse_period("2026.06", "fy")[1], False, "実績は予想ではない")


def test_accounting_standard_marks_are_stripped_and_recorded():
    """`連 2023.03` を読めないと、連結企業の行が丸ごと落ちる（実際に落ちていた）。"""
    eq(FF.parse_period("連 2023.03", "fy")[0], "FY2023-03", "「連」を剥がす")
    eq(FF.parse_period("連\xa0予 2027.03", "fy"), ("FY2027-03", True, ()),
       "「連」+「予」の複合")
    eq(FF.parse_period("単 2003.03*", "fy"),
       ("FY2003-03", False, ("NONCONSOLIDATED", "PERIOD_ASTERISK")),
       "非連結と混在マークを両方残す")
    eq(FF.parse_period("変 2020.12", "fy")[2], ("PERIOD_CHANGED",), "決算期変更")


def test_fiscal_quarter_key_matches_the_calendar_label():
    """IR BANK は「2026/06 期の1Q」、kabutan は「25.07-09」。同じキーになること。"""
    eq(FF.fiscal_quarter_key(2026, 6, 1), FF.parse_period("25.07-09", "fy")[0], "1Q")
    eq(FF.fiscal_quarter_key(2026, 6, 3), FF.parse_period("26.01-03", "fy")[0], "3Q")
    eq(FF.fiscal_quarter_key(2026, 3, 4), FF.parse_period("26.01-03", "fy")[0],
       "3月期の4Qは1-3月")


def test_non_period_rows_are_skipped():
    for label in ("決算期", "前期比", "前年同期比", "過去最高", ""):
        is_none(FF.parse_period(label, "fy"), label)


# =============================================================================
# 照合（本モジュールの中核）
# =============================================================================

def test_two_sites_exactly_equal_is_ok():
    rows = FF.reconcile("9999", [obs("a", "s1", 1588.0, 1.0, 0),
                                 obs("b", "s2", 1588.0, 1.0, 1)], 2, "t")
    eq(len(rows), 1, "1行")
    eq(flags_of(rows[0]), {"OK"}, "完全一致")
    eq(rows[0]["value"], "1588", "採用値が入る")


def test_agreement_within_display_resolution_is_ok_but_marked():
    """'2,638'(百万円) と '26.4億' は同じ数字を別の精度で書いたもの。"""
    rows = FF.reconcile("9999", [obs("kabutan", "s1", 2638.0, 1.0, 0),
                                 obs("irbank", "s2", 2640.0, 10.0, 1)], 2, "t")
    eq(flags_of(rows[0]), {"OK", "ROUNDING"}, "解像度内の一致は ROUNDING を付ける")
    eq(rows[0]["value"], "2638", "採用は精度の高いほう")


def test_difference_beyond_resolution_is_mismatch_and_value_is_empty():
    rows = FF.reconcile("9999", [obs("a", "s1", 107.51, 0.01, 0),
                                 obs("b", "s2", 107.69, 0.01, 1)], 2, "t")
    eq(flags_of(rows[0]), {"MISMATCH"}, "解像度を超える差")
    eq(rows[0]["value"], "", "照合を通っていない値を採用値に格上げしない（D7）")
    eq(rows[0]["value_primary"], "107.51", "生値は両方残す")
    eq(rows[0]["value_secondary"], "107.69", "生値は両方残す")


def test_order_of_magnitude_error_is_always_caught():
    """解像度で緩めても、桁の取り違えは必ず MISMATCH になること。"""
    rows = FF.reconcile("9999", [obs("a", "s1", 3003.0, 1.0, 0),
                                 obs("b", "s2", 304.0, 1.0, 1)], 2, "t")
    eq(flags_of(rows[0]), {"MISMATCH"}, "10倍のずれ")
    eq(rows[0]["value"], "", "採用しない")


def test_single_site_is_not_adopted_even_with_two_pages():
    """同じサイトの別ページが裏付けても、それは独立した確認ではない。"""
    rows = FF.reconcile("9999", [obs("irbank_pl", "irbank", 68.4, 0.1, 0),
                                 obs("irbank_q", "irbank", 68.4, 0.1, 1)], 2, "t")
    eq(flags_of(rows[0]), {"SINGLE_SOURCE"}, "1サイトのみ")
    eq(rows[0]["value"], "", "採用値は空のまま")
    eq(rows[0]["sources_all"], "irbank_pl=68.4|irbank_q=68.4",
       "参加した全ソースを残す（あとから人が見られる形で）")


def test_same_site_pages_disagreeing_is_still_mismatch():
    rows = FF.reconcile("9999", [obs("irbank_pl", "irbank", 68.16, 0.01, 0),
                                 obs("irbank_q", "irbank", 62.97, 0.01, 1)], 2, "t")
    eq(flags_of(rows[0]), {"MISMATCH"}, "同一サイト内の食い違いも隠さない")


def test_one_disagreeing_source_among_three_blocks_adoption():
    """多数決をしない。1つでも食い違えば採用しない。"""
    rows = FF.reconcile("9999", [obs("a", "s1", 100.0, 0.01, 0),
                                 obs("b", "s2", 100.0, 0.01, 1),
                                 obs("c", "s3", 130.0, 0.01, 2)], 2, "t")
    eq(flags_of(rows[0]), {"MISMATCH"}, "2対1でも採用しない")
    eq(rows[0]["value"], "", "採用値は空")


def test_extra_flags_survive_reconcile():
    rows = FF.reconcile("9999", [
        obs("a", "s1", 5.46, 0.01, 0, metric="interest_bearing_debt_ratio",
            unit="x", flags=("UNIT_CONVERTED",)),
        obs("b", "s2", 5.4672, 0.0001, 1, metric="interest_bearing_debt_ratio",
            unit="x", flags=("NONCONSOLIDATED",)),
    ], 2, "t")
    eq(flags_of(rows[0]), {"OK", "ROUNDING", "UNIT_CONVERTED", "NONCONSOLIDATED"},
       "付加フラグは照合結果を上書きせず並ぶ")


def test_reconcile_output_is_deterministic():
    a = [obs("a", "s1", 1.0, 0.1, 0, period="FY2025-06"),
         obs("b", "s2", 1.0, 0.1, 1, period="FY2024-06")]
    eq([r["period"] for r in FF.reconcile("9999", a, 2, "t")],
       [r["period"] for r in FF.reconcile("9999", list(reversed(a)), 2, "t")],
       "入力の順に依らず同じ並びになる（D8）")


def test_fmt_has_no_floating_point_noise():
    eq(FF.fmt(0.01 * 546.72), "5.4672", "換算後の値に浮動小数の余りを出さない")
    eq(FF.fmt(1590.0), "1590", "整数に .0 を付けない")
    eq(FF.fmt(None), "", "空は空")


# =============================================================================
# HTML パース（合成 HTML。実サイトを叩かない）
# =============================================================================

_KABUTAN_HTML = """
<div class="fin_year_t0_d fin_year_result_d"><table>
<tr><th>決算期</th><th>売上高</th><th>営業益</th><th>修正<br>1株益</th></tr>
<tr><td>連 2024.06</td><td>1,740</td><td>58</td><td>29.1</td></tr>
<tr><td>連&nbsp;予 2026.06</td><td>2,403</td><td>92</td><td>21.3</td></tr>
<tr><td>前期比</td><td>+30.3</td><td>黒転</td><td>黒転</td></tr>
</table></div>
<p>※単位について ・業績推移：売上高、営業益は「百万円」。修正1株益は「円」</p>
"""

_KABUTAN_ENTRY = {
    "id": "kabutan_fy", "site": "kabutan", "kind": "rows_period_first",
    "period_kind": "fy", "table_selector": "div.fin_year_result_d table",
    "expect_header": ["決算期", "売上高", "営業益", "修正1株益"],
    "unit_assert": ["売上高、営業益は「百万円」"],
    "columns": [None,
                {"metric": "revenue", "unit": "JPY_million"},
                {"metric": "operating_income", "unit": "JPY_million"},
                {"metric": "eps", "unit": "JPY"}],
}


def test_parse_kabutan_style_table():
    got, tables = FF.parse_source(_KABUTAN_HTML, _KABUTAN_ENTRY, "u", 0, METRICS, NA)
    eq(tables, 1, "表を1つ特定できる")
    found = {(o.period, o.metric): o.value for o in got}
    eq(found[("FY2024-06", "revenue")], 1740.0, "実績")
    eq(found[("FY2026-06", "revenue_plan")], 2403.0, "会社予想は metric に _plan が付く")
    assert ("FY2026-06", "revenue") not in found, "予想を実績と混ぜない"
    assert all("UNIT_UNCONFIRMED" not in o.flags for o in got), "単位注記を確認できている"


def test_missing_unit_note_raises_unit_unconfirmed():
    html = _KABUTAN_HTML.replace("「百万円」", "「千円」")
    got, _ = FF.parse_source(html, _KABUTAN_ENTRY, "u", 0, METRICS, NA)
    assert got, "値そのものは読める"
    assert all("UNIT_UNCONFIRMED" in o.flags for o in got), \
        "単位注記が変わったら黙って通さない"


def test_inserted_column_makes_the_table_unreadable_not_wrong():
    """1列足されたら「読めない」に落ちる。**黙って別の列を読まない**。"""
    html = _KABUTAN_HTML.replace("<th>売上高</th>", "<th>区分</th><th>売上高</th>")
    got, tables = FF.parse_source(html, _KABUTAN_ENTRY, "u", 0, METRICS, NA)
    eq(tables, 0, "ヘッダ不一致の表は選ばない")
    eq(got, [], "欠測として扱う")


def test_missing_table_is_not_an_exception():
    got, tables = FF.parse_source("<html><body>改装しました</body></html>",
                                  _KABUTAN_ENTRY, "u", 0, METRICS, NA)
    eq((got, tables), ([], 0), "セレクタが外れても例外にしない")


_IRBANK_Q_HTML = """
<table class="bar">
<tr><th>科目</th><th>年度</th><th>1Q</th><th>2Q</th><th>3Q</th><th>4Q</th><th>通期</th></tr>
<tr><td>売上高</td><td>2025/06</td><td>536百万</td><td>364百万</td><td>380百万</td>
    <td>565百万</td><td>1844百万</td></tr>
<tr><td>2026/06</td><td>389百万 -27.4%</td><td>382百万 +5%</td><td>722百万 +90.2%</td>
    <td>-</td><td>-</td></tr>
<tr><th>科目</th><th>年度</th><th>1Q</th><th>2Q</th><th>3Q</th><th>4Q</th><th>通期</th></tr>
<tr><td>売上債権</td><td>2026/06</td><td>268百万</td><td>294百万</td><td>654百万</td>
    <td>-</td><td>-</td></tr>
</table>
"""

_IRBANK_Q_ENTRY = {
    "id": "irbank_q", "site": "irbank", "kind": "matrix_metric_year",
    "multi": True,
    "expect_header": ["科目", "年度", "1Q", "2Q", "3Q", "4Q", "通期"],
    "quarter_columns": ["1Q", "2Q", "3Q", "4Q"],
    "annual_column": "通期",
    "metric_map": {"売上高": {"metric": "revenue", "unit": "JPY_suffixed"}},
}


def test_parse_irbank_quarter_matrix():
    got, _ = FF.parse_source(_IRBANK_Q_HTML, _IRBANK_Q_ENTRY, "u", 0, METRICS, NA)
    found = {(o.period, o.metric): o.value for o in got}
    eq(found[("Q2024-07_2024-09", "revenue")], 536.0,
       "2025/06期の1Qは 2024-07〜2024-09（kabutan の '24.07-09' と同じキー）")
    eq(found[("Q2025-07_2025-09", "revenue")], 389.0,
       "2026/06期の1Qは 2025-07〜2025-09")
    eq(found[("FY2025-06", "revenue")], 1844.0, "通期列は年度キーになる")
    assert ("Q2026-04_2026-06", "revenue") not in found, "'-' の四半期は記録しない"
    assert not any(o.metric != "revenue" for o in got), \
        "metric_map に無い科目（売上債権）は読み飛ばす"


def test_matrix_carries_metric_over_rowspan_rows():
    got, _ = FF.parse_source(_IRBANK_Q_HTML, _IRBANK_Q_ENTRY, "u", 0, METRICS, NA)
    found = {(o.period, o.metric): o.value for o in got}
    eq(found[("Q2026-01_2026-03", "revenue")], 722.0,
       "科目が省略された行（rowspan）でも前の科目を引き継ぐ")


_RECORD_HTML = """
<h3>過去最高 【実績】</h3>
<table>
<tr><th></th><th>売上高</th><th>営業益</th></tr>
<tr><th>過去最高</th><td>2,638</td><td>386</td></tr>
<tr><th>決算期</th><td>2020.06</td><td>2020.06</td></tr>
</table>
<h3>3ヵ月決算過去最高 【実績】</h3>
<table>
<tr><th></th><th>売上高</th><th>営業益</th></tr>
<tr><th>過去最高</th><td>723</td><td>160</td></tr>
<tr><th>決算期</th><td>2026.03</td><td>2022.06</td></tr>
</table>
<p>※単位について ・業績推移：売上高、営業益は「百万円」</p>
"""


def _record_entry(heading, period_kind):
    return {
        "id": "rec", "site": "kabutan", "kind": "transposed_record",
        "period_kind": period_kind, "heading_equals": heading,
        "expect_header": ["", "売上高", "営業益"],
        "value_row_label": "過去最高", "period_row_label": "決算期",
        "columns": [None,
                    {"metric": "revenue", "unit": "JPY_million"},
                    {"metric": "operating_income", "unit": "JPY_million"}],
    }


def test_transposed_record_table_uses_per_column_periods():
    entry = _record_entry("3ヵ月決算過去最高 【実績】", "quarter")
    got, _ = FF.parse_source(_RECORD_HTML, entry, "u", 0, METRICS, NA)
    found = {(o.period, o.metric): o.value for o in got}
    eq(found[("Q2026-01_2026-03", "revenue")], 723.0, "列ごとに決算期が違う")
    eq(found[("Q2022-04_2022-06", "operating_income")], 160.0, "列ごとに決算期が違う")


def test_heading_match_is_exact_not_substring():
    """「3ヵ月決算過去最高」は「過去最高」を含む。部分一致だと両方に当たる。"""
    entry = _record_entry("過去最高 【実績】", "fy")
    got, _ = FF.parse_source(_RECORD_HTML, entry, "u", 0, METRICS, NA)
    eq(sorted({o.period for o in got}), ["FY2020-06"], "年度の表だけを読む")


# =============================================================================
# append-only
# =============================================================================

def _write(path, rows):
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FF.FIELDS)
        w.writeheader()
        w.writerows(rows)


def test_append_only_does_not_duplicate_on_rerun():
    rows = FF.reconcile("9999", [obs("a", "s1", 1.0, 0.1, 0),
                                 obs("b", "s2", 1.0, 0.1, 1)], 2, "t")
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "9999.csv"
        led = Path(tmp) / "revisions.csv"
        eq(FF.append_only(path, rows, "t", led)[0], 1, "初回は追記される")
        eq(FF.append_only(path, rows, "t", led)[0], 0,
           "同じ (code, period, metric) は追記しない")
        eq(len(list(csv.DictReader(path.open(encoding="utf-8")))), 1, "行が増えない")


def test_append_only_keeps_the_existing_row_untouched():
    """再取得で値が変わっても過去行を書き換えない（append-only の不変条件）。"""
    old = FF.reconcile("9999", [obs("a", "s1", 1.0, 0.1, 0),
                                obs("b", "s2", 1.0, 0.1, 1)], 2, "t")
    new = FF.reconcile("9999", [obs("a", "s1", 2.0, 0.1, 0),
                                obs("b", "s2", 2.0, 0.1, 1)], 2, "t")
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "9999.csv"
        led = Path(tmp) / "revisions.csv"
        FF.append_only(path, old, "t", led)
        eq(FF.append_only(path, new, "t", led)[0], 0, "上書きしない")
        got = list(csv.DictReader(path.open(encoding="utf-8")))
        eq(got[0]["value"], old[0]["value"], "既存の値がそのまま残る")
        assert not led.exists(), "採用値の書き換えは訂正として記録しない"


def test_append_only_adds_only_new_keys():
    first = FF.reconcile("9999", [obs("a", "s1", 1.0, 0.1, 0, period="FY2024-06"),
                                  obs("b", "s2", 1.0, 0.1, 1, period="FY2024-06")],
                         2, "t")
    second = FF.reconcile("9999", [obs("a", "s1", 1.0, 0.1, 0, period="FY2025-06"),
                                   obs("b", "s2", 1.0, 0.1, 1, period="FY2025-06")],
                          2, "t")
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "9999.csv"
        led = Path(tmp) / "revisions.csv"
        FF.append_only(path, first, "t", led)
        eq(FF.append_only(path, first + second, "t", led)[0], 1,
           "新しいキーだけ追記する")


def test_single_source_row_is_repaired_when_the_second_site_comes_back():
    """片肺の週に書かれた SINGLE_SOURCE が、翌週の照合成立で直る。

    鍵が (code, period, metric) で**二度と新しくならない**ため、旧実装では
    1回の取得失敗がその期の検証状態を恒久的に固定していた。
    直った事実は `data/revisions.csv` に必ず残る（黙って直さない）。
    """
    week1 = FF.reconcile("9999", [obs("a", "s1", 1.0, 0.1, 0)], 2, "t1")
    week2 = FF.reconcile("9999", [obs("a", "s1", 1.0, 0.1, 0),
                                  obs("b", "s2", 1.0, 0.1, 1)], 2, "t2")
    eq(week1[0]["status"], "SINGLE_SOURCE", "1週目は照合不成立")
    eq(week1[0]["value"], "", "1週目は採用値が空")
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "9999.csv"
        led = Path(tmp) / "revisions.csv"
        FF.append_only(path, week1, "t1", led)
        added, fixed = FF.append_only(path, week2, "t2", led)
        eq(added, 0, "行は増えない")
        eq(fixed, 1, "1行が訂正される")
        got = list(csv.DictReader(path.open(encoding="utf-8")))
        eq(len(got), 1, "行が増えない")
        eq(got[0]["value"], week2[0]["value"], "採用値が入る")
        assert "OK" in got[0]["status"].split("|"), "照合成立になる"
        led_rows = list(csv.DictReader(led.open(encoding="utf-8")))
        assert led_rows, "訂正が台帳に記録される"
        assert all(r["kind"] == "repair" for r in led_rows), "向きは repair"
        assert all(r["reason"] for r in led_rows), "理由が残る"


def test_repair_does_not_fire_when_the_adopted_value_would_be_lost():
    """OK → SINGLE_SOURCE の方向（採用の取り下げ）は自動では起きない。

    取得元の一時的な不調で採用値が消えると、直したかった障害
    （指標が算出できない）を自分で起こすことになる。
    """
    good = FF.reconcile("9999", [obs("a", "s1", 1.0, 0.1, 0),
                                 obs("b", "s2", 1.0, 0.1, 1)], 2, "t1")
    half = FF.reconcile("9999", [obs("a", "s1", 1.0, 0.1, 0)], 2, "t2")
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "9999.csv"
        led = Path(tmp) / "revisions.csv"
        FF.append_only(path, good, "t1", led)
        added, fixed = FF.append_only(path, half, "t2", led)
        eq((added, fixed), (0, 0), "何も起きない")
        got = list(csv.DictReader(path.open(encoding="utf-8")))
        eq(got[0]["value"], good[0]["value"], "採用値が消えない")
        assert not led.exists(), "訂正は記録されない"


# =============================================================================
# 実データの不変条件（**値をべた書きしない**。CSV 自身から引く）
# =============================================================================

def _real_files():
    d = ROOT / "data" / "fundamentals"
    return sorted(d.glob("*.csv")) if d.exists() else []


def test_real_data_never_adopts_an_unreconciled_value():
    for path in _real_files():
        for r in csv.DictReader(path.open(encoding="utf-8-sig")):
            flags = set(str(r["status"]).split("|"))
            if r["value"].strip():
                assert "OK" in flags, \
                    "%s %s %s: status=%s なのに value が埋まっている" % (
                        path.name, r["period"], r["metric"], r["status"])
            else:
                assert "OK" not in flags, \
                    "%s %s %s: status=OK なのに value が空" % (
                        path.name, r["period"], r["metric"])


def test_real_data_has_exactly_one_reconcile_status_per_row():
    for path in _real_files():
        for r in csv.DictReader(path.open(encoding="utf-8-sig")):
            flags = [p for p in str(r["status"]).split("|") if p]
            got = [p for p in flags if p in FF.RECONCILE_STATUSES]
            eq(len(got), 1, "%s %s %s status=%s" % (path.name, r["period"],
                                                    r["metric"], r["status"]))
            for p in flags:
                assert p in FF.RECONCILE_STATUSES or p in FF.EXTRA_FLAGS, \
                    "%s: 語彙外のフラグ %r" % (path.name, p)


def test_real_data_ok_rows_agree_with_their_sources():
    """OK の行の採用値が、記録された参加ソースのどれかと一致していること。

    期待値は CSV から引く（実データの数値をテストにべた書きしない）。
    """
    for path in _real_files():
        for r in csv.DictReader(path.open(encoding="utf-8-sig")):
            if "OK" not in str(r["status"]).split("|"):
                continue
            values = [p.split("=", 1)[1] for p in r["sources_all"].split("|") if "=" in p]
            assert r["value"] in values, \
                "%s %s %s: value=%s が sources_all=%s に無い" % (
                    path.name, r["period"], r["metric"], r["value"], r["sources_all"])


def test_real_data_mismatch_rows_keep_both_values():
    for path in _real_files():
        for r in csv.DictReader(path.open(encoding="utf-8-sig")):
            if "MISMATCH" not in str(r["status"]).split("|"):
                continue
            assert r["value_primary"].strip() and r["value_secondary"].strip(), \
                "%s %s %s: 不一致なのに両値が残っていない" % (
                    path.name, r["period"], r["metric"])


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
            print("  PASS  %s" % name)
        except AssertionError as e:
            failed.append((name, str(e)))
            print("  FAIL  %s: %s" % (name, e))
        except Exception as e:  # noqa: BLE001
            failed.append((name, "%s: %s" % (type(e).__name__, e)))
            print("  ERROR %s: %s: %s" % (name, type(e).__name__, e))
    print("\n%d/%d passed" % (len(tests) - len(failed), len(tests)))
    if failed:
        for name, msg in failed:
            print("  - %s: %s" % (name, msg))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
