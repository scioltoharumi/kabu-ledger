"""図の数値を検証済みの CSV から組み立てる（front matter の手書きを置き換える）。

なぜ要るか:
  レポートの図の数値は front matter に人が転記していた。桁を取り違えても
  誰も気づかない。株価の `close` が「2ソースが一致したときだけ入る採用値」で
  あるのと同じ規律を、図の数値にも通す。

原則（破らないこと）:
  - **採用値だけを使う。** fundamentals は status に `OK` を含み `value` が
    入っている行だけ。`MISMATCH` / `SINGLE_SOURCE` は 0 で埋めず欠測にする（D7）。
  - 手書きの `data:` も引き続き使える（CSV に無い項目のため）。ただし
    図の下に「手書き（未検証）」と必ず出す。黙って同じ見た目で並べない。
  - 単位が族ごと違うもの（円 と %）を換算しない。換算できなければ欠測にする。
  - 生成時刻を埋め込まない（D8）。並びは front matter の記述順で固定する。

front matter の書き方（`source:` があれば CSV から引く）:

    charts:
      revenue_10y:
        type: bar
        unit: 億円
        source:
          metric: revenue
          periods:
            - "FY2016-06"
            - {period: "FY2026-06", metric: revenue_plan, label: "2026/6予",
               note: "会社予想", emphasis: true}
        notes: {"FY2020-06": "過去最高"}
        emphasis: ["FY2020-06"]

    q4_gap:
      type: progress
      source:
        points:
          done:   {dataset: tanshin, metric: operating_income, latest: true,
                   cross: "C2025-07_2026-03"}
          target: {metric: operating_income_plan, period: "FY2026-06"}

    price_range:
      type: range
      source: {dataset: prices, window_weeks: 52}

    events_52w:
      type: timeline           # 採用終値の折れ線に、出来事の点を重ねる
      source: {dataset: prices, window_weeks: 52}
      events:
        - {date: "2026-06-24", label: "10年ぶり安値"}

    biz_model:
      type: diagram            # 定性図。数値を持たない（数字が入れば描画拒否）
      steps:
        - {label: "開発案件（フロー型）", note: "案件ごとに大きく入る"}
        - {label: "運用・保守（ストック型）", note: "毎月少しずつ・安定"}

dataset:
  fundamentals  data/fundamentals/{code}.csv の採用値（別サイト2つ以上が一致）
  tanshin       data/tanshin/{code}.csv（決算短信 PDF＝一次情報）。`cross:` に
                fundamentals の期キーを書くと、そこの観測値と一致するかを
                機械的に確かめて注記に出す（人の目視ではない）
  prices        data/prices/daily.csv の採用終値。**高値・安値は主ソースのみで
                照合を通っていないため使わない**（fetch.reconcile を参照）

図に重ねる装飾（`markers` / `band`）は CSV から引けない手書きなので、
**注記に必ず「手書き（未検証）」と出す**。消す方法は用意しない。
"""
from __future__ import annotations

import csv
import re
from dataclasses import dataclass, field
from datetime import date as _date, timedelta
from pathlib import Path

import yamlio as Y

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"

# 単位 -> (族, 基本単位への係数)。族が違う組み合わせは換算しない。
UNIT_BASE: dict[str, tuple[str, float]] = {
    "JPY_million": ("JPY", 1e6),
    "JPY_thousand": ("JPY", 1e3),
    "JPY": ("JPY", 1.0),
    "億円": ("JPY", 1e8),
    "百万円": ("JPY", 1e6),
    "千円": ("JPY", 1e3),
    "円": ("JPY", 1.0),
    "pct": ("pct", 1.0),
    "%": ("pct", 1.0),
    "％": ("pct", 1.0),
    "x": ("ratio", 1.0),
    "倍": ("ratio", 1.0),
}

# 検証状況の表示で使う日本語名。無いものは metric 名のまま出す。
METRIC_JA = {
    "revenue": "売上高",
    "operating_income": "営業利益",
    "ordinary_income": "経常利益",
    "net_income": "最終利益",
    "eps": "1株益",
    "bps": "1株純資産",
    "operating_margin": "営業利益率",
    "cost_ratio": "売上原価率",
    "sga_ratio": "販管費率",
    "roe": "ROE",
    "roa": "ROA",
    "total_assets": "総資産",
    "net_assets": "純資産",
    "equity": "自己資本",
    "shareholders_equity": "株主資本",
    "equity_ratio": "自己資本比率",
    "retained_earnings": "剰余金",
    "interest_bearing_debt": "有利子負債",
    "interest_bearing_debt_ratio": "有利子負債倍率",
    "operating_cf": "営業CF",
    "investing_cf": "投資CF",
    "financing_cf": "財務CF",
    "free_cf": "フリーCF",
    "capex": "設備投資",
    "cash_equivalents": "現金同等物",
}

# tanshin 側の status で「確かめられていない」ことを表す語。注記に必ず出す。
# `YOY_CHECK_NA` は「該当しない」であって未検証ではないので入れない（D23 と同じ区別）。
TANSHIN_WEAK = ("NOT_CROSS_CHECKED",)
# **PDF 自身の中で自己検算に落ちた**ことを表す語。`fetch_tanshin` は
# これらを INFO_FLAGS（採用は止めない）に置いているため値は残るが、
# 「自己資本比率が純資産÷総資産と合っていない」という検算結果が読者に
# 一切届いていなかった。採用値に格上げせず、理由を注記に出す。
TANSHIN_FAILED = ("EPS_CROSS_FAILED", "EQUITY_CROSS_FAILED")

# 手書き図の `metric:` と CSV の metric 名の対応。checks.py は別名を吸収するが
# chartdata は完全一致だったため、`cost_ratio_pct` のような書き方をすると
# 「data/fundamentals に行が無い」と出て、**矛盾の件数が消えていた**。
METRIC_ALIASES = {
    "cost_ratio_pct": "cost_ratio",
    "cost_of_sales_ratio": "cost_ratio",
    "sga_ratio_pct": "sga_ratio",
    "equity_ratio_pct": "equity_ratio",
    "operating_margin_pct": "operating_margin",
    "gross_margin_pct": "gross_margin",
    "roe_pct": "roe",
    "roa_pct": "roa",
}

# 照合済みの実額から導ける率。**CSV に採用値が無いときだけ**使う。
#
# なぜ要るか:
#   率（operating_margin・cost_ratio）は取得元ごとに計算方法と丸めが違い、
#   二重照合がほとんど通らない。実データでは通期の採用済み営業利益率が
#   銘柄あたり1〜2期しかなく、**傾向を引けない**（率が構造的に上がって
#   いるのか、循環で上下しているだけなのかを区別できない）。一方その
#   分子・分母である営業利益と売上は照合が通っている期が4〜6期ある。
#
#   ここに置くのは**定義が一意に決まる率だけ**。ROE / ROA は分母（自己資本か
#   株主資本か・期末か期中平均か）が取得元ごとに違うので入れない。粗利率
#   （cost_ratio）は分子の売上原価を取得していないので、そもそも導けない。
#
#   metric -> (分子, 分母, 注記に出す式)
DERIVED_RATIOS: dict[str, tuple[str, str, str]] = {
    "operating_margin": ("operating_income", "revenue", "営業利益 ÷ 売上高"),
}


# metric 名の接尾辞 -> 表示。長いものから順に見る（_fy_plan が _plan より先）。
METRIC_SUFFIX = (
    ("_fy_plan", "通期の会社計画"),
    ("_prev_year", "前年同期"),
    ("_prev_fy", "前期末"),
    ("_plan", "会社計画"),
)

# 「別の期の数値」を表す接尾辞。同じ期どうしの突き合わせの対象にしない。
CROSS_SKIP_SUFFIX = ("_fy_plan", "_prev_year", "_prev_fy")

_TANSHIN_PERIOD_RE = re.compile(r"^FY(\d{4})Q([1-4])(?:cum|only)$")
_FUND_FY_RE = re.compile(r"^FY(\d{4})-(\d{2})$")
_FUND_SPAN_RE = re.compile(r"^([QHC])(\d{4})-(\d{2})_(\d{4})-(\d{2})$")


def fiscal_year_of(year: int, month: int, fy_end: int) -> int:
    """その年月が属する年度の「決算年」（2026年4月・3月決算 → 2027）。"""
    return year + 1 if month > fy_end else year


def quarter_of(month: int, fy_end: int) -> int:
    """その月が年度の第何四半期か（年度は決算月の翌月に始まる）。"""
    return ((month - (fy_end % 12) - 1) % 12) // 3 + 1


def same_bucket(a: tuple | None, b: tuple | None) -> bool:
    """2つの期バケットが同じ期を指すか。

    **第1四半期だけは「単独」と「累計」が同じ3か月を指す**（定義上同一）。
    ここを区別したままだと、1Qしか開示の無い銘柄で一次情報と二次情報の
    突き合わせが永久に成立しない（6570 で実際に0件になった）。
    """
    if a is None or b is None:
        return False
    if a == b:
        return True
    return a[0] == b[0] and a[1] == b[1] == 1


def _cross_bucket(text: str, fy_end: int | None = None) -> tuple | None:
    """期の表記を (年度末年, 四半期数, 累計か) に正規化する。

    決算短信の期表記（"FY2026Q3cum"）と fundamentals の期キー
    （"C2025-07_2026-03"）は同じ期を指せても文字列が違う。読めなければ
    None（＝比べない）。1銘柄が複数の開示（3Q・通期…）を持つようになると、
    metric 名だけで突き合わせた行は違う期どうしを比べて必ず食い違う。

    単独四半期（"Q2026-04_2026-06"）は決算月（`fy_end`）が分からないと
    年度の第何四半期かが決まらない。渡されなければ None（＝比べない）。
    """
    t = str(text or "").strip()
    m = _TANSHIN_PERIOD_RE.match(t)
    if m:
        return (int(m.group(1)), int(m.group(2)), "cum" in t)
    m = _FUND_SPAN_RE.match(t)
    if m:
        tag = m.group(1)
        if tag == "Q":
            if fy_end is None:
                return None      # 決算月が分からない＝第何四半期か決められない
            y1, m1 = int(m.group(2)), int(m.group(3))
            y2, m2 = int(m.group(4)), int(m.group(5))
            if (y2 * 12 + m2) - (y1 * 12 + m1) != 2:
                return None      # 3か月でない＝単独四半期ではない
            return (fiscal_year_of(y1, m1, fy_end),
                    quarter_of(m1, fy_end), False)
        start = int(m.group(2)) * 12 + int(m.group(3))
        end = int(m.group(4)) * 12 + int(m.group(5))
        months = end - start + 1
        if months <= 0:
            return None
        # 決算期末（年度が終わる月）＝期首から11か月後。fetch_fundamentals の
        # 期キー生成（`_ym_from_index(start + 11)` 相当）と同じ考え方。
        fy_index = start + 11
        fy_year = (fy_index - 1) // 12
        return (fy_year, months // 3, True)
    m = _FUND_FY_RE.match(t)
    if m:
        return (int(m.group(1)), 4, True)   # 通期＝4Q累計
    return None


def metric_ja(metric: str) -> str:
    base = str(metric or "")
    tag = ""
    for suffix, label in METRIC_SUFFIX:
        if base.endswith(suffix):
            base = base[: -len(suffix)]
            tag = label
            break
    name = METRIC_JA.get(base, base)
    if tag:
        return name + "（" + tag + "）"
    return name


def flags_of(status) -> list[str]:
    return [p for p in str(status or "").split("|") if p]


def convert(value: float, src_unit, dst_unit) -> float | None:
    """単位を換算する。族が違う・未知の単位なら None（＝使わない）。"""
    a = UNIT_BASE.get(str(src_unit or "").strip())
    b = UNIT_BASE.get(str(dst_unit or "").strip())
    if a is None or b is None or a[0] != b[0]:
        return None
    return round(value * a[1] / b[1], 6)


def period_label(key: str) -> str:
    """期キーを図の x ラベルにする。読めない形はキーをそのまま返す。"""
    k = str(key or "")
    if k.startswith("FY") and len(k) >= 9:
        return k[2:6] + "/" + k[7:9].lstrip("0")
    if (k.startswith("Q") or k.startswith("H") or k.startswith("C")) and "_" in k:
        head, tail = k[1:].split("_", 1)
        y1, m1 = head[:4], head[5:7].lstrip("0")
        y2, m2 = tail[:4], tail[5:7].lstrip("0")
        if y1 == y2:
            return y1[2:] + "/" + m1 + "-" + m2
        return y1[2:] + "/" + m1 + "-" + y2[2:] + "/" + m2
    return k


# --- CSV の読み込み -------------------------------------------------------

@dataclass(frozen=True)
class Fact:
    """1つの数値と、それがどこまで確かめられているか。"""
    dataset: str
    period: str
    metric: str
    value: float | None
    unit: str
    status: str
    source_url: str
    label: str = ""

    @property
    def adopted(self) -> bool:
        if self.value is None:
            return False
        if self.dataset == "fundamentals":
            return "OK" in flags_of(self.status)
        # tanshin は一次情報だが、**PDF 内の自己検算に落ちた行は採用しない**。
        # `fetch_tanshin` の検算結果が表示に一切届いていなかった。
        return not any(f in TANSHIN_FAILED for f in flags_of(self.status))


def _num(v) -> float | None:
    s = str(v or "").strip().replace(",", "")
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _read(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


_FUND_CACHE: dict[str, dict] = {}
_TANSHIN_CACHE: dict[str, dict] = {}
_PRICE_CACHE: dict[str, list] = {}


def fundamentals(code: str) -> dict:
    """{'facts': [Fact], 'by_key': {(metric, period): Fact},
        'observed': {(metric, period): [(source, value)]},
        'tolerance': {(metric, period): float}}"""
    if code in _FUND_CACHE:
        return _FUND_CACHE[code]
    rows = _read(DATA / "fundamentals" / f"{code}.csv")
    facts: list[Fact] = []
    observed: dict[tuple[str, str], list[tuple[str, float]]] = {}
    tolerance: dict[tuple[str, str], float] = {}
    for r in rows:
        metric = str(r.get("metric", "") or "").strip()
        period = str(r.get("period", "") or "").strip()
        if not metric or not period:
            continue
        f = Fact(dataset="fundamentals", period=period, metric=metric,
                 value=_num(r.get("value")), unit=str(r.get("unit", "") or ""),
                 status=str(r.get("status", "") or ""),
                 source_url=str(r.get("source_url_primary", "") or ""),
                 label=period_label(period))
        facts.append(f)
        key = (metric, period)
        tol = _num(r.get("tolerance"))
        if tol is not None:
            tolerance[key] = tol
        obs: list[tuple[str, float]] = []
        for part in str(r.get("sources_all", "") or "").split("|"):
            if "=" not in part:
                continue
            name, raw = part.split("=", 1)
            v = _num(raw)
            if v is not None:
                obs.append((name.strip(), v))
        observed[key] = obs
    out = {
        "facts": facts,
        "by_key": {(f.metric, f.period): f for f in facts},
        "observed": observed,
        "tolerance": tolerance,
    }
    _FUND_CACHE[code] = out
    return out


def tanshin(code: str) -> dict:
    """{'facts': [Fact], 'by_metric': {metric: [Fact]（日付昇順）}}"""
    if code in _TANSHIN_CACHE:
        return _TANSHIN_CACHE[code]
    rows = _read(DATA / "tanshin" / f"{code}.csv")
    facts: list[Fact] = []
    for r in rows:
        metric = str(r.get("metric", "") or "").strip()
        day = str(r.get("date", "") or "").strip()
        if not metric or not day:
            continue
        definition = str(r.get("definition", "") or "")
        head = definition.split("|")[0] if definition else ""
        facts.append(Fact(
            dataset="tanshin", period=day, metric=metric,
            value=_num(r.get("value")), unit=str(r.get("unit", "") or ""),
            status=str(r.get("status", "") or ""),
            source_url=str(r.get("source_url", "") or ""),
            label=head or day))
    facts.sort(key=lambda f: (f.metric, f.period))
    by_metric: dict[str, list[Fact]] = {}
    for f in facts:
        by_metric.setdefault(f.metric, []).append(f)
    out = {"facts": facts, "by_metric": by_metric}
    _TANSHIN_CACHE[code] = out
    return out


def adopted_close(row: dict) -> float | None:
    """その行の採用終値。**照合が成立していない行は None**。

    `close` の有無だけで判断してはならない。旧 fetch.py は売買不成立の日に
    照合結果を潰して `NO_TRADE` 単独の status を書いていたため、実データには
    **照合を通っていないのに `close` が入っている行が7行ある**
    （`checks.LEGACY_NO_TRADE_STATUS` の例外・append-only なので直せない）。
    ここで status を見ないと、その7行が図の 52週レンジに混ざる。
    """
    v = _num(row.get("close"))
    if v is None:
        return None
    if "OK" not in flags_of(row.get("status")):
        return None
    return v


def prices(code: str) -> list[tuple[str, float]]:
    """(日付, 採用終値) を昇順で返す。**照合が成立した行だけ**（status に OK）。"""
    if code in _PRICE_CACHE:
        return _PRICE_CACHE[code]
    rows = _read(DATA / "prices" / "daily.csv")
    out: list[tuple[str, float]] = []
    for r in rows:
        if str(r.get("code", "")).strip() != code:
            continue
        v = adopted_close(r)
        if v is None:
            continue
        out.append((str(r.get("date", "")), v))
    out.sort()
    _PRICE_CACHE[code] = out
    return out


def price_days(code: str) -> tuple[int, int]:
    """(その銘柄の総行数, うち採用終値が入っている行数)。

    `prices()` と同じ基準で数える。片方だけ緩いと、台帳の「採用終値 N/M日」が
    図に使った点数より多く出る（表示の嘘）。
    """
    rows = _read(DATA / "prices" / "daily.csv")
    mine = [r for r in rows if str(r.get("code", "")).strip() == code]
    ok = sum(1 for r in mine if adopted_close(r) is not None)
    return len(mine), ok


def clear_cache() -> None:
    """テスト用。データを差し替えたら呼ぶ。"""
    _FUND_CACHE.clear()
    _TANSHIN_CACHE.clear()
    _PRICE_CACHE.clear()


# --- 1点を引く -----------------------------------------------------------

@dataclass
class Picked:
    """図の1点。値が入らなかった場合は reason に理由が入る。"""
    label: str
    value: float | None = None
    note: str = ""
    emphasis: bool = False
    reason: str = ""          # 欠測の理由（空なら採用できた）
    origin: str = ""          # "fundamentals" / "tanshin" / "prices"
    detail: str = ""          # 注記に出す短い説明


def _cross_note(code: str, fact: Fact, cross_period: str) -> str:
    """tanshin の値が fundamentals の観測値と一致するかを機械的に確かめる。

    一致すれば「一次情報と二次情報が一致」＝株価の2ソース照合と同じ意味を持つ。
    どちらも書き換えない（読むだけ）ので append-only を侵さない。
    """
    if not cross_period or fact.value is None:
        return ""
    fund = fundamentals(code)
    key = (fact.metric, cross_period)
    obs = fund["observed"].get(key) or []
    if not obs:
        return "照合相手なし"
    tol = fund["tolerance"].get(key)
    unit = ""
    other = fund["by_key"].get(key)
    if other is not None:
        unit = other.unit
    hits = []
    for name, raw in obs:
        v = convert(raw, unit, fact.unit) if unit else raw
        if v is None:
            continue
        limit = tol if tol is not None else 0.0
        if abs(v - fact.value) <= limit:
            hits.append(name)
    if hits:
        return "／".join(sorted(hits)) + " と一致"
    return "照合不成立"


def _derive_ratio(code: str, metric: str, period: str, unit: str,
                  csv_flags: str = "") -> Picked | None:
    """採用済みの実額から率を導く。導けなければ None（欠測の理由は呼び出し側）。

    **導出値は採用値ではない。** CSV には書かない（append-only を侵さない）し、
    図に出るときは注記で必ず「実額から導出」と言う。黙って採用値と同じ顔で
    並べないのは、手書き `data:` に「手書き（未検証）」が必ず付くのと同じ規律
    （D30/D31/D54）。分子・分母の**どちらかが照合を通っていなければ導かない**。
    """
    spec = DERIVED_RATIOS.get(metric)
    if spec is None:
        return None
    num_metric, den_metric, expr = spec
    fund = fundamentals(code)
    num = fund["by_key"].get((num_metric, period))
    den = fund["by_key"].get((den_metric, period))
    if num is None or den is None or not num.adopted or not den.adopted:
        return None
    n = convert(num.value, num.unit, den.unit)   # 分子と分母の単位を揃える
    if n is None or not den.value:               # 0 除算は導出しない
        return None
    v = convert(n / den.value * 100.0, "pct", unit)
    if v is None:                                # 率を円で描けと言われた等
        return None
    detail = "実額から導出（" + expr + "）。分子・分母とも照合済み"
    if csv_flags:
        detail = detail + "／CSV の率は " + csv_flags
    return Picked(label=period_label(period), value=v,
                  origin="fundamentals", detail=detail)


def _pick_fundamentals(code: str, metric: str, period: str,
                       unit: str) -> Picked:
    fund = fundamentals(code)
    fact = fund["by_key"].get((metric, period))
    label = period_label(period)
    if fact is None:
        return _derive_ratio(code, metric, period, unit) or Picked(
            label=label, reason="CSV に行が無い")
    if not fact.adopted:
        flags = "／".join(flags_of(fact.status)) or "不明"
        derived = _derive_ratio(code, metric, period, unit, csv_flags=flags)
        if derived is not None:
            return derived
        return Picked(label=label, reason="照合が成立していない（" + flags + "）")
    v = convert(fact.value, fact.unit, unit)
    if v is None:
        return Picked(label=label,
                      reason="単位を換算できない（" + fact.unit + " → " + str(unit) + "）")
    return Picked(label=label, value=v, origin="fundamentals")


def _pick_tanshin(code: str, metric: str, unit: str, item: dict) -> Picked:
    tan = tanshin(code)
    cands = tan["by_metric"].get(metric) or []
    day = str(item.get("date", "") or "")
    if day:
        cands = [f for f in cands if f.period == day]
    if not cands:
        return Picked(label=metric, reason="決算短信に該当行が無い")
    fact = cands[-1]          # 日付昇順。latest = 最後
    label = fact.label or fact.period
    if fact.value is None:
        return Picked(label=label, reason="値が空")
    failed = [w for w in TANSHIN_FAILED if w in flags_of(fact.status)]
    if failed:
        return Picked(label=label,
                      reason="決算短信の中の自己検算に落ちている（"
                             + "／".join(failed) + "）")
    v = convert(fact.value, fact.unit, unit)
    if v is None:
        return Picked(label=label,
                      reason="単位を換算できない（" + fact.unit + " → " + str(unit) + "）")
    cross = _cross_note(code, fact, str(item.get("cross", "") or ""))
    weak = [w for w in TANSHIN_WEAK if w in flags_of(fact.status)]
    extra = [x for x in (cross, "／".join(weak)) if x]
    detail = "決算短信 " + fact.period + "（一次情報）"
    if extra:
        detail = detail + "・" + "、".join(extra)
    return Picked(label=label, value=v, origin="tanshin", detail=detail)


def _pick(code: str, item, default_metric: str, unit: str) -> Picked:
    """periods / points の1要素を Picked にする。"""
    if isinstance(item, str):
        item = {"period": item}
    if not isinstance(item, dict):
        return Picked(label=str(item), reason="書き方を解釈できない")
    dataset = str(item.get("dataset", "") or "fundamentals")
    metric = str(item.get("metric", "") or default_metric)
    if not metric:
        return Picked(label=str(item.get("label", "")), reason="metric が無い")
    if dataset == "tanshin":
        p = _pick_tanshin(code, metric, unit, item)
    else:
        period = str(item.get("period", "") or "")
        if not period:
            return Picked(label=str(item.get("label", "")), reason="period が無い")
        p = _pick_fundamentals(code, metric, period, unit)
    if item.get("label"):
        p.label = str(item["label"])
    if item.get("note"):
        p.note = str(item["note"])
    if item.get("emphasis"):
        p.emphasis = True
    return p


# --- 図の解決 -------------------------------------------------------------

@dataclass
class Resolved:
    """chart.render に渡せる spec と、その出所の説明。"""
    spec: dict
    origin: str                       # "csv" / "hand" / "mixed"
    note: str = ""                    # 図の下に出す1行
    missing: list[str] = field(default_factory=list)
    used: int = 0
    total: int = 0
    empty_reason: str = ""


def _series(code: str, source: dict, chart: dict,
            unit: str) -> tuple[list[dict], list[str], int, list[str]]:
    """bar / line の data を組み立てる。

    戻り値 (data, 欠測の説明, 採用点数, 採用した点の出所).
    **出所を返すのが要**。旧実装は図の見出しに一律「別サイト2つ以上が一致した値だけ」と
    書いていたが、`dataset: tanshin` の点はまとめサイト側が SINGLE_SOURCE の期でも
    描かれる。見出しの主張がその点については成立していない（表示の嘘・D32 と同型）。
    """
    notes = chart.get("notes") or {}
    emph = chart.get("emphasis") or []
    data: list[dict] = []
    missing: list[str] = []
    origins: list[str] = []
    used = 0
    for item in source.get("periods") or []:
        key = item if isinstance(item, str) else str(
            (item or {}).get("period", "") or (item or {}).get("metric", ""))
        p = _pick(code, item, str(source.get("metric", "") or ""), unit)
        if isinstance(notes, dict) and not p.note and notes.get(key):
            p.note = str(notes[key])
        if isinstance(emph, list) and key in emph:
            p.emphasis = True
        if p.value is None:
            missing.append(p.label + "（" + (p.reason or "理由不明") + "）")
        else:
            used += 1
            origins.append(p.origin)
            if p.detail:
                p.note = (p.note + " / " + p.detail) if p.note else p.detail
        data.append({"label": p.label, "value": p.value, "note": p.note,
                     "emphasis": p.emphasis})
    return data, missing, used, origins


def _points(code: str, source: dict,
            unit: str) -> tuple[dict, list[str], int, int, list[str]]:
    """progress / range の単発値。戻り値 (値の辞書, 欠測, 採用数, 総数, 出所)。"""
    out: dict = {}
    missing: list[str] = []
    origins: list[str] = []
    used = 0
    points = source.get("points") or {}
    keys = [k for k in ("done", "target", "low", "high", "current") if k in points]
    for k in keys:
        p = _pick(code, points[k], str(source.get("metric", "") or ""), unit)
        if p.value is None:
            missing.append(k + "（" + (p.reason or "理由不明") + "）")
            continue
        used += 1
        out[k] = p.value
        origins.append(p.origin)
        if p.detail:
            out.setdefault("_details", []).append(k + ": " + p.detail)
    return out, missing, used, len(keys), origins


def _price_range(code: str, source: dict, unit: str) -> tuple[dict, list[str], str]:
    """52週の採用終値から low / high / current を出す。"""
    series = prices(code)
    if not series:
        return {}, ["採用終値が1日も無い"], ""
    weeks = int(source.get("window_weeks", 52) or 52)
    last_day, last_close = series[-1]
    try:
        start = (_date.fromisoformat(last_day) - timedelta(days=weeks * 7 - 1)).isoformat()
    except ValueError:
        return {}, ["最終営業日を読めない（" + last_day + "）"], ""
    window = [x for x in series if x[0] >= start]
    if not window:
        return {}, ["窓のなかに採用終値が無い"], ""
    lo = min(window, key=lambda x: x[1])
    hi = max(window, key=lambda x: x[1])
    lo_v = convert(lo[1], "円", unit)
    hi_v = convert(hi[1], "円", unit)
    cur_v = convert(last_close, "円", unit)
    if lo_v is None or hi_v is None or cur_v is None:
        return {}, ["単位を換算できない（円 → " + str(unit) + "）"], ""
    detail = (str(weeks) + "週の採用終値 " + str(len(window)) + "営業日。"
              "安値 " + lo[0] + " / 高値 " + hi[0] + " / 現在 " + last_day)
    return {"low": lo_v, "high": hi_v, "current": cur_v}, [], detail


def _timeline(code: str, source: dict, chart: dict,
              unit: str) -> tuple[dict, list[str], int, int, str]:
    """timeline 型: 窓の中の採用終値の折れ線に、出来事（events）の点を重ねる。

    戻り値 (spec に足す値, 欠測の説明, 採用数, 総数, 注記の詳細)。
    y は必ず採用終値から引く（D53）。event の日に採用終値が無ければ
    **窓の中の直前の採用終値**に置き、そのことを点の注記に出す。
    窓に置けない event は黙って捨てず missing に記録する。
    """
    unit = unit or "円"
    series = prices(code)
    if not series:
        return {}, ["採用終値が1日も無い"], 0, 0, ""
    weeks = int(source.get("window_weeks", 52) or 52)
    last_day, _last_close = series[-1]
    try:
        start = (_date.fromisoformat(last_day)
                 - timedelta(days=weeks * 7 - 1)).isoformat()
    except ValueError:
        return {}, ["最終営業日を読めない（" + last_day + "）"], 0, 0, ""
    window = [x for x in series if x[0] >= start]
    if not window:
        return {}, ["窓のなかに採用終値が無い"], 0, 0, ""

    data: list[dict] = []
    for day, close in window:
        v = convert(close, "円", unit)
        if v is None:
            return {}, ["単位を換算できない（円 → " + str(unit) + "）"], 0, 0, ""
        data.append({"label": day, "value": v})

    declared = chart.get("events") or []
    missing: list[str] = []
    normalized: list[tuple[str, str]] = []
    for ev in declared:
        if not isinstance(ev, dict):
            missing.append(str(ev) + "（イベントの書き方を解釈できない）")
            continue
        day = str(ev.get("date", "") or "")
        label = str(ev.get("label", "") or "") or day or "（無題）"
        normalized.append((day, label))

    placed: list[dict] = []
    # 並びは front matter の記述順に任せず日付昇順で固定する（決定論・D8）。
    for day, label in sorted(normalized):
        if not day:
            missing.append(label + "（date が無い）")
            continue
        if day < start or day > last_day:
            missing.append(label + "（窓の外: " + day + "）")
            continue
        prior = [p for p in data if p["label"] <= day]
        if not prior:
            missing.append(label + "（直前の採用終値が無い: " + day + "）")
            continue
        anchor = prior[-1]
        item = {"date": day, "label": label, "value": anchor["value"]}
        if anchor["label"] != day:
            # その日の採用終値が無い（休場・照合不成立）。直前値で置いたことを隠さない。
            item["note"] = "値は直前 " + anchor["label"] + " の採用終値"
        placed.append(item)

    used = len(data) + len(placed)
    total = len(data) + len(declared)
    detail = (str(weeks) + "週の採用終値 " + str(len(data)) + "営業日、"
              "出来事 " + str(len(placed)) + "/" + str(len(declared)) + "件を配置")
    return {"data": data, "events": placed}, missing, used, total, detail


# diagram（定性図）に数字が入っていたときの拒否文。checks・テストと共有する。
# chart._DIGIT_RE と同じ線引き（全角数字・丸数字・括弧付き数字・上付き数字も数字）。
# 漢数字は「一部」「二本立て」など一般語に含まれるため対象外。
_DIAGRAM_DIGIT_RE = re.compile(r"[0-9０-９①-⒛⁰-⁹¹²³]")
DIAGRAM_NUMBER_REASON = ("図解に数値が含まれている"
                         "（数値は検証済みデータ由来の図でしか出さない）")
DIAGRAM_NOTE = "定性図（数値を含まない）"


def _resolve_diagram(spec: dict) -> Resolved:
    """diagram 型（定性図）。数値を持たない構造図なので CSV とは突き合わせない。

    その代わり **数字が1文字でも入っていたら描画を拒否する**。
    「手書き（未検証）」の枠を数値の抜け道にしない（D30/D31 の趣旨を守る
    機械的な線引き）。全角数字（０-９）も同じ扱い。
    """
    steps = spec.get("steps")
    if not isinstance(steps, list) or not steps:
        return Resolved(spec=spec, origin="diagram",
                        empty_reason="steps が無い（定性図は steps で構造を書く）")
    for s in steps:
        if not isinstance(s, dict) or not str(s.get("label", "") or "").strip():
            return Resolved(spec=spec, origin="diagram",
                            empty_reason="label の無い step がある")
        text = str(s.get("label", "") or "") + str(s.get("note", "") or "")
        if _DIAGRAM_DIGIT_RE.search(text):
            return Resolved(spec=spec, origin="diagram",
                            empty_reason=DIAGRAM_NUMBER_REASON)
    spec["source_note"] = DIAGRAM_NOTE
    return Resolved(spec=spec, origin="diagram", note=DIAGRAM_NOTE)


def _hand_note(code: str, chart: dict) -> str:
    """手書きの図に添える説明。CSV に同じ metric があれば状態も出す。"""
    metric = str(chart.get("metric", "") or "")
    base = "手書き（未検証）。front matter に人が転記した数値"
    if not metric:
        return base + "。突き合わせられる検証済みデータが無い項目"
    # 別名を吸収する（checks.py の metric_of と同じ問いに答える）。
    canon = METRIC_ALIASES.get(metric, metric)
    facts = [f for f in fundamentals(code)["facts"] if f.metric == canon]
    if not facts:
        return base + "。data/fundamentals に " + metric + " の行が無い"
    metric = canon
    ok = sum(1 for f in facts if f.adopted)
    counts: dict[str, int] = {}
    for f in facts:
        if f.adopted:
            continue
        for fl in flags_of(f.status):
            if fl in ("MISMATCH", "SINGLE_SOURCE"):
                counts[fl] = counts.get(fl, 0) + 1
    detail = "／".join(k + " " + str(counts[k]) + "件" for k in sorted(counts))
    tail = "。data/fundamentals の " + metric_ja(metric) + " は " + \
        str(len(facts)) + "件あるが採用値は " + str(ok) + "件"
    if detail:
        tail = tail + "（" + detail + "）"
    return base + tail


def _overlay_notes(spec: dict) -> list[str]:
    """図に重ねた装飾のうち、CSV から引いていないものを名指しする。

    `markers`（印）と `band`（参考帯）は front matter に人が書いた数値で、
    どちらも `chartdata` の照合を1度も通っていない。**帯だけが無言だった**：
    4937 の「0〜10%」は筆者が置いた目安なのに、図の上では検証済みの棒と
    同じ画面に同じ確からしさで乗る（D32「表示の嘘」と同型）。
    caption に書くかどうかを書き手の裁量に委ねず、機械が必ず出す。
    """
    out: list[str] = []
    if spec.get("markers"):
        out.append("図中の印は手書き（未検証）")
    band = spec.get("band")
    if isinstance(band, (list, tuple)) and len(band) == 2:
        label = str(spec.get("band_label", "") or "").strip()
        text = "背景の帯（" + str(band[0]) + "〜" + str(band[1]) + "）は手書き（未検証）"
        if not label:
            text = text + "・帯の意味を書いた band_label が無い"
        out.append(text)
    elif band:
        out.append("band の書き方が [下限, 上限] でないため帯は描かれていない")
    return out


def _origin_note(code: str, origins: list[str]) -> str:
    """実際に使った点の出所から、図の下に出す1行を組み立てる。

    **見出しは、その図のすべての点について成立する主張でなければならない。**
    「別サイト2つ以上が一致した値だけ」と書いた図に、まとめサイト側が
    SINGLE_SOURCE の期の決算短信の値が1点混ざっていた（表示の嘘）。
    """
    kinds = sorted(set(o for o in origins if o))
    fund = "data/fundamentals/" + code + ".csv の採用値（別サイト2つ以上が一致した値だけ）"
    tan = "data/tanshin/" + code + ".csv（決算短信PDF＝一次情報。まとめサイト側の照合状態は点ごとの注記を見る）"
    if kinds == ["tanshin"]:
        return tan
    if "tanshin" in kinds:
        n = sum(1 for o in origins if o == "tanshin")
        return (fund + "。ただし " + str(n) + "点は " + tan)
    return fund


def resolve_chart(code: str, cid: str, chart: dict) -> Resolved:
    """front matter の図1件を、描ける spec と出所の説明にする。"""
    spec = dict(chart or {})
    source = spec.pop("source", None)
    spec.pop("notes", None)
    spec.pop("emphasis", None)
    unit = str(spec.get("unit", "") or "")
    kind = str(spec.get("type", "") or "")

    # 定性図は source を持たない（＝手書き扱いにしない）。数字の混入だけ見る。
    if kind == "diagram":
        return _resolve_diagram(spec)

    if not isinstance(source, dict):
        bits = [_hand_note(code, chart or {})] + _overlay_notes(spec)
        spec["source_note"] = "。".join(bits)
        return Resolved(spec=spec, origin="hand", note=spec["source_note"])

    dataset = str(source.get("dataset", "") or "fundamentals")
    path_note = ""
    missing: list[str] = []
    origins: list[str] = []
    used = 0
    total = 0
    detail = ""

    if dataset == "prices":
        if kind == "timeline":
            got, missing, used, total, detail = _timeline(
                code, source, chart or {}, unit)
            spec.pop("events", None)   # 生の宣言を、配置済みのイベントで置き換える
            spec.update(got)
        else:
            got, missing, detail = _price_range(code, source, unit)
            spec.update(got)
            used = len(got)
            total = 3
        origins = ["prices"] * used
        path_note = ("data/prices/daily.csv の採用終値（2つの取得元が一致した終値だけ）。"
                     "ザラ場の高値・安値は照合を通っていないので使っていない")
    elif source.get("points"):
        got, missing, used, total, origins = _points(code, source, unit)
        details = got.pop("_details", [])
        spec.update(got)
        detail = "／".join(details)
    else:
        data, missing, used, origins = _series(code, source, chart or {}, unit)
        spec["data"] = data
        total = len(data)
    if not path_note:
        path_note = _origin_note(code, origins)

    if used == 0:
        reason = "検証済みの数値が1件も無い"
        if missing:
            reason = reason + "（" + "／".join(missing[:3]) + "）"
        return Resolved(spec=spec, origin="csv", note="", missing=missing,
                        used=0, total=total, empty_reason=reason)

    head = "出所: " + path_note
    tail = [str(used) + "/" + str(total) + "点を採用"]
    if detail:
        tail.append(detail)
    if missing:
        tail.append("欠測 " + "／".join(missing))
    tail += _overlay_notes(spec)
    note = head + "。" + "、".join(tail) + "。"
    spec["source_note"] = note
    return Resolved(spec=spec, origin="csv", note=note,
                    missing=missing, used=used, total=total)


def resolve_charts(code: str, charts: dict) -> dict[str, Resolved]:
    out: dict[str, Resolved] = {}
    for cid in sorted(charts or {}):
        chart = charts[cid]
        if not isinstance(chart, dict):
            continue
        out[cid] = resolve_chart(code, cid, chart)
    return out


# --- 検証状況（台帳に出す） ------------------------------------------------

@dataclass
class Verification:
    code: str
    fund_rows: int = 0
    fund_ok: int = 0
    fund_mismatch: int = 0
    fund_single: int = 0
    mismatch_items: list[str] = field(default_factory=list)
    tanshin_rows: int = 0
    tanshin_dates: list[str] = field(default_factory=list)
    tanshin_url: str = ""
    price_rows: int = 0
    price_ok: int = 0
    charts_csv: int = 0
    charts_hand: int = 0
    chart_gaps: list[str] = field(default_factory=list)
    cross_period: str = ""
    cross_agree: list[str] = field(default_factory=list)
    cross_disagree: list[str] = field(default_factory=list)
    cross_nopair: list[str] = field(default_factory=list)
    cross_other: list[str] = field(default_factory=list)

    @property
    def fund_unverified(self) -> int:
        return self.fund_rows - self.fund_ok


def fiscal_year_end(code: str) -> int | None:
    """master.yaml の決算月（`fiscal_year_end: "03"` → 3）。無ければ None。

    単独四半期の期キーが年度の第何四半期かを決めるのに要る。読めなければ
    None を返し、呼び手は**突き合わせを行わない**（推測で比べない）。
    """
    path = DATA / "master.yaml"
    if not path.exists():
        return None
    try:
        master = Y.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception:                                    # noqa: BLE001
        return None
    for stock in master.get("stocks") or []:
        if str(stock.get("code", "")).strip() != str(code):
            continue
        try:
            month = int(str(stock.get("fiscal_year_end", "")).strip())
        except ValueError:
            return None
        return month if 1 <= month <= 12 else None
    return None


def cross_check_tanshin(code: str, cross_period: str,
                        fy_end: int | None = None) -> tuple[list, list, list, list]:
    """決算短信（一次情報）と まとめサイト（二次情報）の突き合わせ。

    同じ metric・同じ期について、両者の観測値が表示解像度の範囲で一致するかを
    機械で確かめる。**どちらの CSV も書き換えない**（読むだけ）。
    戻り値は (一致, 食い違い, 相手なし, 対象外) の metric 名リスト。

    「対象外」は決算短信が同じ表に載せている前年同期・前期末・通期計画。
    別の期の数値なので、この期の突き合わせに混ぜると「相手なし」が水増しされる。
    """
    agree: list[str] = []
    disagree: list[str] = []
    nopair: list[str] = []
    other: list[str] = []
    if not cross_period:
        return agree, disagree, nopair, other
    if fy_end is None:
        fy_end = fiscal_year_end(code)
    target_bucket = _cross_bucket(cross_period, fy_end)
    if target_bucket is None:
        # **期を確定できないなら1件も比べない。** 以前はここで
        # 「フィルタだけを外して全期を比べる」形になっており、通期の実績が
        # 1Qの観測値と突き合わされ得た（偶然一致した銘柄では気づけない）。
        return agree, disagree, nopair, other
    fund = fundamentals(code)
    for fact in tanshin(code)["facts"]:
        if fact.value is None:
            continue
        if any(fact.metric.endswith(s) for s in CROSS_SKIP_SUFFIX):
            other.append(fact.metric)
            continue
        # 短信は複数の開示（3Q累計・通期…）が同じ metric 名で並ぶ。
        # 自分の期が cross_period と同じ期を指していない行は比べない
        # （通期の実績を3Q累計の観測値と突き合わせると必ず食い違う）。
        if not same_bucket(_cross_bucket(fact.label, fy_end), target_bucket):
            continue
        key = (fact.metric, cross_period)
        obs = fund["observed"].get(key) or []
        if not obs:
            nopair.append(fact.metric)
            continue
        pair = fund["by_key"].get(key)
        unit = pair.unit if pair is not None else fact.unit
        tol = fund["tolerance"].get(key)
        limit = tol if tol is not None else 0.0
        hit = False
        for _name, raw in obs:
            v = convert(raw, unit, fact.unit)
            if v is not None and abs(v - fact.value) <= limit:
                hit = True
                break
        if hit:
            agree.append(fact.metric)
        else:
            disagree.append(fact.metric)
    return (sorted(set(agree)), sorted(set(disagree)),
            sorted(set(nopair)), sorted(set(other)))


def verify(code: str, charts: dict | None = None,
           cross_period: str = "") -> Verification:
    """その銘柄の数値がどこまで検証されているか。台帳に出すための集計。"""
    v = Verification(code=code)
    facts = fundamentals(code)["facts"]
    v.fund_rows = len(facts)
    for f in facts:
        fl = flags_of(f.status)
        if f.adopted:
            v.fund_ok += 1
            continue
        if "MISMATCH" in fl:
            v.fund_mismatch += 1
            v.mismatch_items.append(period_label(f.period) + " " + metric_ja(f.metric))
        elif "SINGLE_SOURCE" in fl:
            v.fund_single += 1
    v.mismatch_items.sort()

    tan = tanshin(code)["facts"]
    v.tanshin_rows = len(tan)
    v.tanshin_dates = sorted({f.period for f in tan})
    for f in tan:
        if f.source_url:
            v.tanshin_url = f.source_url
            break

    v.price_rows, v.price_ok = price_days(code)

    v.cross_period = str(cross_period or "")
    if v.cross_period:
        (v.cross_agree, v.cross_disagree, v.cross_nopair,
         v.cross_other) = cross_check_tanshin(code, v.cross_period)

    for cid, res in (resolve_charts(code, charts or {})).items():
        if res.origin == "hand":
            v.charts_hand += 1
            continue
        if res.origin == "diagram":
            # 定性図は数値を持たないので CSV由来にも手書きにも数えない。
            # ただし描画拒否（数字の混入など）は穴として名指しする。
            if res.empty_reason:
                v.chart_gaps.append(cid + ": " + res.empty_reason)
            continue
        v.charts_csv += 1
        if res.empty_reason:
            v.chart_gaps.append(cid + ": " + res.empty_reason)
        elif res.missing:
            v.chart_gaps.append(cid + ": 欠測 " + str(len(res.missing))
                                + "点 — " + "／".join(res.missing))
    return v
