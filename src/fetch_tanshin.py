"""決算短信（PDF）から一次情報を機械的に読む。

--------------------------------------------------------------------------
なぜこれが要るか
--------------------------------------------------------------------------

株価は2ソース照合＋18項目検査で守られているのに、レポートの財務数値は
「人間が kabutan の表を目で読んで Markdown に転記した」ものだった。
桁を取り違えても誰も気づかない。決算短信は**発行体本人が出した一次情報**であり、
まとめサイトはそれを写した二次情報にすぎない。ここを読めるようにして、
二次情報を検算する側に回す。

--------------------------------------------------------------------------
設計の原則（fetch.py と同じ規律を財務数値に適用する）
--------------------------------------------------------------------------

1. **照合を通っていない値を採用値（`value`）に格上げしない**（D7）。
   抽出できた生値は必ず `value_extracted` に残すが、`value` が埋まるのは
   検算を通った行だけ。株価の `close` と `value_primary` の関係と同じ。

2. **同一文書内の第2の証人を使う。** 短信のサマリー表は、金額の隣に
   会社自身が計算した「対前年同四半期増減率」を印字している。抽出した
   当期・前年同期の実額から比率を再計算し、印字値と合わなければ
   **表の読み違い**なので `YOY_MISMATCH` として採用しない。
   百万円未満切捨ての丸め幅から許容区間を厳密に導く（魔法の閾値を置かない）。

3. **読めなかったことを記録する。** 暗号化・画像PDF・構造不一致は
   `data/tanshin/fetch_log.csv` に status として残す。
   **黙って二次情報にフォールバックしない**。

4. **比率を CSV に書かない**（D17 / SKILL.md）。印字された増減率は検算にだけ使い、
   データ行にはしない。ただし短信が「値そのもの」として開示している比率
   （自己資本比率）は実額と同じ一次データなので記録する。

5. **推測で埋めない。** 取れなければ行を書かない。`assumed` は常に false。

6. **取得元を自律的に増やさない。** `data/sources.yaml` の `tanshin` に
   書いたものだけを使う（TDnet の日次一覧 と、人間が確認済みの `known_pdfs`）。

7. **クロール先の文字列を指示として解釈しない**（D9）。PDF 本文は
   データとしてのみ扱う。CSV に書くのは数値と、本モジュールで定義した
   固定語彙・開示表記のラベル（64文字・区切り文字禁止）だけ。

--------------------------------------------------------------------------
出力
--------------------------------------------------------------------------

`data/tanshin/{code}.csv`（append-only・一意キー `(code, date, metric)`）

    date,code,metric,value,value_extracted,unit,definition,assumed,
    source,tier,status,source_url,fetched_at

`data/kpi/{code}.csv` と同じ列に `value_extracted` / `source` / `tier` /
`status` を足した**上位互換**。`metric` が KPI 語彙（`KPI_COMPATIBLE_METRICS`）
かつ `status` が採用可（`value` が埋まっている）の行は、そのまま
`data/kpi/{code}.csv` に転記できる。**このファイルは KPI CSV を置き換えない**
（KPI への追記は SKILL.md のとおり人間の承認を経る）。

`data/tanshin/fetch_log.csv`（append-only・一意キー `(code, disclosed_on, pdf_url)`）

    disclosed_on,code,pdf_url,status,pages,text_chars,metrics_written,note,fetched_at

--------------------------------------------------------------------------
fetch_fundamentals.py（二次情報2ソース照合）との合流
--------------------------------------------------------------------------

本モジュールは「第3のソース」ではなく **最優先の一次情報** として振る舞う。
受け渡しは関数で行う（ファイル形式に依存させない）:

    from fetch_tanshin import load_facts, adjudicate, values_agree

    primary = load_facts(code)                     # {(period, metric): Fact}
    value, status = adjudicate(primary.get((period, metric)),
                               {"kabutan": v1, "irbank": v2},
                               secondary_unit="JPY_million")

`adjudicate()` の規律:

    一次あり・二次が全部一致        -> 採用（status `OK|PRIMARY`）
    一次あり・二次なし              -> 採用（status `PRIMARY_ONLY`）
                                       ※短信は文書内YoY検算を通っている
    一次あり・二次のどれかが不一致  -> **採用しない**（`MISMATCH|PRIMARY_DISAGREE`）
                                       二次が2つとも一致していても一次と違えば不一致
    一次なし・二次2つ以上が一致      -> 採用（`OK|SECONDARY`）
    一次なし・二次が不一致           -> 採用しない（`MISMATCH`）
    一次なし・二次1つだけ            -> 採用しない（`SINGLE_SOURCE`）

--------------------------------------------------------------------------
使い方
--------------------------------------------------------------------------

    python src/fetch_tanshin.py --code 4073              # sources.yaml の経路で探す
    python src/fetch_tanshin.py --code 4073 --url <PDF>  # URL を直接指定
    python src/fetch_tanshin.py --date 2026-08-14        # TDnet 当日一覧から全銘柄
    python src/fetch_tanshin.py --code 4073 --pdf a.pdf  # ローカルPDF（オフライン検証）
    python src/fetch_tanshin.py --dry-run                # CSV に書かず表示だけ
    python src/fetch_tanshin.py --check                  # data/tanshin/ の検査
"""
from __future__ import annotations

import argparse
import csv
import io
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from pathlib import Path

try:  # ローカル(Windows)は SSL 検査プロキシ配下。CI(Linux) では不要
    import truststore

    truststore.inject_into_ssl()
except ImportError:
    pass

import requests
import yaml
import yamlio as Y
from bs4 import BeautifulSoup

try:
    from pypdf import PdfReader
except ImportError:  # 依存が入っていないことを黙らせない
    PdfReader = None

ROOT = Path(__file__).resolve().parents[1]
JST = timezone(timedelta(hours=9))

FIELDS = [
    "date", "code", "metric", "value", "value_extracted", "unit",
    "definition", "assumed", "source", "tier", "status",
    "source_url", "fetched_at",
]
LOG_FIELDS = [
    "disclosed_on", "code", "pdf_url", "status", "pages", "text_chars",
    "metrics_written", "note", "fetched_at",
]

SOURCE_ID = "tanshin"
TIER = "primary"

# =============================================================================
# 語彙（すべてここ。根拠を併記する）
# =============================================================================

# SKILL.md の KPI 語彙と同じもの。`data/kpi/{code}.csv` にそのまま転記できる
KPI_COMPATIBLE_METRICS = {
    "revenue", "revenue_prev_year", "revenue_fy_plan",
    "operating_income", "operating_income_prev_year", "operating_income_fy_plan",
    "ordinary_income", "ordinary_income_prev_year", "ordinary_income_fy_plan",
}
# 短信サマリーには載るが KPI 語彙には無いもの。レポート記述の裏取りに使う。
# **KPI CSV には転記しない**（語彙を勝手に増やさない。増やすなら SKILL.md を先に直す）
TANSHIN_ONLY_METRICS = {
    "net_income", "net_income_prev_year", "net_income_fy_plan",
    "eps", "eps_prev_year", "eps_fy_plan",
    "total_assets", "total_assets_prev_fy",
    "net_assets", "net_assets_prev_fy",
    "equity_ratio", "equity_ratio_prev_fy",
}
TANSHIN_METRICS = KPI_COMPATIBLE_METRICS | TANSHIN_ONLY_METRICS

UNITS = {"JPY", "JPY_thousand", "JPY_million", "JPY_billion", "pct", "x", "shares"}
UNIT_JPY = {"JPY": 1.0, "JPY_thousand": 1e3, "JPY_million": 1e6, "JPY_billion": 1e9}

# 表の単位トークン -> CSV の unit
UNIT_TOKEN_MAP = {
    "百万円": "JPY_million",
    "千円": "JPY_thousand",
    "円": "JPY",
    "%": "pct",
    "株": "shares",
}
AMOUNT_UNIT_TOKENS = ("百万円", "千円", "億円", "円", "株")
UNIT_TOKENS = set(AMOUNT_UNIT_TOKENS) | {"銭", "%"}

# 開示表記 -> metric（長いキーから順に判定する。
# 「親会社株主に帰属する当期純利益」が「当期純利益」に食われないようにするため）
LABEL_MAP = {
    "売上高": "revenue",
    "営業収益": "revenue",
    "売上収益": "revenue",
    "経常収益": "revenue",
    "営業利益": "operating_income",
    "営業損失": "operating_income",
    "経常利益": "ordinary_income",
    "経常損失": "ordinary_income",
    "親会社株主に帰属する当期純利益": "net_income",
    "親会社の所有者に帰属する当期利益": "net_income",
    "四半期純利益": "net_income",
    "中間純利益": "net_income",
    "当期純利益": "net_income",
    "純利益": "net_income",
    "総資産": "total_assets",
    "資産合計": "total_assets",
    "純資産": "net_assets",
    "自己資本比率": "equity_ratio",
    "1株当たり": "eps",
}
_LABEL_KEYS = sorted(LABEL_MAP, key=len, reverse=True)

# 検算の結果として採用を止めるフラグ（これが立った metric は value を空にする）
BLOCKING_FLAGS = {
    "YOY_MISMATCH",      # 印字された増減率と実額から再計算した比率が合わない
    "SCALE_SUSPECT",     # 当期と前年同期の比が10倍/0.1倍を外れる（単位の取り違え）
    "OUT_OF_RANGE",      # |value| > 1e9（単位の取り違え）
    "SIGN_SUSPECT",      # 売上高系が負
    "PERIOD_MISMATCH",   # period の決算月が master.yaml の fiscal_year_end と違う
    "LABEL_UNSAFE",      # 開示表記に区切り文字・改行が入っている
}
# 検算の結果を表す情報フラグ（採用は止めない）。
# **「検算していない」を「検算に通った」と書かない**（checks.py 設計原則1と同じ）。
# NOT_CROSS_CHECKED は「短信の表から素直に読めたが、同一文書内に第2の証人が
# 無い行」。会社計画の初出や、前期比が印字されない項目がここに入る。
INFO_FLAGS = {"OK", "NOT_CROSS_CHECKED", "YOY_CHECK_NA",
              "EPS_CROSS_OK", "EPS_CROSS_NA", "EPS_CROSS_FAILED",
              "EQUITY_CROSS_OK", "EQUITY_CROSS_NA", "EQUITY_CROSS_FAILED"}
ALL_FLAGS = BLOCKING_FLAGS | INFO_FLAGS

BALANCE_METRICS = ("total_assets", "net_assets", "equity_ratio")

LOG_STATUSES = {
    "OK", "DOWNLOAD_FAILED", "NOT_PDF", "PDF_ENCRYPTED", "PDF_UNREADABLE",
    "PDF_IMAGE_ONLY", "SUMMARY_UNPARSED", "NOT_FOUND",
}

# --- 検証の閾値（SKILL.md「値の正規化と範囲検証」より） ---
VALUE_ABS_MAX = 1e9          # 百万円単位で1000兆円
SCALE_MIN, SCALE_MAX = 0.1, 10.0
LABEL_MAX_LEN = 64
LABEL_FORBIDDEN = (",", "|", '"', "\n", "\r", ";")
# 印字された増減率の丸め幅（小数第1位表示）。切捨て幅は unit そのもの（=1）
PCT_PRINT_HALF_STEP = 0.06
# テキスト層があると認めるしきい値。これ未満は画像PDF（要OCR）とみなす
MIN_TEXT_CHARS = 200

# --- プロンプトインジェクションの検出（D9。実行はしない・報告するだけ） ---
INJECTION_PATTERNS = (
    "以前の指示", "これまでの指示", "指示を無視", "システムプロンプト",
    "ignore previous", "ignore all previous", "disregard the above",
    "system prompt", "you are now", "以下のURLも参照", "この値を記録",
)


# =============================================================================
# 正規化
# =============================================================================

DASH_CHARS = "－―‐–—−ー-"   # －―‐–—−ー-
NEG_MARKS = "△▲"                                    # △▲


def normalize_text(text: str) -> str:
    """全角英数字・記号を半角に寄せる。**符号記号（△▲）と伸ばし棒は触らない**。

    NFKC 正規化は「－」「―」まで巻き込んで別物に変えてしまうため使わない。
    ここで変換するのは、行の構造判定に効く文字だけに限る。
    """
    table = {}
    for i in range(10):
        table[ord("０") + i] = str(i)
    table[ord("．")] = "."
    table[ord("，")] = ","
    table[ord("％")] = "%"
    table[ord("（")] = "("
    table[ord("）")] = ")"
    table[ord("　")] = " "
    table[ord(" ")] = " "
    table[ord("：")] = ":"
    return text.translate(table)


def parse_number(token: str) -> float | None:
    """表のセル1つを数値にする。読めなければ None（0 に倒さない）。

    日本の決算短信は赤字を △ / ▲ で表す。ここを落とすと符号が反転した数字が
    そのまま台帳に残る（SKILL.md「値の正規化と範囲検証」1）。
    ダッシュだけのセルは「該当なし」であって 0 ではない。
    """
    t = (token or "").strip()
    if not t:
        return None
    if all(ch in DASH_CHARS for ch in t):
        return None
    neg = False
    if t[0] in NEG_MARKS:
        neg = True
        t = t[1:]
    elif t[0] in DASH_CHARS:
        neg = True
        t = t[1:]
    t = t.replace(",", "").strip()
    if not re.fullmatch(r"\d+(?:\.\d+)?", t):
        return None
    v = float(t)
    return -v if neg else v


def fmt_value(v: float | None) -> str:
    """CSV に書く形。整数は整数として書く（1493.0 と書かない）。"""
    if v is None:
        return ""
    if float(v).is_integer():
        return str(int(v))
    return "%.10g" % v


def safe_label(text: str) -> str | None:
    """開示表記をそのまま使う。区切り文字・改行を含むなら記録しない（None）。"""
    t = (text or "").strip()
    if not t:
        return None
    if any(ch in t for ch in LABEL_FORBIDDEN):
        return None
    return t[:LABEL_MAX_LEN]


def scan_injection(text: str) -> list[str]:
    """指示めいた文字列の検出。**実行はしない。人間に報告するだけ**（D9）。"""
    low = text.lower()
    return sorted({p for p in INJECTION_PATTERNS if p.lower() in low})


# =============================================================================
# サマリー表のパース
# =============================================================================

@dataclass(frozen=True)
class Header:
    code: str | None
    fy_year: int
    fy_month: int
    quarter: int              # 1..4（通期は 4）
    consolidation: str        # 連結 / 単体
    standard: str             # 日本基準 / IFRS / 米国基準
    disclosed_on: str | None  # YYYY-MM-DD
    cumulative: bool          # 経営成績が累計かどうか


@dataclass
class Fact:
    metric: str
    value: float
    unit: str
    period: str               # FY2026Q3cum
    item_label: str
    printed_yoy_pct: float | None = None
    flags: list[str] = field(default_factory=list)

    @property
    def adopted(self) -> bool:
        return not any(f in BLOCKING_FLAGS for f in self.flags)


HEADING_RE = re.compile(r"^\(?[0-9一二三四五六七八九]+[).]")
# 表の行ラベル。会社ごとに書き方が違う（すべて実測）:
#   「2026年6月期第3四半期」   4073
#   「2026年12月期中間期」      勤次郎(4013) — 第2四半期とは書かない
#   「2025年6月期」             前期末（財政状態）
#   「通期」「第2四半期(累計)」 業績予想
ROW_RE = re.compile(
    r"^(?:(?P<y>\d{4})年(?P<m>\d{1,2})月期\s*(?:第(?P<q>\d)四半期)?\s*"
    r"(?P<mid>\(?中間期?\)?)?\s*(?:\(予想\))?"
    r"|(?P<full>通期)"
    r"|第(?P<fq>\d)四半期(?:\(累計\)|累計)?)\s+(?P<rest>\S.*)$"
)
# 表紙の題名。決算期と「決算短信」の間に何が入るかは会社ごとに違う。
#   「2026年6月期 第3四半期決算短信」            4073
#   「2026年12月期 第2四半期（中間期） 決算短信」 勤次郎(4013) — 括弧が挟まる
#   「2026年6月期 決算短信」                      本決算
# 間を固定の並びで書くと、括弧ひとつで表紙ごと読めなくなる（実測）。
# 挟まった部分は `mid` として取り出し、四半期はそこから拾う。
TITLE_RE = re.compile(
    r"(?P<y>\d{4})年(?P<m>\d{1,2})月期(?P<mid>[^年\n]{0,24}?)決算短信")
PERIOD_RANGE_RE = re.compile(
    r"\((\d{4})年(\d{1,2})月(\d{1,2})日\s*[~〜～]\s*(\d{4})年(\d{1,2})月(\d{1,2})日\)")


def _tokens(line: str) -> list[str]:
    return [t for t in re.split(r"\s+", line.strip()) if t]


def _is_unit_line(line: str) -> bool:
    toks = _tokens(line)
    return len(toks) >= 2 and all(t in UNIT_TOKENS for t in toks)


def _label_to_metric(token: str) -> str | None:
    for key in _LABEL_KEYS:
        if key in token:
            return LABEL_MAP[key]
    return None


def _columns_from_units(units: list[str], pair_pct: bool) -> list[int] | None:
    """単位行から「各列が何個のデータトークンを食うか」を出す。

    `百万円 %` は (金額, 増減率) の1列にも、金額の列と率の列の2列にもなりうる。
    どちらの読み方かはラベル数とデータ列数で決める（呼び出し側が両方試す）。
    """
    cols: list[int] = []
    i, n = 0, len(units)
    while i < n:
        u = units[i]
        if u == "円" and i + 1 < n and units[i + 1] == "銭":
            cols.append(1)
            i += 2
        elif u in AMOUNT_UNIT_TOKENS and pair_pct and i + 1 < n and units[i + 1] == "%":
            cols.append(2)
            i += 2
        elif u in AMOUNT_UNIT_TOKENS or u == "%":
            cols.append(1)
            i += 1
        else:
            return None
    return cols


def _collect_labels(lines: list[str], unit_idx: int) -> list[str]:
    """単位行の直前から見出しラベルの行を集める（列見出しは折り返すことがある）。"""
    out: list[str] = []
    for i in range(unit_idx - 1, max(-1, unit_idx - 7), -1):
        line = lines[i].strip()
        if not line:
            continue
        if (HEADING_RE.match(line) or ROW_RE.match(line) or _is_unit_line(line)
                or "表示は" in line or "切捨て" in line or line.startswith("(注")):
            break
        out.append(line)
    out.reverse()
    return out


LABEL_JOIN_MAX = 2


def _joined_label(tokens: list[str], i: int) -> str:
    """折り返した見出しを元の表記に戻す。

    「親会社株主に帰属」「する中間純利益」のように、長い項目名は行をまたいで
    割れる（勤次郎 4013 で実測）。後半だけを label にすると
    `する中間純利益` という意味を成さない文字列が台帳に残る。
    直前の、どの metric にも当たらないトークンを最大2つまで前に繋ぐ。
    """
    parts = [tokens[i]]
    j = i - 1
    while j >= 0 and len(parts) <= LABEL_JOIN_MAX and _label_to_metric(tokens[j]) is None:
        parts.insert(0, tokens[j])
        j -= 1
    return "".join(parts)


def _nearest_heading(lines: list[str], unit_idx: int) -> str:
    for i in range(unit_idx - 1, -1, -1):
        if HEADING_RE.match(lines[i].strip()):
            return lines[i].strip()
    return ""


def _metrics_from_labels(label_lines: list[str], n_cols: int) -> list[str] | None:
    """ラベル行を metric 名の並びにする。折り返しで重複した語は先頭だけ残す。"""
    text = " ".join(label_lines)
    units_only = all(t in UNIT_TOKENS for t in _tokens(text)) if text else True
    if units_only:
        return None

    # 1株当たり利益だけの小表（単位が「円 銭」のみ）は専用に扱う。
    # ラベルが「1株当たり / 四半期純利益 / 潜在株式調整後 / …」と折り返すため、
    # 素直にトークンを並べると「四半期純利益」を独立列と読み違える。
    if "1株当たり" in text.replace(" ", ""):
        compact = text.replace(" ", "")
        if "潜在株式調整後" in compact and n_cols == 2:
            return ["eps", "eps_diluted"]
        if n_cols == 1:
            return ["eps"]

    seen: list[str] = []
    for tok in _tokens(text):
        m = _label_to_metric(tok)
        if m and m not in seen:
            seen.append(m)
    return seen or None


@dataclass
class Table:
    kind: str                 # performance / balance / forecast / other
    heading: str
    metrics: list[str]
    units: list[str]          # 各列の CSV unit
    labels: list[str]         # 各列の開示表記（無ければ metric 名）
    widths: list[int]         # 各列が食うデータトークン数
    rows: list[tuple[str, list[str]]]   # (行ラベル, データトークン)


def _classify(heading: str) -> str:
    if "業績予想" in heading or "業績見通し" in heading:
        return "forecast"
    if "配当" in heading:
        return "other"
    if "財政状態" in heading:
        return "balance"
    if "経営成績" in heading or "業績" in heading:
        return "performance"
    return "other"


def find_tables(lines: list[str]) -> list[Table]:
    """単位行を起点にサマリー表を切り出す。構造に当たらない表は捨てる。"""
    tables: list[Table] = []
    for i, line in enumerate(lines):
        if not _is_unit_line(line):
            continue
        units = _tokens(line)

        # データ行を先に集める（列数の決定に使う）
        rows: list[tuple[str, list[str]]] = []
        for j in range(i + 1, len(lines)):
            m = ROW_RE.match(lines[j].strip())
            if not m:
                break
            label = lines[j].strip()[:m.start("rest")].strip()
            rows.append((label, _tokens(m.group("rest"))))
        if not rows:
            continue
        widths_total = len(rows[0][1])

        label_lines = _collect_labels(lines, i)
        chosen = None
        for pair_pct in (True, False):
            cols = _columns_from_units(units, pair_pct)
            if cols is None:
                continue
            metrics = _metrics_from_labels(label_lines, len(cols))
            if metrics is None:
                continue
            if len(metrics) == len(cols) and sum(cols) == widths_total:
                chosen = (cols, metrics)
                break
        if chosen is None:
            continue
        cols, metrics = chosen

        # 各列の unit は、その列を作った単位トークンから取る
        col_units: list[str] = []
        k = 0
        for w in cols:
            tok = units[k]
            if tok == "%" :
                col_units.append("pct")
            else:
                col_units.append(UNIT_TOKEN_MAP.get(tok, ""))
            k += 2 if (w == 2 or (tok == "円" and k + 1 < len(units)
                                  and units[k + 1] == "銭")) else 1
        while len(col_units) < len(cols):
            col_units.append("")

        # 見出しは列ラベルを組み立てる前に確定させる（ループ変数で潰さないため）
        heading = _nearest_heading(lines, i)

        col_labels: list[str] = []
        raw_tokens = _tokens(" ".join(label_lines))
        for idx, met in enumerate(metrics):
            lab = ""
            if met.startswith("eps"):
                # 「1株当たり」と「中間純利益」は行をまたいで折り返す。
                # 直前の利益名（並びの最後に出るもの）と繋いで元の表記に戻す
                tails = [t for t in raw_tokens if _label_to_metric(t) == "net_income"]
                lab = "1株当たり" + (tails[-1] if tails else "当期純利益")
                if met == "eps_diluted":
                    lab = "潜在株式調整後" + lab
            else:
                for pos_tok, tok in enumerate(raw_tokens):
                    if _label_to_metric(tok) == met:
                        lab = _joined_label(raw_tokens, pos_tok)
                        break
            col_labels.append(lab or met)

        tables.append(Table(_classify(heading), heading, metrics, col_units,
                            col_labels, cols, rows))
    return tables


def parse_header(text: str, lines: list[str]) -> Header | None:
    """表紙から 決算期・四半期・連結区分・会計基準・開示日 を確定する。

    **推測で「連結」と書かない**（SKILL.md）。表紙に無ければ None を返して読まない。
    """
    m = TITLE_RE.search(text)
    if not m:
        return None
    fy_year, fy_month = int(m.group("y")), int(m.group("m"))
    mid = m.group("mid") or ""
    qm = re.search(r"第(\d)四半期", mid)
    if qm:
        quarter = int(qm.group(1))
    elif "中間" in mid:
        quarter = 2                    # 中間決算短信（第2四半期）
    else:
        quarter = 4                    # 通期（本決算）
    if not 1 <= quarter <= 4 or not 1 <= fy_month <= 12:
        return None

    title = m.group(0) + text[m.end():m.end() + 40]
    if "非連結" in title:
        consolidation = "単体"
    elif "連結" in title:
        consolidation = "連結"
    else:
        return None
    if "日本基準" in title:
        standard = "日本基準"
    elif "IFRS" in title or "ＩＦＲＳ" in title:
        standard = "IFRS"
    elif "米国基準" in title:
        standard = "米国基準"
    else:
        return None

    code = None
    for line in lines[:12]:
        compact = line.replace(" ", "")
        cm = re.search(r"コード番号(\d{4,5})", compact)
        if cm:
            code = cm.group(1)[:4]
            break

    disclosed_on = None
    for line in lines[:12]:
        dm = re.fullmatch(r"(\d{4})年(\d{1,2})月(\d{1,2})日", line.strip())
        if dm:
            y, mo, d = (int(x) for x in dm.groups())
            disclosed_on = "%04d-%02d-%02d" % (y, mo, d)
            break

    # 累計かどうか。「(累計)」の明記があるか、対象期間が期首から始まっているか。
    # ここを取り違えると四半期単独の値を累計として記録してしまう。
    cumulative = "累計" in text
    if not cumulative:
        fiscal_start = fy_month % 12 + 1
        rm = PERIOD_RANGE_RE.search(text)
        if rm and int(rm.group(2)) == fiscal_start and int(rm.group(3)) == 1:
            cumulative = True
    if quarter == 4:
        cumulative = True

    return Header(code, fy_year, fy_month, quarter, consolidation, standard,
                  disclosed_on, cumulative)


def _row_period(label: str, header: Header, table: Table) -> tuple[int, int] | None:
    """行ラベルから (決算期の西暦, 四半期) を出す。読めなければ None。"""
    m = ROW_RE.match(label + " x")
    if not m:
        return None
    if m.group("full"):
        fm = re.search(r"(\d{4})年(\d{1,2})月期", table.heading)
        if not fm:
            return None
        return int(fm.group(1)), 4
    if m.group("fq"):
        fm = re.search(r"(\d{4})年(\d{1,2})月期", table.heading)
        if not fm:
            return None
        return int(fm.group(1)), int(m.group("fq"))
    year = int(m.group("y"))
    if m.group("q"):
        quarter = int(m.group("q"))
    elif m.group("mid"):
        quarter = 2          # 「2026年12月期中間期」= 第2四半期累計
    else:
        quarter = 4          # 期の表記だけ＝通期（財政状態の前期末行）
    return year, quarter


def extract_facts(text: str) -> tuple[Header | None, list[Fact], list[str]]:
    """1ページ目のテキストから Fact を作る。notes は人間への申し送り。"""
    text = normalize_text(text)
    lines = [ln.strip() for ln in text.splitlines()]
    notes: list[str] = []

    header = parse_header(text, lines)
    if header is None:
        return None, [], ["表紙（決算期・連結区分・会計基準）を確定できない"]

    facts: dict[tuple[str, str], Fact] = {}
    for table in find_tables(lines):
        if table.kind == "other":
            continue
        for label, toks in table.rows:
            period = _row_period(label, header, table)
            if period is None:
                notes.append("行ラベルを解釈できない: " + label[:40])
                continue
            row_year, row_q = period
            cum = "cum"
            if table.kind == "performance" and row_q != 4 and not header.cumulative:
                notes.append("累計かどうかを確定できないため取らない: " + label[:40])
                continue
            period_str = "FY%dQ%d%s" % (row_year, row_q, cum)

            pos = 0
            for idx, metric in enumerate(table.metrics):
                width = table.widths[idx]
                cells = toks[pos:pos + width]
                pos += width
                if metric == "eps_diluted":
                    continue
                value = parse_number(cells[0]) if cells else None
                if value is None:
                    continue
                pct = parse_number(cells[1]) if width == 2 and len(cells) > 1 else None

                name = _suffix_metric(metric, table, header, row_year, row_q)
                if name is None:
                    continue
                lab = safe_label(table.labels[idx])
                flags: list[str] = []
                if lab is None:
                    lab = metric
                    flags.append("LABEL_UNSAFE")
                if table.kind == "forecast":
                    lab = safe_label(lab + "(会社予想)") or metric
                fact = Fact(name, value, table.units[idx] or "JPY_million",
                            period_str, lab, pct, flags)
                facts.setdefault((period_str, name), fact)

    ordered = [facts[k] for k in sorted(facts)]
    if not ordered:
        notes.append("サマリー表から1件も抽出できない")
    return header, ordered, notes


def _suffix_metric(metric: str, table: Table, header: Header,
                   row_year: int, row_q: int) -> str | None:
    """行がどの期のものかに応じて metric 名を決める。

    同じ開示から出た行は `(code, date, metric)` を一意キーにするので、
    当期・前年同期・会社計画を同じ名前にできない（SKILL.md がこの語彙を持つ理由）。
    """
    if table.kind == "forecast":
        if metric in ("total_assets", "net_assets", "equity_ratio"):
            return None
        return metric + "_fy_plan"
    if table.kind == "balance":
        if metric not in ("total_assets", "net_assets", "equity_ratio"):
            return None
        if row_year == header.fy_year and row_q == header.quarter:
            return metric
        if row_year == header.fy_year - 1:
            return metric + "_prev_fy"
        return None
    # performance
    if metric in ("total_assets", "net_assets", "equity_ratio"):
        return None
    if row_year == header.fy_year and row_q == header.quarter:
        return metric
    if row_year == header.fy_year - 1 and row_q == header.quarter:
        return metric + "_prev_year"
    return None


# =============================================================================
# 検算（同一文書内の第2の証人）
# =============================================================================

def yoy_bounds(cur: float, prev: float, step: float = 1.0) -> tuple[float, float]:
    """切捨て表示された2値から、増減率(%)が取りうる区間を出す。

    百万円未満切捨てなら真の値は [v, v+step) にある。魔法の許容値を置かず、
    表示の丸め幅から区間を導く。印字は小数第1位なので ±0.06 を足す。
    """
    lo = (cur / (prev + step) - 1.0) * 100.0
    hi = ((cur + step) / prev - 1.0) * 100.0
    return lo - PCT_PRINT_HALF_STEP, hi + PCT_PRINT_HALF_STEP


FY4_RE = re.compile(r"FY(\d{4})Q4cum")


def _yoy_pair(cur: Fact, prev: Fact | None) -> None:
    """1組の (当期, 比較対象) を、印字された増減率で突き合わせる。

    **判定は両方の行に付ける。** 検算に参加したのは2つの数字であり、
    片方だけを「検算済み」にすると、もう片方が無検査のまま採用される。
    """
    if (cur.printed_yoy_pct is None or prev is None or prev.value <= 0
            or cur.unit not in UNIT_JPY or prev.unit != cur.unit):
        cur.flags.append("YOY_CHECK_NA")
        if prev is not None:
            prev.flags.append("YOY_CHECK_NA")
        return
    lo, hi = yoy_bounds(cur.value, prev.value)
    verdict = "OK" if lo <= cur.printed_yoy_pct <= hi else "YOY_MISMATCH"
    cur.flags.append(verdict)
    prev.flags.append(verdict)


def verify_yoy(facts: list[Fact]) -> None:
    """印字された増減率と、実額から再計算した比率を突き合わせる。

    合わなければ**表の読み違い**なので、当期・前年同期の両方を採用しない。
    前年同期が負のときは比率が意味を持たないので検算しない（SKILL.md）。

    会社計画（`_fy_plan`）にも「対前期増減率」が印字される。比較対象になる
    前期の通期実績が同じ短信に載っているのは本決算のときだけなので、
    載っていなければ NA になる（四半期短信では自然に NA に落ちる）。
    """
    index = {f.metric: f for f in facts}
    for f in facts:
        if f.metric.startswith(BALANCE_METRICS):
            continue                       # 自己資本比率の検算が担当する
        if f.metric.endswith(("_prev_year", "_prev_fy")):
            continue                       # 当期側から両方に判定が付く
        if f.metric.endswith("_fy_plan"):
            base = index.get(f.metric[: -len("_fy_plan")])
            pm = FY4_RE.fullmatch(f.period)
            bm = FY4_RE.fullmatch(base.period) if base is not None else None
            usable = base if (pm and bm and int(pm.group(1)) - 1 == int(bm.group(1))) \
                else None
            _yoy_pair(f, usable)
            continue
        _yoy_pair(f, index.get(f.metric + "_prev_year"))


def verify_all(facts: list[Fact], summary_text: str, full_text: str = "") -> None:
    """検算をまとめて走らせる。**process() とテストはここを共有する**。

    片方だけに検査を足すと、テストが通ったまま本番の経路に穴が空く。
    """
    apply_range_checks(facts)
    verify_yoy(facts)
    verify_eps(facts, parse_avg_shares(full_text or summary_text))
    verify_equity_ratio(facts, parse_equity_ref(summary_text))
    for f in facts:
        if not f.flags:
            # どの検算にも参加しなかった行。「検査できなかった」を
            # 「検査に通った」と書かない（checks.py 設計原則1）
            f.flags.append("NOT_CROSS_CHECKED")


EQUITY_REF_RE = re.compile(r"\(参考\)\s*自己資本(?P<tail>.*)")


def parse_equity_ref(text: str) -> list[tuple[float, str]]:
    """短信の「(参考) 自己資本 … 185百万円 … 270百万円」を順に読む。

    連結では 自己資本 ≠ 純資産（非支配株主持分がある）ため、自己資本比率の
    検算には純資産ではなくこの行を使う。無ければ検算しない。
    """
    out: list[tuple[float, str]] = []
    m = EQUITY_REF_RE.search(normalize_text(text))
    if not m:
        return out
    for vm in re.finditer(r"([△▲-]?[\d,]+)\s*(百万円|千円|円)", m.group("tail")):
        v = parse_number(vm.group(1))
        if v is not None:
            out.append((v, UNIT_TOKEN_MAP.get(vm.group(2), "")))
    return out


def verify_equity_ratio(facts: list[Fact], equity_ref: list[tuple[float, str]]) -> None:
    """自己資本比率 ≒ 自己資本 ÷ 総資産 を突き合わせる（同一表内の第2の証人）。

    採用は止めない。連結で自己資本の参考行が無い場合など、比率が一致しない
    正当な理由がありうるため、フラグとして残すだけにする。
    """
    index = {f.metric: f for f in facts}
    for pos, suffix in enumerate(("", "_prev_fy")):
        er = index.get("equity_ratio" + suffix)
        ta = index.get("total_assets" + suffix)
        if er is None:
            continue
        equity = None
        if pos < len(equity_ref) and ta is not None and equity_ref[pos][1] == ta.unit:
            equity = equity_ref[pos][0]
        if equity is None or ta is None or ta.value <= 0:
            er.flags.append("EQUITY_CROSS_NA")
            continue
        lo = equity / (ta.value + 1.0) * 100.0 - PCT_PRINT_HALF_STEP
        hi = (equity + 1.0) / ta.value * 100.0 + PCT_PRINT_HALF_STEP
        er.flags.append("EQUITY_CROSS_OK" if lo <= er.value <= hi
                        else "EQUITY_CROSS_FAILED")


def verify_eps(facts: list[Fact], avg_shares: float | None) -> None:
    """1株益 ≒ 純利益 ÷ 期中平均株式数 を突き合わせる（第3の証人）。

    純利益は百万円未満切捨てなので幅を持つ。株式数は別ページから拾うため
    ここでの不一致は**採用を止めない**（フラグとして残すだけ）。止めるのは
    同じ表の中で会社自身が印字した増減率と食い違ったときだけにする。
    """
    ni = next((f for f in facts if f.metric == "net_income"), None)
    eps = next((f for f in facts if f.metric == "eps"), None)
    if eps is None:
        return
    if ni is None or avg_shares in (None, 0) or ni.unit not in UNIT_JPY:
        eps.flags.append("EPS_CROSS_NA")
        return
    step = UNIT_JPY[ni.unit]
    base = ni.value * step
    lo_jpy, hi_jpy = (base, base + step) if base >= 0 else (base - step, base)
    lo, hi = lo_jpy / avg_shares, hi_jpy / avg_shares
    if lo - 0.01 <= eps.value <= hi + 0.01:
        eps.flags.append("EPS_CROSS_OK")
    else:
        eps.flags.append("EPS_CROSS_FAILED")


# 「③ 期中平均株式数（四半期累計） 2026年6月期3Q 2,525,238株 …」から先頭の1件を取る。
# 途中に「2026年6月期3Q」のような4桁の数字が挟まるので、5桁以上の並びだけを拾う
AVG_SHARES_RE = re.compile(r"期中平均株式数[\s\S]{0,80}?(\d[\d,]{4,})\s*株")


def parse_avg_shares(text: str) -> float | None:
    m = AVG_SHARES_RE.search(normalize_text(text))
    return parse_number(m.group(1)) if m else None


def apply_range_checks(facts: list[Fact]) -> None:
    """SKILL.md「値の正規化と範囲検証」の 5〜7 をコードで実行する。

    決算月と `master.yaml` の突き合わせ（8）は表紙から来る情報なので
    `process()` が行う（period は西暦しか持たない）。
    """
    for f in facts:
        if abs(f.value) > VALUE_ABS_MAX:
            f.flags.append("OUT_OF_RANGE")
        if f.metric.startswith("revenue") and f.value < 0:
            f.flags.append("SIGN_SUSPECT")

    # 桁点検: 当期と前年同期の売上高の比が 10倍/0.1倍 を外れたら単位の取り違え。
    # 利益は赤字と黒字を行き来するので比では判定できない（売上高のみ）。
    index = {f.metric: f for f in facts}
    cur, prev = index.get("revenue"), index.get("revenue_prev_year")
    if cur is not None and prev is not None and cur.value and prev.value:
        ratio = abs(cur.value / prev.value)
        if ratio < SCALE_MIN or ratio > SCALE_MAX:
            cur.flags.append("SCALE_SUSPECT")
            prev.flags.append("SCALE_SUSPECT")


# =============================================================================
# PDF の取得と読み取り
# =============================================================================

@dataclass
class PdfResult:
    status: str
    pages_text: list[str] = field(default_factory=list)
    pages: int = 0
    note: str = ""

    @property
    def text(self) -> str:
        return "\n".join(self.pages_text)


def read_pdf_bytes(blob: bytes) -> PdfResult:
    """PDF のテキスト層をページ単位で取り出す。**読めなかった理由を status で残す**。"""
    if PdfReader is None:
        return PdfResult("PDF_UNREADABLE", note="pypdf が入っていない")
    if not blob[:5].startswith(b"%PDF"):
        return PdfResult("NOT_PDF", note="先頭が %PDF でない")
    try:
        reader = PdfReader(io.BytesIO(blob))
        if reader.is_encrypted:
            try:
                if reader.decrypt("") == 0:
                    return PdfResult("PDF_ENCRYPTED", note="空パスワードで開けない")
            except Exception as e:  # noqa: BLE001
                return PdfResult("PDF_ENCRYPTED", note=str(e)[:80])
        parts: list[str] = []
        for page in reader.pages:
            try:
                parts.append(page.extract_text() or "")
            except Exception:  # noqa: BLE001
                parts.append("")   # 1ページ落ちても全体を捨てない
        joined = "\n".join(parts)
        if len(joined.strip()) < MIN_TEXT_CHARS:
            return PdfResult("PDF_IMAGE_ONLY", parts, len(reader.pages),
                             "テキスト層が %d 文字しかない" % len(joined.strip()))
        return PdfResult("OK", parts, len(reader.pages))
    except Exception as e:  # noqa: BLE001
        return PdfResult("PDF_UNREADABLE", note=type(e).__name__ + ": " + str(e)[:80])


def http_get(url: str, pol: dict) -> bytes | None:
    for attempt in range(int(pol.get("retries", 2)) + 1):
        try:
            r = requests.get(url, headers={"User-Agent": pol["user_agent"]},
                             timeout=pol.get("timeout_sec", 20))
            r.raise_for_status()
            return r.content
        except Exception as e:  # noqa: BLE001
            if attempt == int(pol.get("retries", 2)):
                print("  取得失敗 " + url + ": " + str(e)[:120], file=sys.stderr)
    return None


# =============================================================================
# 短信PDFの発見（sources.yaml に書いた経路だけ）
# =============================================================================

@dataclass(frozen=True)
class Disclosure:
    code: str
    disclosed_on: str
    title: str
    pdf_url: str
    route: str


def _title_ok(title: str, cfg: dict) -> bool:
    inc = cfg.get("title_include") or ["決算短信"]
    exc = cfg.get("title_exclude") or []
    if not any(w in title for w in inc):
        return False
    return not any(w in title for w in exc)


def discover_tdnet(codes: set[str], yyyymmdd: str, entry: dict, cfg: dict,
                   pol: dict) -> list[Disclosure]:
    """TDnet の日次一覧から、対象コードの決算短信PDFを拾う。

    コードは一覧では5桁（証券コード+0）で並ぶ。**表題の語で短信だけに絞る**
    （決算説明資料は別物であり、サマリー表を持たない）。
    """
    out: list[Disclosure] = []
    date_iso = yyyymmdd[:4] + "-" + yyyymmdd[4:6] + "-" + yyyymmdd[6:]
    for page in range(1, int(entry.get("max_pages", 8)) + 1):
        url = entry["list_url"].format(page=page, yyyymmdd=yyyymmdd)
        blob = http_get(url, pol)
        time.sleep(pol.get("interval_sec", 3))
        if blob is None:
            break
        soup = BeautifulSoup(blob.decode("utf-8", "replace"), "html.parser")
        table = soup.select_one(entry["table_selector"])
        if table is None:
            break
        rows = table.find_all("tr")
        if not rows:
            break
        for tr in rows:
            cells = [c.get_text(strip=True) for c in tr.find_all("td")]
            if len(cells) < 4:
                continue
            code5, title = cells[1], cells[3]
            code = code5[:4]
            if code not in codes or not _title_ok(title, cfg):
                continue
            link = tr.find("a", href=True)
            if link is None:
                continue
            out.append(Disclosure(code, date_iso, title,
                                  entry["pdf_base"] + link["href"], "tdnet"))
        if len(rows) < 100:
            break
    return out


def known_disclosures(code: str, cfg: dict) -> list[Disclosure]:
    entries = (cfg.get("known_pdfs") or {}).get(code) or []
    out = []
    for e in entries:
        out.append(Disclosure(code, str(e.get("disclosed_on") or ""),
                              str(e.get("title") or ""), str(e["url"]), "known"))
    return out


# =============================================================================
# CSV（append-only）
# =============================================================================

def _csv_safe(row: dict, fields: list[str]) -> str | None:
    for k in fields:
        v = "" if row.get(k) is None else str(row[k])
        if any(ch in v for ch in (",", '"', "\n", "\r")):
            return k
    return None


def append_only(path: Path, rows: list[dict], fields: list[str],
                keys: tuple[str, ...]) -> int:
    """既存キーは書き換えない。fetch.py の append_only と同じ規律。"""
    existing: set[tuple] = set()
    if path.exists():
        with path.open(encoding="utf-8-sig", newline="") as f:
            for r in csv.DictReader(f):
                existing.add(tuple(str(r.get(k) or "") for k in keys))
    new = []
    for r in rows:
        k = tuple(str(r.get(c) or "") for c in keys)
        if k in existing:
            continue
        bad = _csv_safe(r, fields)
        if bad is not None:
            print("  区切り文字を含むため記録しない: " + bad, file=sys.stderr)
            continue
        existing.add(k)
        new.append(r)
    if not new:
        return 0
    path.parent.mkdir(parents=True, exist_ok=True)
    write_header = not path.exists()
    new.sort(key=lambda r: tuple(str(r.get(c) or "") for c in keys))
    with path.open("a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        if write_header:
            w.writeheader()
        w.writerows(new)
    return len(new)


def facts_to_rows(code: str, header: Header, facts: list[Fact],
                  source_url: str, fetched_at: str) -> list[dict]:
    rows = []
    for f in facts:
        if f.metric not in TANSHIN_METRICS:
            continue
        if f.unit not in UNITS:
            continue
        definition = "|".join([f.period, header.consolidation, header.standard,
                               f.item_label])
        # 既定は verify_all() が付ける（NOT_CROSS_CHECKED）。
        # ここで "OK" に倒すと「検査できなかった」が「検査に通った」になる
        flags = sorted(set(f.flags)) or ["NOT_CROSS_CHECKED"]
        rows.append({
            "date": header.disclosed_on or "",
            "code": code,
            "metric": f.metric,
            "value": fmt_value(f.value) if f.adopted else "",
            "value_extracted": fmt_value(f.value),
            "unit": f.unit,
            "definition": definition,
            "assumed": "false",
            "source": SOURCE_ID,
            "tier": TIER,
            "status": "|".join(flags),
            "source_url": source_url,
            "fetched_at": fetched_at,
        })
    return rows


# =============================================================================
# 一次情報の読み出しと、二次情報との調停（fetch_fundamentals.py 用の API）
# =============================================================================

def load_facts(code: str, data_dir: Path | None = None) -> dict[tuple[str, str], dict]:
    """`data/tanshin/{code}.csv` を `{(period, metric): row}` で返す。

    採用値（`value`）が空の行も返す。呼び出し側が「一次情報はあるが検算に
    通っていない」を見分けられるようにするため（黙って落とすと二次情報だけで
    採用してしまう）。同じ period/metric が複数あれば **date が新しい行**を採る
    （訂正短信は新しい date で追記されるため）。
    """
    base = data_dir or (ROOT / "data")
    path = base / "tanshin" / (str(code) + ".csv")
    if not path.exists():
        return {}
    out: dict[tuple[str, str], dict] = {}
    with path.open(encoding="utf-8-sig", newline="") as f:
        for r in csv.DictReader(f):
            definition = str(r.get("definition") or "")
            period = definition.split("|")[0] if definition else ""
            key = (period, str(r.get("metric") or ""))
            prev = out.get(key)
            if prev is None or str(r.get("date") or "") >= str(prev.get("date") or ""):
                out[key] = r
    return out


def to_jpy(value: float | None, unit: str) -> float | None:
    if value is None or unit not in UNIT_JPY:
        return None
    return value * UNIT_JPY[unit]


# 金額でない指標（比率・倍率）の一致許容。開示は小数第1位までなので、
# 丸めの半幅ぶんだけ許す。単位が違うもの（% と 百万円）は比べない。
NON_JPY_TOLERANCE = {"pct": 0.05, "x": 0.05, "shares": 0.0}


def values_agree(a: float, unit_a: str, b: float, unit_b: str) -> bool:
    """粗い単位への切捨てぶんだけ許して一致とみなす。

    短信は 百万円未満切捨て、まとめサイトは 百万円 か 千円。同じ数字でも
    表示単位が違えば末尾が落ちる。**許容幅は粗いほうの単位そのもの**であり、
    それ以上は一致とみなさない（魔法の相対誤差を置かない）。

    自己資本比率のような比率は円に換算できない。金額と同じ経路に通すと
    **常に不一致**になり、比率の照合が丸ごと死ぬ（実測で発覚）ので分けて扱う。
    """
    if unit_a in UNIT_JPY and unit_b in UNIT_JPY:
        ja, jb = a * UNIT_JPY[unit_a], b * UNIT_JPY[unit_b]
        step = max(UNIT_JPY[unit_a], UNIT_JPY[unit_b])
        return abs(ja - jb) < step
    if unit_a != unit_b:
        return False                     # % と 百万円 を比べない
    tol = NON_JPY_TOLERANCE.get(unit_a)
    return False if tol is None else abs(a - b) <= tol


def adjudicate(primary: dict | None, secondary: dict[str, float | None],
               secondary_unit: str = "JPY_million") -> tuple[float | None, str]:
    """一次情報（短信）を最優先にして採用値と status を決める。

    二次情報が2つとも一致していても、一次情報と食い違えば MISMATCH。
    一次情報が「あるが採用不可（検算落ち）」のときは、二次情報で上書きしない。
    """
    named = {k: v for k, v in (secondary or {}).items() if v is not None}
    if primary is not None:
        pv = primary.get("value")
        if pv in (None, ""):
            return None, "MISMATCH|PRIMARY_UNVERIFIED"
        pval = float(pv)
        punit = str(primary.get("unit") or "JPY_million")
        if not named:
            return pval, "PRIMARY_ONLY"
        for value in named.values():
            if not values_agree(pval, punit, float(value), secondary_unit):
                return None, "MISMATCH|PRIMARY_DISAGREE"
        return pval, "OK|PRIMARY"

    if len(named) >= 2:
        vals = list(named.values())
        first = float(vals[0])
        for v in vals[1:]:
            if not values_agree(first, secondary_unit, float(v), secondary_unit):
                return None, "MISMATCH"
        return first, "OK|SECONDARY"
    if len(named) == 1:
        return None, "SINGLE_SOURCE"
    return None, "FETCH_FAILED"


# =============================================================================
# 1件の短信を処理する
# =============================================================================

def process(disc: Disclosure, master_stock: dict, pol: dict,
            local_pdf: Path | None = None) -> tuple[list[dict], dict]:
    """短信1本を読み、データ行と取得ログ行を返す。"""
    now = datetime.now(JST).isoformat(timespec="seconds")
    log = {
        "disclosed_on": disc.disclosed_on,
        "code": disc.code,
        "pdf_url": disc.pdf_url,
        "status": "",
        "pages": "",
        "text_chars": "",
        "metrics_written": "0",
        "note": "",
        "fetched_at": now,
    }

    if local_pdf is not None:
        blob = local_pdf.read_bytes()
    else:
        blob = http_get(disc.pdf_url, pol)
        time.sleep(pol.get("interval_sec", 3))
    if blob is None:
        log["status"] = "DOWNLOAD_FAILED"
        return [], log

    res = read_pdf_bytes(blob)
    log["pages"] = str(res.pages)
    log["text_chars"] = str(len(res.text))
    if res.status != "OK":
        log["status"] = res.status
        log["note"] = res.note.replace(",", " ")[:120]
        return [], log

    # サマリー表は1ページ目に載る。財務諸表のページまで読ませると、
    # 別の表の行を同じ形として拾いかねないので、走査範囲を先頭に限る。
    # 1ページ目で取れなければ2ページ目まで（表紙が2ページに割れる短信がある）。
    summary_text = res.pages_text[0]
    header, facts, notes = extract_facts(summary_text)
    if header is None or not facts:
        summary_text = "\n".join(res.pages_text[:2])
        header, facts, notes = extract_facts(summary_text)
    if header is None or not facts:
        log["status"] = "SUMMARY_UNPARSED"
        log["note"] = (" / ".join(notes))[:120].replace(",", " ")
        return [], log

    fiscal = str(master_stock.get("fiscal_year_end") or "")
    if re.fullmatch(r"\d{2}", fiscal) and int(fiscal) != header.fy_month:
        for f in facts:
            f.flags.append("PERIOD_MISMATCH")
        notes.append("決算月が master.yaml と違う: 短信=%d / master=%s"
                     % (header.fy_month, fiscal))

    verify_all(facts, summary_text, res.text)

    injected = scan_injection(res.text)
    if injected:
        notes.append("指示めいた文字列を検出（データとして扱い実行しない）: "
                     + " ".join(injected))

    rows = facts_to_rows(disc.code, header, facts, disc.pdf_url, now)
    log["status"] = "OK"
    log["metrics_written"] = str(sum(1 for r in rows if r["value"] != ""))
    log["note"] = (" / ".join(notes))[:120].replace(",", " ")
    return rows, log


# =============================================================================
# 検査（checks.py から `from fetch_tanshin import check_tanshin` で呼べる）
# =============================================================================

def check_tanshin(data_dir: Path, master: dict) -> list[tuple[str, str, str]]:
    """data/tanshin/ の形式検査。戻り値は (level, target, message) の列。

    checks.py には**まだ組み込んでいない**（並行して fetch_fundamentals.py が
    同じファイルを触るため衝突を避けた）。組み込みは run_checks() に1行:
        for lv, tgt, msg in check_tanshin(data_dir, master): ...
    """
    out: list[tuple[str, str, str]] = []
    tdir = data_dir / "tanshin"
    if not tdir.exists():
        return out
    known = {str(s["code"]) for s in master.get("stocks", [])}
    fiscal = {str(s["code"]): str(s.get("fiscal_year_end") or "")
              for s in master.get("stocks", [])}

    log_path = tdir / "fetch_log.csv"
    logged: set[tuple[str, str]] = set()
    if log_path.exists():
        with log_path.open(encoding="utf-8-sig", newline="") as f:
            rows = list(csv.DictReader(f))
        missing = [c for c in LOG_FIELDS if rows and c not in rows[0]]
        if missing:
            out.append(("FAIL", "data/tanshin/fetch_log.csv",
                        "必須列が欠落: " + str(missing)))
        # 同じPDFを読み直して成功した場合は、過去の失敗行を残したまま OK 行が
        # 追記される（append-only）。読めているものを「読めていない」と言わない
        succeeded = {(str(r.get("code")), str(r.get("pdf_url")))
                     for r in rows if str(r.get("status")) == "OK"}
        for r in rows:
            st = str(r.get("status") or "")
            code_url = (str(r.get("code")), str(r.get("pdf_url")))
            if st not in LOG_STATUSES:
                out.append(("FAIL", "data/tanshin/fetch_log.csv",
                            "status が定義外: " + st))
            if st != "OK" and code_url not in succeeded:
                out.append(("WARN", "data/tanshin/fetch_log.csv",
                            str(r.get("code")) + " " + str(r.get("disclosed_on"))
                            + ": 短信を読めていない（" + st + "）"
                            + str(r.get("note") or "")))
            elif st == "OK":
                logged.add((str(r.get("code")), str(r.get("disclosed_on"))))
    else:
        out.append(("WARN", "data/tanshin/fetch_log.csv",
                    "取得ログが無い（読めた/読めなかったの記録が残っていない）"))

    for path in sorted(tdir.glob("*.csv")):
        if path.name == "fetch_log.csv":
            continue
        code = path.stem
        target = "data/tanshin/" + path.name
        with path.open(encoding="utf-8-sig", newline="") as f:
            rows = list(csv.DictReader(f))
        if not rows:
            out.append(("WARN", target, "行が1つも無い"))
            continue
        missing = [c for c in FIELDS if c not in rows[0]]
        if missing:
            out.append(("FAIL", target, "必須列が欠落: " + str(missing)))
            continue
        if code not in known:
            out.append(("FAIL", target, "マスタ未登録のコード: " + code))

        seen: set[tuple[str, str, str]] = set()
        for r in rows:
            date = str(r.get("date") or "")
            metric = str(r.get("metric") or "")
            key = (str(r.get("code")), date, metric)
            label = date + " " + metric
            if key in seen:
                out.append(("FAIL", target, "一意キーが重複: " + label))
            seen.add(key)
            if str(r.get("code")) != code:
                out.append(("FAIL", target, "code がファイル名と違う: " + label))
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
                out.append(("FAIL", target, "date が読めない: " + label))
            if metric not in TANSHIN_METRICS:
                out.append(("FAIL", target, "metric が定義外: " + label))
            if str(r.get("unit") or "") not in UNITS:
                out.append(("FAIL", target, "unit が定義外: " + label))
            if str(r.get("assumed") or "") != "false":
                out.append(("FAIL", target,
                            "短信からの抽出に推測は無い（assumed は false）: " + label))
            if str(r.get("source") or "") != SOURCE_ID or str(r.get("tier") or "") != TIER:
                out.append(("FAIL", target, "source/tier が定義外: " + label))
            if not str(r.get("source_url") or "") or not str(r.get("fetched_at") or ""):
                out.append(("FAIL", target,
                            "source_url または fetched_at が空: " + label))

            definition = str(r.get("definition") or "")
            parts = definition.split("|")
            if len(parts) != 4 or not all(parts):
                out.append(("FAIL", target, "definition の形式が違う: " + label))
            else:
                pm = re.fullmatch(r"FY(\d{4})Q([1-4])(cum|only)", parts[0])
                if pm is None:
                    out.append(("FAIL", target, "period の形式が違う: " + label))
                if parts[1] not in ("連結", "単体"):
                    out.append(("FAIL", target, "連結区分が定義外: " + label))
                if parts[2] not in ("日本基準", "IFRS", "米国基準"):
                    out.append(("FAIL", target, "会計基準が定義外: " + label))

            flags = [p for p in str(r.get("status") or "").split("|") if p]
            if not flags or any(p not in ALL_FLAGS for p in flags):
                out.append(("FAIL", target, "status が定義外: " + label))
            blocked = any(p in BLOCKING_FLAGS for p in flags)
            value, extracted = str(r.get("value") or ""), str(r.get("value_extracted") or "")
            if blocked and value:
                out.append(("FAIL", target,
                            "検算に落ちた行が採用値に格上げされている: " + label))
            if not blocked and value != extracted:
                out.append(("FAIL", target,
                            "採用値と抽出値が食い違う: " + label))
            if not extracted:
                out.append(("FAIL", target, "抽出値が空: " + label))
            if extracted:
                try:
                    v = float(extracted)
                except ValueError:
                    out.append(("FAIL", target, "value_extracted が数値でない: " + label))
                else:
                    if abs(v) > VALUE_ABS_MAX and not blocked:
                        out.append(("FAIL", target, "桁が大きすぎる: " + label))
                    if metric.startswith("revenue") and v < 0 and not blocked:
                        out.append(("FAIL", target, "売上高系が負: " + label))
            if blocked:
                out.append(("WARN", target,
                            "検算に落ちて採用していない: " + label
                            + "（" + str(r.get("status")) + "）"))
            if (str(r.get("code")), date) not in logged:
                out.append(("WARN", target,
                            "対応する取得ログ（status=OK）が無い: " + label))
            fy = fiscal.get(code) or ""
            if re.fullmatch(r"\d{2}", fy) and len(parts) == 4:
                pass   # 決算月は表紙で確認済み。period は西暦しか持たない
    return out


# =============================================================================
# 実行
# =============================================================================

def load_config() -> tuple[dict, dict]:
    cfg = yaml.safe_load((ROOT / "data" / "sources.yaml").read_text(encoding="utf-8"))
    master = yaml.safe_load((ROOT / "data" / "master.yaml").read_text(encoding="utf-8"))
    return cfg, master


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="決算短信PDFから一次情報を読む")
    ap.add_argument("--code", help="証券コード（省略時は master.yaml の全銘柄）")
    ap.add_argument("--url", help="短信PDFのURLを直接指定する")
    ap.add_argument("--pdf", type=Path, help="ローカルPDFを読む（オフライン検証）")
    ap.add_argument("--date", help="TDnet の日次一覧を見る日（YYYY-MM-DD）")
    ap.add_argument("--dry-run", action="store_true", help="CSV に書かない")
    ap.add_argument("--check", action="store_true", help="data/tanshin/ を検査する")
    args = ap.parse_args(argv)

    cfg, master = load_config()
    if args.check:
        results = check_tanshin(ROOT / "data", master)
        for level, target, message in results:
            print("[" + level + "] tanshin " + target + ": " + message)
        fails = sum(1 for r in results if r[0] == "FAIL")
        warns = len(results) - fails
        print("\nFAIL %d / WARN %d" % (fails, warns))
        return 1 if fails else 0

    tcfg = cfg.get("tanshin") or {}
    pol = cfg["fetch_policy"]
    # stocks は対象外も持つ（--code で名指しすれば取れる抜け道を残す）。
    # **既定の対象は監視中の銘柄だけ。**
    stocks = {str(s["code"]): s for s in master["stocks"]}
    codes = ({args.code} if args.code
             else {str(s["code"]) for s in Y.watched_stocks(master)})
    unknown = sorted(c for c in codes if c not in stocks)
    if unknown:
        print("master.yaml に無いコード: " + ", ".join(unknown), file=sys.stderr)
        return 1

    discs: list[Disclosure] = []
    if args.url or args.pdf:
        if not args.code:
            print("--url / --pdf は --code と一緒に指定する", file=sys.stderr)
            return 1
        # --date を併せて渡すと開示日になる（読めなかった場合でもログの
        # キーが埋まる。読めた場合は短信の表紙の日付で上書きされる）
        discs.append(Disclosure(args.code, args.date or "", "",
                                args.url or str(args.pdf), "manual"))
    elif args.date:
        yyyymmdd = args.date.replace("-", "")
        for entry in tcfg.get("discovery") or []:
            if entry.get("id") == "tdnet":
                discs += discover_tdnet(codes, yyyymmdd, entry, tcfg, pol)
    else:
        for code in sorted(codes):
            discs += known_disclosures(code, tcfg)

    if not discs:
        print("対象の決算短信が見つからない（sources.yaml の経路のみを試す）")
        return 0

    total_rows, total_logs = 0, 0
    for disc in discs:
        print("読み取り: " + disc.code + " " + (disc.disclosed_on or "-")
              + " " + disc.pdf_url)
        rows, log = process(disc, stocks[disc.code], pol,
                            args.pdf if args.pdf else None)
        adopted = sum(1 for r in rows if r["value"] != "")
        print("  status=" + log["status"] + " / 抽出 " + str(len(rows))
              + "件 / 採用 " + str(adopted) + "件")
        if log["note"]:
            print("  note: " + log["note"])
        for r in rows:
            print("    " + r["metric"] + " = " + (r["value"] or "(採用せず)")
                  + " " + r["unit"] + " [" + r["status"] + "] " + r["definition"])
        if args.dry_run:
            continue
        if rows:
            date = rows[0]["date"]
            if not log["disclosed_on"]:
                log["disclosed_on"] = date
            total_rows += append_only(
                ROOT / "data" / "tanshin" / (disc.code + ".csv"),
                rows, FIELDS, ("code", "date", "metric"))
        # キーに status を含める。含めないと「1回目は読めず、2回目に読めた」が
        # 追記されず、ログが永久に「読めていない」と言い続ける
        total_logs += append_only(ROOT / "data" / "tanshin" / "fetch_log.csv",
                                  [log], LOG_FIELDS,
                                  ("code", "disclosed_on", "pdf_url", "status"))

    if not args.dry_run:
        print("\n" + str(total_rows) + "行を追記 / 取得ログ " + str(total_logs) + "行")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
