"""chartdata.py の回帰テスト（図の数値を検証済み CSV から引く層）。

**ネットワークを使わない。** 合成した CSV を一時ディレクトリに置き、
`chartdata.DATA` を差し替えて読ませる。

なぜこのファイルが要るか:
  図の数値は front matter に人が転記していた。桁を取り違えても誰も気づかない。
  ここは「照合を通っていない値を図に出さない」（D7）を守る最後の砦なので、
  status=MISMATCH / SINGLE_SOURCE の行が **0 で埋められたり、こっそり
  採用されたりしないこと** を実データと合成データの両方で確かめる。

期待値はデータから引く。**日付や実データの数値をべた書きしない**
（実データは週次で増えるため）。
"""
from __future__ import annotations

import csv
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import chart as CH      # noqa: E402（timeline / diagram は描画側も検査する）
import chartdata as CD  # noqa: E402
import report as R      # noqa: E402


def eq(actual, expected, label=""):
    assert actual == expected, f"{label}: expected {expected!r}, got {actual!r}"


def is_none(actual, label=""):
    assert actual is None, f"{label}: expected None, got {actual!r}"


# =============================================================================
# 合成データの土台
# =============================================================================

FUND_COLS = ["period", "code", "metric", "value", "unit", "tolerance", "status",
             "source_primary", "value_primary", "raw_primary",
             "source_secondary", "value_secondary", "raw_secondary",
             "sources_all", "source_url_primary", "source_url_secondary",
             "fetched_at"]

TANSHIN_COLS = ["date", "code", "metric", "value", "value_extracted", "unit",
                "definition", "assumed", "source", "tier", "status",
                "source_url", "fetched_at"]

PRICE_COLS = ["date", "code", "open", "high", "low", "close", "volume",
              "status", "source_primary", "value_primary",
              "source_secondary", "value_secondary", "fetched_at"]


class Sandbox:
    """一時ディレクトリに data/ を組み立てて chartdata に読ませる。"""

    def __init__(self):
        self.root = Path(tempfile.mkdtemp(prefix="kabu-chartdata-"))
        (self.root / "fundamentals").mkdir(parents=True)
        (self.root / "tanshin").mkdir(parents=True)
        (self.root / "prices").mkdir(parents=True)
        self._saved = CD.DATA
        CD.DATA = self.root
        CD.clear_cache()

    def close(self):
        CD.DATA = self._saved
        CD.clear_cache()
        shutil.rmtree(self.root, ignore_errors=True)

    def _write(self, path: Path, cols: list[str], rows: list[dict]):
        with path.open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=cols)
            w.writeheader()
            for r in rows:
                w.writerow({c: r.get(c, "") for c in cols})
        CD.clear_cache()

    def fundamentals(self, code: str, rows: list[dict]):
        self._write(self.root / "fundamentals" / f"{code}.csv", FUND_COLS, rows)

    def tanshin(self, code: str, rows: list[dict]):
        self._write(self.root / "tanshin" / f"{code}.csv", TANSHIN_COLS, rows)

    def prices(self, rows: list[dict]):
        self._write(self.root / "prices" / "daily.csv", PRICE_COLS, rows)


def frow(period, metric, value, status, unit="JPY_million", tol="1",
         sources_all="", code="9999"):
    return {"period": period, "code": code, "metric": metric,
            "value": "" if value is None else str(value), "unit": unit,
            "tolerance": tol, "status": status, "sources_all": sources_all,
            "source_url_primary": "https://example.invalid/x",
            "fetched_at": "2026-01-01T00:00:00+09:00"}


def trow(day, metric, value, unit="JPY_million", status="OK",
         definition="FY2026Q3cum|単体|日本基準|x", code="9999"):
    return {"date": day, "code": code, "metric": metric, "value": str(value),
            "value_extracted": str(value), "unit": unit,
            "definition": definition, "assumed": "false", "source": "tanshin",
            "tier": "primary", "status": status,
            "source_url": "https://example.invalid/t.pdf",
            "fetched_at": "2026-01-01T00:00:00+09:00"}


def prow(day, close, high, low, status="OK", code="9999"):
    return {"date": day, "code": code, "open": str(low), "high": str(high),
            "low": str(low), "close": "" if close is None else str(close),
            "volume": "1000", "status": status, "source_primary": "a",
            "value_primary": str(high), "source_secondary": "b",
            "value_secondary": str(high),
            "fetched_at": "2026-01-01T00:00:00+09:00"}


def values_of(resolved) -> list:
    return [p["value"] for p in resolved.spec["data"]]


# =============================================================================
# 単位・ラベル・metric 名
# =============================================================================

def test_convert_within_family():
    eq(CD.convert(1844, "JPY_million", "億円"), 18.44, "百万円→億円")
    eq(CD.convert(1844, "JPY_million", "百万円"), 1844.0, "同じ単位")
    eq(CD.convert(9.9, "pct", "%"), 9.9, "pct→%")
    eq(CD.convert(2.5, "x", "倍"), 2.5, "x→倍")


def test_convert_refuses_across_families():
    """円 と % を混ぜない。混ぜたら図の値が意味を失う。"""
    is_none(CD.convert(100, "JPY_million", "%"), "円→%")
    is_none(CD.convert(100, "pct", "億円"), "%→円")
    is_none(CD.convert(100, "JPY_million", "ドル"), "未知の単位")
    is_none(CD.convert(100, "", "億円"), "単位が空")


def test_period_label():
    eq(CD.period_label("FY2026-06"), "2026/6", "通期")
    eq(CD.period_label("Q2026-01_2026-03"), "26/1-3", "四半期")
    eq(CD.period_label("H2026-01_2026-06"), "26/1-6", "半期")
    eq(CD.period_label("C2025-07_2026-03"), "25/7-26/3", "累計（年またぎ）")
    eq(CD.period_label("なにか"), "なにか", "読めない形はそのまま")


def test_metric_ja_suffixes():
    eq(CD.metric_ja("revenue"), "売上高")
    eq(CD.metric_ja("revenue_plan"), "売上高（会社計画）")
    eq(CD.metric_ja("revenue_fy_plan"), "売上高（通期の会社計画）")
    eq(CD.metric_ja("revenue_prev_year"), "売上高（前年同期）")
    eq(CD.metric_ja("equity_ratio_prev_fy"), "自己資本比率（前期末）")
    eq(CD.metric_ja("unknown_thing"), "unknown_thing", "未知はそのまま")


# =============================================================================
# 採用値だけを使う（D7）
# =============================================================================

def test_only_adopted_rows_become_points():
    """OK 以外は 0 で埋めず欠測にする。**ここが本丸**。"""
    sb = Sandbox()
    try:
        sb.fundamentals("9999", [
            frow("FY2024-06", "revenue", 1000, "OK"),
            frow("FY2025-06", "revenue", None, "SINGLE_SOURCE"),
            frow("FY2026-06", "revenue", None, "MISMATCH"),
        ])
        chart = {"type": "bar", "unit": "百万円", "source": {
            "metric": "revenue",
            "periods": ["FY2024-06", "FY2025-06", "FY2026-06", "FY2027-06"]}}
        res = CD.resolve_chart("9999", "c", chart)
        eq(values_of(res), [1000.0, None, None, None], "採用値だけ")
        eq(res.used, 1, "採用点数")
        eq(res.total, 4, "総点数")
        eq(len(res.missing), 3, "欠測の件数")
        assert "SINGLE_SOURCE" in res.missing[0], res.missing
        assert "MISMATCH" in res.missing[1], res.missing
        assert "行が無い" in res.missing[2], res.missing
    finally:
        sb.close()


def test_mismatch_row_with_a_value_is_still_not_adopted():
    """status=MISMATCH なのに値が入っている壊れた行を、図に格上げしない。

    CSV 側の FAIL 検査（checks.py）をすり抜けた行があっても、
    表示側でもう一度落とす。
    """
    sb = Sandbox()
    try:
        sb.fundamentals("9999", [
            frow("FY2024-06", "revenue", 1234, "MISMATCH"),
            frow("FY2025-06", "revenue", 999, "SINGLE_SOURCE"),
        ])
        chart = {"type": "bar", "unit": "百万円",
                 "source": {"metric": "revenue",
                            "periods": ["FY2024-06", "FY2025-06"]}}
        res = CD.resolve_chart("9999", "c", chart)
        eq(values_of(res), [None, None], "どちらも使わない")
        eq(res.used, 0, "採用0")
        assert res.empty_reason, "描けない理由が出ること"
    finally:
        sb.close()


def test_zero_is_a_real_value_not_a_gap():
    """0 は欠測ではない。0 と「無い」を取り違えない。"""
    sb = Sandbox()
    try:
        sb.fundamentals("9999", [frow("FY2024-06", "operating_income", 0, "OK")])
        chart = {"type": "bar", "unit": "百万円",
                 "source": {"metric": "operating_income",
                            "periods": ["FY2024-06"]}}
        res = CD.resolve_chart("9999", "c", chart)
        eq(values_of(res), [0.0], "0 は採用値")
        eq(res.used, 1, "採用1")
    finally:
        sb.close()


def test_series_converts_units_and_honours_overrides():
    sb = Sandbox()
    try:
        sb.fundamentals("9999", [
            frow("FY2025-06", "revenue", 1844, "OK|ROUNDING"),
            frow("FY2026-06", "revenue_plan", 2403, "OK"),
        ])
        chart = {"type": "bar", "unit": "億円",
                 "notes": {"FY2025-06": "直近"},
                 "emphasis": ["FY2025-06"],
                 "source": {"metric": "revenue", "periods": [
                     "FY2025-06",
                     {"period": "FY2026-06", "metric": "revenue_plan",
                      "label": "2026/6予", "note": "会社予想",
                      "emphasis": True}]}}
        res = CD.resolve_chart("9999", "c", chart)
        eq(values_of(res), [18.44, 24.03], "億円に換算")
        eq(res.spec["data"][0]["label"], "2025/6", "既定ラベル")
        eq(res.spec["data"][0]["note"], "直近", "notes が効く")
        eq(res.spec["data"][0]["emphasis"], True, "emphasis が効く")
        eq(res.spec["data"][1]["label"], "2026/6予", "ラベル上書き")
        eq(res.spec["data"][1]["note"], "会社予想", "note 上書き")
    finally:
        sb.close()


def test_unit_family_mismatch_becomes_a_gap_not_a_wrong_number():
    sb = Sandbox()
    try:
        sb.fundamentals("9999", [
            frow("FY2025-06", "equity_ratio", 13.5, "OK", unit="pct")])
        chart = {"type": "bar", "unit": "億円",
                 "source": {"metric": "equity_ratio",
                            "periods": ["FY2025-06"]}}
        res = CD.resolve_chart("9999", "c", chart)
        eq(values_of(res), [None], "換算できないものは出さない")
        assert "単位" in res.missing[0], res.missing
    finally:
        sb.close()


# =============================================================================
# 決算短信（一次情報）との突き合わせ
# =============================================================================

def test_tanshin_point_cross_checks_against_summary_sites():
    sb = Sandbox()
    try:
        sb.fundamentals("9999", [
            frow("C2025-07_2026-03", "operating_income", None, "SINGLE_SOURCE",
                 sources_all="kabutan_ytd3q=-78"),
            frow("FY2026-06", "operating_income_plan", 92, "OK"),
        ])
        sb.tanshin("9999", [trow("2026-05-15", "operating_income", -78)])
        chart = {"type": "progress", "unit": "百万円", "source": {"points": {
            "done": {"dataset": "tanshin", "metric": "operating_income",
                     "cross": "C2025-07_2026-03"},
            "target": {"metric": "operating_income_plan",
                       "period": "FY2026-06"}}}}
        res = CD.resolve_chart("9999", "c", chart)
        eq(res.spec["done"], -78.0, "決算短信の値")
        eq(res.spec["target"], 92.0, "採用値の計画")
        eq(res.used, 2, "2点とも採用")
        assert "一致" in res.note, res.note
        assert "決算短信" in res.note, res.note
    finally:
        sb.close()


def test_tanshin_cross_check_reports_disagreement():
    sb = Sandbox()
    try:
        sb.fundamentals("9999", [
            frow("C2025-07_2026-03", "revenue", None, "SINGLE_SOURCE",
                 sources_all="kabutan_ytd3q=1400")])
        sb.tanshin("9999", [trow("2026-05-15", "revenue", 1493)])
        chart = {"type": "progress", "unit": "百万円", "source": {"points": {
            "done": {"dataset": "tanshin", "metric": "revenue",
                     "cross": "C2025-07_2026-03"},
            "target": {"dataset": "tanshin", "metric": "revenue"}}}}
        res = CD.resolve_chart("9999", "c", chart)
        assert "照合不成立" in res.note, res.note
    finally:
        sb.close()


def test_cross_check_excludes_other_period_columns():
    """決算短信の「前年同期」「通期計画」を同じ期の照合に混ぜない。"""
    sb = Sandbox()
    try:
        sb.fundamentals("9999", [
            frow("C2025-07_2026-03", "revenue", None, "SINGLE_SOURCE",
                 sources_all="kabutan_ytd3q=1493")])
        sb.tanshin("9999", [
            trow("2026-05-15", "revenue", 1493),
            trow("2026-05-15", "revenue_prev_year", 1279),
            trow("2026-05-15", "revenue_fy_plan", 2403),
            trow("2026-05-15", "net_assets", 185),
        ])
        agree, disagree, nopair, other = CD.cross_check_tanshin(
            "9999", "C2025-07_2026-03")
        eq(agree, ["revenue"], "一致")
        eq(disagree, [], "食い違い")
        eq(nopair, ["net_assets"], "相手なし")
        eq(other, ["revenue_fy_plan", "revenue_prev_year"], "対象外")
    finally:
        sb.close()


def test_cross_check_ignores_a_later_disclosure_of_a_different_period():
    """通期の短信が出た後も、3Q累計の突き合わせは3Q累計どうしで行うこと。

    実例（4073）: 3Q累計短信のあとに通期短信が出ると、data/tanshin/{code}.csv に
    同じ metric 名（revenue 等）で違う期の行が並ぶ。metric 名だけで突き合わせると、
    通期の実績（2252）が3Q累計の観測値（1493）と比べられて必ず食い違う。
    """
    sb = Sandbox()
    try:
        sb.fundamentals("9999", [
            frow("C2025-07_2026-03", "revenue", None, "SINGLE_SOURCE",
                 sources_all="kabutan_ytd3q=1493")])
        sb.tanshin("9999", [
            trow("2026-05-15", "revenue", 1493, definition="FY2026Q3cum|単体|日本基準|売上高"),
            trow("2026-05-16", "revenue", 2252, definition="FY2026Q4cum|単体|日本基準|売上高"),
        ])
        agree, disagree, nopair, other = CD.cross_check_tanshin(
            "9999", "C2025-07_2026-03")
        eq(agree, ["revenue"], "3Q累計どうしは一致")
        eq(disagree, [], "通期の実績は比較対象から外れる（別の期）")
    finally:
        sb.close()


# =============================================================================
# 株価レンジ（採用終値だけ）
# =============================================================================

def test_price_range_uses_adopted_close_only():
    """ザラ場の高値・安値は照合を通っていないので使わない。

    high/low 列に極端な値を置いても、レンジがそれに引きずられないことを見る。
    """
    sb = Sandbox()
    try:
        sb.prices([
            prow("2026-01-05", 500, 9999, 1),
            prow("2026-01-06", 400, 9999, 1),
            prow("2026-01-07", 600, 9999, 1),
            prow("2026-01-08", None, 9999, 1, status="SINGLE_SOURCE"),
            prow("2026-01-09", 550, 9999, 1),
        ])
        chart = {"type": "range", "unit": "円",
                 "source": {"dataset": "prices", "window_weeks": 52}}
        res = CD.resolve_chart("9999", "c", chart)
        eq(res.spec["low"], 400.0, "安値＝採用終値の最小")
        eq(res.spec["high"], 600.0, "高値＝採用終値の最大")
        eq(res.spec["current"], 550.0, "現在＝最後の採用終値")
        assert "4営業日" in res.note, res.note
    finally:
        sb.close()


def test_price_range_window_cuts_old_rows():
    sb = Sandbox()
    try:
        sb.prices([
            prow("2024-01-05", 100, 100, 100),   # 窓の外
            prow("2026-01-05", 500, 500, 500),
            prow("2026-01-09", 550, 550, 550),
        ])
        chart = {"type": "range", "unit": "円",
                 "source": {"dataset": "prices", "window_weeks": 52}}
        res = CD.resolve_chart("9999", "c", chart)
        eq(res.spec["low"], 500.0, "1年より前の安値は入れない")
    finally:
        sb.close()


def test_price_range_without_adopted_close_is_empty():
    sb = Sandbox()
    try:
        sb.prices([prow("2026-01-05", None, 500, 400, status="SINGLE_SOURCE")])
        chart = {"type": "range", "unit": "円",
                 "source": {"dataset": "prices"}}
        res = CD.resolve_chart("9999", "c", chart)
        assert res.empty_reason, "描けない理由を出すこと"
        assert "low" not in res.spec, "値をでっち上げない"
    finally:
        sb.close()


# =============================================================================
# timeline（採用終値の折れ線＋出来事）
# =============================================================================

def test_timeline_uses_adopted_close_only():
    """折れ線もイベントの y も採用終値だけから引く（D53）。

    照合を通っていない日（SINGLE_SOURCE）は折れ線に入れず、その日の
    イベントは**直前の採用終値**に置いて、そのことを注記に出す。
    """
    sb = Sandbox()
    try:
        sb.prices([
            prow("2026-01-05", 500, 9999, 1),
            prow("2026-01-06", None, 9999, 1, status="SINGLE_SOURCE"),
            prow("2026-01-07", 600, 9999, 1),
        ])
        chart = {"type": "timeline", "unit": "円",
                 "source": {"dataset": "prices", "window_weeks": 52},
                 "events": [
                     {"date": "2026-01-06", "label": "未照合日の出来事"},
                     {"date": "2026-01-07", "label": "高値の日"},
                 ]}
        res = CD.resolve_chart("9999", "c", chart)
        eq(res.origin, "csv", "全点が検証済みデータ由来")
        eq([p["label"] for p in res.spec["data"]],
           ["2026-01-05", "2026-01-07"], "未照合の日は折れ線に入れない")
        events = res.spec["events"]
        eq(len(events), 2, "2件とも配置")
        eq(events[0]["value"], 500.0, "採用終値が無い日は直前の採用終値")
        assert "直前 2026-01-05" in events[0].get("note", ""), events[0]
        eq(events[1]["value"], 600.0, "その日の採用終値")
        assert "note" not in events[1], "その日の値ならフォールバック注記は出ない"
        assert "出来事 2/2件を配置" in res.note, res.note
    finally:
        sb.close()


def test_timeline_unplaceable_events_become_missing():
    """窓に置けないイベントは黙って捨てず missing に記録する。"""
    sb = Sandbox()
    try:
        sb.prices([
            prow("2026-01-05", 500, 500, 500),
            prow("2026-06-01", 600, 600, 600),
        ])
        chart = {"type": "timeline", "unit": "円",
                 "source": {"dataset": "prices", "window_weeks": 52},
                 "events": [
                     {"date": "2024-01-01", "label": "昔の出来事"},
                     # 窓の中（52週=2025-06-03〜）だが最初の採用終値より前
                     {"date": "2025-12-01", "label": "直前値の無い出来事"},
                 ]}
        res = CD.resolve_chart("9999", "c", chart)
        eq(res.spec["events"], [], "置けないイベントは描かない")
        eq(len(res.missing), 2, "2件とも欠測に記録")
        assert "窓の外" in res.missing[0], res.missing
        assert "直前の採用終値が無い" in res.missing[1], res.missing
        assert "出来事 0/2件を配置" in res.note, res.note
    finally:
        sb.close()


def test_timeline_output_is_deterministic():
    """front matter のイベント記述順に依存しない（日付昇順に固定。D8）。"""
    sb = Sandbox()
    try:
        sb.prices([
            prow("2026-01-05", 500, 500, 500),
            prow("2026-01-07", 600, 600, 600),
            prow("2026-01-09", 550, 550, 550),
        ])
        events = [{"date": "2026-01-09", "label": "後の出来事"},
                  {"date": "2026-01-05", "label": "先の出来事"}]

        def resolve(evs):
            CD.clear_cache()
            return CD.resolve_chart("9999", "c", {
                "type": "timeline", "unit": "円",
                "source": {"dataset": "prices", "window_weeks": 52},
                "events": list(evs)})

        a, b = resolve(events), resolve(reversed(events))
        eq(a.spec, b.spec, "記述順を変えても同じ spec")
        eq(a.note, b.note, "注記も同じ")
        eq([e["date"] for e in a.spec["events"]],
           ["2026-01-05", "2026-01-09"], "日付昇順")
        svg = CH.render(a.spec)
        eq(svg, CH.render(b.spec), "SVG も同じ")
        assert "先の出来事" in svg and "後の出来事" in svg, "ラベルが描かれる"
    finally:
        sb.close()


# =============================================================================
# diagram（定性図。数値を持たない）
# =============================================================================

def test_diagram_is_qualitative_not_hand():
    """定性図は「手書き（未検証）」扱いにしない（数値を主張していない）。"""
    chart = {"type": "diagram", "caption": "収益構造",
             "steps": [
                 {"label": "開発案件（フロー型）", "note": "案件ごとに大きく入る"},
                 {"label": "運用・保守（ストック型）", "note": "毎月少しずつ・安定"},
             ]}
    res = CD.resolve_chart("9999", "d", chart)
    eq(res.origin, "diagram", "hand でも csv でもない")
    eq(res.empty_reason, "", "描ける")
    eq(res.note, CD.DIAGRAM_NOTE, "定性図であることを必ず出す")
    svg = CH.render(res.spec)
    assert svg, "SVG が出る"
    assert "開発案件（フロー型）" in svg and "毎月少しずつ・安定" in svg, svg


def test_diagram_with_digits_is_refused():
    """数字（半角・全角とも）が1文字でも入っていたら描画拒否。

    「定性図」の枠を、未検証の数値の抜け道にしない（D30/D31 の趣旨）。
    """
    bads = [
        [{"label": "売上", "note": "1件あたり大きい"}],   # note に半角数字
        [{"label": "１０年ぶり安値", "note": ""}],        # label に全角数字
    ]
    for steps in bads:
        res = CD.resolve_chart("9999", "d", {"type": "diagram", "steps": steps})
        eq(res.origin, "diagram", f"{steps}: origin")
        eq(res.empty_reason, CD.DIAGRAM_NUMBER_REASON, f"{steps}: 拒否文")
        eq(CH.render(res.spec), "", f"{steps}: 表示側でも描かない")


def test_diagram_without_steps_says_why():
    res = CD.resolve_chart("9999", "d", {"type": "diagram"})
    eq(res.origin, "diagram", "origin")
    assert res.empty_reason, "描けない理由が出ること"


def test_verify_does_not_count_diagram_as_csv_or_hand():
    """定性図は CSV由来にも手書きにも数えない。拒否された図は穴として名指し。"""
    sb = Sandbox()
    try:
        charts = {
            "ok": {"type": "diagram",
                   "steps": [{"label": "フロー", "note": ""}]},
            "bad": {"type": "diagram",
                    "steps": [{"label": "3事業", "note": ""}]},
        }
        v = CD.verify("9999", charts)
        eq((v.charts_csv, v.charts_hand), (0, 0), "どちらにも数えない")
        eq(len(v.chart_gaps), 1, "拒否された図だけが穴")
        assert v.chart_gaps[0].startswith("bad: "), v.chart_gaps
    finally:
        sb.close()


# =============================================================================
# 手書きの図
# =============================================================================

def test_handwritten_chart_keeps_data_and_is_labelled():
    sb = Sandbox()
    try:
        sb.fundamentals("9999", [
            frow("FY2024-06", "cost_ratio", None, "MISMATCH", unit="pct"),
            frow("FY2025-06", "cost_ratio", None, "SINGLE_SOURCE", unit="pct"),
        ])
        chart = {"type": "line", "unit": "%", "metric": "cost_ratio",
                 "data": [{"label": "2025/6", "value": 68.4}]}
        res = CD.resolve_chart("9999", "c", chart)
        eq(res.origin, "hand", "手書き扱い")
        eq(res.spec["data"], chart["data"], "data はそのまま使える")
        assert res.spec["source_note"].startswith("手書き"), res.spec["source_note"]
        assert "採用値は 0件" in res.spec["source_note"], res.spec["source_note"]
        assert "MISMATCH 1件" in res.spec["source_note"], res.spec["source_note"]
    finally:
        sb.close()


def test_handwritten_chart_without_csv_counterpart():
    sb = Sandbox()
    try:
        sb.fundamentals("9999", [])
        chart = {"type": "bar", "unit": "円",
                 "data": [{"label": "x", "value": 1}]}
        res = CD.resolve_chart("9999", "c", chart)
        eq(res.origin, "hand", "手書き扱い")
        assert "検証済みデータが無い" in res.spec["source_note"], \
            res.spec["source_note"]
    finally:
        sb.close()


# =============================================================================
# 集計
# =============================================================================

def test_verify_counts_states_separately():
    sb = Sandbox()
    try:
        sb.fundamentals("9999", [
            frow("FY2024-06", "revenue", 1000, "OK"),
            frow("FY2025-06", "revenue", None, "SINGLE_SOURCE"),
            frow("FY2026-06", "revenue", None, "MISMATCH"),
        ])
        sb.tanshin("9999", [trow("2026-05-15", "revenue", 1493)])
        sb.prices([prow("2026-01-05", 500, 500, 500),
                   prow("2026-01-06", None, 500, 500, status="SINGLE_SOURCE")])
        charts = {
            "auto": {"type": "bar", "unit": "百万円",
                     "source": {"metric": "revenue",
                                "periods": ["FY2024-06", "FY2025-06"]}},
            "hand": {"type": "bar", "unit": "百万円",
                     "data": [{"label": "x", "value": 1}]},
        }
        v = CD.verify("9999", charts)
        eq(v.fund_rows, 3, "行数")
        eq(v.fund_ok, 1, "採用値")
        eq(v.fund_single, 1, "1ソースのみ")
        eq(v.fund_mismatch, 1, "食い違い")
        eq(v.mismatch_items, ["2026/6 売上高"], "食い違った項目")
        eq(v.tanshin_rows, 1, "決算短信の行数")
        eq((v.price_rows, v.price_ok), (2, 1), "株価の採用終値")
        eq((v.charts_csv, v.charts_hand), (1, 1), "図の内訳")
        eq(len(v.chart_gaps), 1, "穴のある図")
    finally:
        sb.close()


# =============================================================================
# 実データに対する不変条件（値をべた書きしない）
# =============================================================================

def _declared_periods(source: dict) -> list:
    out = []
    for item in source.get("periods") or []:
        out.append({"period": item} if isinstance(item, str) else dict(item))
    for key in ("done", "target", "low", "high", "current"):
        item = (source.get("points") or {}).get(key)
        if item is not None:
            out.append(dict(item))
    return out


def test_real_reports_never_show_unadopted_fundamentals():
    """実データ: `source:` の図に、照合を通っていない財務数値が出ていないこと。

    期待値は CSV から引く（数値をテストに書かない）。銘柄・期が増えても壊れない。
    """
    reports_dir = ROOT / "reports"
    if not reports_dir.exists():
        return
    checked = 0
    for path in sorted(reports_dir.glob("*.md")):
        code = path.stem
        rep = R.load_report(code)
        if rep is None:
            continue
        fund = CD.fundamentals(code)
        for cid, chart in sorted((rep.charts or {}).items()):
            source = chart.get("source")
            if not isinstance(source, dict):
                continue
            default_metric = str(source.get("metric", "") or "")
            resolved = CD.resolve_chart(code, cid, chart)
            for item in _declared_periods(source):
                if str(item.get("dataset", "fundamentals")) != "fundamentals":
                    continue
                metric = str(item.get("metric", "") or default_metric)
                period = str(item.get("period", "") or "")
                if not metric or not period:
                    continue
                fact = fund["by_key"].get((metric, period))
                checked += 1
                if fact is not None and fact.adopted:
                    continue
                label = CD.period_label(period)
                shown = [p for p in resolved.spec.get("data", [])
                         if p.get("value") is not None
                         and str(p.get("label")) == label]
                eq(shown, [], f"{code}/{cid} {metric} {period} は採用値でない")
    assert checked > 0, "実データの図を1件も検査できていない（結線が切れている）"


def test_real_reports_charts_resolve_or_say_why():
    """実データ: 図が黙って消えていないこと。描けないなら理由が付くこと。"""
    reports_dir = ROOT / "reports"
    if not reports_dir.exists():
        return
    for path in sorted(reports_dir.glob("*.md")):
        rep = R.load_report(path.stem)
        if rep is None:
            continue
        for cid, res in CD.resolve_charts(path.stem, rep.charts).items():
            if res.origin == "hand":
                assert res.spec.get("source_note"), f"{cid}: 手書きの表示が無い"
                continue
            if res.origin == "diagram":
                # 定性図は数値を持たない。描けたなら「定性図」の1行、
                # 拒否されたなら理由が必ず付く。
                assert res.empty_reason or res.spec.get("source_note"), \
                    f"{cid}: 定性図の表示が無い"
                continue
            if res.used == 0:
                assert res.empty_reason, f"{cid}: 描けない理由が無い"
            else:
                assert res.spec.get("source_note"), f"{cid}: 出所の表示が無い"


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
