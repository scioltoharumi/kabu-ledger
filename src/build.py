"""docs/ を生成する（GitHub Pages 用の静的台帳）。

要件: requirements.md F4（F4-1〜F4-7）、review-findings.md F-04/F-06/F-07/F-08。

設計原則（破らないこと）:
  1. **決定論的生成**（F4-1・D8）。出力に生成時刻を埋め込まない。
     可変要素は「集計基準日」だけで、それも壁時計ではなく **data の最終足の日付**から採る。
     date.today() を使わない（使うと日付が変わるだけで全ページが diff る）。
     行順・辞書順はすべて固定する。git diff が「先週から何が変わったか」そのものになる。
  2. **計算も判定もしない**。判定は judge.py、指標は indicators.py が持つ（SSoT）。
     このモジュールは受け取った値を表示するだけで、閾値や式をここで再定義しない。
  3. **欠測を隠さない**（D7）。値が無いところは「—」で出し、台帳冒頭に欠測一覧を出す。
     `TO_VERIFY` は番兵値として「未検証」に落とす（F-08。PyYAML では文字列で読まれるため
     isinstance(str) では落ちない）。
  4. **リンクは深さを持たせる**（F-06）。stock/ 配下は 1階層深いので nav に `../` を付ける。

出力:
  docs/index.html    台帳（判定スタンプ・指標の実値・スクリーニング5条件・保有）
  docs/data.html     データ台帳（取得元・取得日時・二重照合結果）
  docs/scoring.html  予測採点
  docs/formula.html  算出ロジックの全文開示（F-07）
  docs/guide.html    読み方（非専門家向け・用語集つき／F-07）
  docs/stock/{code}.html
  scoring/stamps.json  notify.py の入力（F4-7・F-04）
"""
from __future__ import annotations

import csv
import html
import json
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"
sys.path.insert(0, str(Path(__file__).resolve().parent))

import checks as C  # noqa: E402
import indicators as tech  # noqa: E402
import judge as J  # noqa: E402
import score as S  # noqa: E402


# =============================================================================
# 表示語彙（ここ以外にリテラルを置かない）
# =============================================================================

SITE_TITLE = "成長株 監視台帳"
TO_VERIFY = "TO_VERIFY"
DASH = "—"

# 判定スタンプ → CSS クラス（日本語＋括弧はクラス名に使えないので ASCII に写す）
STAMP_CLASS = {
    J.STAMP_LIQUIDITY: "liq",
    J.STAMP_TREND: "trend",
    J.STAMP_SELL: "sell",
    J.STAMP_OVERHEAT: "hot",
    J.STAMP_PROBE: "probe",
    J.STAMP_WATCH: "watch",
    J.STAMP_BUY: "buy",
}

# 判定スタンプの意味（index と formula と guide で同じ文言を使う）
STAMP_MEANING = {
    J.STAMP_LIQUIDITY: "20日平均売買代金が閾値未満。他の指標を見ずにここで確定する",
    J.STAMP_TREND: "週足中期移動平均線が下向き。鉄則の第一条により買いで入らない",
    J.STAMP_SELL: "雲の下、または逆指値ライン抵触、または基準到達×デッドクロス気味",
    J.STAMP_OVERHEAT: "RSI・25日乖離率・信用倍率のいずれかが過熱域。入るなら押し目を待つ",
    J.STAMP_PROBE: "必要な指標が算出できないか、ファンダ条件を満たさない。買を出さずに止める",
    J.STAMP_WATCH: "保有中で6か月2倍ライン未到達。様子見・損切り確認",
    J.STAMP_BUY: "①〜⑤のゲートをすべて通過した（鉄則の全項目を確認したわけではない）。"
                 "売買の実行判断は人間が行う",
}

RESULT_CLASS = {J.PASS: "pass", J.FAIL: "fail", J.UNKNOWN: "unknown",
                J.SKIPPED: "skipped", J.NA: "na"}
RESULT_LABEL = {J.PASS: "通過", J.FAIL: "該当", J.UNKNOWN: "未計算",
                J.SKIPPED: "未評価", J.NA: "対象外"}

CLOUD_LABEL = {"above": "雲の上", "in": "雲の中", "below": "雲の下"}
DIRECTION_LABEL = {"up": "上向き", "down": "下向き", "flat": "横ばい"}
CROSS_LABEL = {"golden": "ゴールデンクロス", "dead": "デッドクロス", "parallel": "平行",
               "golden_ish": "ゴールデンクロス気味", "dead_ish": "デッドクロス気味"}
CLOUD_CROSS_LABEL = {"breakout_up": "雲を上抜け", "breakdown": "雲を下抜け"}

MARK_CLASS = {J.MARK_OK: "mk-ok", J.MARK_NG: "mk-ng", J.MARK_UNKNOWN: "mk-unk"}

# 決算比率の状態。**「該当しない」と「未計算」を混同させない**（judge が語彙の正）。
KPI_STATUS_LABEL = {
    J.KPI_OK: "算出できた",
    J.KPI_UNKNOWN: "未計算（決算行が無い／単位不一致／数値が読めない）",
    J.KPI_NOT_APPLICABLE: "該当しない（直近の開示が1Q累計ではない。条件から外す）",
    J.KPI_BASE_NOT_POSITIVE: "前年同期が0以下のため比率を定義できない",
}

# status は `|` 区切りで複数のフラグを持つ（例: `SINGLE_SOURCE|NO_TRADE`）。
# 前半4つが照合結果（必ず1つ）、後半2つが付加情報。
STATUS_MEANING = [
    ("OK", "2ソースの生終値が完全一致。採用値として close 列に入る"),
    ("MISMATCH", "2ソースが不一致。両値を記録し、close 列は空にする"),
    ("SINGLE_SOURCE", "1ソースのみ取得。照合が成立しないので close 列は空にする"),
    ("FETCH_FAILED", "全ソース取得失敗。欠測として記録する（推定値で埋めない）"),
    ("NO_TRADE", "出来高0（売買不成立）。始値・高値・安値は存在しないので終値で"
                 "代替している。照合結果と併記する（例 SINGLE_SOURCE|NO_TRADE）。"
                 "2026-08-12 以前に書かれた行は `NO_TRADE` 単独で、"
                 "照合を経ないまま close が入っている（過去行は書き換えない）"),
    ("VOLUME_MISMATCH", "2ソースの出来高が2倍以上食い違う。列の取り違え・単位違いの疑い。"
                        "終値の採用可否とは独立（close の扱いは照合結果が決める）"),
]

MARGIN_STATUS_MEANING = [
    ("OK", "売残・買残・倍率・単位のすべてを取得した"),
    ("RATIO_NA", "倍率が「－」。売り残0で定義できないだけで、"
                 "「過熱していない」とは読み替えない"),
    ("UNIT_UNKNOWN", "単位見出しが読めない。残高の解釈を保留する"),
    ("BALANCE_MISSING", "売残または買残が数値として読めない"),
    ("RATIO_INCONSISTENT", "表示倍率と 買い残÷売り残 が乖離。列の取り違え検出用"),
]

NAV_ITEMS = (
    ("index.html", "台帳"),
    ("scoring.html", "予測採点"),
    ("data.html", "データ台帳"),
    ("formula.html", "算出ロジック"),
    ("guide.html", "読み方"),
)

CSS = """
:root{--paper:#fdfdfc;--ink:#1c1c1a;--dim:#6b6b66;--rule:#d8d6d0;--soft:#f4f2ed;
--buy:#1f6f43;--watch:#6b6b66;--probe:#23548c;--liq:#9a3324;--trend:#7d5a3c;
--sell:#b3261e;--hot:#8a5a00;--warn:#8a5a00}
*{box-sizing:border-box}
body{background:var(--paper);color:var(--ink);margin:0;padding:2rem 1.25rem 4rem;
font-family:"Hiragino Kaku Gothic ProN","Yu Gothic","Noto Sans JP",sans-serif;
line-height:1.75;font-size:15px;-webkit-text-size-adjust:100%}
main{max-width:64rem;margin:0 auto}
h1{font-size:1.6rem;font-weight:700;letter-spacing:.02em;margin:0 0 .3rem}
h2{font-size:1.05rem;font-weight:700;margin:2.75rem 0 .6rem;
padding-bottom:.35rem;border-bottom:1px solid var(--rule)}
h3{font-size:.95rem;font-weight:700;margin:1.8rem 0 .3rem}
p{margin:.6rem 0}
.lede{color:var(--dim);margin:0 0 1.5rem;font-size:.9rem}
.scroll{overflow-x:auto;-webkit-overflow-scrolling:touch;margin:.75rem 0}
.scroll table{margin:0}
table{width:100%;border-collapse:collapse;margin:.75rem 0;font-size:.85rem}
th,td{text-align:left;padding:.5rem .6rem;border-bottom:1px solid var(--rule);
vertical-align:top}
th{font-weight:600;color:var(--dim);font-size:.75rem;letter-spacing:.06em;
white-space:nowrap}
td.n,th.n{text-align:right;font-variant-numeric:tabular-nums;
font-family:ui-monospace,"SF Mono",Menlo,monospace}
td.c,th.c{text-align:center}
th.w{white-space:normal;min-width:6rem}
.stamp{display:inline-block;padding:.05rem .45rem;border-radius:2px;
font-size:.72rem;font-weight:700;color:#fff;white-space:nowrap}
.s-buy{background:var(--buy)}.s-watch{background:var(--watch)}
.s-probe{background:var(--probe)}.s-liq{background:var(--liq)}
.s-trend{background:var(--trend)}.s-sell{background:var(--sell)}
.s-hot{background:var(--hot)}
.mk{font-weight:700;font-size:1.05rem;line-height:1.2}
.mk-ok{color:var(--buy)}.mk-ng{color:var(--liq)}.mk-unk{color:var(--warn)}
.res{display:inline-block;min-width:3.6rem;text-align:center;padding:.02rem .35rem;
border-radius:2px;font-size:.7rem;font-weight:700;white-space:nowrap}
.r-pass{background:#e6f0e9;color:var(--buy)}
.r-fail{background:#f7e9e6;color:var(--liq)}
.r-unknown{background:#fbf1dd;color:var(--warn)}
.r-skipped{background:var(--soft);color:var(--dim)}
.r-na{background:var(--soft);color:var(--dim)}
.gate{border-left:3px solid var(--liq);background:#fbf3f1;padding:.6rem .8rem;
margin:1rem 0;font-size:.85rem}
.notice{border-left:3px solid var(--warn);background:#fdf8ee;padding:.6rem .8rem;
margin:1rem 0;font-size:.85rem}
.note{border-left:3px solid var(--rule);background:var(--soft);padding:.6rem .8rem;
margin:1rem 0;font-size:.85rem}
.src{color:var(--dim);font-size:.75rem}
ul,ol{margin:.6rem 0;padding-left:1.3rem}
li{margin:.3rem 0}
code{font-family:ui-monospace,"SF Mono",Menlo,monospace;font-size:.82em;
background:var(--soft);padding:.05rem .3rem;border-radius:2px;
overflow-wrap:anywhere}
pre{background:var(--soft);padding:.8rem;overflow-x:auto;font-size:.8rem;
line-height:1.65;border-radius:2px;white-space:pre-wrap;overflow-wrap:anywhere}
a{color:inherit;text-decoration:underline;text-underline-offset:2px}
nav{font-size:.8rem;margin:0 0 2rem;color:var(--dim)}
nav a{margin-right:1rem;display:inline-block}
footer{margin-top:4rem;padding-top:1rem;border-top:1px solid var(--rule);
color:var(--dim);font-size:.75rem}
@media(max-width:640px){body{padding:1.25rem .8rem 3rem}table{font-size:.78rem}
h1{font-size:1.3rem}th,td{padding:.45rem .45rem}}
"""


# =============================================================================
# 小道具（欠測は必ず DASH。0 と欠測を混同しない）
# =============================================================================

def e(v) -> str:
    """HTML エスケープ。None は空文字。"""
    if v is None:
        return ""
    return html.escape(str(v))


def num(v, spec: str = ",.2f") -> str:
    if v is None:
        return DASH
    try:
        return format(v, spec)
    except (TypeError, ValueError):
        return str(v)


def pct(v, spec: str = ",.2f") -> str:
    if v is None:
        return DASH
    return num(v, spec) + "%"


def times(v, spec: str = ",.2f") -> str:
    if v is None:
        return DASH
    return num(v, spec) + "倍"


def man(v) -> str:
    """円 → 万円表記。"""
    if v is None:
        return DASH
    return format(v / 10000.0, ",.0f") + "万円"


def jpy(v) -> str:
    if v is None:
        return DASH
    return format(v, ",.0f") + "円"


def label_of(table: dict, key) -> str:
    if key is None:
        return DASH
    return table.get(key, str(key))


def verified(v):
    """`TO_VERIFY` 番兵値を落とす（F-08）。

    PyYAML はクォート無しの TO_VERIFY を**文字列**として読むため、
    isinstance(str) の判定では落ちない。値そのものを見て判定する。
    リストは要素ごとに落とし、全滅なら None を返す。
    """
    if v is None:
        return None
    if isinstance(v, str):
        s = v.strip()
        return None if s in ("", TO_VERIFY) else v
    if isinstance(v, (list, tuple)):
        kept = [x for x in (verified(i) for i in v) if x is not None]
        return kept or None
    return v


def shown(v, dash: str = "未検証") -> str:
    """master.yaml の値を表示文字列にする。TO_VERIFY は「未検証」。

    リストは、確定している要素を並べたうえで**落とした件数を明示する**。
    黙って消すと「ピアは1社しかない」と読めてしまう。
    """
    if isinstance(v, (list, tuple)):
        kept = [str(x) for x in v if verified(x) is not None]
        dropped = len(v) - len(kept)
        if not kept:
            return dash
        text = "、".join(kept)
        if dropped:
            count = str(dropped)
            text = text + "（他 " + count + "件は未検証）"
        return text
    r = verified(v)
    if r is None:
        return dash
    return str(r)


def slope_diff_text(diff) -> str:
    """日足MA2本の傾き差（%/日）。単位を落とすと週足の傾きと混同する。"""
    if diff is None:
        return DASH
    body = num(diff, "+.3f")
    return body + "%/日"


def slope_text(slope, direction) -> str:
    if slope is None and direction is None:
        return DASH
    s = num(slope, "+.3f")
    d = label_of(DIRECTION_LABEL, direction)
    return f"{s}%/週（{d}）"


def stamp_html(stamp: str) -> str:
    cls = STAMP_CLASS.get(stamp, "watch")
    text = e(stamp)
    return f'<span class="stamp s-{cls}">{text}</span>'


def result_html(result: str) -> str:
    cls = RESULT_CLASS.get(result, "skipped")
    text = e(RESULT_LABEL.get(result, result))
    return f'<span class="res r-{cls}">{text}</span>'


def mark_html(mark: str) -> str:
    cls = MARK_CLASS.get(mark, "mk-unk")
    text = e(mark)
    return f'<span class="mk {cls}">{text}</span>'


def scroll(table: str) -> str:
    return f'<div class="scroll">{table}</div>'


def table_html(headers: str, rows: list[str], empty: str, cols: int) -> str:
    if rows:
        body = "".join(rows)
    else:
        body = f'<tr><td colspan="{cols}">{e(empty)}</td></tr>'
    return (f"<table><thead><tr>{headers}</tr></thead>"
            f"<tbody>{body}</tbody></table>")


def kv_rows(pairs: list[tuple[str, str]]) -> str:
    out = []
    for k, v in pairs:
        out.append(f"<tr><th>{k}</th><td>{v}</td></tr>")
    return "".join(out)


def kv_table(pairs: list[tuple[str, str]]) -> str:
    rows = kv_rows(pairs)
    return f"<table><tbody>{rows}</tbody></table>"


def bullets(items: list[str]) -> str:
    out = "".join(f"<li>{i}</li>" for i in items)
    return f"<ul>{out}</ul>"


# =============================================================================
# ページの外枠
# =============================================================================

def nav_html(depth: int) -> str:
    prefix = "../" * depth
    links = []
    for href, label in NAV_ITEMS:
        links.append(f'<a href="{prefix}{href}">{label}</a>')
    joined = "".join(links)
    return f"<nav>{joined}</nav>"


def footer_html(as_of: str) -> str:
    d = e(as_of)
    return (f"<footer>集計基準日 {d}（株価データの最終営業日）。"
            "本サイトは個人の検討用であり、投資助言ではありません。"
            "判定スタンプは候補提示であって推奨ではありません。"
            "数値はすべて出所と取得日を併記しています。</footer>")


def page(title: str, body: str, as_of: str) -> str:
    t = e(title)
    foot = footer_html(as_of)
    return ('<!DOCTYPE html><html lang="ja"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            '<meta name="robots" content="noindex,nofollow">'
            f"<title>{t}</title><style>{CSS}</style></head>"
            f"<body><main>{body}{foot}</main></body></html>")


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


# =============================================================================
# 入力（ここだけがファイルを読む）
# =============================================================================

def read_csv(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def load_master() -> dict:
    p = ROOT / "data" / "master.yaml"
    return yaml.safe_load(p.read_text(encoding="utf-8")) or {}


def load_prices() -> dict[str, list[dict]]:
    """{code: [行, ...]}。行は日付昇順（同日は入力順を保つため date のみで安定ソート）。"""
    out: dict[str, list[dict]] = {}
    for r in read_csv(ROOT / "data" / "prices" / "daily.csv"):
        code = str(r.get("code") or "").strip()
        if code:
            out.setdefault(code, []).append(r)
    for code in out:
        out[code].sort(key=lambda x: str(x.get("date") or ""))
    return out


def load_margins() -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for p in sorted((ROOT / "data" / "margin").glob("*.csv")):
        rows = [r for r in read_csv(p) if str(r.get("date") or "").strip()]
        rows.sort(key=lambda x: str(x.get("date") or ""))
        out[p.stem] = rows
    return out


def load_indices() -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for p in sorted((ROOT / "data" / "indices").glob("*.csv")):
        rows = [r for r in read_csv(p) if str(r.get("date") or "").strip()]
        rows.sort(key=lambda x: str(x.get("date") or ""))
        out[p.stem] = rows
    return out


def load_predictions() -> list[dict]:
    out: list[dict] = []
    for p in sorted((ROOT / "predictions").glob("*.yaml")):
        doc = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        for pred in doc.get("predictions", []) or []:
            row = dict(pred)
            row["_file"] = p.name
            row["_week"] = doc.get("week")
            out.append(row)
    out.sort(key=lambda x: str(x.get("id") or ""))
    return out


def load_summary() -> dict:
    p = ROOT / "scoring" / "summary.yaml"
    if not p.exists():
        return {}
    return yaml.safe_load(p.read_text(encoding="utf-8")) or {}


def load_thesis(code: str) -> str | None:
    p = ROOT / "theses" / f"{code}.md"
    if not p.exists():
        return None
    return p.read_text(encoding="utf-8")


def data_as_of(prices: dict[str, list[dict]]) -> str:
    """集計基準日 = 株価データの最終営業日。**壁時計を使わない**（F4-1・D8）。"""
    dates = []
    for rows in prices.values():
        for r in rows:
            d = str(r.get("date") or "").strip()
            if d:
                dates.append(d)
    return max(dates) if dates else "未取得"


# =============================================================================
# 欠測の集約（台帳冒頭に出す・D7）
# =============================================================================

def price_gap_notes(master: dict, prices: dict[str, list[dict]],
                    as_of: str) -> list[str]:
    """株価データ側の欠測。最終足の status と、基準日の行の有無を見る。"""
    notes: list[str] = []
    for s in sorted(master.get("stocks", []), key=lambda x: str(x["code"])):
        code = str(s["code"])
        head = f'{code} {s.get("name", "")}'
        rows = prices.get(code) or []
        if not rows:
            notes.append(f"{head}: 株価データが1行も無い")
            continue
        last = rows[-1]
        status = str(last.get("status") or "").strip()
        date = str(last.get("date") or "").strip()
        if status != "OK":
            notes.append(f"{head}: 最終足 {date} の状態が {status}"
                         "（2ソース照合が成立せず終値が採用値になっていない）")
        elif date != as_of:
            notes.append(f"{head}: 最終足が {date} で集計基準日 {as_of} より古い")
    return notes


def unknown_notes(verdicts) -> list[str]:
    """判定で未計算だった指標。**未計算は通過ではない**（F5-4）。"""
    notes: list[str] = []
    for v in verdicts:
        if v.unknowns:
            joined = "／".join(v.unknowns)
            notes.append(f"{v.code} {v.name}: 未計算 — {joined}")
    return notes


def master_gap_notes(master: dict) -> list[str]:
    """master.yaml の TO_VERIFY 残数（F-08 で表示側は落とすが、件数は明示する）。

    **キーごと無い場合も未検証として数える**。`TO_VERIFY` より情報が少ないのに
    黙っていると「その銘柄は埋まっている」と読めてしまう。
    """
    notes: list[str] = []
    for s in sorted(master.get("stocks", []), key=lambda x: str(x["code"])):
        missing = []
        for key in ("sector", "fiscal_year_end", "ir_url", "peers", "next_earnings"):
            if key not in s:
                missing.append(f"{key}（項目ごと無い）")
            elif verified(s.get(key)) is None:
                missing.append(key)
        if missing:
            joined = "、".join(missing)
            head = f'{s["code"]} {s.get("name", "")}'
            notes.append(f"{head}: master.yaml の未検証項目 — {joined}")
    return notes


def gaps_html(notes: list[str]) -> str:
    if not notes:
        return ('<div class="note"><strong>欠測なし</strong>'
                "：判定に必要な指標はすべて算出できている。</div>")
    items = "".join(f"<li>{e(n)}</li>" for n in notes)
    return ('<div class="notice"><strong>欠測・未検証あり</strong>'
            "（推定値で埋めていない。未計算の指標があるゲートは通過ではなく"
            "「調査」で止まる）<ul>" + items + "</ul></div>")


# =============================================================================
# 銘柄一覧（判定スタンプ）
# =============================================================================

def stock_label_html(stock: dict, depth: int) -> str:
    """銘柄名（詳細ページへのリンク）＋ コード／市場／業種。"""
    prefix = "../" * depth
    code = e(stock.get("code"))
    name = e(stock.get("name"))
    market = e(shown(stock.get("market")))
    sector = e(shown(stock.get("sector")))
    return (f'<a href="{prefix}stock/{code}.html">{name}</a>'
            f'<br><span class="src">{code}／{market}／{sector}</span>')


def verdict_row(v, stock: dict) -> str:
    label = stock_label_html(stock, 0)
    stamp = stamp_html(v.stamp)
    close = num(v.metrics.get("close"), ",.1f")
    screen_price = num(stock.get("screen_price"), ",.1f")
    stage = e(v.stage_label)
    reason = e(v.reason)
    return (f"<tr><td>{stamp}</td><td>{label}</td>"
            f'<td class="n">{close}</td><td class="n">{screen_price}</td>'
            f"<td>{stage}<br><span class=\"src\">{reason}</span></td></tr>")


def verdict_table(verdicts, by_code: dict) -> str:
    rows = []
    for v in verdicts:
        rows.append(verdict_row(v, by_code.get(v.code, {})))
    headers = ('<th>判定</th><th>銘柄</th><th class="n">終値</th>'
               '<th class="n">抽出時株価</th><th class="w">確定段階と根拠</th>')
    return scroll(table_html(headers, rows, "銘柄が登録されていない", 5))


# =============================================================================
# 指標の実値
# =============================================================================

def margin_text(m: dict) -> str:
    """信用倍率の表示。**値が無い＝未計算とは限らない**。

    残高ゼロ・買い一辺倒・制度信用が買建のみ、のいずれも倍率は定義できないが
    「過熱かどうか」の判定としては決着している。「—」だけを出すと、
    台帳の「未計算の指標」欄と食い違って見える。
    """
    if m.get("margin_ratio") is not None:
        return times(m.get("margin_ratio"))
    if m.get("margin_state") in (J.PASS, J.FAIL, J.NA):
        return f'{DASH}<br><span class="src">定義不能</span>'
    return DASH


def metric_row(v) -> str:
    m = v.metrics
    code = e(v.code)
    name = e(v.name)
    close = num(m.get("close"), ",.1f")
    rsi = num(m.get("rsi14"), ",.1f")
    dev = pct(m.get("ma25_deviation_pct"))
    cloud = label_of(CLOUD_LABEL, m.get("ichimoku_position"))
    margin = margin_text(m)
    vol = times(m.get("volume_ratio_3m"))
    slope13 = slope_text(m.get("weekly_ma_mid_slope_pct"),
                         m.get("weekly_ma_mid_direction"))
    slope26 = slope_text(m.get("weekly_ma_long_slope_pct"),
                         m.get("weekly_ma_long_direction"))
    turnover = man(m.get("avg_turnover_20d"))
    median = man(m.get("median_turnover_20d"))
    return (f'<tr><td>{name}<br><span class="src">{code}</span></td>'
            f'<td class="n">{close}</td><td class="n">{rsi}</td>'
            f'<td class="n">{dev}</td><td>{cloud}</td>'
            f'<td class="n">{margin}</td><td class="n">{vol}</td>'
            f"<td>{slope13}</td><td>{slope26}</td>"
            f'<td class="n">{turnover}<br><span class="src">中央値 {median}</span></td>'
            "</tr>")


def metric_table(verdicts) -> str:
    rows = [metric_row(v) for v in verdicts]
    headers = ('<th>銘柄</th><th class="n">終値</th><th class="n">RSI(14)</th>'
               '<th class="n">25日乖離率</th><th>雲の位置</th>'
               '<th class="n">信用倍率</th><th class="n">出来高3か月比<br>'
               '<span class="src">自社定義</span></th>'
               '<th class="w">週足13週MAの傾き</th>'
               '<th class="w">週足26週MAの傾き</th>'
               '<th class="n">20日平均売買代金</th>')
    return scroll(table_html(headers, rows, "データなし", 10))


# =============================================================================
# スクリーニング5条件（○×）
# =============================================================================

SCREEN_UNIT = {
    "revenue_yoy_pct": "%",
    "ordinary_income_yoy_pct": "%",
    "ma25_deviation_pct": "%",
    "volume_ratio_3m": "倍",
}


def screen_value_text(check) -> str:
    value = check.value
    if value is None:
        return DASH
    if isinstance(value, (int, float)):
        unit = SCREEN_UNIT.get(check.key, "")
        body = num(value, ",.2f")
        return body + unit
    if value in CLOUD_CROSS_LABEL:
        return CLOUD_CROSS_LABEL[value]
    return label_of(CLOUD_LABEL, value)


def screen_cell(check) -> str:
    mark = mark_html(check.mark)
    value = e(screen_value_text(check))
    return f'<td class="c">{mark}<br><span class="src">{value}</span></td>'


def screen_row(v) -> str:
    code = e(v.code)
    name = e(v.name)
    cells = "".join(screen_cell(c) for c in v.screen)
    return (f'<tr><td>{name}<br><span class="src">{code}</span></td>'
            f"{cells}</tr>")


def screen_headers(verdicts) -> str:
    if not verdicts:
        return "<th>銘柄</th>"
    cells = []
    for c in verdicts[0].screen:
        # 閾値の文言は judge の ScreenCheck.threshold_label が正（SSoT）。
        # 数値でない条件（「雲を上抜け」等）や定義未確認の項目もそこに入っている。
        th_text = c.threshold_label
        if not th_text:
            threshold = c.threshold
            unit = SCREEN_UNIT.get(c.key, "")
            th_text = (num(threshold, ",.0f") + unit + " 以上"
                       if isinstance(threshold, (int, float)) else str(threshold))
        label = e(c.label)
        th_text = e(th_text)
        cells.append(f'<th class="c w">{label}<br>'
                     f'<span class="src">{th_text}</span></th>')
    joined = "".join(cells)
    return f"<th>銘柄</th>{joined}"


def screen_table(verdicts) -> str:
    rows = [screen_row(v) for v in verdicts]
    headers = screen_headers(verdicts)
    cols = 1 + (len(verdicts[0].screen) if verdicts else 0)
    return scroll(table_html(headers, rows, "データなし", cols))


# =============================================================================
# 保有管理（F13）
# =============================================================================

def stop_loss_text(h) -> str:
    """逆指値ラインの表示。**抵触判定は終値ではなく安値で行う**（実際の約定はザラ場）。"""
    price = jpy(h.stop_loss_price)
    if h.stop_loss_hit is None:
        return f"{price}（抵触判定なし）"
    if not h.stop_loss_hit:
        return f"{price}（安値でも未抵触）"
    if h.stop_loss_intraday_only:
        return f"{price}（安値で抵触・終値は戻した）"
    return f"{price}（抵触）"


def reached_text(h) -> str:
    if h.reached is None:
        return "判定不能"
    return "到達" if h.reached else "未到達"


def holding_pairs(v) -> list[tuple[str, str]]:
    h = v.holding
    close = jpy(v.metrics.get("close"))
    pairs = [
        ("判定", stamp_html(v.stamp)),
        ("買値", jpy(h.buy_price)),
        ("買付日", e(h.buy_date) or DASH),
        ("数量", num(h.shares, ",.0f")),
        ("現値", close),
        ("逆指値ライン（買値-10%）", e(stop_loss_text(h))),
        ("経過月数", num(h.elapsed_months, ",.0f")),
        ("6か月2倍ラインの基準", pct(h.target_pct, "+.0f")),
        ("買値からの騰落率", pct(h.return_pct, "+.2f")),
        ("基準への到達率", num(h.achievement_ratio, ".2f")),
        ("到達判定", e(reached_text(h))),
        ("日足5/25クロス", e(label_of(CROSS_LABEL, h.cross_kind))),
        ("鉄則にもとづく扱い", e(h.action or DASH)),
    ]
    if h.note:
        pairs.append(("補足", e(h.note)))
    if h.status_note:
        pairs.append(("holding の記入エラー", e(h.status_note)))
    return pairs


def holding_block(v) -> str:
    name = e(v.name)
    code = e(v.code)
    body = kv_table(holding_pairs(v))
    return f"<h3>{name}（{code}）</h3>{body}"


def holdings_section(verdicts) -> str:
    held = [v for v in verdicts if v.holding.status == "holding"]
    broken = [v for v in verdicts if v.holding.status == J.HOLDING_UNKNOWN]
    if broken:
        items = "".join(f"<li>{e(v.code)} {e(v.name)}: "
                        f"{e(v.holding.status_note or '')}</li>" for v in broken)
        head = ('<div class="notice"><strong>holding の記入を確認すること</strong>'
                "：語彙外の値、または none なのに買値が入っている銘柄がある。"
                "保有状態を確定できないので「調査」で止めている。"
                f"<ul>{items}</ul></div>")
    else:
        head = ""
    if not held:
        return head + ('<div class="note">現在、保有中の銘柄はない'
                       "（master.yaml の holding.status がすべて none）。"
                       "保有を登録すると、逆指値ライン（買値-10%）と"
                       "6か月2倍ラインへの到達率がここに出る。</div>")
    blocks = "".join(holding_block(v) for v in held)
    return head + ('<div class="note">保有は毎週フラットに再評価する'
                   "（前週の判断を引き継がない）。売りシグナル（雲の下・逆指値抵触・"
                   "基準到達×デッドクロス気味）は、流動性・トレンド・過熱より"
                   "<strong>先に</strong>評価する。"
                   "逆指値の抵触は終値ではなく<strong>安値</strong>で判定する"
                   "（実際の逆指値注文はザラ場で約定するため）。"
                   "</div>" + blocks)


# =============================================================================
# 判定スタンプの凡例
# =============================================================================

def stamp_legend_table() -> str:
    rows = []
    for stamp in J.STAMPS:
        badge = stamp_html(stamp)
        meaning = e(STAMP_MEANING.get(stamp, ""))
        rows.append(f"<tr><td>{badge}</td><td>{meaning}</td></tr>")
    headers = '<th>判定</th><th class="w">意味</th>'
    return table_html(headers, rows, "", 2)


# =============================================================================
# index.html
# =============================================================================

def gate_box(gate: dict) -> str:
    limit = man(gate.get("min_avg_turnover_20d_jpy"))
    on_fail = e(gate.get("on_fail") or "")
    rationale = e(gate.get("rationale") or "")
    return ('<div class="gate"><strong>流動性ゲート</strong>：'
            f"20日平均売買代金が {limit} 未満の銘柄は、他の指標を一切見ずに"
            f"「{on_fail}」で確定する。{rationale}。"
            "売買代金が算出できない場合は通過ではなく「調査」で止める。</div>")


def screen_source_note(scr: dict) -> str:
    captured = e(scr.get("captured_at") or "不明")
    source = e(scr.get("source") or "不明")
    return ('<p class="src">抽出時株価は ' + captured + " 時点（" + source
            + "）。15分ディレイのザラ場値であり終値ではない。"
              "終値列は日足の生終値を2ソース照合した採用値で、照合が成立しない日は空になる。</p>")


def build_index(master: dict, prices: dict, verdicts, as_of: str) -> str:
    by_code = {str(s.get("code")): s for s in master.get("stocks", [])}
    scr = master.get("screening") or {}
    gate = master.get("liquidity_gate") or {}
    notes = (price_gap_notes(master, prices, as_of)
             + unknown_notes(verdicts)
             + master_gap_notes(master))
    screening_name = e(scr.get("name") or "")

    parts = [
        f"<h1>{SITE_TITLE}</h1>",
        '<p class="lede">楽天証券スクリーニング「' + screening_name + "」の通過銘柄を、"
        "鉄則の順序（流動性 → トレンド → 雲 → 過熱 → ファンダ）で毎週判定する。"
        "判定は候補提示までで、売買の実行判断は含まない。</p>",
        nav_html(0),
        gaps_html(notes),
        cautions_html(verdicts),
        gate_box(gate),
        "<h2>銘柄一覧（証券コード順）</h2>",
        verdict_table(verdicts, by_code),
        screen_source_note(scr),
        "<h2>指標の実値</h2>",
        metric_table(verdicts),
        '<p class="src">すべて日足から決定論的に計算した値（計算式は'
        '<a href="formula.html">算出ロジック</a>）。'
        "「—」は算出できなかったことを表し、0 や直近値で代替していない。"
        "信用倍率のみ外部取得（週次公表）。</p>",
        "<h2>スクリーニング5条件の充足状況</h2>",
        screen_table(verdicts),
        '<p class="src">○＝条件を満たす／×＝満たさない／'
        "?＝算出できない、または定義が未確認で○×を出せない。"
        "?を○にも×にも丸めない。この表は楽天証券スクリーニング「成長株0606」の条件を"
        "本プロジェクトの定義で再計算したもので、判定ゲートとは別物"
        "（通過しても「買」にはならない）。"
        "出来高比と一目均衡表は元のスクリーナーを再現できていない"
        '（<a href="formula.html">算出ロジック</a>に検証結果を記載）。</p>',
        "<h2>保有銘柄</h2>",
        holdings_section(verdicts),
        "<h2>判定スタンプ</h2>",
        stamp_legend_table(),
        '<p class="src">判定の順序・閾値・根拠の全文は'
        '<a href="formula.html">算出ロジック</a>に開示している。'
        '専門用語なしの解説は<a href="guide.html">読み方</a>を参照。</p>',
    ]
    return "".join(parts)


# =============================================================================
# data.html
# =============================================================================

def price_row(r: dict) -> str:
    date = e(r.get("date"))
    code = e(r.get("code"))
    o = e(r.get("open") or DASH)
    h = e(r.get("high") or DASH)
    lo = e(r.get("low") or DASH)
    c = e(r.get("close") or DASH)
    vol = e(r.get("volume") or DASH)
    status = e(r.get("status"))
    src1 = e(r.get("source_primary") or DASH)
    src2 = e(r.get("source_secondary") or DASH)
    fetched = e(str(r.get("fetched_at") or "")[:19])
    return (f"<tr><td>{date}</td><td>{code}</td>"
            f'<td class="n">{o}</td><td class="n">{h}</td><td class="n">{lo}</td>'
            f'<td class="n">{c}</td><td class="n">{vol}</td><td>{status}</td>'
            f'<td class="src">{src1}／{src2}</td>'
            f'<td class="src">{fetched}</td></tr>')


def price_table(prices: dict, tail: int) -> str:
    rows = []
    for code in sorted(prices):
        for r in prices[code][-tail:]:
            rows.append(price_row(r))
    headers = ('<th>日付</th><th>コード</th><th class="n">始値</th>'
               '<th class="n">高値</th><th class="n">安値</th>'
               '<th class="n">終値</th><th class="n">出来高</th><th>状態</th>'
               "<th>照合元</th><th>取得日時</th>")
    return scroll(table_html(headers, rows, "データ未取得", 10))


def margin_row(r: dict) -> str:
    date = e(r.get("date"))
    code = e(r.get("code"))
    short_bal = e(r.get("short_balance") or DASH)
    long_bal = e(r.get("long_balance") or DASH)
    ratio = e(r.get("ratio") or DASH)
    unit = e(r.get("unit") or DASH)
    status = e(r.get("status"))
    fetched = e(str(r.get("fetched_at") or "")[:19])
    return (f"<tr><td>{date}</td><td>{code}</td>"
            f'<td class="n">{short_bal}</td><td class="n">{long_bal}</td>'
            f'<td class="n">{ratio}</td><td>{unit}</td><td>{status}</td>'
            f'<td class="src">{fetched}</td></tr>')


def margin_table(margins: dict) -> str:
    rows = []
    for code in sorted(margins):
        for r in margins[code]:
            rows.append(margin_row(r))
    headers = ('<th>日付</th><th>コード</th><th class="n">売り残</th>'
               '<th class="n">買い残</th><th class="n">倍率</th><th>単位</th>'
               "<th>状態</th><th>取得日時</th>")
    return scroll(table_html(headers, rows, "データ未取得", 8))


def index_row(name: str, r: dict) -> str:
    date = e(r.get("date"))
    label = e(name)
    close = e(r.get("close") or DASH)
    primary = e(r.get("value_primary") or DASH)
    status = e(r.get("status"))
    src1 = e(r.get("source_primary") or DASH)
    src2 = e(r.get("source_secondary") or DASH)
    return (f"<tr><td>{date}</td><td>{label}</td>"
            f'<td class="n">{close}</td><td class="n">{primary}</td>'
            f'<td>{status}</td><td class="src">{src1}／{src2}</td></tr>')


def index_table(indices: dict, tail: int) -> str:
    rows = []
    for name in sorted(indices):
        for r in indices[name][-tail:]:
            rows.append(index_row(name, r))
    headers = ('<th>日付</th><th>指数</th><th class="n">採用終値</th>'
               '<th class="n">第1ソース値</th><th>状態</th><th>照合元</th>')
    return scroll(table_html(headers, rows, "データ未取得", 6))


def status_meaning_table(pairs: list) -> str:
    rows = []
    for key, meaning in pairs:
        k = e(key)
        v = e(meaning)
        rows.append(f"<tr><th>{k}</th><td>{v}</td></tr>")
    body = "".join(rows)
    return f"<table><tbody>{body}</tbody></table>"


def build_data(prices: dict, margins: dict, indices: dict, quality) -> str:
    parts = [
        "<h1>データ台帳</h1>",
        '<p class="lede">全数値の取得元・取得日時・二重照合結果。'
        "取得に失敗した項目は推定値で埋めず、欠測のまま残している。"
        "過去行は書き換えない（append-only）。</p>",
        nav_html(0),
        "<h2>データ品質検査の結果</h2>",
        "<p>ビルドを止めるのは FAIL のみ。WARN は止めずに、"
        "ここに全件出す（黙って流さない）。</p>",
        quality_html(quality),
        "<h2>株価（銘柄ごとに直近20営業日）</h2>",
        price_table(prices, 20),
        '<p class="src">終値列は2ソースの生終値が一致したときだけ入る採用値。'
        "一致しなかった日は空にし、各ソースの値は value_primary / value_secondary に残す。"
        "調整後終値は混在させない。</p>",
        "<h2>株価の状態の意味</h2>",
        status_meaning_table(STATUS_MEANING),
        "<h2>信用残高（週次公表・全期間）</h2>",
        margin_table(margins),
        "<h2>信用残高の状態の意味</h2>",
        status_meaning_table(MARGIN_STATUS_MEANING),
        '<p class="src">単位はページ側の見出しから読み取って記録している'
        "（コード側で単位を仮定しない）。</p>",
        "<h2>指数（直近10営業日）</h2>",
        index_table(indices, 10),
        '<p class="src">東証グロース市場250指数は第2ソースが未確定のため'
        "常に SINGLE_SOURCE になり、採用終値は空になる。"
        "値は第1ソース値の列にのみ入る。「採用終値が空＝データが無い」ではない。</p>",
        "<h2>取得の作法</h2>",
        bullets([
            "取得元は data/sources.yaml のチェーンのみ。自律的に追加しない",
            "2ソース一致で採用。1件のみは SINGLE_SOURCE、不一致は MISMATCH として記録する",
            "取得間隔 3 秒・User-Agent を明示する",
            "数値は必ず出所と取得日時を伴う。伴わない値は記録しない",
        ]),
    ]
    return "".join(parts)


# =============================================================================
# scoring.html
# =============================================================================

def prediction_row(p: dict) -> str:
    pid = e(p.get("id"))
    code = e(p.get("code"))
    metric = e(p.get("metric"))
    operator = e(p.get("operator"))
    reference = e(p.get("reference"))
    resolve_by = e(p.get("resolve_by"))
    confidence = num(p.get("confidence"), ".2f")
    status = e(p.get("status"))
    result = e(p.get("result") or DASH)
    return (f"<tr><td>{pid}</td><td>{code}</td><td>{metric}</td>"
            f'<td class="n">{operator} {reference}</td><td>{resolve_by}</td>'
            f'<td class="n">{confidence}</td><td>{status}</td>'
            f"<td>{result}</td></tr>")


def prediction_table(preds: list) -> str:
    rows = [prediction_row(p) for p in preds]
    headers = ("<th>ID</th><th>コード</th><th>metric</th><th>条件</th>"
               "<th>期限</th><th>確信度</th><th>状態</th><th>結果</th>")
    return scroll(table_html(headers, rows, "予測が未登録", 8))


def summary_table(s: dict) -> str:
    pairs = [
        ("集計日", e(s.get("as_of") or DASH)),
        ("登録数", num(s.get("total"), ",.0f")),
        # 期限前（open）を出さないと「登録3・的中0・外れ0・判定不能0」が並び、
        # 全部外したように読める。summary.yaml には最初から入っている。
        ("期限前（未採点）", num(s.get("open"), ",.0f")),
        ("的中", num(s.get("hit"), ",.0f")),
        ("外れ", num(s.get("miss"), ",.0f")),
        ("判定不能", num(s.get("unresolvable"), ",.0f")),
        ("的中率", num(s.get("hit_rate"), ".3f")),
        ("ブライアススコア", num(s.get("brier"), ".4f")),
    ]
    return kv_table(pairs)


def metric_catalog_table() -> str:
    """予測に書ける metric の一覧（score.catalog_rows() が正）。"""
    rows = []
    for m in S.catalog_rows():
        rows.append(f'<tr><td><code>{e(m["name"])}</code></td>'
                    f'<td>{e(m["label"])}</td><td>{e(m["source"])}</td>'
                    f'<td>{e(m["unit"] or DASH)}</td><td>{e(m["note"])}</td></tr>')
    headers = ('<th class="w">metric</th><th class="w">内容</th><th>取得元</th>'
               '<th>単位</th><th class="w">注記</th>')
    return scroll(table_html(headers, rows, "", 5))


def build_scoring(summary: dict, preds: list) -> str:
    parts = [
        "<h1>予測採点</h1>",
        '<p class="lede">毎週、翌週以降に機械的に検証できる予測を先に登録し、'
        "コードだけで採点する。当たったかどうかを人間や LLM が判定しない。"
        "この累積成績が、システムを信用してよいかを測る唯一の指標。</p>",
        nav_html(0),
        "<h2>累積成績</h2>",
        summary_table(summary),
        '<div class="notice">年間の登録数は3銘柄×週3件で約150件。'
        "同一セクター内では予測が相関するため実効サンプルはさらに小さく、"
        "1年程度の的中率で有効性は判定できない。"
        "確信度つきのブライアススコアを主指標とする（低いほど良い）。</div>",
        "<h2>登録済みの予測</h2>",
        prediction_table(preds),
        '<p class="src">予測は削除しない。状態遷移は open → resolved / '
        "open → expired のみ。operator と reference が機械解決できない予測は登録しない。</p>",
        "<h2>予測に書ける metric</h2>",
        "<p>この一覧に無い名前は決算 CSV の実額として解決を試みる。"
        "比率（前年同期比・進捗率・構成比）はすべてコードが計算するので、"
        "決算 CSV に比率を書いてはいけない。</p>",
        metric_catalog_table(),
        "<h2>手法の未検証点</h2>",
        "<p>決算由来の metric（売上高・経常利益・1Q進捗率）は、"
        "決算が出るまで解決できない。株価由来の metric（20日平均売買代金など）は"
        "毎週自動で採点できる。現状は決算パイプラインが未整備のため、"
        "決算由来の予測は期限を過ぎると「判定不能」になる。"
        "判定不能が多いことは手法の成績ではなく、予測の書き方の問題として扱う。</p>",
        "<p>不動産査定と異なり、成約実績に相当する正解データが存在しない。"
        "将来の実現リターンとの相関で事後較正する枠のみ用意している。</p>",
    ]
    return "".join(parts)


# =============================================================================
# stock/{code}.html
# =============================================================================

def check_row(c) -> str:
    label = e(c.label)
    badge = result_html(c.result)
    detail = e(c.detail)
    return f"<tr><td>{label}</td><td>{badge}</td><td>{detail}</td></tr>"


def check_table(v) -> str:
    rows = [check_row(c) for c in v.checks]
    headers = '<th class="w">段階</th><th>結果</th><th class="w">内容</th>'
    return scroll(table_html(headers, rows, "評価なし", 3))


def screen_detail_row(c) -> str:
    label = e(c.label)
    mark = mark_html(c.mark)
    value = e(screen_value_text(c))
    detail = e(c.detail)
    return (f"<tr><td>{label}</td><td>{mark}</td>"
            f'<td class="n">{value}</td><td>{detail}</td></tr>')


def screen_detail_table(v) -> str:
    rows = [screen_detail_row(c) for c in v.screen]
    headers = '<th class="w">条件</th><th>判定</th><th class="n">実値</th><th class="w">内容</th>'
    return scroll(table_html(headers, rows, "評価なし", 4))


def metric_pairs(v) -> list[tuple[str, str]]:
    m = v.metrics
    cloud_cross = m.get("ichimoku_recent_cross")
    cross_date = m.get("ichimoku_recent_cross_date")
    if cloud_cross is None:
        cross_text = "直近に上抜け・下抜けなし"
    else:
        kind = label_of(CLOUD_CROSS_LABEL, cloud_cross)
        when = e(cross_date)
        cross_text = f"{when} に{kind}"
    return [
        ("判定基準日", e(m.get("as_of"))),
        ("日足の本数", num(m.get("bars"), ",.0f")),
        ("週足の本数", num(m.get("weekly_bars"), ",.0f")
         + ("（うちトレンド判定に使用 "
            + num(m.get("weekly_bars_used"), ",.0f") + "本。最終週は未了のため除外）"
            if m.get("weekly_last_incomplete") else "")),
        ("終値", jpy(m.get("close"))),
        ("最終足の安値", jpy(m.get("low"))),
        ("20日平均売買代金", man(m.get("avg_turnover_20d"))),
        ("20日中央値売買代金", man(m.get("median_turnover_20d"))),
        ("週足13週MAの傾き", slope_text(m.get("weekly_ma_mid_slope_pct"),
                                        m.get("weekly_ma_mid_direction"))),
        ("週足26週MAの傾き", slope_text(m.get("weekly_ma_long_slope_pct"),
                                        m.get("weekly_ma_long_direction"))),
        ("雲に対する位置", label_of(CLOUD_LABEL, m.get("ichimoku_position"))),
        ("前日の雲に対する位置", label_of(CLOUD_LABEL, m.get("ichimoku_prev_position"))),
        ("雲の上端", jpy(m.get("ichimoku_cloud_top"))),
        ("雲の下端", jpy(m.get("ichimoku_cloud_bottom"))),
        ("直近の雲の抜け", e(cross_text)),
        ("RSI(14)", num(m.get("rsi14"), ",.2f")),
        ("25日移動平均", jpy(m.get("ma25"))),
        ("25日移動平均乖離率", pct(m.get("ma25_deviation_pct"))),
        ("3か月前出来高増加率（自社定義）", times(m.get("volume_ratio_3m"))),
        ("信用倍率", times(m.get("margin_ratio"))),
        ("信用倍率の扱い", e(m.get("margin_detail") or DASH)),
        ("信用残の状態", e(m.get("margin_status") or DASH)),
        ("信用残の日付", e(m.get("margin_date") or DASH)),
        ("日足5/25クロス", e(label_of(CROSS_LABEL, m.get("daily_cross_kind")))),
        ("日足5/25の傾き差", slope_diff_text(m.get("daily_cross_slope_diff_pct"))),
        ("決算の開示日", e(m.get("kpi_disclosure_date") or DASH)),
        ("売上高 前年同四半期比", pct(m.get("revenue_yoy_pct"), "+.2f")),
        ("経常利益 前年同四半期比", pct(m.get("ordinary_income_yoy_pct"), "+.2f")),
        ("1Q進捗率", pct(m.get("q1_progress_pct"))
         + (f"（{e(str(m.get('q1_progress_date')))} の1Q開示）"
            if m.get("q1_progress_date") else "")),
        ("1Q進捗率の状態", e(KPI_STATUS_LABEL.get(str(m.get("q1_progress_status")),
                                                  DASH))),
    ]


SKIP_MASTER_KEYS = ("holding", "code", "name")


def master_info_table(stock: dict) -> str:
    pairs = []
    for key in sorted(stock):
        if key in SKIP_MASTER_KEYS:
            continue
        label = e(key)
        value = e(shown(stock.get(key)))
        pairs.append((label, value))
    return kv_table(pairs)


def thesis_html(code: str) -> str:
    text = load_thesis(code)
    if text is None:
        return ('<div class="note">テーゼ未作成。'
                "テーゼと反証条件が無い銘柄は、値動きの理由を検証できない。</div>")
    escaped = e(text)
    return f"<pre>{escaped}</pre>"


def unknown_block(v) -> str:
    if not v.unknowns:
        return ""
    items = "".join(f"<li>{e(u)}</li>" for u in v.unknowns)
    return ('<div class="notice"><strong>未計算の指標</strong>'
            "（0 や直近値で代替していない。これらに依存するゲートは通過扱いにしない）"
            "<ul>" + items + "</ul></div>")


def verdict_summary_table(v) -> str:
    pairs = [
        ("判定", stamp_html(v.stamp)),
        ("判定基準日", e(v.as_of)),
        ("確定した段階", e(v.stage_label)),
        ("確定の理由", e(v.reason)),
    ]
    return kv_table(pairs)


def build_stock(stock: dict, v, rows: list[dict]) -> str:
    code = str(stock.get("code"))
    name = e(stock.get("name"))
    code_e = e(code)
    parts = [
        f"<h1>{name}（{code_e}）</h1>",
        '<p class="lede">判定に使ったすべての指標と、各ゲートの評価結果を開示する。'
        "評価手法は業種に応じて切り替えている。</p>",
        nav_html(1),
        verdict_summary_table(v),
        unknown_block(v),
        cautions_html([v]),
        "<h2>ゲートの評価（上から順に評価し、該当した時点で確定する）</h2>",
        check_table(v),
        '<p class="src">「未計算」は条件を満たしたことを意味しない。'
        "未計算のゲートに当たった時点で「調査」で止める（買い側の原則）。"
        "保有中の売りシグナル（HS）だけは例外で、"
        "上流のゲートが該当していても未計算でも先に評価する"
        "（売りを上流に隠さない）。"
        '判定の順序と閾値は<a href="../formula.html">算出ロジック</a>を参照。</p>',
        "<h2>スクリーニング5条件</h2>",
        screen_detail_table(v),
        "<h2>指標の実値</h2>",
        scroll(kv_table(metric_pairs(v))),
    ]
    if v.holding.status == "holding":
        parts.append("<h2>保有管理</h2>")
        parts.append(kv_table(holding_pairs(v)))
    parts += [
        "<h2>基本情報</h2>",
        scroll(master_info_table(stock)),
        '<p class="src">「未検証」は master.yaml に TO_VERIFY が入っている項目。'
        "推測で埋めていない。</p>",
        "<h2>テーゼと反証条件</h2>",
        thesis_html(code),
        "<h2>直近20営業日の株価</h2>",
        price_table({code: rows}, 20),
    ]
    return "".join(parts)


# =============================================================================
# formula.html（算出ロジックの全文開示・F-07）
# =============================================================================

STAGE_STAMPS = {
    "holding_sell": (J.STAMP_SELL,),
    "holding_stop_loss": (J.STAMP_PROBE,),
    "liquidity": (J.STAMP_LIQUIDITY,),
    "trend": (J.STAMP_TREND,),
    "cloud": (J.STAMP_SELL,),
    "overheat": (J.STAMP_OVERHEAT,),
    "holding_target": (J.STAMP_WATCH,),
    "fundamentals": (J.STAMP_PROBE,),
    "all_clear": (J.STAMP_BUY,),
}

STAGE_RATIONALE = {
    "holding_sell": "鉄則「雲を下に抜けたらすぐ売る（損切設定しておく）」"
                    "「基準に到達かつデッドクロス気味＝売り」は無条件の売り指示。"
                    "流動性・トレンド・過熱の下に置くと、上流が該当したり未計算に"
                    "なったりしただけで売りが評価されなくなる。"
                    "「調査で止める」は買い側の原則であって、売り側に適用しない",
    "holding_stop_loss": "鉄則「上手くなるまでは買値の-10%で逆指値を必ずかける」。"
                         "抵触していれば上の HS で確定するので、ここは"
                         "「そもそも算出できるか」だけを見る",
    "liquidity": "時価総額十数億規模では、判定が正しくても建てられず降りられない。"
                 "だから流動性を判定の最上位に置く",
    "trend": "鉄則の第一条「週足中期移動平均線を見てトレンドが下がっていたら"
             "決して買いで入らない」",
    "cloud": "鉄則「雲を下に抜けたらすぐ売る（損切設定しておく）」",
    "overheat": "鉄則「RSIが8割超えになっていないか」"
                "「日移動平均線乖離率で…7〜8%超えてたら怪しい」"
                "「信用倍率が極端（5倍超え）になっていないか」",
    "holding_target": "鉄則「基準に到達かつデッドクロス気味＝売り／"
                      "基準に到達かつゴールデンクロス気味＝買い増しオーケー／"
                      "基準に到達していない＝今まで通り様子見・損切り確認」",
    "fundamentals": "スクリーニング基準（売上高・経常利益の前年同四半期比30%以上）と、"
                    "鉄則「1Q等の進捗率30%超えているか（2Qでの上方修正を狙える）」",
    "all_clear": "候補提示であって推奨ではない。売買の実行判断は人間が行う",
}


def stage_conditions(cfg: dict, gate: dict) -> dict:
    """各段階の「該当条件」を、実際に使っている閾値そのもので書き出す。"""
    limit = man(gate.get("min_avg_turnover_20d_jpy"))
    weekly_n = "／".join(f"{int(p)}週" for p in cfg["weekly_trend_periods"])
    tol = num(cfg["trend_negative_tolerance_pct_per_week"], ",.2f")
    rsi_th = num(cfg["rsi_overheat"], ",.0f")
    dev_th = num(cfg["ma_deviation_overheat_pct"], ",.0f")
    margin_th = num(cfg["margin_ratio_overheat"], ",.0f")
    rev_th = num(cfg["revenue_yoy_min_pct"], ",.0f")
    ord_th = num(cfg["ordinary_income_yoy_min_pct"], ",.0f")
    q1_th = num(cfg["q1_progress_min_pct"], ",.0f")
    streak = num(cfg["fundamentals_min_streak"], ",.0f")
    return {
        "holding_sell": "保有中に次のいずれか — 安値 ≤ 逆指値ライン ／ 終値が雲の下 ／ "
                        "6か月2倍ライン到達かつ日足5/25がデッドクロス気味",
        "holding_stop_loss": "逆指値ライン（買値 ×（1 + stop_loss_pct）。既定は買値の-10%）"
                             "を算出できない場合。抵触そのものは HS で確定する",
        "liquidity": f"20日平均売買代金 < {limit}",
        "trend": f"週足MA（{weekly_n}）の傾きが直近4週の回帰で負"
                 f"（許容は丸め誤差ぶんの -{tol}%/週 まで）。"
                 "どちらか一方でも負なら該当。未了週は計算から除く",
        "cloud": "終値が雲の下（下端未満）。上抜け・下抜けは直近5営業日を走査する",
        "overheat": f"RSI(14) > {rsi_th} ／ 25日移動平均乖離率 > {dev_th}% ／ "
                    f"信用倍率 > {margin_th}倍 のいずれか1つでも該当",
        "holding_target": "6か月2倍ラインに未到達、または到達したが日足5/25が平行"
                          "（到達×デッドクロス気味は HS で「売り」として確定済み）",
        "fundamentals": f"売上高 前年同四半期比 ≥ {rev_th}% かつ "
                        f"経常利益 前年同四半期比 ≥ {ord_th}% かつ "
                        f"1Q進捗率 ≥ {q1_th}%（継続 {streak}期以上）。"
                        "直近の開示が1Q累計でない期間は1Q進捗率を条件から外す"
                        "（該当しない＝未計算ではない）",
        "all_clear": "①〜⑤のすべてを通過し、保有中なら HS・H0・H5 も通過している",
    }


def stage_stamp_html(stage_id: str) -> str:
    stamps = STAGE_STAMPS.get(stage_id, ())
    return " ".join(stamp_html(s) for s in stamps)


def formula_stage_table(cfg: dict, gate: dict) -> str:
    conditions = stage_conditions(cfg, gate)
    rows = []
    for stage_id, label in J.STAGE_ORDER:
        name = e(label)
        cond = e(conditions.get(stage_id, ""))
        stamps = stage_stamp_html(stage_id)
        why = e(STAGE_RATIONALE.get(stage_id, ""))
        rows.append(f'<tr><td class="w">{name}</td><td>{cond}</td>'
                    f"<td>{stamps}</td><td>{why}</td></tr>")
    headers = ('<th class="w">段階</th><th class="w">該当条件</th>'
               "<th>該当時の判定</th><th>根拠</th>")
    return scroll(table_html(headers, rows, "", 4))


def indicator_rows_trend(gate: dict) -> list[tuple[str, str, str, str]]:
    """トレンド系（流動性・週足MA・雲）。（指標名, 定義, 必要なデータ, 閾値）。"""
    turnover_n = num(tech.TURNOVER_PERIODS, ",.0f")
    gate_limit = man(gate.get("min_avg_turnover_20d_jpy"))
    mid_n = num(tech.WEEKLY_MA_MID_PERIODS, ",.0f")
    long_n = num(tech.WEEKLY_MA_LONG_PERIODS, ",.0f")
    weekly_look = num(tech.SLOPE_LOOKBACK_WEEKS, ",.0f")
    tol_w = num(tech.TREND_GATE_NEGATIVE_TOLERANCE_PCT_PER_WEEK, ",.2f")
    flat_w = num(tech.SLOPE_FLAT_PCT_PER_WEEK, ",.2f")
    # 週足MAが張れるのに必要な**週足の本数**。sma_series は先頭 n-1 本が None なので、
    # n 本目で初めて値が出る。そこから傾きの回帰に look 本を使うので n + look - 1 本。
    need_mid = num(tech.WEEKLY_MA_LONG_PERIODS + tech.SLOPE_LOOKBACK_WEEKS - 1, ",.0f")
    tenkan = num(tech.ICHIMOKU_TENKAN_PERIODS, ",.0f")
    kijun = num(tech.ICHIMOKU_KIJUN_PERIODS, ",.0f")
    span_b = num(tech.ICHIMOKU_SPAN_B_PERIODS, ",.0f")
    disp = num(tech.ICHIMOKU_DISPLACEMENT, ",.0f")
    cloud_base = num(tech.ICHIMOKU_SPAN_B_PERIODS + tech.ICHIMOKU_DISPLACEMENT, ",.0f")
    cloud_need = num(tech.ICHIMOKU_SPAN_B_PERIODS + tech.ICHIMOKU_DISPLACEMENT + 1, ",.0f")
    return [
        ("20日平均売買代金",
         f"直近{turnover_n}営業日の（終値 × 出来高）の平均。"
         "売買代金そのものではなく（終値 × 出来高）の近似である"
         "（典型価格 (高値+安値+終値)/3 で計算すると実測で -3.5%〜+0.3% ずれる）。"
         "取引が無かった日は売買代金 0 として算入する"
         "（除外すると流動性を過大評価する）。同じ期間の中央値も併記する",
         f"終値と出来高が{turnover_n}営業日ぶん、欠測なく揃うこと",
         f"{gate_limit} 未満で「見送(流動性)」。算出できない場合は「調査」。"
         "ゲートは平均で引いており、中央値は判断材料として表示するだけ"
         "（中央値でも引くかはマスターの判断事項）"),
        (f"週足{mid_n}週MA・{long_n}週MAの傾き",
         f"日足をISO週（月曜始まり）で週足化 → 終値の単純移動平均（{mid_n}週と{long_n}週）"
         f" → 直近{weekly_look}週を最小二乗回帰した傾きを水準で割って %/週 に正規化。"
         "最終週が金曜で終わっていない（未了）ときは、その週を除いて計算する"
         "（営業日1日ぶんの終値が完成週と同じ重みで入ると傾きの符号が変わりうる）",
         f"週足が {need_mid} 本ぶん、欠測なく揃うこと"
         f"（{long_n}週MA＋回帰{weekly_look}週）",
         f"どちらか一方でも -{tol_w}%/週 を下回れば「見送(トレンド)」。"
         "鉄則の「中期」が13週か26週かは投資ルールに書かれていないため、"
         "確定するまで両方を見て保守側を採る。"
         f"表示ラベル（上向き／横ばい／下向き）だけは ±{flat_w}%/週 の帯を使う"),
        ("一目均衡表の雲",
         f"転換線=（{tenkan}日高値+{tenkan}日安値）/2、"
         f"基準線=（{kijun}日高値+{kijun}日安値）/2、"
         f"先行スパンA=（転換線+基準線）/2、"
         f"先行スパンB=（{span_b}日高値+{span_b}日安値）/2。"
         f"両スパンを{disp}本先にずらしたものが雲",
         f"日足の高値・安値・終値が {cloud_need} 本ぶん必要"
         f"（雲の完成{cloud_base}本＋抜け判定1本）",
         "終値が雲の下なら「売り」。前足が雲の中/下から当足で雲の上に出たら"
         "「上抜け」（スクリーニング条件）"),
    ]


def indicator_rows_signal(cfg: dict) -> list[tuple[str, str, str, str]]:
    """過熱・出来高・クロス系。（指標名, 定義, 必要なデータ, 閾値）。"""
    rsi_n = num(tech.RSI_PERIODS, ",.0f")
    rsi_warm = num(tech.RSI_PERIODS * tech.RSI_WARMUP_MULTIPLE + 1, ",.0f")
    rsi_th = num(cfg["rsi_overheat"], ",.0f")
    dev_n = num(tech.MA_DEVIATION_PERIODS, ",.0f")
    dev_screen = num(tech.MA_DEVIATION_SCREEN_PCT, ",.0f")
    dev_over = num(cfg["ma_deviation_overheat_pct"], ",.0f")
    vol_back = num(tech.VOLUME_RATIO_LOOKBACK_DAYS, ",.0f")
    vol_win = num(tech.VOLUME_RATIO_WINDOW_DAYS, ",.0f")
    vol_screen = num(tech.VOLUME_RATIO_SCREEN, ",.0f")
    margin_th = num(cfg["margin_ratio_overheat"], ",.0f")
    margin_age = num(cfg["margin_max_age_days"], ",.0f")
    cross_s = num(J.HOLDING_CROSS_SHORT, ",.0f")
    cross_l = num(J.HOLDING_CROSS_LONG, ",.0f")
    cross_look = num(tech.CROSS_LOOKBACK_PERIODS, ",.0f")
    flat_d = num(tech.CROSS_PARALLEL_SLOPE_DIFF_PCT, ",.2f")
    return [
        (f"RSI({rsi_n})",
         "Wilder の平滑平均。初期値は最初の14本の値幅の単純平均、"
         "以降は avg=(前avg×13+当期値)/14。RSI=100-100/(1+平均上昇幅/平均下降幅)",
         f"終値が {rsi_n}+1 本以上（直近{rsi_warm}本に欠測が無いこと）。"
         "値動きが完全に無い期間は 50 を返す",
         f"{rsi_th} 超で過熱（鉄則「RSIが8割超え」）"),
        (f"{dev_n}日移動平均乖離率",
         f"（終値 − {dev_n}日単純移動平均）÷ {dev_n}日単純移動平均 × 100",
         f"終値が {dev_n} 本ぶん、欠測なく揃うこと",
         f"スクリーニング条件は {dev_screen}% 以上、"
         f"過熱ラインは {dev_over}% 超（鉄則「7〜8%超えてたら怪しい」）"),
        ("3か月前出来高増加率（本プロジェクトの定義）",
         f"直近{vol_win}日の平均出来高 ÷ 「{vol_back}営業日前を終点とする"
         f"{vol_win}日平均出来高」。"
         "楽天証券の定義は未確認で、これは本プロジェクト独自の定義である。"
         "解釈を4通り試したが、スクリーニング通過4銘柄すべてが5倍以上を満たす定義は"
         "見つからなかった",
         f"出来高が {vol_back}+{vol_win} 本ぶん必要。比較先が 0 のときは算出しない"
         "（0 からの増加は倍率で表せない）",
         f"元のスクリーニング条件は {vol_screen}倍 以上だが、"
         "定義が一致していないので○×を出さず値だけ示す"),
        ("信用倍率",
         "買い残 ÷ 売り残（取得元の表示倍率をそのまま記録し、"
         "買い残÷売り残と乖離したら RATIO_INCONSISTENT を立てる）",
         f"週次公表。基準日から {margin_age} 日より古い残高は未計算として扱う",
         f"{margin_th}倍 超で過熱。売り残0で倍率が定義できない場合は"
         "「買い一辺倒」として過熱側に倒す（「過熱していない」とは読み替えない）"),
        (f"日足{cross_s}/{cross_l}のクロス",
         f"{cross_s}日移動平均と{cross_l}日移動平均の差の符号が"
         f"直近{cross_look}本で反転したかを見る。反転していない場合は両線の傾き差で"
         "「気味」を判定する",
         f"両方の移動平均が {cross_look}+1 本ぶん算出できること",
         f"傾き差が {flat_d}%/日 未満なら「平行」とし、"
         "鉄則が除外する状態として「気味」の判定を出さない"),
    ]


def indicator_rows(cfg: dict, gate: dict) -> list[tuple[str, str, str, str]]:
    return indicator_rows_trend(gate) + indicator_rows_signal(cfg)


def formula_indicator_table(cfg: dict, gate: dict) -> str:
    rows = []
    for name, formula, need, threshold in indicator_rows(cfg, gate):
        n = e(name)
        f = e(formula)
        d = e(need)
        t = e(threshold)
        rows.append(f'<tr><td class="w">{n}</td><td>{f}</td>'
                    f"<td>{d}</td><td>{t}</td></tr>")
    headers = ('<th class="w">指標</th><th class="w">定義・計算式</th>'
               '<th class="w">必要なデータ</th><th class="w">閾値と使いどころ</th>')
    return scroll(table_html(headers, rows, "", 4))


def formula_holding_table(master: dict) -> str:
    stop_pct = master.get("stop_loss_pct")
    ladder = master.get("target_ladder") or {}
    if stop_pct is None:
        stop_text = "stop_loss_pct が master.yaml に無いため算出しない"
    else:
        factor = num(1.0 + stop_pct, ".2f")
        rate = num(stop_pct, "+.0%")
        stop_text = f"買値 × {factor}（買値の {rate}）。現値がこれ以下なら「売り」"
    steps = []
    for k in sorted(ladder, key=lambda x: int(x)):
        month = e(k)
        value = num(ladder[k], "+.0%")
        steps.append(f"{month}か月 {value}")
    ladder_text = e("／".join(steps)) if steps else DASH
    pairs = [
        ("逆指値ライン", stop_text
         + "。抵触は終値ではなく安値で判定する（逆指値注文はザラ場で約定するため）"),
        ("6か月2倍ライン", ladder_text),
        ("経過月数", "買付日から基準日までの完了月数（日が足りなければ切り捨て）。"
                     "基準日は日足の最終営業日であって実行日ではない"
                     "（同じ入力から常に同じ結論が出るようにするため。"
                     "取得が長く止まると経過月数が実態より小さく出る）。"
                     "1か月未満は基準ラインが立たないので「未到達」として扱う。"
                     "買付日が基準日より未来なら入力の破損として「調査」で止める"),
        ("到達率", "買値からの騰落率 ÷ 経過月数に対応する基準。1.00 で到達"),
        ("到達後の扱い", "デッドクロス気味なら「売り」、ゴールデンクロス気味なら"
                         "ファンダ確認へ進んで「買」（買い増し可）、"
                         "平行なら鉄則が除外する状態として「監視」"),
        ("売りの優先", "雲の下・逆指値抵触・基準到達×デッドクロス気味のいずれかが"
                       "成立していれば、流動性・トレンド・過熱より先に「売り」で確定する。"
                       "「未計算なら調査で止める」は買い側だけの原則"),
        ("再評価", "保有は毎週フラットに再評価する。前週の判断を入力に持たない"),
    ]
    escaped = [(e(k), e(v)) for k, v in pairs]
    return kv_table(escaped)


def build_formula(master: dict) -> str:
    cfg = J.merge_config(master)
    gate = master.get("liquidity_gate") or {}
    parts = [
        "<h1>算出ロジック</h1>",
        '<p class="lede">判定に使っている順序・式・閾値・根拠をすべて開示する。'
        "ここに書いていない基準は判定に使っていない。"
        "計算と判定はすべてコードが行い、LLM は数値の計算にも判定にも関与しない。</p>",
        nav_html(0),
        "<h2>判定の順序</h2>",
        "<p>上から順に評価し、該当した時点で確定する。"
        "H0 と H5 は保有銘柄にだけ適用され、保有していない銘柄では「対象外」になる。</p>",
        formula_stage_table(cfg, gate),
        "<h2>未計算をどう扱うか</h2>",
        '<div class="notice"><strong>未計算は「条件を満たした」ではない。</strong>'
        "どの段階でも、必要な指標が算出できない場合はその場で「調査」に落として止める。"
        "たとえば出来高が取れず20日平均売買代金が計算できないとき、"
        "流動性ゲートを素通りさせて先へ進めることはしない。"
        "「流動性が確認できない」を「流動性ゲート通過」と扱うのは"
        "フェイルセーフの向きが逆であり、過去の実装にあった不具合である。</div>",
        "<h2>各指標の定義</h2>",
        formula_indicator_table(cfg, gate),
        "<h2>スクリーニング5条件</h2>",
        "<p>楽天証券スクリーニング「成長株0606」の条件を、"
        "<strong>本プロジェクトの定義で再計算したもの</strong>。"
        "監視対象の4銘柄はこの条件の通過銘柄である。"
        "○×は「今も条件を満たしているか」を毎週示すもので、"
        "判定ゲートとは別系統である（5条件をすべて満たしても「買」にはならない）。</p>",
        '<div class="notice"><strong>元のスクリーナーを再現できていない条件がある。</strong>'
        "実データ（4銘柄・2026-08-10）で再計算したところ、"
        "25日移動平均乖離率5%は4銘柄とも再現したが、"
        "3か月前出来高増加率5倍は解釈を4通り試しても4銘柄すべては満たさなかった。"
        "一目均衡表も「直近5営業日に上抜け」なら1銘柄、「雲の上にある」なら4銘柄で、"
        "元の条件がどちらの意味だったかは確認できていない。"
        "したがって出来高比は○×を出さず値だけを示し、"
        "一目均衡表はイベント（上抜け）と状態（雲の上）を分けて両方出している。</div>",
        formula_screen_table(cfg),
        "<h2>保有管理の式</h2>",
        formula_holding_table(master),
        "<h2>判定スタンプ</h2>",
        stamp_legend_table(),
        "<h2>鉄則のうち、この台帳が評価していない項目</h2>",
        "<p>判定は鉄則の全部をかけているわけではない。"
        "下の項目は鉄則に書かれているが、必要なデータを持っていないため"
        "<strong>評価していない</strong>。"
        "「買」は下の項目を確認した結果ではなく、"
        "①〜⑤のゲートを通過したという意味しか持たない。</p>",
        unevaluated_rules_table(),
        "<h2>この台帳がやらないこと</h2>",
        bullets([
            "売買の執行、数量の決定、ポートフォリオ管理",
            "割安さの単一軸ソート。マルチプルの低さには理由があり、"
            "割安順に並べると構造的に減速企業が上位に集まる",
            "欠測を推定値で埋めること。埋めるとしても必ず根拠と推測フラグを併記する",
            "LLM による数値の計算・判定。LLM の担当は決算資料からの数値抽出のみ",
        ]),
        '<p class="src">判定軸の出典は本人が定めた投資鉄則'
        "（週足中期MA・6か月2倍ライン・雲の下抜け・逆指値・過熱チェック）と、"
        "楽天証券スクリーニング「成長株0606」の5条件。</p>",
    ]
    return "".join(parts)


def formula_screen_table(cfg: dict) -> str:
    rev = num(cfg["screen_revenue_yoy_min_pct"], ",.0f")
    ordi = num(cfg["screen_ordinary_income_yoy_min_pct"], ",.0f")
    dev = num(cfg["screen_ma_deviation_min_pct"], ",.0f")
    vol = num(cfg["screen_volume_ratio_min"], ",.0f")
    look = num(cfg["ichimoku_cross_lookback_days"], ",.0f")
    pairs = [
        ("売上高変化率 前年同四半期比", f"{rev}% 以上（決算が入るまで未計算）"),
        ("経常利益変化率 前年同四半期比", f"{ordi}% 以上（決算が入るまで未計算）"),
        ("25日移動平均線乖離率", f"{dev}% 以上（4銘柄とも再現できている）"),
        ("3か月前出来高増加率",
         f"元の条件は {vol}倍 以上。定義が未確認のため○×を出さず値のみ表示する"),
        ("一目均衡表 上抜け（イベント）", f"直近{look}営業日以内に雲を上抜け"),
        ("（参考）雲の上にあるか", "終値が雲の上端より上にある状態"),
    ]
    escaped = [(e(k), e(v)) for k, v in pairs]
    return kv_table(escaped)


def unevaluated_rules_table() -> str:
    """鉄則に書かれているが判定に使っていない項目（judge.UNEVALUATED_RULES が正）。"""
    rows = []
    for item, why in J.UNEVALUATED_RULES:
        rows.append(f'<tr><td class="w">{e(item)}</td><td>{e(why)}</td></tr>')
    headers = '<th class="w">鉄則の項目</th><th class="w">評価していない理由</th>'
    return scroll(table_html(headers, rows, "", 2))


def caution_rows(verdicts) -> list[str]:
    """判定に効かないが読み手に伝える注意（judge.Verdict.cautions）。"""
    out: list[str] = []
    for v in verdicts:
        for c in v.cautions:
            out.append(f"{v.code} {v.name}: {c}")
    return out


def cautions_html(verdicts) -> str:
    notes = caution_rows(verdicts)
    if not notes:
        return ""
    items = "".join(f"<li>{e(n)}</li>" for n in notes)
    return ('<div class="notice"><strong>注意（ゲートではない）</strong>'
            "：判定は変えないが、読むときに知っておくべきこと。"
            f"<ul>{items}</ul></div>")


def quality_html(results) -> str:
    """checks.py の検査結果（WARN）を台帳に出す（F2-6）。

    追記性（append_only）は git ベースラインを要求し、実行環境によって結果が変わる。
    出力を決定論的に保つため、ここでは data/ の内容だけで決まる検査に絞る。
    追記性は data ジョブの `python src/checks.py` が検証しており、
    FAIL していればそもそもこのページは生成されない。
    """
    rows = []
    for r in results:
        if r.check == "append_only":
            continue
        rows.append(f'<tr><td>{e(r.level)}</td><td>{e(r.check)}</td>'
                    f"<td>{e(r.target)}</td><td>{e(r.message)}</td></tr>")
    headers = ('<th>種別</th><th>検査</th><th class="w">対象</th>'
               '<th class="w">内容</th>')
    body = scroll(table_html(headers, rows, "指摘なし", 4))
    return ('<p class="src">追記性（過去行の改変・削除）の検査は git の履歴を要するため'
            "ここには出していない。data ジョブの <code>python src/checks.py</code> が"
            "検証し、FAIL していればこのページは生成されない。</p>" + body)


# =============================================================================
# guide.html（読み方・非専門家が一人で読み切れること・F-07）
# =============================================================================

GUIDE_STEPS = [
    "<strong>まず判定スタンプの色を見る。</strong>"
    "赤系（売り・見送）は「今は買わない／降りる」、"
    "青（調査）は「まだ判断できない」、灰（監視）は「持ったまま様子を見る」、"
    "琥珀（様子見(過熱)）は「上がりすぎているので今日は追わない」、"
    "緑（買）は「条件をすべて満たした候補」。",
    "<strong>次に「確定段階と根拠」を読む。</strong>"
    "どの段階でその判定になったかが書いてある。"
    "たとえば「① 流動性ゲート」で確定していれば、"
    "その銘柄については他の指標を一切見ていない。",
    "<strong>最後に指標の実値を見る。</strong>"
    "「—」が並んでいたら、その銘柄は判定に必要な材料がまだ足りていない。"
    "数字が無いことを悪い材料と読む必要はないが、"
    "良い材料と読むこともできない。",
]

GUIDE_DONTS = [
    "<strong>「買」を買い推奨と読まないこと。</strong>"
    "この台帳は候補を並べるところまでで、いくら買うか・本当に買うかは決めていない。",
    "<strong>「買」を「全部確認した」と読まないこと。</strong>"
    "確認しているのは①〜⑤のゲートだけで、"
    "季節性・同業他社の決算・出来高を伴うトレンドか・マクロは見ていない。"
    "何を見ていないかは「算出ロジック」に一覧で書いてある。",
    "<strong>「—」を 0 と読まないこと。</strong>"
    "「—」は「計算できなかった」であって「ゼロだった」ではない。",
    "<strong>「?」を○にも×にも読み替えないこと。</strong>"
    "条件を満たすかどうかが分からない状態は、それ自体が情報である。",
    "<strong>先週の判定を引きずらないこと。</strong>"
    "判定は毎週ゼロから計算し直している。先週「買」でも今週「売り」になり得る。",
]

GLOSSARY = [
    ("株価（終値）", "その日の取引が終わったときの値段。この台帳では、"
                     "2つのサイトから取った値段が一致したときだけ採用する。"
                     "一致しなかった日は空欄にして、どちらの値も記録に残す。"),
    ("出来高", "その日に売買が成立した株数。多いほど取引が活発。"),
    ("売買代金", "出来高 × 株価。金額でみた取引の活発さ。"),
    ("流動性", "売りたいときに売れる・買いたいときに買える度合い。"
               "売買代金が小さい銘柄は、判断が正しくても実際には売買できないことがある。"),
    ("移動平均", "直近◯日の株価の平均。日々の上下をならして流れを見るための線。"
                 "25日移動平均なら直近25営業日の平均。"),
    ("乖離率", "今の株価が移動平均からどれだけ離れているかの割合。"
               "プラスに大きいほど「上がりすぎ」の目安になる。"),
    ("日足・週足", "1日ぶんを1点として並べたものが日足、1週間ぶんを1点にまとめたものが週足。"
                   "週足のほうが大きな流れを見るのに向いている。"),
    ("RSI", "直近の値動きのうち、上がった分がどれくらいの割合を占めるかを 0〜100 で表した数字。"
            "80 を超えると「買われすぎ」の目安。"),
    ("一目均衡表・雲", "過去の高値と安値から作る帯（雲）を先の日付にずらして描いたもの。"
                       "株価が雲の上にあれば強い、雲の下に落ちたら弱い、と読む。"
                       "この台帳では雲の下に落ちたら「売り」にする。"),
    ("ゴールデンクロス／デッドクロス",
     "短い期間の移動平均が長い期間の移動平均を下から上に抜けるのがゴールデンクロス（強い）、"
     "上から下に抜けるのがデッドクロス（弱い）。"
     "この台帳では、2本の線がほぼ平行なときは「どちらでもない」として扱う。"),
    ("信用取引・信用倍率",
     "お金や株を借りて行う売買のこと。買った人の残高（買い残）を"
     "売った人の残高（売り残）で割ったものが信用倍率。"
     "5倍を超えると「あとで売られる圧力が溜まっている」目安になる。"),
    ("逆指値", "「ここまで下がったら自動的に売る」という注文。"
               "この台帳では買値の -10% を基準線として表示する。"),
    ("6か月2倍ライン",
     "信用取引が6か月までしか持てないことに合わせた進捗の目安。"
     "1か月で +12%、2か月で +26%、3か月で +41%、4か月で +59%、"
     "5か月で +78%、6か月で +100%。到達率 1.00 でちょうど基準どおり。"),
    ("前年同四半期比", "同じ3か月間を1年前と比べた増減率。"
                       "季節で売上が変わる会社を公平に比べるために使う。"),
    ("経常利益", "本業のもうけに、利息などの本業以外の損益を足し引きした利益。"),
    ("1Q進捗率", "1年間の計画に対して、最初の3か月で何%まで進んだか。"
                 "30% を超えていると、その後の上方修正が期待できる目安になる。"),
    ("スクリーニング", "条件を決めて、当てはまる銘柄を機械的に絞り込むこと。"
                       "この台帳の4銘柄はその結果である。"),
    ("ブライアススコア", "予測の当て方の良し悪しを測る点数。"
                         "「自信あり」と言って外すほど悪くなる。低いほど良い。"),
    ("TOPIX・グロース250", "市場全体の値動きを表す指数。"
                            "個別銘柄が上がったのか市場全体が上がったのかを分けて見るために使う。"),
    ("append-only", "記録を後から書き換えない、という決まり。"
                    "追記だけを許すことで、過去の判断の履歴が改変されないようにしている。"),
]


def guide_step_list() -> str:
    items = "".join(f"<li>{s}</li>" for s in GUIDE_STEPS)
    return f"<ol>{items}</ol>"


def guide_dont_list() -> str:
    items = "".join(f"<li>{s}</li>" for s in GUIDE_DONTS)
    return f"<ul>{items}</ul>"


def glossary_table() -> str:
    rows = []
    for term, meaning in GLOSSARY:
        t = e(term)
        m = e(meaning)
        rows.append(f'<tr><th class="w">{t}</th><td>{m}</td></tr>')
    body = "".join(rows)
    return f"<table><tbody>{body}</tbody></table>"


def build_guide() -> str:
    parts = [
        "<h1>この台帳の読み方</h1>",
        '<p class="lede">専門用語を知らなくても読み切れるように書いた案内。'
        "後半に用語集がある。分からない言葉が出てきたら用語集を見れば足りる。</p>",
        nav_html(0),
        "<h2>この台帳は何か</h2>",
        "<p>条件を決めて機械的に絞り込んだ4つの会社の株について、"
        "毎週おなじ手順で状態を測り直し、記録として残しているページ。"
        "「今どういう状態か」を毎週おなじ物差しで書き留めることが目的で、"
        "「買え」「売れ」と勧めるものではない。"
        "実際に売買するかどうかは、この台帳を見た人間が決める。</p>",
        "<p>大事なのは、毎週おなじ計算を、おなじ順番で行っていること。"
        "後から「あのときは特別だった」と言い訳できないように、"
        "手順のほうを先に固定してある。</p>",
        "<h2>見る順番</h2>",
        guide_step_list(),
        "<h2>判定スタンプの意味</h2>",
        stamp_legend_table(),
        "<p>判定は上から順に見ていって、当てはまった時点で止まる。"
        "たとえば売買が細すぎる銘柄は、業績がどれだけ良くても"
        "そこで「見送(流動性)」になり、それより下は評価しない。"
        "これは手抜きではなく、「売りたいときに売れない株は、"
        "判断が当たっていても意味がない」という考え方によるもの。</p>",
        "<p>ただし<strong>買っている銘柄の「売り」だけは順番の外</strong>にある。"
        "雲の下に落ちた・逆指値の線を割った・目標に届いたが勢いが落ちた、"
        "のいずれかが起きていれば、他の項目がどうであっても先に「売り」を出す。"
        "「まだ判断できない」で止めてよいのは買う側の話で、"
        "降りる側でそれをやると降り遅れるため。</p>",
        "<h2>「—」と「?」の意味</h2>",
        '<div class="note">この台帳では、分からないことを分からないまま表示する。'
        "「—」は数字が計算できなかったこと、「?」は条件を満たすか判定できなかったことを表す。"
        "それらしい数字で埋めることはしない。"
        "そして、判定に必要な数字が欠けているときは、"
        "先に進めずに「調査」で止める決まりにしている。</div>",
        "<h2>スクリーニング5条件の表</h2>",
        "<p>もともとこの4銘柄を選んだときの条件が、今も満たされているかを○×で示している。"
        "選んだ後に条件から外れることはよくあるので、そこを毎週見えるようにしている。"
        "この5条件は判定とは別系統で、5つ全部○でも自動的に「買」にはならない。</p>",
        "<h2>保有銘柄の見方</h2>",
        "<p>実際に買った銘柄は、2本の線で管理する。"
        "1本目は「買値の -10%」で、ここまで下がったら降りる線。"
        "2本目は「6か月で2倍」に届くための進み具合の線で、"
        "たとえば買ってから3か月なら +41% 進んでいれば予定どおりとする。"
        "到達率が 1.00 なら予定どおり、それ未満なら遅れている。</p>",
        "<h2>やってはいけない読み方</h2>",
        guide_dont_list(),
        "<h2>用語集</h2>",
        glossary_table(),
        '<p class="src">計算式そのものを確認したい場合は'
        '<a href="formula.html">算出ロジック</a>に、'
        'もとの数字と取得元を確認したい場合は<a href="data.html">データ台帳</a>に'
        "すべて書いてある。</p>",
    ]
    return "".join(parts)


# =============================================================================
# 出力
# =============================================================================

def write_stamps(verdicts) -> Path:
    """scoring/stamps.json（notify.py の入力・F4-7・F-04）。

    キー順を固定し、生成時刻を埋め込まない。notify.py はこれと last_stamps.json を
    比較して、判定が変化した銘柄だけ Issue を起票する。
    """
    stamps = {v.code: v.stamp for v in verdicts}
    text = json.dumps(stamps, ensure_ascii=False, sort_keys=True, indent=2)
    path = ROOT / "scoring" / "stamps.json"
    write(path, text + "\n")
    return path


def main() -> int:
    master = load_master()
    prices = load_prices()
    margins = load_margins()
    indices = load_indices()
    summary = load_summary()
    preds = load_predictions()
    as_of = data_as_of(prices)

    verdicts = J.judge_all(master)
    by_code = {v.code: v for v in verdicts}

    # データ品質検査の結果を台帳に載せる（F2-6「WARN は台帳に表示して続行する」）。
    # ベースラインは渡さない＝出力が data/ の内容だけで決まる（決定論的生成・D8）。
    # 追記性の検証は data ジョブの checks.py が git 履歴を使って行う。
    quality = C.run_checks(ROOT / "data", baseline=None).results

    DOCS.mkdir(parents=True, exist_ok=True)
    write(DOCS / ".nojekyll", "")

    write(DOCS / "index.html",
          page(SITE_TITLE, build_index(master, prices, verdicts, as_of), as_of))
    write(DOCS / "data.html",
          page("データ台帳", build_data(prices, margins, indices, quality), as_of))
    write(DOCS / "scoring.html",
          page("予測採点", build_scoring(summary, preds), as_of))
    write(DOCS / "formula.html",
          page("算出ロジック", build_formula(master), as_of))
    write(DOCS / "guide.html", page("読み方", build_guide(), as_of))

    for stock in sorted(master.get("stocks", []), key=lambda s: str(s["code"])):
        code = str(stock["code"])
        v = by_code.get(code)
        if v is None:
            continue
        title = f'{stock.get("name", "")}（{code}）'
        body = build_stock(stock, v, prices.get(code) or [])
        write(DOCS / "stock" / f"{code}.html", page(title, body, as_of))

    write_stamps(verdicts)
    print(f"docs/ を生成（集計基準日 {as_of}・銘柄 {len(verdicts)}件）")
    for v in verdicts:
        print(f"  {v.code} {v.name}: {v.stamp}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
