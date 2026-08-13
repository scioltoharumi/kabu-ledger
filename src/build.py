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
import sys
from pathlib import Path

import markdown as md
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))

import report as R
from style import CSS

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


def to_html(markdown_text: str) -> str:
    """Markdown を HTML にする。表を横スクロールできるよう包む。"""
    raw = md.markdown(markdown_text, extensions=MD_EXT)
    return raw.replace("<table>", '<div class="scroll"><table>').replace(
        "</table>", "</table></div>")


# --- データ ---------------------------------------------------------------

def load_master() -> dict:
    path = ROOT / "data" / "master.yaml"
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def load_weekly_close(code: str) -> tuple[str, float | None]:
    """daily.csv から最終営業日と採用終値を取る。無ければ (—, None)。"""
    path = ROOT / "data" / "prices" / "daily.csv"
    if not path.exists():
        return "—", None
    last_date = "—"
    last_close: float | None = None
    with path.open(encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if row["code"] != code:
                continue
            if row["date"] > last_date or last_date == "—":
                last_date = row["date"]
                try:
                    last_close = float(row["close"]) if row["close"] else None
                except ValueError:
                    last_close = None
    return last_date, last_close


def as_of_date() -> str:
    """集計基準日 = 株価データの最終営業日。実行時刻は使わない（D8）。"""
    path = ROOT / "data" / "prices" / "daily.csv"
    if not path.exists():
        return "—"
    latest = ""
    with path.open(encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if row["date"] > latest:
                latest = row["date"]
    return latest or "—"


# --- 一覧ページ -----------------------------------------------------------

def render_card(stock: dict, rep: R.Report | None) -> str:
    code = stock["code"]
    name = html.escape(stock.get("name", code))
    market = html.escape(str(stock.get("market", "")))
    date, close = load_weekly_close(code)

    if rep is None:
        body = (
            f'<div class="card"><h3>{name}</h3>'
            f'<p class="meta">{html.escape(code)}／{market}</p>'
            f'<div class="none">レポート未作成。'
            f'この銘柄は <code>reports/{html.escape(code)}.md</code> がまだありません。</div>'
            f"</div>"
        )
        return body

    flag = '<span class="flag">深掘り中</span>' if rep.deep_dive else ""
    oneline = html.escape(R.one_liner(rep))
    close_txt = f"{close:,.0f}円" if close is not None else "—"
    earn = rep.meta.get("next_earnings")
    earn_pill = ""
    if earn:
        earn_pill = f'<span class="pill pill-warn">次の決算 {html.escape(str(earn))}</span>'

    latest = rep.latest_week()
    week_block = ""
    if latest is not None:
        head, body_md = latest
        first_para = body_md.split("\n\n")[0]
        week_html = to_html(first_para)
        week_block = (
            f'<div class="week"><span class="wk">最新の動き — {html.escape(head)}</span>'
            f"{week_html}</div>"
        )

    return (
        f'<div class="card">'
        f'<h3><a href="stock/{html.escape(code)}.html">{name}</a>{flag}</h3>'
        f'<p class="meta">{html.escape(code)}／{market}／'
        f'終値 <span class="num">{close_txt}</span>（{html.escape(date)}）</p>'
        f'<p>{earn_pill}</p>'
        f'<p class="oneline">{oneline}</p>'
        f"{week_block}</div>"
    )


def build_index(master: dict, reports: dict[str, R.Report], as_of: str) -> None:
    stocks = sorted(master["stocks"], key=lambda s: s["code"])
    cards = [render_card(s, reports.get(s["code"])) for s in stocks]
    scr = master.get("screening", {})
    scr_name = html.escape(str(scr.get("name", "")))

    intro = (
        "<h1>銘柄調査台帳</h1>"
        f'<p class="lede">楽天証券スクリーニング「{scr_name}」を通過した銘柄について、'
        "<strong>その会社が何をしている会社なのか</strong>を調べて記録する。"
        "スクリーニング通過は入口であって結論ではない。"
        "毎週の動きを積み重ねて、理解を深めることを目的にしている。</p>"
        + nav(0)
    )

    howto = (
        "<h2>この台帳の使い方</h2>"
        "<ul>"
        "<li>銘柄名をクリックすると、その会社の調査レポートが開く</li>"
        "<li>レポートは <strong>① 何の会社か → ② 財務 → ③ 展望とリスク → "
        "④ 週ごとの動き</strong> の順に並んでいる</li>"
        "<li><span class=\"flag\">深掘り中</span> が付いた銘柄は、毎週すべての項目を"
        "見直している。付いていない銘柄はニュースと値動きだけ追っている</li>"
        "<li>深掘り対象を変えたいときは Claude に「4073 を深掘りして」と言えばよい</li>"
        "<li>すべての記述に出典 URL を付けている。気になったら元の記事を開いて確認できる</li>"
        "</ul>"
    )

    body = intro + "<h2>銘柄</h2>" + "".join(cards) + howto
    (DOCS / "index.html").write_text(page("銘柄調査台帳", body, as_of, 0),
                                     encoding="utf-8")


# --- 銘柄ページ -----------------------------------------------------------

def render_section(rep: R.Report, key: str, title: str) -> str:
    body_md = rep.sections.get(key)
    if not body_md:
        return ""
    if key == "updates":
        return render_updates(rep, title)
    return f"<h2>{html.escape(title)}</h2>" + to_html(body_md)


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
        parts.append(to_html(body_md))
        parts.append("</div>")
    return "".join(parts)


def build_stock_page(rep: R.Report, as_of: str) -> None:
    (DOCS / "stock").mkdir(parents=True, exist_ok=True)
    name = html.escape(rep.name)
    code = html.escape(rep.code)
    market = html.escape(str(rep.meta.get("market", "")))
    flag = '<span class="flag">深掘り中</span>' if rep.deep_dive else ""

    head = (
        f"<h1>{name}（{code}）{flag}</h1>"
        f'<p class="lede">{market}／レポート更新 {html.escape(rep.updated)}</p>'
        + nav(1)
    )
    lead_md = strip_title(rep.lead)
    body = head + to_html(lead_md)
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
