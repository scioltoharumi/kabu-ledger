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
import json
import re
import sys
from pathlib import Path

import markdown as md
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))

import chart as C
import chartdata as CD
import judge as J
import report as R
import verification as VF
from style import CSS

# 本文に置いた {{chart:id}} を図に差し替える。Markdown を HTML にした後に
# 適用するため、<p> で包まれた形も拾う。
CHART_RE = re.compile(r"(?:<p>)?\{\{chart:([a-z0-9_]+)\}\}(?:</p>)?")

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"
STAMPS = ROOT / "scoring" / "stamps.json"

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
    """Markdown を HTML にする。表を横スクロールで包み、{{chart:id}} を図にする。

    charts は chartdata.resolve_charts の戻り（id -> Resolved）。
    """
    raw = md.markdown(markdown_text, extensions=MD_EXT)
    # prose-table: 本文（出典の「内容」列など）が1列目に来るので折り返させる。
    # 付けないと style.py の `td:first-child{white-space:nowrap}` に当たって
    # 表が画面外まで伸びる。
    out = raw.replace("<table>", '<div class="scroll"><table class="prose-table">'
                      ).replace("</table>", "</table></div>")
    return expand_charts(out, charts or {})


def expand_charts(text: str, charts: dict) -> str:
    """{{chart:id}} を SVG に置き換える。**欠落を隠さない**。

    検証済みの数値が1件も無い図は、それらしい形を描かずに理由を出す。
    0 で埋めた図を出すほうが、図が無いより有害である（D7）。
    """
    def sub(m: re.Match) -> str:
        cid = m.group(1)
        res = charts.get(cid)
        if res is None:
            return f'<p class="none">図「{html.escape(cid)}」は未定義</p>'
        svg = C.render(res.spec)
        if not svg:
            why = res.empty_reason or "描くための値がそろっていない"
            return (f'<p class="none">図「{html.escape(cid)}」を描けなかった — '
                    f"{html.escape(why)}</p>")
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

# Markdown の強調 `**…**`。改行をまたがせない（表の1セルに収まる範囲だけ拾う）。
_STRONG_RE = re.compile(r"\*\*([^*\n]+?)\*\*")


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
            f'<td data-l="状態"><span class="pill">レポート未作成</span></td>'
            f"</tr>"
        )

    flag = '<span class="flag">深掘り</span>' if rep.deep_dive else ""
    site = ""
    for lk in rep.links:
        if lk.get("primary"):
            site = ext_link(str(lk.get("url", "")), str(lk.get("label", "公式")))
            break

    # 「一行でいうと」はレポート本文の引用ブロックなので Markdown の強調を含む。
    # 銘柄ページ側は Markdown を通すが、一覧はエスケープしかしていなかったため
    # `**` がそのまま画面に出ていた。**エスケープしたあとに**強調だけ戻す
    # （順序が逆だと `<strong>` ごとエスケープされる／注入経路にもなる）。
    oneline = _STRONG_RE.sub(r"<strong>\1</strong>",
                             html.escape(R.one_liner(rep)))
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

    # 裏取りの状態を一覧にも出す。銘柄ページを開かないと分からない状態にしない。
    passed, claims, present, stale = verify_stat(code)
    if not present:
        verify_pill_html = '<span class="pill pill-warn">裏取り未実施</span>'
    elif stale:
        verify_pill_html = '<span class="pill pill-warn">裏取りの記録が古い</span>'
    elif passed == claims:
        verify_pill_html = (f'<span class="pill pill-good">裏取り '
                            f"{passed}/{claims}</span>")
    else:
        verify_pill_html = (f'<span class="pill pill-warn">裏取り '
                            f"{passed}/{claims}</span>")

    return (
        f"<tr>"
        f'<td data-l="銘柄"><span class="nm">'
        f'<a href="stock/{html.escape(code)}.html">{name}</a>{flag}{site}</span>'
        f'<span class="sub">{html.escape(code)}／{market}／{earn_pill}'
        f"{verify_pill_html}</span>"
        f'<span class="one">{oneline}</span></td>'
        f'<td data-l="終値" class="num">{close_txt}<span class="sub">'
        f"{html.escape(date)}</span></td>"
        f'<td data-l="今週"><span class="sub">{week_head}</span>{week_txt}</td>'
        f"</tr>"
    )


_ORDER_MARKS = "①②③④⑤⑥⑦⑧⑨⑩"


def section_order_text() -> str:
    """一覧の案内文に出す「レポートの節の並び」を `report.SECTIONS` から作る。

    案内文と実際の並びが食い違うのを、手書きをやめることで構造的に防ぐ。
    出力は SECTIONS だけで決まるので決定論的（D8）。
    """
    parts = []
    for i, (_key, title) in enumerate(R.SECTIONS):
        mark = _ORDER_MARKS[i] if i < len(_ORDER_MARKS) else f"{i + 1}."
        parts.append(f"{mark} {title}")
    return " → ".join(parts)


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
        '<div class="scroll"><table class="list-table prose-table"><thead><tr>'
        "<th>銘柄</th><th>終値</th><th>今週の動き</th>"
        "</tr></thead><tbody>" + "".join(rows) + "</tbody></table></div>"
    )

    # 節の並びは `report.SECTIONS` が正。ここに順序を手書きすると、並びを変えた
    # 週に案内文だけが取り残される（「週次アップデートを最上部に」で実際に起きた）。
    order = html.escape(section_order_text())

    howto = (
        "<h2>この台帳の読み方</h2>"
        "<ul>"
        "<li>銘柄名を押すと、その会社の調査レポートが開く</li>"
        f"<li>レポートは <strong>{order}</strong> の順に並んでいる</li>"
        '<li><span class="flag">深掘り</span> が付いた銘柄は毎週すべての項目を'
        "見直している。付いていない銘柄はニュースと値動きだけ追っている</li>"
        "<li>深掘り対象を変えたいときは Claude に「4073 を深掘りして」と言えばよい</li>"
        "<li>銘柄名の横の小さなリンクは会社の公式サイト。"
        "各レポートの末尾には、使ったすべての出典 URL を載せている</li>"
        "</ul>"
    )

    body = intro + summary + table + howto + unevaluated_block()
    (DOCS / "index.html").write_text(page("銘柄調査台帳", body, as_of, 0),
                                     encoding="utf-8")


def unevaluated_block() -> str:
    """鉄則のうち、このシステムが**評価していない**項目を台帳に出す。

    `judge.UNEVALUATED_RULES` が正。以前は「`docs/formula.html` に自動で出る」と
    CLAUDE.md / README.md が案内していたが、**そのファイルは一度も生成されて
    いなかった**（`build.py` に formula の文字列が1つも無い）。
    つまり「何を見ていないか」は判定を読む人にまったく届いておらず、
    「①〜⑤を通過した」が「鉄則を全部かけた」と読める状態だった。
    `judge.py` を CLI で叩いた人だけが知っている、では開示になっていない。
    """
    rules = J.UNEVALUATED_RULES
    if not rules:
        return ""
    rows = "".join(
        f'<tr><td data-l="観点">{html.escape(name)}</td>'
        f'<td data-l="なぜ見ていないか">{html.escape(why)}</td></tr>'
        for name, why in rules
    )
    return (
        '<h2 id="unevaluated">この台帳が見ていない鉄則</h2>'
        '<p class="lede">判定スタンプの「買」は、この台帳が実装しているゲートを'
        "通過したという意味しかない。<strong>鉄則には、まだ機械が評価していない"
        f"観点が {len(rules)} 件ある。</strong>通過しなかったのではなく、"
        "<strong>見ていない</strong>。実装したらこの表から消える"
        "（<code>src/judge.py</code> の <code>UNEVALUATED_RULES</code> が正）。</p>"
        '<div class="scroll"><table class="prose-table"><thead><tr>'
        "<th>鉄則の観点</th><th>なぜ評価していないか</th>"
        "</tr></thead><tbody>" + rows + "</tbody></table></div>"
    )


# --- 銘柄ページ -----------------------------------------------------------

def render_section(rep: R.Report, key: str, title: str, charts: dict) -> str:
    body_md = rep.sections.get(key)
    if not body_md:
        return ""
    if key == "updates":
        return render_updates(rep, title, charts)
    return f"<h2>{html.escape(title)}</h2>" + to_html(body_md, charts)


def render_updates(rep: R.Report, title: str, charts: dict) -> str:
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
        parts.append(to_html(body_md, charts))
        parts.append("</div>")
    return "".join(parts)


# --- 検証状況（この銘柄の数値がどこまで確かめられているか） -----------------

ORIGIN_JA = {
    "csv": "検証済みCSVから自動で組み立て",
    "hand": "front matter に人が書き写した値",
}


def chart_origin_rows(charts: dict) -> str:
    """図ごとの出どころ。1行1図で、CSV由来と手書きを並べて見せる。"""
    rows = []
    for cid in sorted(charts):
        res = charts[cid]
        origin = ORIGIN_JA.get(res.origin, res.origin)
        if res.origin == "hand":
            pill = '<span class="pill pill-warn">未検証</span>'
            state = "—"
        elif res.empty_reason:
            pill = '<span class="pill pill-danger">描けず</span>'
            state = html.escape(res.empty_reason)
        elif res.missing:
            pill = '<span class="pill pill-warn">一部欠測</span>'
            state = f"{res.used}/{res.total}点を採用（残りは照合不成立）"
        else:
            pill = '<span class="pill pill-good">照合済み</span>'
            state = f"{res.used}/{res.total}点すべて採用値"
        rows.append(
            f"<tr><td><code>{html.escape(cid)}</code></td>"
            f"<td>{pill}{html.escape(origin)}</td>"
            f"<td>{state}</td></tr>"
        )
    if not rows:
        return ""
    return (
        '<div class="scroll"><table><thead><tr><th>図</th>'
        "<th>数値の出どころ</th><th>状態</th></tr></thead><tbody>"
        + "".join(rows) + "</tbody></table></div>"
    )


def render_cross(v) -> str:
    """決算短信（一次情報）と まとめサイト（二次情報）の突き合わせ結果。

    出所が独立した2つなので、一致すれば株価の2ソース照合と同じ意味を持つ。
    """
    def names(items: list[str]) -> str:
        if not items:
            return "—"
        return "、".join(html.escape(CD.metric_ja(m)) for m in items)

    label = html.escape(CD.period_label(v.cross_period))
    return (
        "<h3>決算短信（一次情報）と まとめサイトの突き合わせ</h3>"
        f"<p>直近の決算短信PDFから抜いた数値を、同じ期（{label}）の"
        "まとめサイトの値と機械で照らし合わせた結果。"
        "<strong>出所が独立した2つなので、一致すれば株価の2ソース照合と同じ意味を持つ。</strong></p>"
        '<div class="scroll"><table><thead><tr><th>結果</th><th>件数</th>'
        "<th>項目</th></tr></thead><tbody>"
        f'<tr><td><span class="pill pill-good">一致</span></td>'
        f'<td class="num">{len(v.cross_agree)}</td><td>{names(v.cross_agree)}</td></tr>'
        f'<tr><td><span class="pill pill-danger">食い違い</span></td>'
        f'<td class="num">{len(v.cross_disagree)}</td>'
        f"<td>{names(v.cross_disagree)}</td></tr>"
        f'<tr><td><span class="pill">相手なし</span></td>'
        f'<td class="num">{len(v.cross_nopair)}</td>'
        f"<td>{names(v.cross_nopair)}</td></tr>"
        f'<tr><td><span class="pill">対象外</span></td>'
        f'<td class="num">{len(v.cross_other)}</td>'
        "<td>前年同期・前期末・通期計画（別の期の数値なので"
        "この照合には混ぜない）</td></tr>"
        "</tbody></table></div>"
        "<p>「相手なし」は、まとめサイト側が同じ期の同じ項目を持っていないもの。"
        "決算短信の値そのものは一次情報なので使えるが、"
        "<strong>独立した2つ目の確認は取れていない</strong>。</p>"
    )


# --- 記述の裏取り（別コンテキストの検証・F3） -------------------------------
#
# `data/fundamentals` の照合が守れるのは**数値**だけで、レポートの大半を占める
# 散文（「導入180社以上」「アナリストのカバーは0社」）は素通りしていた。
# ここは、レポートを書いたのとは別の文脈が出典URLを実際に取り直して
# 1件ずつ判定した結果を、**読み手が消せない形で**出す欄（D31 と同じ形）。
#
# 記録が無い銘柄は「未検証」と出す。黙って何も出さない選択肢は用意しない。

VERDICT_PILL = {
    "supported": "pill-good",
    "superseded": "pill-warn",
    "unsupported": "pill-warn",
    "contradicted": "pill-danger",
    "unverifiable": "pill-warn",
}


def verify_pill(verdict: str) -> str:
    cls = VERDICT_PILL.get(verdict, "")
    label = VF.VERDICTS.get(verdict, verdict)
    return f'<span class="pill {cls}">{html.escape(label)}</span>'


MD_MARK_RE = re.compile(r"[*`]+")


def verify_rows(claims: list) -> str:
    """1行1記述。何を確かめ、何が落ちたかを本文の引用つきで出す。

    記録側の `quote` は本文の**厳密な部分文字列**（checks.py が本文と突き合わせる）
    なので、表示のときだけ Markdown の強調記号を落とす。記録は書き換えない。

    `evidence` / `action` は突合の対象ではなく、書き手が強調を意図して
    `**…**` を書く。落とさずに `<strong>` に起こす（一覧の「一行でいうと」と
    同じ扱い。エスケープしたあとに置換する）。
    """
    rows = []
    for c in claims:
        quote = html.escape(MD_MARK_RE.sub("", c.quote))
        why = _STRONG_RE.sub(r"<strong>\1</strong>", html.escape(c.evidence))
        act = (_STRONG_RE.sub(r"<strong>\1</strong>", html.escape(c.action))
               if c.action else "—")
        srcs = []
        for s in c.sources:
            if s.startswith("http"):
                srcs.append(ext_link(s, "出典"))
            else:
                srcs.append(f"<code>{html.escape(s)}</code>")
        src_html = " ".join(srcs) if srcs else "—"
        tier = html.escape(c.tier_ja)
        rows.append(
            f"<tr><td>{verify_pill(c.verdict)}</td>"
            f"<td>{quote}</td><td>{why}</td>"
            f"<td>{tier}<br>{src_html}</td><td>{act}</td></tr>"
        )
    if not rows:
        return ""
    return (
        '<div class="scroll"><table><thead><tr><th>判定</th><th>本文の記述</th>'
        "<th>再取得して分かったこと</th><th>出典</th><th>どうするか</th>"
        "</tr></thead><tbody>" + "".join(rows) + "</tbody></table></div>"
    )


def verify_stat(code: str) -> tuple:
    """(裏付けあり, 総数, 記録あり, 本文が検証後に変わったか) を返す。

    claim は **id で畳んで最新の判定**を数える（`VF.Record.folded`）。
    最新 run だけを見ると、claim 1件だけの run を足すだけで
    「15/26件」が「1/1件」に化け、過去の指摘が台帳から消える。
    """
    rec = VF.load(code)
    if rec is None or rec.latest is None:
        return (0, 0, False, False)
    run = rec.latest
    now = VF.report_sha256(code)
    stale = bool(run.report_sha256 and now and run.report_sha256 != now)
    folded = rec.folded()
    passed = sum(1 for c, _ in folded if c.passed)
    return (passed, len(folded), True, stale)


def verify_headline(code: str) -> str:
    """ページ冒頭に置く1行。**警告ブロックで埋めない**（F5-4）ので1行に収める。

    ★本文が検証後に書き換わっているときは **件数を出さない**。
      「15/26件が裏付けあり」の横に小さく「記録が古い」を添えると、
      具体的な件数のほうが目立って打ち消しにならない（表示の嘘・D32 と同型）。
    """
    passed, total, present, stale = verify_stat(code)
    if not present:
        return ('<br><span class="pill pill-warn">未検証</span>'
                "本文の記述はまだ出典に当て直していない")
    if stale:
        return ('<br><span class="pill pill-warn">記録が古い</span>'
                '<a href="#verify">本文がこの検証のあとに書き換えられている'
                "（件数は現在の本文に適用できない）</a>")
    rest = total - passed
    tail = "" if rest == 0 else f"／{rest}件は未確認・要修正"
    return (f'<br>記述の裏取り <a href="#verify">{passed}/{total}件が裏付けあり'
            f"{tail}</a>")


def render_verify(rep: R.Report) -> str:
    """記述の裏取り欄。裏が取れなかったものを先に、取れたものは畳んで出す。"""
    code = rep.code
    rec = VF.load(code)
    head = '<h2 id="verify">記述の裏取り</h2>'
    lede = (
        '<p class="lede">'
        "このレポートを書いたのとは<strong>別の文脈</strong>が、本文に書かれている"
        "出典URLを実際にもう一度取りに行き、"
        "<strong>その記述がその出典で裏付けられるか</strong>を1件ずつ判定した結果。"
        "書き手が「こう書いてある」と言っていることは根拠にしていない。</p>"
    )
    if rec is None or rec.latest is None:
        return (head + lede + '<p class="none">'
                '<span class="pill pill-warn">未検証</span>'
                "この銘柄はまだ裏取りしていない。本文の記述は、"
                "出典に当て直した確認を経ていない。</p>")

    run = rec.latest
    now = VF.report_sha256(code)
    stale = bool(run.report_sha256 and now and run.report_sha256 != now)
    # claim は id で畳んで最新の判定を採る（最新 run だけを見ると過去の指摘が消える）。
    claims = [c for c, _owner in rec.folded()]
    counts: dict = {}
    for c in claims:
        counts[c.verdict] = counts.get(c.verdict, 0) + 1

    parts = [head, lede]

    if stale:
        # **件数を出さない。** 具体的な数字のほうが目立ち、小さな注記が
        # 打ち消しにならない（表示の嘘・D32 と同型）。
        parts.append(
            '<p class="none"><span class="pill pill-warn">記録が古い</span>'
            "この検証のあとにレポート本文が書き換えられている。"
            "<strong>下の判定は現在の本文に適用できない。</strong>"
            "件数は出さない（現在の本文について確かめられていないため）。</p>")
    else:
        readout = ['<p class="readout">',
                   f"<span>検証した記述 <b>{len(claims)}</b></span>"]
        for key in VF.VERDICTS:
            n = counts.get(key, 0)
            if n:
                label = html.escape(VF.VERDICTS[key])
                readout.append(f"<span>{label} <b>{n}</b></span>")
        readout.append(
            f"<span>再取得した出典 <b>{len(run.fetched_ok())}</b></span>")
        readout.append("</p>")
        parts.append("".join(readout))

    failed = [c for c in claims if not c.passed]
    passed = [c for c in claims if c.passed]

    if failed:
        parts.append("<h3>裏が取れなかった記述</h3>")
        parts.append(
            "<p>出典を取り直しても確かめられなかったもの。"
            "<strong>本文に残っているが、確認済みとして読んではいけない。</strong></p>")
        parts.append(verify_rows(failed))
    else:
        parts.append("<h3>裏が取れなかった記述</h3><p>なし。</p>")

    if passed:
        parts.append(
            f"<details><summary>裏付けが取れた記述 {len(passed)}件</summary>"
            + verify_rows(passed) + "</details>")

    urls = "".join(
        f"<li>{ext_link(u, u)}（HTTP {s}）</li>" for u, s in run.urls if u)
    dele = "".join(
        f"<li>{ext_link(u, u)} → <code>{html.escape(t)}</code></li>"
        for u, t in run.delegated if u)
    checked = html.escape(run.run)
    parts.append(
        "<h3>この欄の材料</h3>"
        f"<p>検証した時刻 {checked}。記録は <code>data/verification/{html.escape(code)}"
        ".yaml</code>（append-only）。</p>"
        "<h4>実際に取り直した出典</h4><ul>" + (urls or "<li>なし</li>") + "</ul>"
        "<h4>機械抽出に委ねている出典</h4>"
        "<p>毎週コードが表を抜いて突き合わせているもの。"
        "人が読み直すより、そちらの記録のほうが強い。</p>"
        "<ul>" + (dele or "<li>なし</li>") + "</ul>"
    )
    return "".join(parts)


def render_verification(rep: R.Report, charts: dict) -> str:
    """銘柄ページの末尾。何がどこまで確かめられているかを開示する。"""
    cross = str(rep.meta.get("tanshin_cross_period", "") or "")
    v = CD.verify(rep.code, rep.charts, cross)
    code = html.escape(rep.code)

    readout = (
        '<p class="readout">'
        f"<span>2ソース照合済み <b>{v.fund_ok}</b></span>"
        f"<span>2ソースが食い違う <b>{v.fund_mismatch}</b></span>"
        f"<span>1ソースのみ <b>{v.fund_single}</b></span>"
        f"<span>決算短信から直接 <b>{v.tanshin_rows}</b></span>"
        f"<span>株価の採用終値 <b>{v.price_ok}</b>/{v.price_rows}日</span>"
        "</p>"
    )

    lede = (
        '<p class="lede">'
        "この銘柄の数値が<strong>どこまで機械的に確かめられているか</strong>を開示する。"
        "財務の数値は2つの取得元から別々に抜き、"
        "<strong>値が一致したものだけを採用値</strong>にしている。"
        "一致しなかったもの・1つの取得元しか持っていないものは採用せず、"
        "図では欠測として抜いてある（0 で埋めない）。</p>"
    )

    parts = ["<h2>数値の検証状況</h2>", lede, readout, chart_origin_rows(charts)]

    if v.cross_period:
        parts.append(render_cross(v))

    if v.mismatch_items:
        shown = v.mismatch_items[:12]
        rest = len(v.mismatch_items) - len(shown)
        items = "、".join(html.escape(s) for s in shown)
        if rest > 0:
            items += f"、ほか{rest}件"
        parts.append(
            "<h3>2つの取得元が食い違った項目</h3>"
            "<p>どちらが正しいか決められないため、"
            "<strong>どちらの値も採用していない</strong>。"
            "この項目を本文で断定形で書いてはいけない。</p>"
            f"<p>{items}</p>"
        )

    if v.chart_gaps:
        gaps = "".join(f"<li>{html.escape(g)}</li>" for g in sorted(v.chart_gaps))
        parts.append("<h3>図に穴があるところ</h3><ul>" + gaps + "</ul>")

    src = ""
    if v.tanshin_url:
        src = ext_link(v.tanshin_url, "決算短信PDF")
    dates = "／".join(html.escape(d) for d in v.tanshin_dates) or "—"
    parts.append(
        "<h3>この欄の材料</h3><ul>"
        f"<li>財務数値: <code>data/fundamentals/{code}.csv</code>"
        f"（{v.fund_rows}行）— 2サイトの表を機械的に抜いて突き合わせた結果</li>"
        f"<li>決算短信: <code>data/tanshin/{code}.csv</code>"
        f"（{v.tanshin_rows}行・開示日 {dates}）— PDF本文からの抽出 {src}</li>"
        f"<li>株価: <code>data/prices/daily.csv</code> の <code>close</code>。"
        "始値・高値・安値・出来高は主ソースのみで照合を通っていないため、"
        "図には使っていない</li>"
        "</ul>"
    )
    return "".join(parts)


def build_stock_page(rep: R.Report, as_of: str) -> None:
    (DOCS / "stock").mkdir(parents=True, exist_ok=True)
    name = html.escape(rep.name)
    code = html.escape(rep.code)
    market = html.escape(str(rep.meta.get("market", "")))
    flag = '<span class="flag">深掘り中</span>' if rep.deep_dive else ""

    charts = CD.resolve_charts(rep.code, rep.charts)

    links = "".join(ext_link(str(lk.get("url", "")), str(lk.get("label", "")))
                    for lk in rep.links if lk.get("url"))
    verify_line = verify_headline(rep.code)
    head = (
        f"<h1>{name}（{code}）{flag}</h1>"
        f'<p class="lede">{market}／レポート更新 {html.escape(rep.updated)}'
        f"{verify_line}"
        f"<br>{links}</p>"
        + nav(1)
    )
    lead_md = strip_title(rep.lead)
    body = head + to_html(lead_md, charts)
    for key, title in R.SECTIONS:
        body += render_section(rep, key, title, charts)
    body += render_verify(rep)
    body += render_verification(rep, charts)

    title = f"{rep.name}（{rep.code}）"
    (DOCS / "stock" / f"{rep.code}.html").write_text(
        page(title, body, as_of, 1), encoding="utf-8")


def strip_title(lead: str) -> str:
    """リードから `# 見出し` を落とす（h1 は別に組み立てているため）。"""
    lines = [ln for ln in lead.splitlines() if not ln.startswith("# ")]
    return "\n".join(lines).strip()


# --- データの出どころ -----------------------------------------------------

def verification_rows(master: dict, reports: dict[str, R.Report]) -> tuple[str, dict]:
    """全銘柄の検証状況の表と、合計値。"""
    rows = []
    total = {"ok": 0, "mismatch": 0, "single": 0, "tanshin": 0,
             "price_ok": 0, "price_rows": 0, "verify_ok": 0, "verify_all": 0}
    for s in sorted(master["stocks"], key=lambda x: x["code"]):
        code = s["code"]
        rep = reports.get(code)
        charts = rep.charts if rep is not None else {}
        v = CD.verify(code, charts)
        total["ok"] += v.fund_ok
        total["mismatch"] += v.fund_mismatch
        total["single"] += v.fund_single
        total["tanshin"] += v.tanshin_rows
        total["price_ok"] += v.price_ok
        total["price_rows"] += v.price_rows
        name = html.escape(s.get("name", code))
        if rep is None:
            link = name
        else:
            link = f'<a href="stock/{html.escape(code)}.html">{name}</a>'
        price = f"{v.price_ok}/{v.price_rows}"
        passed, claims, present, stale = verify_stat(code)
        if not present:
            verify_cell = '<span class="pill pill-warn">未検証</span>'
        elif stale:
            # 件数を出さない（本文が書き換わっているので現在の本文に適用できない）
            verify_cell = '<span class="pill pill-warn">記録が古い</span>'
        else:
            verify_cell = f"{passed}/{claims}"
            total["verify_ok"] += passed
            total["verify_all"] += claims
        rows.append(
            f"<tr><td>{html.escape(code)}</td><td>{link}</td>"
            f'<td class="num">{v.fund_ok}</td>'
            f'<td class="num">{v.fund_mismatch}</td>'
            f'<td class="num">{v.fund_single}</td>'
            f'<td class="num">{v.tanshin_rows}</td>'
            f'<td class="num">{price}</td>'
            f'<td class="num">{v.charts_csv}／{v.charts_hand}</td>'
            f'<td class="num">{verify_cell}</td></tr>'
        )
    table = (
        '<div class="scroll"><table><thead><tr>'
        "<th>コード</th><th>銘柄</th><th>照合済み</th><th>食い違い</th>"
        "<th>1ソース</th><th>決算短信</th><th>採用終値</th><th>図 自動／手書き</th>"
        "<th>記述の裏取り</th>"
        "</tr></thead><tbody>" + "".join(rows) + "</tbody></table></div>"
    )
    return table, total


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

    verify_table, total = verification_rows(master, reports)
    checked = total["ok"] + total["mismatch"] + total["single"]
    summary = (
        '<p class="readout">'
        f"<span>2ソース照合済み <b>{total['ok']}</b></span>"
        f"<span>2ソースが食い違う <b>{total['mismatch']}</b></span>"
        f"<span>1ソースのみ <b>{total['single']}</b></span>"
        f"<span>財務数値の合計 <b>{checked}</b></span>"
        f"<span>決算短信から直接 <b>{total['tanshin']}</b></span>"
        f"<span>株価の採用終値 <b>{total['price_ok']}</b>"
        f"/{total['price_rows']}日</span>"
        f"<span>記述の裏取り <b>{total['verify_ok']}</b>"
        f"/{total['verify_all']}件</span>"
        "</p>"
    )

    body = (
        "<h1>データの出どころ</h1>"
        '<p class="lede">数値がどこから来たのかを開示する。'
        "各銘柄の詳しい出典は、それぞれのレポート末尾「出典」にある。</p>"
        + nav(0)
        + "<h2>どこまで確かめられているか</h2>"
        + '<p class="lede">財務の数値は、2つのまとめサイトから'
        "<strong>別々に機械で抜いて突き合わせている</strong>。"
        "値が一致した行だけを採用値にし、食い違った行・片方しか持っていない行は"
        "<strong>採用しないまま残す</strong>。"
        "レポートの図は採用値だけで描き、足りないところは欠測として抜いてある。</p>"
        + summary
        + verify_table
        + '<p class="lede">「図 自動／手書き」は、その銘柄のレポートの図のうち、'
        "検証済みCSVから自動で組み立てたものと、人が front matter に転記したものの数。"
        "手書きの図には、図そのものに「未検証」と表示している。</p>"
        + '<p class="lede">「記述の裏取り」は、<strong>本文の言い回しそのもの</strong>を'
        "別の文脈が出典に当て直した結果（裏付けが取れた件数／検証した件数）。"
        "数値の照合が届かない散文——「導入180社以上」「カバーは0社」のような記述——は"
        "ここで確かめる。裏が取れなかったものは各銘柄ページの"
        "<strong>「記述の裏取り」欄に理由つきで残している</strong>。</p>"
        + "<h2>株価</h2>"
        + '<div class="scroll"><table><thead><tr><th>コード</th><th>銘柄</th>'
        + "<th>終値</th><th>最終営業日</th><th>レポート</th></tr></thead><tbody>"
        + "".join(rows)
        + "</tbody></table></div>"
        + "<h2>集め方の原則</h2><ul>"
        + "<li>株価は複数のサイトから取り、<strong>値が一致したときだけ採用</strong>している。"
        "一致しなかった日は空欄のまま残す（推測で埋めない）</li>"
        + "<li><strong>財務数値にも同じ規律を通している。</strong>"
        "株探と IR BANK の表を機械で抜き、表示されている桁の範囲で一致した値だけを採用する。"
        "同じサイトの別ページどうしが裏付けても、独立した確認ではないので採用しない</li>"
        + "<li>決算短信（PDF・一次情報）からも直接抜いている。"
        "まとめサイトの値と一致するかを機械で確かめ、結果を図の下に出している</li>"
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

    write_stamps(master)

    made = ", ".join(sorted(reports)) or "なし"
    print(f"docs/ を生成（基準日 {as_of}）")
    print(f"  レポートあり: {made}")
    missing = [c for c in codes if c not in reports]
    if missing:
        print(f"  レポート未作成: {', '.join(missing)}")
    report_chart_provenance(reports)
    return 0


def write_stamps(master: dict) -> None:
    """`scoring/stamps.json`（notify.py の唯一の入力）を書く。

    **v2.0 改稿でこの出力が丸ごと落ちていた**。weekly.yml は
    「build.py は docs/ と scoring/stamps.json を出力する」と書き、
    notify.py はそれを唯一の入力にしているのに、書く側が誰もいなかった。
    結果 stamps.json は誰かが手で置いた値のまま凍り、`changed = {}` が
    永久に続いて **判定が変わっても Issue が出ない**状態だった。

    決定論的（D8）: 生成時刻を入れず、キー順を固定する。
    判定の再計算に失敗したときは **書かない**（前回の状態を壊さない・F-04）。
    """
    try:
        verdicts = J.judge_all(master)
    except Exception as e:                                   # noqa: BLE001
        print(f"  [WARN] 判定スタンプを再計算できなかった: "
              f"{type(e).__name__}: {e}。scoring/stamps.json は更新しない")
        return
    stamps = {v.code: v.stamp for v in verdicts}
    if not stamps:
        print("  [WARN] 判定スタンプが0件。scoring/stamps.json は更新しない")
        return
    STAMPS.parent.mkdir(parents=True, exist_ok=True)
    body = json.dumps(stamps, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if STAMPS.exists() and STAMPS.read_text(encoding="utf-8") == body:
        print(f"  判定スタンプ {len(stamps)}件（変化なし）")
        return
    STAMPS.write_text(body, encoding="utf-8")
    print(f"  判定スタンプ {len(stamps)}件を scoring/stamps.json に出力")


def report_chart_provenance(reports: dict[str, R.Report]) -> None:
    """図の出どころを標準出力にも出す（CI のログで気づけるようにする）。

    ページには出しているが、**描けなかった図・手書きのままの図は
    ログでも見えるようにする**。黙って欠けているのが一番まずい。
    """
    for code in sorted(reports):
        charts = CD.resolve_charts(code, reports[code].charts)
        if not charts:
            continue
        csv_n = sum(1 for r in charts.values() if r.origin == "csv")
        hand = sorted(cid for cid, r in charts.items() if r.origin == "hand")
        empty = sorted(cid for cid, r in charts.items() if r.empty_reason)
        gaps = sorted(cid for cid, r in charts.items()
                      if not r.empty_reason and r.missing)
        print(f"  {code}: 図 {len(charts)}件（検証済みCSV {csv_n} / 手書き "
              f"{len(hand)}）")
        if hand:
            print(f"    手書きのまま: {', '.join(hand)}")
        if gaps:
            print(f"    欠測あり: {', '.join(gaps)}")
        if empty:
            print(f"    描けなかった: {', '.join(empty)}")


if __name__ == "__main__":
    raise SystemExit(main())
