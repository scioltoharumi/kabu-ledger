"""予測の機械採点。

要件: requirements.md F3 / gap-analysis.md §5、decisions.md D3/D4、
      review-findings.md F-03（予測3件すべてが判定不能）・F-13（最終行で採点していた）。

設計原則（破らないこと）:
  1. **当否の判定は機械のみ**（D3）。LLM に「当たったか」を判定させない。
     `operator` は `< <= > >=` の4種だけで、比較は数値どうしに限る。
  2. **`resolve_by` 時点の値で採点する**（F-13）。CSV の最終行ではない。
     解決が遅れても採点結果が変わらない＝翌週に甘い解釈をする余地が構造的に無い。
  3. **`resolve_by` が到来するまで採点しない**。株価由来の metric は毎日動くので、
     期限前に「もう当たっている」と確定させると先取りの当たりを作れてしまう。
     期限前は `open` のまま何も書かない。
  4. **一度確定した予測は上書きしない**（`resolved` / `expired` は凍結）。
     status 遷移は `open → resolved` / `open → expired` のみ（CLAUDE.md 不変条件）。
  5. **出力に壁時計を埋め込まない**。`summary.yaml` の `as_of` は日足の最終営業日。
     今日の日付は「期限が到来したか」の判定にだけ使い、出力には書かない。

metric の解決経路は3つ（gap-analysis.md §5 の3群に対応する）:

  ① price  — 日足から `indicators.py` が計算する。**人手ゼロで毎週採点できる**
             rsi14 / ma25_deviation_pct / volume_ratio_3m / ichimoku_position /
             avg_turnover_20d / weekly_ma_mid_* / relative_perf_4w / 12w など
  ② margin — `data/margin/{code}.csv`（信用倍率・買残・売残）
  ③ kpi    — `data/kpi/{code}.csv`。実額はそのまま、比率は**コードが計算する**
             （F8-4。Claude は実額を書くだけで比率を書かない）

旧実装は3経路すべてを kpi CSV に向けていたため、KPI が1本も無い現状では
`avg_turnover_20d`（株価から算出できる値）まで判定不能になっていた（F-03）。

カテゴリ値（雲に対する位置など）の扱い:
  `operator` を `< <= > >=` に限る不変条件を保つため、順序のあるカテゴリは
  **順序数に写像して**比較する。`reference` には水準名をそのまま書ける。
      metric: ichimoku_position, operator: '>=', reference: above   → 1.0 >= 1.0
  水準名は ORDINAL_LEVELS が正。順序の無い分類（status など）は metric にしない。

このモジュールは `judge.py` の I/O ヘルパと `derive_kpi_metrics()` を再利用する。
同じ式を2箇所に書かない（SSoT）。
"""
from __future__ import annotations

import argparse
import csv
import sys
from datetime import date as _date
from pathlib import Path
from typing import Any, NamedTuple, Sequence

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))

import indicators as tech  # noqa: E402
import judge as J  # noqa: E402


# =============================================================================
# 定数
# =============================================================================

OPS = {
    "<":  lambda a, b: a < b,
    "<=": lambda a, b: a <= b,
    ">":  lambda a, b: a > b,
    ">=": lambda a, b: a >= b,
}

SRC_PRICE, SRC_MARGIN, SRC_KPI = "price", "margin", "kpi"

STATUS_OPEN, STATUS_RESOLVED, STATUS_EXPIRED = "open", "resolved", "expired"
RESULT_HIT, RESULT_MISS, RESULT_NA = "的中", "外れ", "判定不能"

# 相対パフォーマンスの分母（F9-1「対 TOPIX の 4週 / 12週」）。
# growth250 は第2ソースが無く close が全行空のため、分母には使えない（sources.yaml 注記）。
RELATIVE_PERF_INDEX = "topix"

# `stock_revenue_ratio`（テーゼ 4073 のストック比率）の分子となるセグメント。
# 語彙は .claude/skills/kabu-ledger/SKILL.md の derived metric 定義が正。
# 他銘柄が別のセグメントを見るようになったら、ここを銘柄別設定に移すこと。
STOCK_REVENUE_SEGMENT = "payment_service"

# 信用残の鮮度。judge の閾値を参照する（再定義しない）。
MARGIN_MAX_AGE_DAYS = J.DEFAULT_JUDGE["margin_max_age_days"]

# 信用残の status のうち、数値を信用してはいけないもの（sources 仕様）。
MARGIN_BAD_STATUS = ("UNIT_UNKNOWN", "BALANCE_MISSING", "RATIO_INCONSISTENT")

# 順序のあるカテゴリ値 → 順序数。**この表が語彙の正**。
ORDINAL_LEVELS: dict[str, dict[str, float]] = {
    "ichimoku_position": {"below": -1.0, "in": 0.0, "above": 1.0},
    "weekly_ma_mid_direction": {"down": -1.0, "flat": 0.0, "up": 1.0},
    "weekly_ma_long_direction": {"down": -1.0, "flat": 0.0, "up": 1.0},
    "daily_cross_kind": {"dead": -2.0, "dead_ish": -1.0, "parallel": 0.0,
                         "golden_ish": 1.0, "golden": 2.0},
}

# 確信度 0.5（＝どちらとも言えない）を出し続けたときのブライアススコア。
# 「当てに行った予測が 0.25 を下回っているか」が唯一の読み方（D4）。
BRIER_BASELINE = 0.25


# =============================================================================
# metric カタログ（予測に書ける metric の全部。ここに無いものは kpi 実額として扱う）
# =============================================================================

class MetricSpec(NamedTuple):
    name: str
    source: str      # price / margin / kpi
    kind: str        # number / ordinal
    unit: str        # 表示単位（無単位は ""）
    label: str
    note: str = ""


CATALOG: tuple[MetricSpec, ...] = (
    # --- ① 株価から計算（人手ゼロ・毎週採点できる） ---------------------------
    MetricSpec("close", SRC_PRICE, "number", "円", "終値",
               "2ソース照合済みの生終値。照合不成立の日は未計算"),
    MetricSpec("ma5", SRC_PRICE, "number", "円", "5日移動平均"),
    MetricSpec("ma25", SRC_PRICE, "number", "円", "25日移動平均"),
    MetricSpec("ma25_deviation_pct", SRC_PRICE, "number", "%", "25日移動平均乖離率",
               "スクリーニング基準5% / 鉄則の過熱ライン8%"),
    MetricSpec("rsi14", SRC_PRICE, "number", "", "RSI(14)",
               "鉄則「RSIが8割超えになっていないか」"),
    MetricSpec("volume_ratio_3m", SRC_PRICE, "number", "倍", "3か月前出来高増加率",
               "スクリーニング基準5倍"),
    MetricSpec("avg_turnover_20d", SRC_PRICE, "number", "円", "20日平均売買代金",
               "流動性ゲート（判定の最上位）の入力"),
    MetricSpec("median_turnover_20d", SRC_PRICE, "number", "円",
               "20日中央値売買代金",
               "平均が単日集中で持ち上がっていないかを見るための補助。ゲートには使わない"),
    MetricSpec("ichimoku_position", SRC_PRICE, "ordinal", "", "雲に対する位置",
               "below=-1 / in=0 / above=1"),
    MetricSpec("ichimoku_cloud_top", SRC_PRICE, "number", "円", "雲の上端"),
    MetricSpec("ichimoku_cloud_bottom", SRC_PRICE, "number", "円", "雲の下端"),
    MetricSpec("weekly_ma_mid_slope_pct", SRC_PRICE, "number", "%/週",
               "週足13週MAの傾き",
               "鉄則の第一条が見る線。「中期」が13週か26週かは未確定なので両方持つ"),
    MetricSpec("weekly_ma_mid_direction", SRC_PRICE, "ordinal", "",
               "週足13週MAの向き", "down=-1 / flat=0 / up=1"),
    MetricSpec("weekly_ma_long_slope_pct", SRC_PRICE, "number", "%/週",
               "週足26週MAの傾き", "信用の期限（6か月）と一致する線"),
    MetricSpec("weekly_ma_long_direction", SRC_PRICE, "ordinal", "",
               "週足26週MAの向き", "down=-1 / flat=0 / up=1"),
    MetricSpec("daily_cross_kind", SRC_PRICE, "ordinal", "", "日足5/25のクロス",
               "dead=-2 / dead_ish=-1 / parallel=0 / golden_ish=1 / golden=2"),
    MetricSpec("relative_perf_4w", SRC_PRICE, "number", "%pt",
               "対TOPIX相対パフォーマンス 4週", "個別騰落率 − TOPIX騰落率"),
    MetricSpec("relative_perf_12w", SRC_PRICE, "number", "%pt",
               "対TOPIX相対パフォーマンス 12週", "個別騰落率 − TOPIX騰落率"),

    # --- ② 信用残高（週次公表・自動取得） -------------------------------------
    MetricSpec("margin_ratio", SRC_MARGIN, "number", "倍", "信用倍率",
               "鉄則「5倍以上は下降の可能性」。売り残0の週は定義できず未計算"),
    MetricSpec("margin_long_balance", SRC_MARGIN, "number", "", "信用買残",
               "単位はページから読んだ unit 列のまま（換算しない）"),
    MetricSpec("margin_short_balance", SRC_MARGIN, "number", "", "信用売残",
               "単位はページから読んだ unit 列のまま（換算しない）"),

    # --- ③ 決算（Claude が実額を抽出 → コードが比率を計算） -------------------
    MetricSpec("revenue_yoy_pct", SRC_KPI, "number", "%", "売上高 前年同四半期比",
               "コードが計算する（judge.derive_kpi_metrics）"),
    MetricSpec("ordinary_income_yoy_pct", SRC_KPI, "number", "%",
               "経常利益 前年同四半期比", "コードが計算する"),
    MetricSpec("q1_progress_pct", SRC_KPI, "number", "%", "1Q進捗率",
               "コードが計算する（period が Q1cum の開示のときのみ）"),
    MetricSpec("stock_revenue_ratio", SRC_KPI, "number", "",
               "ストック売上構成比",
               f"segment_revenue:{STOCK_REVENUE_SEGMENT} ÷ revenue。コードが計算する"),
)

_CATALOG_BY_NAME = {m.name: m for m in CATALOG}

# kpi CSV にそのまま入っている実額 metric（SKILL.md の語彙）。
# カタログに無い metric 名はここに載っているものとみなして CSV を引く。
# 実額の比較は **開示された単位のまま** 行う（unit 列を換算しない）ため、
# 予測の reference も同じ単位で書くこと。
KPI_RAW_PREFIXES = ("segment_revenue:",)
KPI_RAW_METRICS = (
    "revenue", "revenue_prev_year", "revenue_fy_plan",
    "operating_income", "operating_income_prev_year", "operating_income_fy_plan",
    "ordinary_income", "ordinary_income_prev_year", "ordinary_income_fy_plan",
)


def spec_of(metric: str) -> MetricSpec:
    """metric の仕様。カタログに無ければ kpi 実額として扱う。"""
    m = _CATALOG_BY_NAME.get(metric)
    if m is not None:
        return m
    return MetricSpec(metric, SRC_KPI, "number", "", metric,
                      "kpi CSV の実額（単位は unit 列のまま）")


def catalog_rows() -> list[dict[str, str]]:
    """台帳（scoring.html）に「予測に書ける metric」を出すための素材。"""
    return [{"name": m.name, "source": m.source, "kind": m.kind, "unit": m.unit,
             "label": m.label, "note": m.note} for m in CATALOG]


# =============================================================================
# 値の型
# =============================================================================

class MetricValue(NamedTuple):
    """metric 1件の解決結果。

    value が None なら未計算。**未計算は「条件を満たさない」ではない**。
    detail に理由を必ず書く（欠測を黙って通さない）。
    """
    metric: str
    value: float | None
    display: str            # 実値の表示（未計算は "—"）
    source: str             # price / margin / kpi
    as_of: str | None       # その値の基準日（resolve_by ではなく実データの日付）
    detail: str


def _unresolved(metric: str, source: str, detail: str) -> MetricValue:
    return MetricValue(metric, None, "—", source, None, detail)


def _fmt(value: float | None, spec: MetricSpec, raw: Any = None) -> str:
    if value is None:
        return "—"
    if spec.kind == "ordinal":
        return f"{raw}（{value:+.0f}）" if raw is not None else f"{value:+.0f}"
    if spec.unit == "円" and abs(value) >= 10000:
        return f"{value:,.0f}円（{value / 10000:,.1f}万円）"
    return f"{value:,.4f}".rstrip("0").rstrip(".") + spec.unit


def format_value(metric: str, value: Any, raw: Any = None) -> str:
    """metric の単位に沿った表示。台帳・Issue 本文が同じ物差しを使うための入口。

    カテゴリ値（"above" 等の文字列）はそのまま返す。欠測は必ず「—」。
    """
    if value is None:
        return "—"
    if isinstance(value, str):
        return value
    return _fmt(float(value), spec_of(metric), raw)


def _ordinal(metric: str, raw: str | None) -> float | None:
    if raw is None:
        return None
    return ORDINAL_LEVELS.get(metric, {}).get(str(raw))


# =============================================================================
# 相対パフォーマンス（F9 / I-17）
#
# CLAUDE.md は計算先を indicators.py と書いているが、現時点の indicators.py は
# 期間の定数（RELATIVE_PERF_*_PERIODS）だけを持ち関数を持たない。二重実装を避けるため
# **算出はここ1箇所**に置き、定数は indicators.py を参照する。
# indicators.py 側に実装が入ったら、この関数は委譲に置き換えること。
# =============================================================================

def relative_perf_pct(bars: Sequence[tech.Bar], index_bars: Sequence[tech.Bar],
                      periods: int) -> float | None:
    """個別銘柄の騰落率 − 指数の騰落率（%pt）。

    市場要因と個社要因を分離するための指標。**同じ営業日どうし**で比べる
    （個別と指数で基準日がずれると、ずれた日数ぶんの市場変動が個社要因に混ざる）。

    2ソース照合済みの `close` のみを使う。照合不成立（`value_primary` にしか値が
    無い行）は None のままにして未計算とする。埋めない（D7）。
    """
    if not bars or periods is None or periods < 1 or len(bars) < periods + 1:
        return None
    start, end = bars[-1 - periods], bars[-1]
    idx = {b.date: b.close for b in index_bars}
    c0, c1 = start.close, end.close
    i0, i1 = idx.get(start.date), idx.get(end.date)
    if None in (c0, c1, i0, i1) or c0 == 0 or i0 == 0:
        return None
    return (c1 / c0 - 1.0) * 100.0 - (i1 / i0 - 1.0) * 100.0


# =============================================================================
# 読み込み（ここだけがファイルを触る）
# =============================================================================

class Repo:
    """CSV / YAML を1回だけ読み、(code, as_of) 単位で指標をキャッシュする。

    同じ入力からは常に同じ値を返す（決定論的）。テストでは root を差し替えられる。
    """

    def __init__(self, root: Path = ROOT) -> None:
        self.root = Path(root)
        self._master: dict | None = None
        self._bars: dict[str, list[tech.Bar]] = {}
        self._index: dict[str, list[tech.Bar]] = {}
        self._margin: dict[str, list[dict]] = {}
        self._kpi: dict[str, list[dict]] = {}
        self._ind: dict[tuple[str, str], Any] = {}

    # --- 素材 ----------------------------------------------------------------
    def master(self) -> dict:
        if self._master is None:
            p = self.root / "data" / "master.yaml"
            self._master = yaml.safe_load(p.read_text(encoding="utf-8")) if p.exists() else {}
        return self._master

    def _read_csv(self, path: Path) -> list[dict]:
        if not path.exists():
            return []
        with path.open(encoding="utf-8", newline="") as f:
            return list(csv.DictReader(f))

    def bars(self, code: str) -> list[tech.Bar]:
        if code not in self._bars:
            rows = self._read_csv(self.root / "data" / "prices" / "daily.csv")
            self._bars[code] = tech.bars_from_rows(rows, code=code)
        return self._bars[code]

    def index_bars(self, index_id: str = RELATIVE_PERF_INDEX) -> list[tech.Bar]:
        if index_id not in self._index:
            rows = self._read_csv(self.root / "data" / "indices" / f"{index_id}.csv")
            self._index[index_id] = tech.bars_from_rows(rows, code=index_id)
        return self._index[index_id]

    def margin_rows(self, code: str) -> list[dict]:
        if code not in self._margin:
            rows = self._read_csv(self.root / "data" / "margin" / f"{code}.csv")
            self._margin[code] = sorted(
                (r for r in rows if str(r.get("date") or "").strip()),
                key=lambda r: str(r["date"]))
        return self._margin[code]

    def kpi_rows(self, code: str) -> list[dict]:
        if code not in self._kpi:
            rows = self._read_csv(self.root / "data" / "kpi" / f"{code}.csv")
            self._kpi[code] = [r for r in rows if str(r.get("date") or "").strip()]
        return self._kpi[code]

    # --- 派生 ----------------------------------------------------------------
    def bars_upto(self, code: str, as_of: str) -> list[tech.Bar]:
        return [b for b in self.bars(code) if b.date <= as_of]

    def indicators(self, code: str, as_of: str):
        """as_of 以前の日足だけから算出した指標一式。無ければ None。"""
        key = (code, as_of)
        if key not in self._ind:
            bars = self.bars_upto(code, as_of)
            self._ind[key] = J.compute(bars, self.master()) if bars else None
        return self._ind[key]

    def data_as_of(self) -> str | None:
        """日足の最終営業日（台帳の集計基準日）。壁時計の代わりに使う。"""
        rows = self._read_csv(self.root / "data" / "prices" / "daily.csv")
        dates = [str(r.get("date") or "").strip() for r in rows]
        dates = [d for d in dates if d]
        return max(dates) if dates else None


# =============================================================================
# 経路① 株価から計算
# =============================================================================

def _price_raw(ind, metric: str) -> tuple[Any, float | None]:
    """(表示用の生の値, 比較用の数値) を返す。"""
    ich = ind.ichimoku
    table: dict[str, tuple[Any, float | None]] = {
        "close": (ind.close, ind.close),
        "ma5": (ind.ma_short, ind.ma_short),
        "ma25": (ind.ma_mid, ind.ma_mid),
        "ma25_deviation_pct": (ind.ma_deviation_pct, ind.ma_deviation_pct),
        "rsi14": (ind.rsi14, ind.rsi14),
        "volume_ratio_3m": (ind.volume_ratio_3m, ind.volume_ratio_3m),
        "avg_turnover_20d": (ind.avg_turnover_20d, ind.avg_turnover_20d),
        "median_turnover_20d": (ind.median_turnover_20d, ind.median_turnover_20d),
        "ichimoku_position": (ich.position, _ordinal("ichimoku_position", ich.position)),
        "ichimoku_cloud_top": (ich.cloud_top, ich.cloud_top),
        "ichimoku_cloud_bottom": (ich.cloud_bottom, ich.cloud_bottom),
        "weekly_ma_mid_slope_pct": (ind.weekly_ma_mid_slope_pct,
                                    ind.weekly_ma_mid_slope_pct),
        "weekly_ma_mid_direction": (
            ind.weekly_ma_mid_direction,
            _ordinal("weekly_ma_mid_direction", ind.weekly_ma_mid_direction)),
        "weekly_ma_long_slope_pct": (ind.weekly_ma_long_slope_pct,
                                     ind.weekly_ma_long_slope_pct),
        "weekly_ma_long_direction": (
            ind.weekly_ma_long_direction,
            _ordinal("weekly_ma_long_direction", ind.weekly_ma_long_direction)),
        "daily_cross_kind": (ind.daily_cross.kind,
                             _ordinal("daily_cross_kind", ind.daily_cross.kind)),
    }
    return table.get(metric, (None, None))


def resolve_price_metric(code: str, metric: str, as_of: str,
                         repo: Repo) -> MetricValue:
    spec = spec_of(metric)
    bars = repo.bars_upto(code, as_of)
    if not bars:
        return _unresolved(metric, SRC_PRICE,
                           f"{as_of} 以前の日足が data/prices/daily.csv に無い")
    ind = repo.indicators(code, as_of)
    bar_date = bars[-1].date

    if metric in ("relative_perf_4w", "relative_perf_12w"):
        periods = (tech.RELATIVE_PERF_4W_PERIODS if metric == "relative_perf_4w"
                   else tech.RELATIVE_PERF_12W_PERIODS)
        idx = [b for b in repo.index_bars() if b.date <= as_of]
        value = relative_perf_pct(bars, idx, periods)
        if value is None:
            return _unresolved(
                metric, SRC_PRICE,
                f"対{RELATIVE_PERF_INDEX}の相対騰落率を算出できない"
                f"（営業日 {len(bars)}日 / 指数 {len(idx)}日・"
                f"必要 {periods + 1}日、両者の終値が同一営業日で揃うこと）")
        return MetricValue(metric, value, _fmt(value, spec), SRC_PRICE, bar_date,
                           f"{bar_date} 時点・直近{periods}営業日")

    raw, value = _price_raw(ind, metric)
    if value is None:
        return _unresolved(
            metric, SRC_PRICE,
            f"{bar_date} 時点で算出できない（営業日 {len(bars)}日・"
            f"期間不足または終値/出来高の欠測）")
    return MetricValue(metric, value, _fmt(value, spec, raw), SRC_PRICE, bar_date,
                       f"{bar_date} 時点")


# =============================================================================
# 経路② 信用残高
# =============================================================================

def _to_float(text) -> float | None:
    if text is None:
        return None
    s = str(text).strip()
    if s == "":
        return None
    try:
        return float(s)
    except ValueError:
        return None


def resolve_margin_metric(code: str, metric: str, as_of: str,
                          repo: Repo) -> MetricValue:
    """信用残高から引く。**古い残高・信用できない status は未計算にする**。

    「倍率が取れなかった」を「過熱していない」と読み替えないのは judge と同じ。
    ここでは判定ではなく採点なので、読めない値は必ず未計算（判定不能）にする。
    """
    spec = spec_of(metric)
    rows = [r for r in repo.margin_rows(code) if str(r["date"]) <= as_of]
    if not rows:
        return _unresolved(metric, SRC_MARGIN,
                           f"{as_of} 以前の信用残高が data/margin/{code}.csv に無い")
    row = rows[-1]
    m_date = str(row["date"])
    status = str(row.get("status") or "")
    bad = [s for s in MARGIN_BAD_STATUS if s in status]
    if bad:
        return _unresolved(metric, SRC_MARGIN,
                           f"信用残の数値を信用できない（{m_date} status={status}）")

    age = J._days_between(m_date, as_of)
    if age is None:
        return _unresolved(metric, SRC_MARGIN, f"信用残の日付が読めない（{m_date!r}）")
    if age > MARGIN_MAX_AGE_DAYS:
        return _unresolved(
            metric, SRC_MARGIN,
            f"信用残が古い（{m_date}・期限から{age}日前 > {MARGIN_MAX_AGE_DAYS}日）")

    column = {"margin_ratio": "ratio",
              "margin_long_balance": "long_balance",
              "margin_short_balance": "short_balance"}[metric]
    value = _to_float(row.get(column))
    if value is None:
        why = ("売り残0で倍率が定義できない（RATIO_NA）"
               if metric == "margin_ratio" and "RATIO_NA" in status
               else f"{column} が数値として読めない")
        return _unresolved(metric, SRC_MARGIN, f"{why}（{m_date} status={status}）")
    unit = str(row.get("unit") or "").strip()
    tail = f"・単位 {unit}" if unit and metric != "margin_ratio" else ""
    return MetricValue(metric, value, _fmt(value, spec), SRC_MARGIN, m_date,
                       f"{m_date} 公表{tail}")


# =============================================================================
# 経路③ 決算（KPI）
# =============================================================================

def _kpi_pair_ratio(group: dict[str, dict], numerator: str,
                    denominator: str) -> float | None:
    """同一開示内の2行から比を作る。**単位が違えば None**（換算しない）。

    judge.derive_kpi_metrics が持たない比（stock_revenue_ratio）用。
    前年同期比・進捗率は judge 側が正なので、ここでは作らない。
    """
    a, b = group.get(numerator), group.get(denominator)
    if not a or not b:
        return None
    av, bv = _to_float(a.get("value")), _to_float(b.get("value"))
    if av is None or bv is None or bv == 0:
        return None
    if str(a.get("unit") or "").strip() != str(b.get("unit") or "").strip():
        return None
    return av / bv


def resolve_kpi_metric(code: str, metric: str, as_of: str,
                       repo: Repo) -> MetricValue:
    """kpi CSV から引く。比率はコードが計算する（F8-4）。

    行の採否は `date`（開示日）が `as_of` 以前であること。訂正開示は新しい `date` の
    行として積まれるため、同じ metric が複数あれば最も新しい開示を採る（SKILL.md）。
    """
    spec = spec_of(metric)

    # 語彙の誤りは「データが無い」とは別物なので先に切り分ける。
    # KPI CSV に実体が無い現状でも、metric 名の間違いはその場で分かるようにする。
    known = (metric in _CATALOG_BY_NAME or metric in KPI_RAW_METRICS
             or metric.startswith(KPI_RAW_PREFIXES))
    if not known:
        return _unresolved(
            metric, SRC_KPI,
            f"metric '{metric}' は未定義（カタログにも kpi の語彙にも無い）。"
            "予測の登録時に metric 名を確認すること（src/score.py --catalog）")

    rows = [r for r in repo.kpi_rows(code) if str(r["date"]) <= as_of]
    if not rows:
        p = f"data/kpi/{code}.csv"
        return _unresolved(metric, SRC_KPI,
                           f"{as_of} 以前の決算行が {p} に無い（KPI未取得）")

    # --- コードが計算する比率 ---------------------------------------------
    if metric in ("revenue_yoy_pct", "ordinary_income_yoy_pct", "q1_progress_pct"):
        derived = J.derive_kpi_metrics(rows)      # 式の正は judge。再実装しない
        value = derived.get(metric)
        d = derived.get("disclosure_date")
        if value is None:
            return _unresolved(
                metric, SRC_KPI,
                f"直近開示（{d}）から算出できない"
                "（当期・前年同期・通期計画のいずれかが欠測、単位不一致、"
                "または前年同期が0以下）")
        return MetricValue(metric, value, _fmt(value, spec), SRC_KPI, d,
                           f"{d} 開示から算出")

    if metric == "stock_revenue_ratio":
        by_date: dict[str, dict[str, dict]] = {}
        for r in rows:
            by_date.setdefault(str(r["date"]), {})[str(r.get("metric") or "")] = r
        for d in sorted(by_date, reverse=True):
            value = _kpi_pair_ratio(by_date[d],
                                    f"segment_revenue:{STOCK_REVENUE_SEGMENT}",
                                    "revenue")
            if value is not None:
                return MetricValue(metric, value, _fmt(value, spec), SRC_KPI, d,
                                   f"{d} 開示から算出"
                                   f"（segment_revenue:{STOCK_REVENUE_SEGMENT} ÷ revenue）")
        return _unresolved(
            metric, SRC_KPI,
            f"segment_revenue:{STOCK_REVENUE_SEGMENT} と revenue が"
            "同一開示・同一単位で揃っていない")

    # --- Claude が書いた実額をそのまま引く --------------------------------
    hits = [r for r in rows
            if str(r.get("metric") or "") == metric and str(r.get("value") or "").strip()]
    if not hits:
        return _unresolved(
            metric, SRC_KPI,
            f"{as_of} 以前の開示に metric '{metric}' の行が無い"
            f"（data/kpi/{code}.csv）")
    hits.sort(key=lambda r: str(r["date"]))
    row = hits[-1]
    value = _to_float(row.get("value"))
    if value is None:
        return _unresolved(metric, SRC_KPI,
                           f"{row['date']} の value が数値として読めない")
    unit = str(row.get("unit") or "").strip()
    assumed = str(row.get("assumed") or "").strip().lower() == "true"
    detail = f"{row['date']} 開示・単位 {unit or '不明'}"
    if assumed:
        detail += "・**推定値（assumed: true）**"
    return MetricValue(metric, value, f"{value:,.4f}".rstrip("0").rstrip(".") + (
        f" {unit}" if unit else ""), SRC_KPI, str(row["date"]), detail)


# =============================================================================
# 統合
# =============================================================================

def resolve_metric(code: str, metric: str, as_of: str,
                   repo: Repo | None = None) -> MetricValue:
    """`as_of` 以前の最終値を引く（F-13 の修正の本体）。

    経路は metric 名で決まる（カタログ）。カタログに無い名前は kpi 実額として扱う。
    """
    r = repo or Repo()
    spec = spec_of(metric)
    if spec.source == SRC_PRICE:
        return resolve_price_metric(code, metric, as_of, r)
    if spec.source == SRC_MARGIN:
        return resolve_margin_metric(code, metric, as_of, r)
    return resolve_kpi_metric(code, metric, as_of, r)


def load_metric(code: str, metric: str, as_of: str,
                repo: Repo | None = None) -> float | None:
    """後方互換の薄いラッパ。**`as_of` は必須**（旧実装は最終行を返していた）。"""
    return resolve_metric(code, metric, as_of, repo).value


def resolve_reference(pred: dict, as_of: str, repo: Repo) -> MetricValue:
    """`reference` を数値に解決する。

      数値            → そのまま
      カテゴリ水準名  → 同じ metric の順序数（"above" など）
      metric 名       → その metric を同じ as_of で解決
    """
    ref = pred["reference"]
    metric = str(pred["metric"])
    if isinstance(ref, bool):
        return _unresolved("reference", spec_of(metric).source,
                           "reference が真偽値。数値・水準名・metric 名のいずれかにする")
    if isinstance(ref, (int, float)):
        v = float(ref)
        # 表示は metric の単位に合わせる（実値と閾値が同じ物差しで並ぶようにする）
        return MetricValue("reference", v, _fmt(v, spec_of(metric)),
                           "literal", None, "予測に書かれた数値")
    name = str(ref)
    level = _ordinal(metric, name)
    if level is not None:
        return MetricValue("reference", level, f"{name}（{level:+.0f}）",
                           "literal", None, f"{metric} の水準名")
    if name in _CATALOG_BY_NAME or name in KPI_RAW_METRICS \
            or name.startswith(KPI_RAW_PREFIXES):
        return resolve_metric(str(pred["code"]), name, as_of, repo)
    return _unresolved("reference", "literal",
                       f"reference '{name}' を解決できない"
                       f"（数値・{metric} の水準名・metric 名のいずれでもない）")


# =============================================================================
# 採点
# =============================================================================

def is_valid(pred: dict) -> str | None:
    """登録として成立していない予測を弾く（F3-2）。理由を返す。成立していれば None。"""
    for key in ("id", "code", "metric", "operator", "reference", "resolve_by"):
        if key not in pred:
            return f"必須項目 '{key}' が無い"
    if pred["operator"] not in OPS:
        return (f"operator '{pred['operator']}' は機械解決できない"
                f"（{' '.join(sorted(OPS))} のみ）")
    try:
        _date.fromisoformat(str(pred["resolve_by"]))
    except (TypeError, ValueError):
        return f"resolve_by '{pred['resolve_by']}' が日付として読めない"
    c = float(pred.get("confidence", 0.5))
    if not 0.0 <= c <= 1.0:
        return f"confidence {c} が 0〜1 の外"
    return None


def resolve(pred: dict, today: str, repo: Repo) -> dict:
    """予測1件を採点する。

    - `resolved` / `expired` は凍結する（一度出た結論を後から書き換えない）
    - `resolve_by` が到来するまでは `open` のまま**何も書かない**（先取りの当たりを作らない）
    - 到来後は **`resolve_by` 以前の最終値**で採点する。引けなければ `expired`（判定不能）
    """
    out = dict(pred)
    if pred.get("status") in (STATUS_RESOLVED, STATUS_EXPIRED):
        return out

    bad = is_valid(pred)
    if bad:
        # 機械解決できない予測は採点しない。status も動かさない（人間が直す対象）。
        out["invalid"] = bad
        return out

    resolve_by = str(pred["resolve_by"])
    if today <= resolve_by:
        return out                      # 期限前。株価由来 metric を先取りで確定させない

    code = str(pred["code"])
    metric = str(pred["metric"])
    actual = resolve_metric(code, metric, resolve_by, repo)
    reference = resolve_reference(pred, resolve_by, repo)

    if actual.value is None or reference.value is None:
        why = actual.detail if actual.value is None else reference.detail
        out.update(status=STATUS_EXPIRED, result=RESULT_NA,
                   metric_source=actual.source, reason=why)
        return out

    hit = OPS[pred["operator"]](actual.value, reference.value)
    out.update(
        status=STATUS_RESOLVED,
        result=RESULT_HIT if hit else RESULT_MISS,
        actual=round(actual.value, 6),
        actual_display=actual.display,
        reference_value=round(reference.value, 6),
        metric_source=actual.source,
        metric_as_of=actual.as_of,          # 採点に使った値の基準日（壁時計ではない）
        reason=f"{actual.display} {pred['operator']} {reference.display}"
               f"（{actual.detail}）",
    )
    return out


def brier(preds: Sequence[dict]) -> float | None:
    """確信度つきの評価（D4）。低いほど良い。

    的中率だけでは年150件・同一セクター内で相関するため有意差が出ない。
    """
    scored = [p for p in preds if p.get("result") in (RESULT_HIT, RESULT_MISS)]
    if not scored:
        return None
    return sum((float(p.get("confidence", 0.5))
                - (1.0 if p["result"] == RESULT_HIT else 0.0)) ** 2
               for p in scored) / len(scored)


def summarize(preds: Sequence[dict], data_as_of: str | None) -> dict:
    """`scoring/summary.yaml` の中身。**壁時計を埋め込まない**（as_of は日足の最終営業日）。"""
    hit = sum(1 for p in preds if p.get("result") == RESULT_HIT)
    miss = sum(1 for p in preds if p.get("result") == RESULT_MISS)
    na = sum(1 for p in preds if p.get("result") == RESULT_NA)
    open_n = sum(1 for p in preds if p.get("status") == STATUS_OPEN)
    invalid = sorted(f"{p.get('id')}: {p['invalid']}" for p in preds if p.get("invalid"))
    b = brier(preds)

    by_source: dict[str, int] = {}
    for p in preds:
        src = p.get("metric_source") or spec_of(str(p.get("metric", ""))).source
        by_source[src] = by_source.get(src, 0) + 1

    return {
        "as_of": data_as_of,
        "total": len(preds),
        "hit": hit,
        "miss": miss,
        "unresolvable": na,
        "open": open_n,
        "hit_rate": round(hit / (hit + miss), 3) if hit + miss else None,
        "brier": round(b, 4) if b is not None else None,
        "brier_baseline": BRIER_BASELINE,
        "by_metric_source": {k: by_source[k] for k in sorted(by_source)},
        "invalid": invalid,
        "note": "実弾投入の判断材料はブライアススコア（低いほど良い・確信度0.5一律なら"
                f"{BRIER_BASELINE}）。的中率は参考。"
                "採点は resolve_by 時点の値で行い、期限前は open のまま据え置く。"
                "判定不能が多い場合は予測の書き方（metric の取得経路）に問題がある。"
                "as_of は日足の最終営業日であって実行時刻ではない。",
    }


# =============================================================================
# I/O
# =============================================================================

def _write_if_changed(path: Path, text: str) -> bool:
    """内容が変わったときだけ書く（無意味な diff を作らない）。"""
    if path.exists() and path.read_text(encoding="utf-8") == text:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return True


def _dump(obj: Any) -> str:
    return yaml.safe_dump(obj, allow_unicode=True, sort_keys=False)


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="予測を機械採点する")
    ap.add_argument("--dry-run", action="store_true",
                    help="ファイルを書かずに結果だけ表示する")
    ap.add_argument("--today", default=None,
                    help="期限到来の判定に使う日付（既定は今日。テスト用）")
    ap.add_argument("--catalog", action="store_true",
                    help="予測に書ける metric の一覧を表示して終了する")
    args = ap.parse_args(argv)

    if args.catalog:
        print(f"{'metric':<26}{'経路':<8}{'型':<9}{'単位':<6}説明")
        for m in CATALOG:
            print(f"{m.name:<26}{m.source:<8}{m.kind:<9}{m.unit:<6}{m.label} {m.note}")
        print("\n（上記に無い metric 名は data/kpi/{code}.csv の実額として引く）")
        return 0

    today = args.today or _date.today().isoformat()
    repo = Repo()

    all_preds: list[dict] = []
    changed_files: list[str] = []
    for path in sorted((ROOT / "predictions").glob("*.yaml")):
        doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        preds = [resolve(p, today, repo) for p in doc.get("predictions") or []]
        # 出力に残すのは採点結果だけ。invalid は summary で報告し YAML には書かない
        doc["predictions"] = [{k: v for k, v in p.items() if k != "invalid"}
                              for p in preds]
        if not args.dry_run and _write_if_changed(path, _dump(doc)):
            changed_files.append(str(path.relative_to(ROOT)))
        all_preds += preds

    summary = summarize(all_preds, repo.data_as_of())
    if not args.dry_run and _write_if_changed(ROOT / "scoring" / "summary.yaml",
                                              _dump(summary)):
        changed_files.append("scoring/summary.yaml")

    print(f"{'id':<16}{'code':<7}{'metric':<26}{'status':<10}{'result':<8}詳細")
    for p in sorted(all_preds, key=lambda x: str(x.get("id"))):
        detail = p.get("reason") or p.get("invalid") or \
            (f"期限 {p.get('resolve_by')} まで採点しない"
             if p.get("status") == STATUS_OPEN else "")
        print(f"{str(p.get('id')):<16}{str(p.get('code')):<7}"
              f"{str(p.get('metric')):<26}{str(p.get('status')):<10}"
              f"{str(p.get('result') or '—'):<8}{detail}")

    print("\n--- summary ---")
    for k, v in summary.items():
        print(f"  {k}: {v}")
    if summary["invalid"]:
        print("\n[WARN] 機械解決できない予測がある（人間が predictions/*.yaml を直すこと）")
    print(f"\n更新: {', '.join(changed_files) if changed_files else 'なし（差分なし）'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
