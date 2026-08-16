"""財務数値の取得（決算・財務・CF・比率）。**株価と同じ2ソース照合を財務にも適用する。**

これが要る理由:
  `reports/{code}.md` の数値は、人間が kabutan / IR BANK の表を目で読んで
  Markdown に転記したものだった。出典URLは残っているが、**再取得して裏を取る仕組みが
  無い**ため、桁を取り違えても誰も気づかない。株価の `close` は2ソースの一致でしか
  採用値に格上げされないのに（D7）、レポートの本体である財務数値には同じ規律が
  無かった。ここを埋める。

原則（fetch.py と同じ）:
  - 取得元は sources.yaml の `fundamentals.sources` のみ。探索しない。
  - **照合を通っていない値を採用値（`value` 列）に格上げしない**（D7）。
  - CSV は append-only。既存 (code, period, metric) は上書きしない。
  - 推測で埋めない。取れなければ空欄 + status。
  - 比率はコードが計算する。ここが記録するのは**サイトが表示している生値**だけで、
    このモジュールは四則演算を一切しない（単位の正規化のみ）。

--------------------------------------------------------------------------
照合の作法（株価との違い。ここが本モジュールで唯一むずかしい所）
--------------------------------------------------------------------------

株価の終値は、どのサイトも「1円単位の同じ数」を表示するので**完全一致**で照合できる。
財務数値はそうならない。実地で確認した 4073 の例:

    売上高 2020/06   kabutan "2,638"（百万円）   IR BANK "26.4億"
    営業益 2022/06   kabutan "-55"（百万円）     IR BANK "-56百万"

前者は IR BANK が3桁に丸めているだけ、後者は丸め方向の違い（切り捨て/四捨五入）で、
どちらも「桁の取り違え」ではない。完全一致を要求すると実データのほぼ全行が MISMATCH に
なり、検査そのものが無意味になる。

そこで **表示解像度**（その表示の最後の桁が持つ重み）を値と一緒に持ち、

    |a - b| <= max(解像度a, 解像度b)

で一致とみなす。"26.4億" の解像度は 0.1億 = 10百万、"2,638" の解像度は 1百万なので
許容は 10百万。差 2百万は通る。一方、桁の取り違え（10倍・100倍）は解像度の何十倍にも
なるので必ず MISMATCH になる。**このモジュールが守りたいのは桁であって、
どちらのサイトも表示していない1百万円の差ではない。**

完全一致ではなく解像度内の一致で通した行には `ROUNDING` を付ける。
「厳密に同じ値だった」のか「表示精度の範囲で一致した」のかを後から区別できる。

--------------------------------------------------------------------------
status（`|` 区切り。照合結果はちょうど1つ）
--------------------------------------------------------------------------

  照合結果   OK             2つ以上の**別サイト**が一致した。`value` が埋まる
             MISMATCH       参加したソースのどれかが食い違う。`value` は空
             SINGLE_SOURCE  1サイトしか値を持っていない。`value` は空（D7）
             FETCH_FAILED   語彙としては持つが、全滅時は行を書かない（fetch.py と同じ）

  付加       ROUNDING          完全一致ではなく表示解像度の範囲で一致した
             UNIT_CONVERTED    ソース間で単位が違い、正規化してから照合した
             UNIT_UNCONFIRMED  ページ側の単位注記を確認できなかった
             NONCONSOLIDATED   kabutan の「単」＝非連結（単独）決算の期
             US_GAAP / IFRS    kabutan の「U」「I」＝日本基準以外の期
             PERIOD_CHANGED    kabutan の「変」＝決算期変更のあった期
             PERIOD_ASTERISK   kabutan の `*` （連結と非連結を混在表記している期）

会計基準のマークを落とさず記録するのは、**同じ「売上高」の列に連結と非連結が
混ざっている**ことがあるため（kabutan 自身がページ末尾で明記している）。
数字が違う理由が「取得の失敗」なのか「そもそも別の決算だから」なのかを、
あとから区別できるようにしておく。

--------------------------------------------------------------------------
使い方
--------------------------------------------------------------------------

    python src/fetch_fundamentals.py               # 全銘柄
    python src/fetch_fundamentals.py --code 4073   # 1銘柄だけ
    python src/fetch_fundamentals.py --dry-run     # CSV に書かず結果だけ出す
"""
from __future__ import annotations

import argparse
import csv
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from pathlib import Path

try:  # ローカル(Windows)は SSL 検査プロキシ配下。CI(Linux) では不要
    import truststore

    truststore.inject_into_ssl()
except ImportError:
    pass

import requests
import yaml
from bs4 import BeautifulSoup

sys.path.insert(0, str(Path(__file__).resolve().parent))

import revise as RV  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
JST = timezone(timedelta(hours=9))

FIELDS = [
    "period", "code", "metric", "value", "unit", "tolerance", "status",
    "source_primary", "value_primary", "raw_primary",
    "source_secondary", "value_secondary", "raw_secondary",
    "sources_all", "source_url_primary", "source_url_secondary",
    "fetched_at",
]

# 照合結果（ちょうど1つ入る）と付加フラグ。checks.py の語彙と対応させる。
RECONCILE_STATUSES = ("OK", "MISMATCH", "SINGLE_SOURCE", "FETCH_FAILED")
EXTRA_FLAGS = ("ROUNDING", "UNIT_CONVERTED", "UNIT_UNCONFIRMED",
               "NONCONSOLIDATED", "US_GAAP", "IFRS", "PERIOD_CHANGED",
               "PERIOD_ASTERISK")

# kabutan は決算期セルの先頭に会計基準・区分のマークを置く（ページ末尾の凡例が正）。
#   「連」日本会計基準[連結] / 「単」日本会計基準[非連結] / 「U」米国基準 / 「I」IFRS
#   「予」予想業績 / 「旧」修正前予想 / 「新」修正後予想 / 「実」実績 / 「変」決算期変更
# 落とさずに読み、日本基準・連結からの逸脱だけをフラグとして残す。
PERIOD_MARKS = {
    "連": None, "新": None, "旧": None, "実": None,
    "単": "NONCONSOLIDATED",
    "U": "US_GAAP", "Ｕ": "US_GAAP",
    "I": "IFRS", "Ｉ": "IFRS",
    "変": "PERIOD_CHANGED",
}
PLAN_MARK = "予"

# 日本語の桁接尾辞 → 百万円換算の倍率。**長いものから照合する**（千万 > 千 / 百万 > 万）。
SCALE_SUFFIX = {
    "兆": 1_000_000.0,
    "億": 100.0,
    "千万": 10.0,
    "百万": 1.0,
    "万": 0.01,
    "千": 0.001,
}
SUFFIX_ALT = "兆|億|千万|百万|万|千"
NUM_RE = re.compile(
    r"^([+\-−])?([0-9,]+)(?:\.([0-9]+))?(" + SUFFIX_ALT + r")?[%％]?$")

# 単位の語彙。宣言単位 → 正規化後の単位。
#   JPY_suffixed = セル自身が「億」「百万」を持つ表記（IR BANK）。百万円に正規化する
DECLARED_UNITS = {
    "JPY_million": "JPY_million",
    "JPY_suffixed": "JPY_million",
    "JPY": "JPY",
    "pct": "pct",
    "x": "x",
}
# 正規化後の単位どうしの換算。ここに無い組み合わせは「換算できない」として値を捨てる。
UNIT_CONVERSION = {("pct", "x"): 0.01, ("x", "pct"): 100.0}

FLOAT_EPS = 1e-9

PERIOD_FY_RE = re.compile(r"^(\d{4})[./](\d{1,2})$")
PERIOD_RANGE_RE = re.compile(r"^(\d{2})[./](\d{1,2})[-−](\d{1,2})$")
SPAN_TAG = {3: "Q", 6: "H", 9: "C", 12: "FY"}


# =============================================================================
# 観測値
# =============================================================================

@dataclass(frozen=True)
class Obs:
    """1つの取得元が1つの (期間, 指標) について表示していた値。

    `resolution` は表示の最後の桁の重み（正規化後の単位で）。"26.4億" なら 10.0（百万円）。
    照合の許容はこの値から決める。**推定値ではなく、表示の精度そのもの。**
    """
    period: str
    metric: str
    value: float
    resolution: float
    unit: str
    raw: str
    source: str
    site: str
    url: str
    order: int
    flags: tuple


# =============================================================================
# パースの部品
# =============================================================================

def squeeze(text) -> str:
    """空白（全角・NBSP 含む）をすべて落とす。見出し・ヘッダの照合に使う。"""
    return re.sub(r"\s+", "", (text or "").replace("\xa0", " ").replace("　", " "))


def parse_number(text: str, na_marks) -> tuple | None:
    """表示文字列を (値, 表示解像度, 接尾辞) に分解する。読めなければ None。

    '‐56百万'        -> (-56.0, 1.0, '百万')
    '26.4億'         -> (26.4, 0.1, '億')       ※倍率は呼び出し側で掛ける
    '349百万 +4.9%'  -> (349.0, 1.0, '百万')    ※後続の前年比は落とす
    '赤字' / '-'     -> None
    """
    t = squeeze(text)
    if not t or t in na_marks:
        return None
    # IR BANK は「349百万 +4.9%」のように前年比を同じセルに書く。先頭トークンだけ見る。
    head = (text or "").replace("\xa0", " ").replace("　", " ").strip().split()
    head = squeeze(head[0]) if head else ""
    if not head or head in na_marks:
        return None
    m = NUM_RE.match(head)
    if not m:
        return None
    sign, int_part, frac, suffix = m.group(1), m.group(2), m.group(3), m.group(4)
    digits = int_part.replace(",", "")
    if not digits:
        return None
    literal = digits + ("." + frac if frac else "")
    try:
        value = float(literal)
    except ValueError:
        return None
    if sign in ("-", "−"):
        value = -value
    step = 10.0 ** (-len(frac)) if frac else 1.0
    return (value, step, suffix)


def normalize_value(text: str, declared: str, target: str, na_marks) -> tuple | None:
    """セル文字列を「metric の正規化単位」に揃えた (値, 解像度, 換算したか) にする。

    単位を仮定しない。`JPY_suffixed`（IR BANK）で接尾辞が無いセルは読めないものとして
    捨てる（"28,000" を 28,000百万円 と読む事故を防ぐ）。
    """
    base = DECLARED_UNITS.get(declared)
    if base is None:
        return None
    parsed = parse_number(text, na_marks)
    if parsed is None:
        return None
    value, step, suffix = parsed

    if declared == "JPY_suffixed":
        if suffix is None:
            return None                      # 単位を仮定しない
        scale = SCALE_SUFFIX[suffix]
        value *= scale
        step *= scale
    elif suffix is not None:
        return None                          # 単位が宣言と食い違う。読まない

    converted = False
    if base != target:
        factor = UNIT_CONVERSION.get((base, target))
        if factor is None:
            return None                      # 換算できない組み合わせは記録しない
        value *= factor
        step *= factor
        converted = True
    return (value, step, converted)


def strip_period_marks(text: str) -> tuple:
    """決算期セルの先頭マークを剥がし、(残り, 予想か, フラグ) を返す。

        '連 2023.03'    -> ('2023.03', False, ())
        '連\xa0予 2027.03' -> ('2027.03', True,  ())
        '単 2003.03*'   -> ('2003.03*', False, ('NONCONSOLIDATED',))
        '2026/06 予'    -> ('2026/06', True,  ())        ※IR BANK は後置

    マークを剥がさないと `連 2023.03` が日付として読めず、**連結企業の行が丸ごと
    落ちる**（実際に 3851 / 4937 / 6570 で全滅していた）。
    """
    t = squeeze(text)
    plan = False
    flags = []
    while t:
        head = t[0]
        if head == PLAN_MARK:
            plan = True
            t = t[1:]
            continue
        if head in PERIOD_MARKS:
            flag = PERIOD_MARKS[head]
            if flag and flag not in flags:
                flags.append(flag)
            t = t[1:]
            continue
        break
    while t.endswith(PLAN_MARK):
        plan = True
        t = t[:-1]
    return (t, plan, tuple(flags))


def parse_period(label: str, period_kind: str) -> tuple | None:
    """決算期の表記を正規化された期間キーにする。読めなければ None（見出し行など）。

        '連 2022.06' / '2022/06' -> ('FY2022-06', False, ())
        '予 2026.06'             -> ('FY2026-06', True,  ())
        '単 2016.06*'            -> ('FY2016-06', False, ('NONCONSOLIDATED','PERIOD_ASTERISK'))
        '24.04-06'               -> ('Q2024-04_2024-06', False, ())
        '23.01-06'               -> ('H2023-01_2023-06', False, ())
        '25.07-03'               -> ('C2025-07_2026-03', False, ())  ※3Q累計

    `period_kind: quarter` は「2026.03」のように四半期末だけが書かれている表
    （kabutan の「3ヵ月決算過去最高」）向け。3か月遡って範囲にする。
    """
    t, plan, flags = strip_period_marks(label)
    if not t:
        return None
    if t.endswith("*") or t.endswith("＊"):
        flags = flags + ("PERIOD_ASTERISK",)
        t = t.rstrip("*＊")

    m = PERIOD_FY_RE.match(t)
    if m:
        year, month = int(m.group(1)), int(m.group(2))
        if not 1 <= month <= 12:
            return None
        if period_kind == "quarter":
            end = year * 12 + month
            return (_range_key("Q", end - 2, end), plan, flags)
        return ("FY%04d-%02d" % (year, month), plan, flags)

    m = PERIOD_RANGE_RE.match(t)
    if m:
        yy, m1, m2 = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if not (1 <= m1 <= 12 and 1 <= m2 <= 12):
            return None
        y1 = 2000 + yy
        y2 = y1 + (1 if m2 < m1 else 0)
        start = y1 * 12 + m1
        end = y2 * 12 + m2
        tag = SPAN_TAG.get(end - start + 1)
        if tag is None:
            return None
        if tag == "FY":
            return ("FY%04d-%02d" % (y2, m2), plan, flags)
        return (_range_key(tag, start, end), plan, flags)
    return None


def _ym(index: int) -> tuple:
    """通し月インデックス（year*12+month）を (年, 月) に戻す。"""
    return ((index - 1) // 12, (index - 1) % 12 + 1)


def _range_key(tag: str, start: int, end: int) -> str:
    sy, sm = _ym(start)
    ey, em = _ym(end)
    return "%s%04d-%02d_%04d-%02d" % (tag, sy, sm, ey, em)


def fiscal_quarter_key(fy_year: int, fy_month: int, quarter: int) -> str:
    """決算期末（年・月）と四半期番号から期間キーを作る。

    2026/06 期の 1Q は 2025-07〜2025-09。kabutan の '25.07-09' と同じキーになる。
    """
    fy_end = fy_year * 12 + fy_month
    end = fy_end - (4 - quarter) * 3
    return _range_key("Q", end - 2, end)


# =============================================================================
# テーブルの特定
# =============================================================================

def first_row_cells(table) -> list:
    for tr in table.find_all("tr"):
        cells = [squeeze(c.get_text()) for c in tr.find_all(["td", "th"])]
        if cells:
            return cells
    return []


def select_tables(soup: BeautifulSoup, entry: dict) -> list:
    """`table_selector` / `match_th` / `heading_equals` で候補を絞り、
    **先頭行がヘッダ定義と完全一致するもの**だけを返す。

    列の位置だけで読むと、ページに1列足されただけで別の数字を拾う（review-findings F-02
    と同型の壊れ方）。ヘッダの完全一致を通過条件にすることで、列構成が変わったら
    「読めなかった」に落ちる。**黙って別の列を読むより、欠測のほうが安全。**
    """
    want = [squeeze(h) for h in entry.get("expect_header") or []]
    if entry.get("table_selector"):
        candidates = soup.select(entry["table_selector"])
    else:
        candidates = soup.find_all("table")

    out = []
    for table in candidates:
        if entry.get("match_th"):
            ths = [squeeze(x.get_text()) for x in table.find_all("th")]
            if not all(any(w in th for th in ths) for w in entry["match_th"]):
                continue
        if entry.get("heading_equals"):
            node = table.find_previous(["h2", "h3", "h4", "caption"])
            if node is None or squeeze(node.get_text()) != squeeze(entry["heading_equals"]):
                continue
        if want and first_row_cells(table) != want:
            continue
        out.append(table)
        if not entry.get("multi"):
            break
    return out


def table_rows(table) -> list:
    rows = []
    for tr in table.find_all("tr"):
        cells = [c.get_text(" ", strip=True) for c in tr.find_all(["td", "th"])]
        if cells:
            rows.append(cells)
    return rows


# =============================================================================
# パーサ（kind ごと）
# =============================================================================

def _emit(out: list, spec: dict, cell: str, period: str, plan: bool,
          entry: dict, url: str, order: int, metrics: dict, na_marks,
          base_flags: tuple) -> None:
    """1セルを Obs にして out へ。読めなければ何もしない（推測で埋めない）。"""
    metric = spec.get("metric")
    if not metric:
        return
    name = metric + "_plan" if plan else metric
    target = metrics.get(metric)
    if target is None:
        return
    got = normalize_value(cell, spec.get("unit", ""), target, na_marks)
    if got is None:
        return
    value, resolution, converted = got
    flags = base_flags + (("UNIT_CONVERTED",) if converted else ())
    out.append(Obs(
        period=period, metric=name, value=value, resolution=resolution,
        unit=target, raw=squeeze(cell), source=entry["id"], site=entry["site"],
        url=url, order=order, flags=flags,
    ))


def parse_rows_period_first(table, entry, url, order, metrics, na_marks,
                            base_flags) -> list:
    """1列目が決算期、残りの列が指標。kabutan の各表と IR BANK の年度表。

    行の長さがヘッダより短いことがある（kabutan の10年表は利益列が有料で欠ける）。
    その場合も**存在する列だけ**読む。長さ一致を要求すると、読める売上高まで落ちる。
    """
    cols = entry.get("columns") or []
    period_kind = entry.get("period_kind", "fy")
    out = []
    for cells in table_rows(table):
        if len(cells) < 2:
            continue
        parsed = parse_period(cells[0], period_kind)
        if parsed is None:
            continue                     # 見出し行・「前期比」行など
        period, plan, period_flags = parsed
        flags = base_flags + period_flags
        for i, spec in enumerate(cols):
            if not spec or i >= len(cells) or i == 0:
                continue
            _emit(out, spec, cells[i], period, plan, entry, url, order,
                  metrics, na_marks, flags)
    return out


def parse_transposed_record(table, entry, url, order, metrics, na_marks,
                            base_flags) -> list:
    """縦横が逆の表（kabutan「過去最高【実績】」）。

        行0: ['', '売上高', '営業益', ...]        ← ヘッダ
        行1: ['過去最高', '2,638', '386', ...]    ← 値
        行2: ['決算期',  '2020.06', '2020.06', ...] ← 列ごとの期間
    """
    cols = entry.get("columns") or []
    period_kind = entry.get("period_kind", "fy")
    rows = table_rows(table)
    value_row, period_row = None, None
    for cells in rows[1:]:
        label = squeeze(cells[0]) if cells else ""
        if label == squeeze(entry.get("value_row_label", "")):
            value_row = cells
        elif label == squeeze(entry.get("period_row_label", "")):
            period_row = cells
    if value_row is None or period_row is None:
        return []

    out = []
    for i, spec in enumerate(cols):
        if not spec or i == 0 or i >= len(value_row) or i >= len(period_row):
            continue
        parsed = parse_period(period_row[i], period_kind)
        if parsed is None:
            continue
        period, plan, period_flags = parsed
        flags = base_flags + period_flags
        _emit(out, spec, value_row[i], period, plan, entry, url, order,
              metrics, na_marks, flags)
    return out


def parse_matrix_metric_year(table, entry, url, order, metrics, na_marks,
                             base_flags) -> list:
    """科目 × 年度 の行列（IR BANK の四半期ページ）。

        ['科目', '年度', '1Q', '2Q', '3Q', '4Q', '通期']   ← ヘッダ（表の途中で再掲される）
        ['売上高', '2022/06', ...]                          ← 科目が始まる行（7セル）
        ['2023/06', ...]                                    ← 科目は rowspan で省略（6セル）

    四半期の期間は**決算期末の年月と四半期番号から計算する**。2026/06 期の 1Q は
    2025-07〜2025-09 で、kabutan の '25.07-09' と同じキーになる。
    """
    metric_map = {squeeze(k): v for k, v in (entry.get("metric_map") or {}).items()}
    header = [squeeze(h) for h in entry.get("expect_header") or []]
    quarter_cols = entry.get("quarter_columns") or ["1Q", "2Q", "3Q", "4Q"]
    annual_col = squeeze(entry.get("annual_column", "通期"))
    width = len(header)
    out = []
    current = None

    for cells in table_rows(table):
        squeezed = [squeeze(c) for c in cells]
        if squeezed == header:
            continue
        if len(cells) == width:
            current = metric_map.get(squeezed[0])
            year_cell, values = cells[1], cells[2:width]
        elif len(cells) == width - 1:
            year_cell, values = cells[0], cells[1:width - 1]
        else:
            continue
        if current is None:
            continue
        year_text, _, period_flags = strip_period_marks(year_cell)
        m = PERIOD_FY_RE.match(year_text)
        if not m:
            continue
        fy_year, fy_month = int(m.group(1)), int(m.group(2))
        if not 1 <= fy_month <= 12:
            continue
        fy_key = "FY%04d-%02d" % (fy_year, fy_month)
        row_flags = base_flags + period_flags

        for i, label in enumerate(header[2:]):
            if i >= len(values):
                break
            if label in [squeeze(q) for q in quarter_cols]:
                period = fiscal_quarter_key(fy_year, fy_month,
                                            [squeeze(q) for q in quarter_cols].index(label) + 1)
            elif label == annual_col:
                period = fy_key
            else:
                continue
            _emit(out, current, values[i], period, False, entry, url, order,
                  metrics, na_marks, row_flags)
    return out


PARSERS = {
    "rows_period_first": parse_rows_period_first,
    "transposed_record": parse_transposed_record,
    "matrix_metric_year": parse_matrix_metric_year,
}


def parse_source(html: str, entry: dict, url: str, order: int, metrics: dict,
                 na_marks) -> tuple:
    """1つの取得元の HTML から Obs を作る。返り値は (Obs のリスト, 見つけた表の数)。

    セレクタが外れたら例外にせず空リスト（欠測）。表の数を返すのは、
    「表そのものが見つからない（ページ構造の変化）」と「表はあるが値を読めない
    （行の書式の変化）」を**別の壊れ方として報告する**ため。同じ空リストでも
    直すべき場所が違う。
    """
    soup = BeautifulSoup(html, "html.parser")

    base_flags = ()
    asserts = entry.get("unit_assert") or []
    if asserts:
        page = squeeze(soup.get_text(" "))
        if not all(squeeze(a) in page for a in asserts):
            base_flags = ("UNIT_UNCONFIRMED",)

    parser = PARSERS.get(entry.get("kind", "rows_period_first"))
    if parser is None:
        return ([], 0)
    tables = select_tables(soup, entry)
    out = []
    for table in tables:
        out.extend(parser(table, entry, url, order, metrics, na_marks, base_flags))
    return (out, len(tables))


# =============================================================================
# 取得
# =============================================================================

class Fetcher:
    """URL 単位でキャッシュする HTTP 取得。同じページを8回叩かないため。"""

    def __init__(self, pol: dict) -> None:
        self.pol = pol
        self.cache: dict = {}

    def get(self, url: str) -> str | None:
        if url in self.cache:
            return self.cache[url]
        text = None
        for attempt in range(self.pol["retries"] + 1):
            try:
                r = requests.get(url,
                                 headers={"User-Agent": self.pol["user_agent"]},
                                 timeout=self.pol["timeout_sec"])
                r.raise_for_status()
                text = r.text
                break
            except Exception as e:  # noqa: BLE001
                if attempt == self.pol["retries"]:
                    print("  取得失敗: %s (%s: %s)" % (url, type(e).__name__, e),
                          file=sys.stderr)
        time.sleep(self.pol["interval_sec"])
        self.cache[url] = text
        return text


def resolve_irbank_code(fetcher: Fetcher, cfg: dict, code: str,
                        name: str) -> str | None:
    """証券コードから IR BANK の企業コード（E36666 等）を**一覧ページから解決する**。

    証券コードと企業コードは別体系で、規則的な対応も無い。推測で URL を組み立てない
    （.claude/skills/kabu-ledger/SKILL.md「source_url のでっち上げ禁止」）。解決したうえで、ページのタイトルに
    証券コードと会社名の両方が含まれることを確認する（別会社を掴んでいないことの確認）。
    """
    spec = cfg.get("irbank_index") or {}
    url = spec.get("url", "").format(code=code)
    if not url:
        return None
    html = fetcher.get(url)
    if html is None:
        return None
    soup = BeautifulSoup(html, "html.parser")
    title = squeeze(soup.title.get_text()) if soup.title else ""
    if code not in title or squeeze(name) not in title:
        print("  [%s] IR BANK のタイトルが一致しない: %r" % (code, title),
              file=sys.stderr)
        return None
    pattern = re.compile(spec.get("company_link_re", r"^/?(E\d{5})(/|$)"))
    found = []
    for a in soup.find_all("a", href=True):
        m = pattern.match(a["href"])
        if m and m.group(1) not in found:
            found.append(m.group(1))
    if len(found) != 1:
        print("  [%s] IR BANK の企業コードを一意に決められない: %r" % (code, found),
              file=sys.stderr)
        return None
    return found[0]


# =============================================================================
# 照合
# =============================================================================

def agree(a: Obs, b: Obs) -> bool:
    """2つの表示が「同じ数字を別の精度で書いたもの」と言えるか。

    許容は**粗いほうの表示解像度**。どちらのサイトも表示していない桁の差を
    不一致とは呼ばない（モジュール冒頭の説明を参照）。
    """
    tol = max(a.resolution, b.resolution)
    return abs(a.value - b.value) <= tol + FLOAT_EPS


def fmt(value) -> str:
    """CSV に書く数値表記。浮動小数の余りを出さない（決定論的生成）。"""
    if value is None:
        return ""
    text = "%.6f" % value
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return "-0" if text == "-0" else text


def reconcile(code: str, observations: list, required_sites: int,
              now: str) -> list:
    """(期間, 指標) ごとに全ソースを突き合わせ、CSV の行を作る。

    - どれか1組でも食い違えば MISMATCH。**採用値は空のまま**（D7）。
    - 全組が一致し、別サイトが `required_sites` 以上あれば OK。採用値が埋まる。
    - 一致していても1サイトだけなら SINGLE_SOURCE。採用値は空。
      同一サイトの別ページが裏付けていても、それは独立した確認ではない。
    """
    grouped: dict = {}
    for o in observations:
        grouped.setdefault((o.period, o.metric), []).append(o)

    rows = []
    for key in sorted(grouped):
        period, metric = key
        obs = sorted(grouped[key], key=lambda o: (o.order, o.source))
        sites = sorted({o.site for o in obs})

        disagreed = False
        exact = True
        disagreeing_pair = None
        for i in range(len(obs)):
            for j in range(i + 1, len(obs)):
                if not agree(obs[i], obs[j]):
                    disagreed = True
                    if disagreeing_pair is None:
                        disagreeing_pair = (obs[i], obs[j])
                elif abs(obs[i].value - obs[j].value) > FLOAT_EPS:
                    exact = False

        flags = []
        adopted = None
        if disagreed:
            flags.append("MISMATCH")
        elif len(sites) >= required_sites:
            flags.append("OK")
            best = min(obs, key=lambda o: (o.resolution, o.order))
            adopted = best.value
            if not exact:
                flags.append("ROUNDING")
        else:
            flags.append("SINGLE_SOURCE")

        for extra in EXTRA_FLAGS:
            if extra == "ROUNDING":
                continue
            if any(extra in o.flags for o in obs):
                flags.append(extra)

        # MISMATCH のときは、実際に食い違っている組を主副として残す。
        # obs[0]/obs[1] を機械的に取ると、3ソース目が食い違っているだけの行で
        # 先頭2件がたまたま一致し「MISMATCH なのに主副が一致」という
        # 矛盾した表示になる（checks.py の schema 検査がこれを検出する）。
        if disagreed and disagreeing_pair is not None:
            primary, secondary = disagreeing_pair
        else:
            primary = obs[0]
            secondary = obs[1] if len(obs) > 1 else None
        parts = ["%s=%s" % (o.source, fmt(o.value)) for o in obs]
        # 照合に使った許容幅（＝一番粗い表示の解像度）。
        # 「どこまでの差なら同じ数字と見なしたか」を行そのものに残す。
        # checks.py の独立検算（四半期の合計と通期の突き合わせ）もこれを使う。
        tolerance = max(o.resolution for o in obs)
        rows.append({
            "period": period,
            "code": code,
            "metric": metric,
            "value": fmt(adopted),
            "unit": primary.unit,
            "tolerance": fmt(tolerance),
            "status": "|".join(flags),
            "source_primary": primary.source,
            "value_primary": fmt(primary.value),
            "raw_primary": primary.raw,
            "source_secondary": secondary.source if secondary else "",
            "value_secondary": fmt(secondary.value) if secondary else "",
            "raw_secondary": secondary.raw if secondary else "",
            "sources_all": "|".join(parts),
            "source_url_primary": primary.url,
            "source_url_secondary": secondary.url if secondary else "",
            "fetched_at": now,
        })
    return rows


def withdraw_invalid(path: Path, rows: list, now: str, reason: str,
                     ledger: Path | None = None) -> int:
    """採用が成立しなくなった行の**採用値を取り下げる**（人間が実行する）。

    自動化しない。取得元の一時的な不調で採用値が消えると、直したかった障害
    （指標・図が欠測になる）を自分で起こすからである。使うのは
    **照合そのものが無効だったと分かったとき**:

      実例（2026-08-13）: `irbank_bs` の4列目は「株主資本」であって「自己資本」では
      ない（自己資本 = 株主資本 + その他の包括利益累計額）。それを kabutan の
      `equity`（自己資本）と突き合わせていたため、**別々の勘定科目どうしの比較**が
      OK/MISMATCH を出していた。差が表示解像度に収まった期だけ採用され、
      収まらない期は MISMATCH ——判定が丸め幅で決まっていた。
      sources.yaml を直したうえで、その比較で入った採用値をここで取り下げる。

    取り下げた事実・前後の値・理由は `data/revisions.csv` に必ず残る。
    """
    if not path.exists():
        return 0
    existing = {}
    order = []
    with path.open(encoding="utf-8-sig", newline="") as f:
        for r in csv.DictReader(f):
            key = (r["code"], r["period"], r["metric"])
            existing[key] = r
            order.append(key)

    fresh = {(r["code"], r["period"], r["metric"]): r for r in rows}
    taken = []
    for key, old in existing.items():
        new = fresh.get(key)
        if new is None:
            continue
        if str(old.get("value") or "").strip() == "":
            continue
        if "OK" in str(new.get("status") or "").split("|"):
            continue
        diffs = RV.diff_columns(old, new, FIELDS)
        if diffs:
            taken.append((key, new, diffs))

    if not taken:
        return 0
    rel = "data/fundamentals/%s.csv" % path.stem
    for key, new, diffs in taken:
        existing[key] = {c: new.get(c) for c in FIELDS}
        RV.append_records(rel, "/".join(key), diffs, RV.WITHDRAW, reason,
                          now, ledger)
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(existing[k] for k in order)
    return len(taken)


def append_only(path: Path, rows: list, now: str = "",
                ledger: Path | None = None) -> tuple:
    """既存 (code, period, metric) は書き換えない。冪等性の担保。

    **例外は「照合不成立 → 成立」の訂正だけ**（`revise.py` の原則）。
    鍵に期しか入らないため、株価と違って**その行は二度と新しくならない**。
    片方のサイトが1回落ちた週に書かれた `SINGLE_SOURCE` は、翌週以降
    どれだけ正常に取得できても永久に採用値が空のままだった
    （実測: irbank が落ちた1回で 4073 の採用値が OK 0件のまま確定した）。

    採用値を下げる方向（OK → 空）は自動では行わない。取り下げは
    `--withdraw` で人間が理由を書いて実行する。

    戻り値は (追記した行数, 訂正した行数)。
    """
    existing = {}
    order = []
    if path.exists():
        with path.open(encoding="utf-8-sig", newline="") as f:
            for r in csv.DictReader(f):
                key = (r["code"], r["period"], r["metric"])
                existing[key] = r
                order.append(key)

    new = [r for r in rows
           if (r["code"], r["period"], r["metric"]) not in existing]
    repairs = []
    for r in rows:
        key = (r["code"], r["period"], r["metric"])
        old = existing.get(key)
        if old is None or not RV.is_repair(old, r, "value"):
            continue
        diffs = RV.diff_columns(old, r, FIELDS)
        if diffs:
            repairs.append((key, r, diffs))

    if repairs:
        rel = "data/fundamentals/%s.csv" % path.stem
        for key, fixed, diffs in repairs:
            existing[key] = {c: fixed.get(c) for c in FIELDS}
            RV.append_records(
                rel, "/".join(key), diffs, RV.REPAIR,
                "照合不成立で採用値が空だった行が、今回の取得で別サイト2つ以上と一致した",
                now, ledger)
        with path.open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=FIELDS)
            w.writeheader()
            w.writerows(existing[k] for k in order)

    if new:
        path.parent.mkdir(parents=True, exist_ok=True)
        write_header = not path.exists()
        new.sort(key=lambda r: (r["period"], r["metric"], r["code"]))
        with path.open("a", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=FIELDS)
            if write_header:
                w.writeheader()
            w.writerows(new)
    return len(new), len(repairs)


# =============================================================================
# 実行
# =============================================================================

def collect(code: str, name: str, cfg: dict, pol: dict,
            fetcher: Fetcher) -> list:
    """1銘柄ぶんの Obs をすべての取得元から集める。"""
    metrics = cfg["metrics"]
    na_marks = [squeeze(m) for m in cfg.get("na_marks") or []]
    ecode = None
    needs_ecode = any("{ecode}" in s["url"] for s in cfg["sources"])
    if needs_ecode:
        ecode = resolve_irbank_code(fetcher, cfg, code, name)
        if ecode:
            print("  IR BANK 企業コード: %s" % ecode)

    observations = []
    attempts = {}
    for order, entry in enumerate(cfg["sources"]):
        if "{ecode}" in entry["url"]:
            if not ecode:
                continue
            url = entry["url"].format(code=code, ecode=ecode)
        else:
            url = entry["url"].format(code=code)
        html = fetcher.get(url)
        if html is None:
            continue
        got, tables = parse_source(html, entry, url, order, metrics, na_marks)
        group = entry.get("variant_group") or entry["id"]
        attempts.setdefault(group, []).append((entry, url, got, tables))
        observations.extend(got)

    # 同じ表の書式違い（irbank の会計基準別ヘッダ、kabutan の累計表の見出し違い）は
    # **どれか1つだけが当たるのが正常**。1つも当たらなかった群だけを報告する。
    for group in sorted(attempts):
        tried = attempts[group]
        if any(got for _, _, got, _ in tried):
            continue
        entry, url, _, tables = tried[0]
        why = "表が見つからない（ヘッダ不一致）" if all(t == 0 for _, _, _, t in tried) \
            else "表はあるが値を1つも読めない（行の書式が変わった）"
        ids = "/".join(e["id"] for e, _, _, _ in tried)
        print("  [%s] %s: %s %s" % (code, ids, why, url), file=sys.stderr)
    return observations


def summarize(rows: list) -> dict:
    counts = {}
    for r in rows:
        for flag in str(r["status"]).split("|"):
            counts[flag] = counts.get(flag, 0) + 1
    return counts


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="財務数値の2ソース照合取得")
    ap.add_argument("--code", help="1銘柄だけ取得する")
    ap.add_argument("--dry-run", action="store_true", help="CSV に書かない")
    ap.add_argument("--withdraw-invalid", action="store_true",
                    help="照合が成立しなくなった既存行の採用値を取り下げる"
                         "（人間が実行する。--reason 必須）")
    ap.add_argument("--reason", default="",
                    help="--withdraw-invalid の根拠。data/revisions.csv に残る")
    args = ap.parse_args(argv)
    if args.withdraw_invalid and not args.reason.strip():
        ap.error("--withdraw-invalid には --reason が要る"
                 "（何を根拠に取り下げたのかを記録に残す）")

    sources = yaml.safe_load(
        (ROOT / "data" / "sources.yaml").read_text(encoding="utf-8"))
    master = yaml.safe_load(
        (ROOT / "data" / "master.yaml").read_text(encoding="utf-8"))
    cfg = sources["fundamentals"]
    pol = sources["fetch_policy"]
    required = cfg.get("required_sites", 2)
    fetcher = Fetcher(pol)
    now = datetime.now(JST).isoformat()

    stocks = [s for s in master["stocks"]
              if not args.code or str(s["code"]) == args.code]
    failed = []

    for stock in stocks:
        code = str(stock["code"])
        print("取得中: %s %s" % (code, stock["name"]))
        observations = collect(code, str(stock["name"]), cfg, pol, fetcher)
        if not observations:
            failed.append(code)
            print("  1件も取得できなかった", file=sys.stderr)
            continue

        rows = reconcile(code, observations, required, now)
        counts = summarize(rows)
        order = ["OK", "MISMATCH", "SINGLE_SOURCE", "ROUNDING",
                 "UNIT_CONVERTED", "UNIT_UNCONFIRMED", "PERIOD_ASTERISK"]
        shown = " / ".join("%s %d" % (k, counts[k]) for k in order if k in counts)
        print("  観測 %d件 → %d行（%s）" % (len(observations), len(rows), shown))

        for r in rows:
            if "MISMATCH" in str(r["status"]).split("|"):
                print("    MISMATCH %s %s: %s" % (r["period"], r["metric"],
                                                  r["sources_all"]))

        if args.dry_run:
            continue
        path = ROOT / "data" / "fundamentals" / ("%s.csv" % code)
        if args.withdraw_invalid:
            taken = withdraw_invalid(path, rows, now, args.reason.strip())
            if taken:
                print("  %d件の採用値を取り下げた（data/revisions.csv に記録）"
                      % taken)
        added, fixed = append_only(path, rows, now)
        print("  %d件を追記（既存分は据え置き）" % added)
        if fixed:
            print("  %d件を訂正（照合不成立→成立。data/revisions.csv に記録）"
                  % fixed)

    if failed:
        print("要確認: 財務数値の取得失敗 %s" % ", ".join(failed), file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
