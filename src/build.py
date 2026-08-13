"""docs/ を生成する（v2.0: 銘柄レポートが主役）。

v1.0 との違い:
  - テクニカル指標と判定スタンプを主役から降ろした
  - reports/{code}.md（人間が読む調査レポート）が本体
  - 週次アップデートは append-only。積み上がった順に見せる

決定論的生成（D8）:
  - 生成時刻を埋め込まない。可変要素は集計基準日のみ
  - 辞書順・行順を固定する。git diff が週次の差分そのものになる

実装上の注意:
  - f-string の中に複雑な式を書かない（値は変数に切り出す）
  - HTML は小さな関数に分ける。1関数は短く保つ
"""
from __future__ import annotations

import csv
import html
import re
import sys
from pathlib import Path

import markdown as md
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))

import chart as C
import report as R
from style import CSS

# 本文に置いた {{chart:id}} を図に差し替える。Markdown を HTML にした後に
# 適用するため、<p> で包まれた形も拾う。
CHART_RE = re.compile(r"(?:<p>)?\{\{chart:([a-z0-9_]+)\}\}(?:</p>)?")

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"

MD_EXT = ["tables", "fenced_code", "sane_lists"]

NAV_ITEMS = [
    ("index.html", "台帳"),
    ("data.html", "データの出どころ"),
]

DISCLAIMER = ("本サイトは個人の検討用であり、投資助言ではありません。"
              "売買の判断は人間が行います。数値はすべて出所と取得日を併記しています。")


# --- 骨組み ---------------------------------------------------------------

def nav(depth: int = 0) -> str:
    """depth はサブディレクトリの深さ。stock/ 配下は depth=1。"""
    prefix = "../" * depth
    links = []
    for href, label in NAV_ITEMS:
        links.append(f'<a href="{prefix}{href}">{html.escape(label)}</a>')
    return "<nav>" + "".join(links) + "</nav>"


def page(title: str, body: str, as_of: str, depth: int = 0) -> str:
    esc_title = html.escape(title)
    foot = f"<footer>集計基準日 {html.escape(as_of)}／{DISCLAIMER}</footer>"
    head = (
        '<!DOCTYPE html><html lang="ja"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<meta name="robots" content="noindex,nofollow">'
        f"<title>{esc_title}</title><style>{CSS}</style></head>"
    )
    return f"{head}<body><main>{body}{foot}</main></body></html>"


def to_html(markdown_text: str, charts: dict | None = None) -> str:
    """Markdown を HTML にする。表を横スクロールで包み、{{chart:id}} を図にする。"""
    raw = md.markdown(markdown_text, extensions=MD_EXT)
    out = raw.replace("<table>", '<div class="scroll"><table>').replace(
        "</table>", "</table></div>")
    return expand_charts(out, charts or {})


def expand_charts(text: str, charts: dict) -> str:
    """{{chart:id}} を SVG に置き換える。定義が無ければ欠落を隠さず残す。"""
    def sub(m: re.Match) -> str:
        cid = m.group(1)
        spec = charts.get(cid)
        if not spec:
            return f'<p class="none">図「{html.escape(cid)}」は未定義</p>'
        svg = C.render(spec)
        if not svg:
            return f'<p class="none">図「{html.escape(cid)}」を描けなかった</p>'
        return svg
    return CHART_RE.sub(sub, text)


def ext_link(url: str, label: str) -> str:
    """見出しの横に置く小さな外部リンク。"""
    safe = html.escape(url, quote=True)
    return (f'<a class="ext" href="{safe}" target="_blank" '
            f'rel="noopener">{html.escape(label)} ↗</a>')


# --- データ ---------------------------------------------------------------

def load_master() -> dict:
    path = ROOT / "data" / "master.yaml"
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def load_weekly_close(code: str) -> tuple[str, float | None]:
    """その銘柄の「確定した」最終営業日と採用終値を返す。

    close は2ソース照合が成立したときだけ入る採用値である。最新営業日は
    片方の取得元がまだ当日分を出しておらず（minkabu は翌日）空のことがあり、
    その日は指標の対象外になる（indicators.drop_unconfirmed_tail）。
    台帳の表示もその確定日に揃える。照合を通っていない値は使わない（D7）。
    """
    path = ROOT / "data" / "prices" / "daily.csv"
    if not path.exists():
        return "—", None
    last_date = ""
    last_close: float | None = None
    with path.open(encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if row["code"] != code or not row["close"]:
                continue
            if row["date"] > last_date:
                try:
                    last_close = float(row["close"])
                    last_date = row["date"]
                except ValueError:
                    continue
    return (last_date or "—"), last_close


def as_of_date() -> str:
    """集計基準日 = 確定している最後の営業日。実行時刻は使わない（D8）。

    「確定」= close（照合を通った採用値）が入っている日。未確定の当日を
    基準日に出すと、指標は前日で計算されているのに日付だけ新しく見え、
    読み手を誤らせる。
    """
    path = ROOT / "data" / "prices" / "daily.csv"
    if not path.exists():
        return "—"
    latest = ""
    with path.open(encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if row["close"] and row["date"] > latest:
                latest = row["date"]
    return latest or "—"


# --- 一覧ページ -----------------------------------------------------------

def first_sentence(text: str, limit: int = 78) -> str:
    """最新週の要約を1文だけ取り出す（一覧を詰まらせないため）。"""
    plain = re.sub(r"[*_`>#\-]", "", text).strip()
    plain = re.sub(r"\s+", " ", plain)
    for sep in ("。", "．"):
        if sep in plain:
            plain = plain.split(sep)[0] + sep
            break
    if len(plain) > limit:
        plain = plain[: limit - 1] + "…"
    return plain


def render_row(stock: dict, rep: R.Report | None) -> str:
    """一覧の1行。銘柄が増えても詰まらないよう、1銘柄1行に収める。

    スマホでは CSS 側でカード状に積み替える（横スクロールさせない）。
    """
    code = stock["code"]
    name = html.escape(stock.get("name", code))
    market = html.escape(str(stock.get("market", "")))
    date, close = load_weekly_close(code)
    close_txt = f"{close:,.0f}" if close is not None else "—"

    if rep is None:
        return (
            f'<tr><td data-l="銘柄"><span class="nm">{name}</span>'
            f'<span class="sub">{html.escape(code)}／{market}</span></td>'
            f'<td data-l="終値" class="num">{close_txt}</td>'
            f'<td data-l="状態" colspan="2"><span class="pill">レポート未作成</span></td>'
            f"</tr>"
        )

    flag = '<span class="flag">深掘り</span>' if rep.deep_dive else ""
    site = ""
    for lk in rep.links:
        if lk.get("primary"):
            site = ext_link(str(lk.get("url", "")), str(lk.get("label", "公式")))
            break

    oneline = html.escape(R.one_liner(rep))
    earn = rep.meta.get("next_earnings")
    earn_pill = ""
    if earn:
        earn_pill = (f'<span class="pill pill-warn">決算 '
                     f'{html.escape(str(earn))}</span>')

    latest = rep.latest_week()
    week_txt = "—"
    week_head = ""
    if latest is not None:
        week_head = html.escape(latest[0].split("（")[0])
        week_txt = html.escape(first_sentence(latest[1]))

    return (
        f"<tr>"
        f'<td data-l="銘柄"><span class="nm">'
        f'<a href="stock/{html.escape(code)}.html">{name}</a>{flag}{site}</span>'
        f'<span class="sub">{html.escape(code)}／{market}／{earn_pill}</span>'
        f'<span class="one">{oneline}</span></td>'
        f'<td data-l="終値" class="num">{close_txt}<span class="sub">'
        f"{html.escape(date)}</span></td>"
        f'<td data-l="今週"><span class="sub">{week_head}</span>{week_txt}</td>'
        f"</tr>"
    )


def build_index(master: dict, reports: dict[str, R.Report], as_of: str) -> None:
    stocks = sorted(master["stocks"], key=lambda s: s["code"])
    rows = [render_row(s, reports.get(s["code"])) for s in stocks]
    scr = master.get("screening", {})
    scr_name = html.escape(str(scr.get("name", "")))
    n_deep = sum(1 for r in reports.values() if r.deep_dive)

    intro = (
        "<h1>銘柄調査台帳</h1>"
        f'<p class="lede">楽天証券スクリーニング「{scr_name}」を通過した銘柄について、'
        "<strong>その会社が何をしている会社なのか</strong>を調べて記録する。"
        "スクリーニング通過は入口であって結論ではない。"
        "毎週の動きを積み重ねて理解を深めることを目的にしている。</p>"
        + nav(0)
    )

    summary = (
        f'<p class="readout"><span>登録 <b>{len(stocks)}</b> 銘柄</span>'
        f'<span>レポートあり <b>{len(reports)}</b></span>'
        f'<span>深掘り中 <b>{n_deep}</b></span>'
        f'<span>基準日 <b>{html.escape(as_of)}</b></span></p>'
    )

    table = (
        '<div class="scroll"><table class="list-table"><thead><tr>'
        "<th>銘柄</th><th>終値</th><th>今週の動き</th>"
        "</tr></thead><tbody>" + "".join(rows) + "</tbody></table></div>"
    )

    howto = (
        "<h2>この台帳の読み方</h2>"
        "<ul>"
        "<li>銘柄名を押すと、その会社の調査レポートが開く</li>"
        "<li>レポートは <strong>① 何の会社か → ② 財務 → ③ 展望とリスク → "
        "④ 週ごとの動き → ⑤ 値動きと市場の評価</strong> の順に並んでいる</li>"
        '<li><span class="flag">深掘り</span> が付いた銘柄は毎週すべての項目を'
        "見直している。付いていない銘柄はニュースと値動きだけ追っている</li>"
        "<li>深掘り対象を変えたいときは Claude に「4073 を深掘りして」と言えばよい</li>"
        "<li>銘柄名の横の小さなリンクは会社の公式サイト。"
        "各レポートの末尾には、使ったすべての出典 URL を載せている</li>"
        "</ul>"
    )

    body = intro + summary + table + howto
    (DOCS / "index.html").write_text(page("銘柄調査台帳", body, as_of, 0),
                                     encoding="utf-8")


# --- 銘柄ページ -----------------------------------------------------------

def render_section(rep: R.Report, key: str, title: str) -> str:
    body_md = rep.sections.get(key)
    if not body_md:
        return ""
    if key == "updates":
        return render_updates(rep, title)
    return f"<h2>{html.escape(title)}</h2>" + to_html(body_md, rep.charts)


def render_updates(rep: R.Report, title: str) -> str:
    """週次アップデートは新しい週を上に。過去の記述は残したまま並べる。"""
    entries = rep.week_entries()
    if not entries:
        return ""
    parts = [f"<h2>{html.escape(title)}</h2>"]
    parts.append('<p class="lede">新しい週を上に置いている。'
                 "過去の記述は書き換えず、そのまま残している。</p>")
    for head, body_md in entries:
        parts.append('<div class="upd">')
        parts.append(f"<h3>{html.escape(head)}</h3>")
        parts.append(to_html(body_md, rep.charts))
        parts.append("</div>")
    return "".join(parts)


def build_stock_page(rep: R.Report, as_of: str) -> None:
    (DOCS / "stock").mkdir(parents=True, exist_ok=True)
    name = html.escape(rep.name)
    code = html.escape(rep.code)
    market = html.escape(str(rep.meta.get("market", "")))
    flag = '<span class="flag">深掘り中</span>' if rep.deep_dive else ""

    links = "".join(ext_link(str(lk.get("url", "")), str(lk.get("label", "")))
                    for lk in rep.links if lk.get("url"))
    head = (
        f"<h1>{name}（{code}）{flag}</h1>"
        f'<p class="lede">{market}／レポート更新 {html.escape(rep.updated)}'
        f"<br>{links}</p>"
        + nav(1)
    )
    lead_md = strip_title(rep.lead)
    body = head + to_html(lead_md, rep.charts)
    for key, title in R.SECTIONS:
        body += render_section(rep, key, title)

    title = f"{rep.name}（{rep.code}）"
    (DOCS / "stock" / f"{rep.code}.html").write_text(
        page(title, body, as_of, 1), encoding="utf-8")


def strip_title(lead: str) -> str:
    """リードから `# 見出し` を落とす（h1 は別に組み立てているため）。"""
    lines = [ln for ln in lead.splitlines() if not ln.startswith("# ")]
    return "\n".join(lines).strip()


# --- データの出どころ -----------------------------------------------------

def build_data_page(master: dict, reports: dict[str, R.Report], as_of: str) -> None:
    rows = []
    for s in sorted(master["stocks"], key=lambda x: x["code"]):
        code = s["code"]
        rep = reports.get(code)
        state = "レポートあり" if rep else "未作成"
        date, close = load_weekly_close(code)
        close_txt = f"{close:,.0f}" if close is not None else "—"
        name = html.escape(s.get("name", code))
        rows.append(
            f"<tr><td>{html.escape(code)}</td><td>{name}</td>"
            f'<td class="num">{close_txt}</td><td>{html.escape(date)}</td>'
            f"<td>{state}</td></tr>"
        )

    body = (
        "<h1>データの出どころ</h1>"
        '<p class="lede">数値がどこから来たのかを開示する。'
        "各銘柄の詳しい出典は、それぞれのレポート末尾「⑥ 出典」にある。</p>"
        + nav(0)
        + "<h2>株価</h2>"
        + '<div class="scroll"><table><thead><tr><th>コード</th><th>銘柄</th>'
        + "<th>終値</th><th>最終営業日</th><th>レポート</th></tr></thead><tbody>"
        + "".join(rows)
        + "</tbody></table></div>"
        + "<h2>集め方の原則</h2><ul>"
        + "<li>株価は複数のサイトから取り、<strong>値が一致したときだけ採用</strong>している。"
        "一致しなかった日は空欄のまま残す（推測で埋めない）</li>"
        + "<li>レポートの記述は、まとめサイトを含めて広く集めている。"
        "<strong>一次情報（決算短信・企業IR）と二次情報（まとめサイト・報道）を区別して</strong>"
        "出典欄に書き分けている</li>"
        + "<li>調べきれなかったことは「未確認」として残す。"
        "分かったふりをしない</li>"
        + "</ul>"
    )
    (DOCS / "data.html").write_text(page("データの出どころ", body, as_of, 0),
                                    encoding="utf-8")


# --- main -----------------------------------------------------------------

def main() -> int:
    DOCS.mkdir(exist_ok=True)
    (DOCS / ".nojekyll").write_text("", encoding="utf-8")

    master = load_master()
    codes = [s["code"] for s in master["stocks"]]
    reports = R.load_all(codes)
    as_of = as_of_date()

    build_index(master, reports, as_of)
    build_data_page(master, reports, as_of)
    for code in sorted(reports):
        build_stock_page(reports[code], as_of)

    made = ", ".join(sorted(reports)) or "なし"
    print(f"docs/ を生成（基準日 {as_of}）")
    print(f"  レポートあり: {made}")
    missing = [c for c in codes if c not in reports]
    if missing:
        print(f"  レポート未作成: {', '.join(missing)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
