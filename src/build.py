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
import datetime as _dt
import html
import json
import re
import sys
from pathlib import Path

import markdown as md

sys.path.insert(0, str(Path(__file__).resolve().parent))

import chart as C
import chartdata as CD
import estimate as EST
import judge as J
import report as R
import verification as VF
import yamlio as Y
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
    ("about.html", "読み方"),
]

DISCLAIMER = ("本サイトは個人の検討用であり、投資助言ではありません。"
              "売買の判断は人間が行います。数値はすべて出所と取得日を併記しています。")


# --- 骨組み ---------------------------------------------------------------

def site_header(depth: int = 0) -> str:
    """全ページ共通の固定ヘッダー。depth はサブディレクトリの深さ
    （stock/ 配下は depth=1）。ページ内に nav を重ねて置かない。"""
    prefix = "../" * depth
    links = []
    for href, label in NAV_ITEMS:
        links.append(f'<a href="{prefix}{href}">{html.escape(label)}</a>')
    return ('<header class="site"><div class="site-in">'
            f'<a class="brand" href="{prefix}index.html">銘柄調査台帳</a>'
            "<nav>" + "".join(links) + "</nav></div></header>")


def page(title: str, body: str, as_of: str, depth: int = 0,
         wide: bool = False) -> str:
    """wide=True は PC の広い画面で本文列を広げる（一覧が主役のトップ用。
    読み物が主役の銘柄ページ・説明ページは 52rem のまま）。"""
    esc_title = html.escape(title)
    foot = f"<footer>集計基準日 {html.escape(as_of)}／{DISCLAIMER}</footer>"
    head = (
        '<!DOCTYPE html><html lang="ja"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<meta name="robots" content="noindex,nofollow">'
        f"<title>{esc_title}</title><style>{CSS}</style></head>"
    )
    main_cls = ' class="wide"' if wide else ""
    return (f"{head}<body>{site_header(depth)}"
            f"<main{main_cls}>{body}{foot}</main></body></html>")


# 検証状態の記号（表記規約 2026-08-23）。本文の値の直後に付ける。
# 記号 -> (クラス, 意味)。意味は title 属性と docs/about.html の凡例が持つ。
VM_BADGES = {
    "✓": ("vm-ok", "2ソース照合済みの採用値（status OK）"),
    "※": ("vm-ref", "未照合・参考値（SINGLE_SOURCE / MISMATCH / 出来高・信用残）"),
    "†": ("vm-pri", "決算短信（一次情報）から機械抽出。まとめサイトとの2ソース照合なし"),
}
_VM_RE = re.compile("[" + "".join(VM_BADGES) + "]")
_CODE_SPLIT_RE = re.compile(r"(<code>.*?</code>)", re.S)


def mark_badges(html_text: str) -> str:
    """検証状態の記号 ✓※† に、意味の tooltip 付きバッジを着せる。

    **expand_charts の前に呼ぶこと。** 図（SVG の <text> と figcaption）は
    chart.render が後から差し込むため、この置換の対象にならない
    （SVG 内に <span> が入ると描画が壊れる。図キャプションはプレーン文字でよい）。
    """
    def sub(m: re.Match) -> str:
        ch = m.group(0)
        cls, title = VM_BADGES[ch]
        return f'<span class="vm {cls}" title="{html.escape(title)}">{ch}</span>'
    # <code>…</code> の中は置換しない（ファイル名やコマンドの中の記号は
    # 検証状態の印ではない）。split の偶数番目だけが code の外。
    parts = _CODE_SPLIT_RE.split(html_text)
    for i in range(0, len(parts), 2):
        parts[i] = _VM_RE.sub(sub, parts[i])
    return "".join(parts)


# Markdown の自動リンク `<https://…>` は `<a href="U">U</a>` になる
# （href と本文が同一文字列。手書きの `[ラベル](U)` はここに当たらない）。
_AUTOLINK_RE = re.compile(r'<a href="([^"]+)">\1</a>')

# よく使う取得元は名前で呼ぶ。それ以外はホスト名（www を除く）で出す。
# サブドメイン（s.kabutan.jp 等）も同じ運営なので同じ名前に寄せる。
_HOST_LABELS = [
    ("kabutan.jp", "株探"),
    ("minkabu.jp", "みんかぶ"),
    ("irbank.net", "IR BANK"),
    ("release.tdnet.info", "TDnet"),
    ("nikkei.com", "日経"),
]


def short_link_label(url: str) -> str:
    """生URLの代わりに出す短いラベル。ホスト名から決める（決定論的）。"""
    m = re.match(r"https?://([^/?#]+)", url)
    host = (m.group(1) if m else url).split("@")[-1].split(":")[0].lower()
    bare = host[4:] if host.startswith("www.") else host
    for dom, label in _HOST_LABELS:
        if bare == dom or bare.endswith("." + dom):
            return label
    return bare


def shorten_autolinks(html_text: str) -> str:
    """本文に生URLをそのまま出さない。リンク先（href）は変えない。

    対象は「リンクテキストが URL そのもの」の自動リンクだけ。
    書き手がラベルを付けたリンクには触らない。見た目は ext_link と同じ
    （小さなピル・↗・別タブ）に揃える。
    """
    def sub(m: re.Match) -> str:
        href = m.group(1)                       # markdown が escape 済み
        label = short_link_label(html.unescape(href))
        return (f'<a class="ext" href="{href}" target="_blank" '
                f'rel="noopener">{html.escape(label)} ↗</a>')
    return _AUTOLINK_RE.sub(sub, html_text)


def to_html(markdown_text: str, charts: dict | None = None) -> str:
    """Markdown を HTML にする。表を横スクロールで包み、{{chart:id}} を図にする。

    charts は chartdata.resolve_charts の戻り（id -> Resolved）。
    記号バッジとURL短縮は **expand_charts より前** に適用する
    （SVG の中身を置換しないため。mark_badges の docstring 参照）。
    """
    raw = md.markdown(markdown_text, extensions=MD_EXT)
    # prose-table: 本文（出典の「内容」列など）が1列目に来るので折り返させる。
    # 付けないと style.py の `td:first-child{white-space:nowrap}` に当たって
    # 表が画面外まで伸びる。
    out = raw.replace("<table>", '<div class="scroll"><table class="prose-table">'
                      ).replace("</table>", "</table></div>")
    out = shorten_autolinks(mark_badges(out))
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
    return Y.safe_load(path.read_text(encoding="utf-8"))


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


def load_adopted_series(code: str) -> list[tuple[str, float]]:
    """その銘柄の採用終値（status に OK がある行だけ）を日付昇順で返す。

    `close` の有無で数えない（D53。判定は `chartdata.adopted_close` に一本化し、
    LEGACY_NO_TRADE の7行が混ざらないようにする）。
    """
    path = ROOT / "data" / "prices" / "daily.csv"
    if not path.exists():
        return []
    rows: list[tuple[str, float]] = []
    with path.open(encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if row.get("code") != code:
                continue
            v = CD.adopted_close(row)
            if v is not None:
                rows.append((row["date"], v))
    rows.sort()
    return rows


def _tile(label: str, value_html: str, sub: str) -> str:
    return ('<div class="kpi-tile">'
            f'<span class="k-label">{html.escape(label)}</span>'
            f'<span class="k-value">{value_html}</span>'
            f'<span class="k-sub">{html.escape(sub)}</span></div>')


def load_stamp(code: str) -> str | None:
    """`scoring/stamps.json` からその銘柄の判定スタンプを引く。

    無い・読めない・銘柄が載っていないときは None（タイルを出さない）。
    推測で埋めない——スタンプは judge.py の機械判定だけが書く。
    """
    try:
        stamps = json.loads(STAMPS.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    v = stamps.get(code) if isinstance(stamps, dict) else None
    return str(v) if v else None


def _future_earnings(meta: dict | None, as_of: str) -> str:
    """next_earnings が集計基準日より先のときだけ返す（過去日付は予定ではない）。"""
    earn = str((meta or {}).get("next_earnings") or "")
    return earn if earn and (not as_of or as_of == "—" or earn > as_of) else ""


def kpi_tiles(code: str, meta: dict | None = None, as_of: str = "") -> str:
    """銘柄ページ冒頭の KPI タイル。値はすべて検証済みデータ由来（D8 決定論）。

    - 終値・レンジは採用終値（2ソース照合成立日）だけで組む。D53
    - 前日比は「直前の採用日」との比較（暦日の前日ではない）。sub に日付を明示
    - 騰落の色は図と同じ意味づけ（青=上・赤=下）。符号も必ず出す
    - meta はレポートの front matter（next_earnings を引くためだけに使う）
    """
    series = load_adopted_series(code)
    if not series:
        return ""
    last_date, last_close = series[-1]

    tiles = [_tile("採用終値",
                   f'{last_close:,.0f}<span class="k-unit">円</span>',
                   f"{last_date}・2ソース照合済み")]

    if len(series) >= 2:
        prev_date, prev_close = series[-2]
        if prev_close:
            pct = (last_close - prev_close) / prev_close * 100
            cls = "chg-pos" if pct >= 0 else "chg-neg"
            tiles.append(_tile("前採用日比",
                               f'<span class="{cls}">{pct:+.1f}%</span>',
                               f"{prev_date} 比"))

    # 52週 = 最新採用日から遡って365日（実行日は使わない。D8）
    y, m, d = (int(x) for x in last_date.split("-"))
    since = f"{y - 1:04d}-{m:02d}-{d:02d}"
    window = [c for dt, c in series if dt >= since]
    if window:
        lo, hi = min(window), max(window)
        tiles.append(_tile("52週レンジ",
                           f'<span class="k-value-sm">{lo:,.0f} 〜 {hi:,.0f}</span>',
                           "採用終値ベース・円"))

    # 判定スタンプ。無ければ出さない（未計算を「通過」に見せない）
    stamp = load_stamp(code)
    if stamp:
        tiles.append(_tile("判定",
                           f'<span class="k-value-sm">{html.escape(stamp)}</span>',
                           "src/judge.py の機械判定"))

    # 次回決算。front matter に書かれた予定日。**過去日付は出さない**
    # （発表済みの日付を「次回」として掲げるのは表示の嘘。intake が
    # next_earnings を更新するまでタイルを消しておく）
    earn = _future_earnings(meta, as_of)
    if earn:
        tiles.append(_tile("次回決算",
                           f'<span class="k-value-sm">{html.escape(earn)}</span>',
                           "発表予定日"))

    passed, claims, present, stale = verify_stat(code)
    if not present:
        tiles.append(_tile("記述の裏取り",
                           '<span class="k-value-sm">未検証</span>', "記録なし"))
    elif stale:
        tiles.append(_tile("記述の裏取り",
                           '<span class="k-value-sm">記録が古い</span>',
                           "本文が更新されてから未実施"))
    else:
        tiles.append(_tile("記述の裏取り",
                           f'{passed}<span class="k-unit">/{claims}</span>',
                           "裏付けあり／全記述"))

    return '<div class="kpi">' + "".join(tiles) + "</div>"


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
    # Markdown 記号の除去。「-」を全域で消すと **マイナス記号まで消え**、
    # 「週間 -0.7%」が「週間 0.7%」と符号ごと反転して見える（表示の嘘）。
    # 箇条書き・引用・見出しの記号は行頭だけ落とし、行中の - は残す。
    plain = re.sub(r"(?m)^\s*[->#]+\s*", "", text)
    plain = re.sub(r"[*_`#>]", "", plain).strip()
    plain = re.sub(r"\s+", " ", plain)
    # 「（台帳への新規登録週。…）」のような括弧書きの冒頭は、1文で切ると
    # 閉じ括弧が落ちて「（台帳への新規登録週。」と壊れて見える。開き括弧を落とす
    if plain.startswith("（"):
        plain = plain[1:]
    for sep in ("。", "．"):
        if sep in plain:
            plain = plain.split(sep)[0] + sep
            break
    if len(plain) > limit:
        plain = plain[: limit - 1] + "…"
    return plain


def week_change(series: list[tuple[str, float]]) -> tuple[str, float] | None:
    """前週末比。最新採用日の週（月曜起点）より前の、最後の採用終値と比べる。

    実行日は使わない（D8）。両端とも2ソース照合済みの採用値なので断定形でよい。
    前週の採用値が無い（新規登録直後など）ときは None（出さない）。
    """
    if len(series) < 2:
        return None
    d0, c0 = series[-1]
    day = _dt.date.fromisoformat(d0)
    monday = (day - _dt.timedelta(days=day.weekday())).isoformat()
    prev = [(d, c) for d, c in series if d < monday]
    if not prev or not prev[-1][1]:
        return None
    d1, c1 = prev[-1]
    return d1, (c0 - c1) / c1 * 100.0


SPARK_DAYS = 60   # 一覧のスパークラインに使う採用日数（約3か月）


def sparkline(series: list[tuple[str, float]]) -> str:
    """一覧に置く小さな値動きの線。採用終値（2ソース照合済み）だけで描く。

    数値軸・目盛は持たない（傾向の手がかり。数値は銘柄ページの図が正）。
    座標は固定小数1桁で出す（D8 決定論）。
    """
    pts = series[-SPARK_DAYS:]
    if len(pts) < 2:
        return ""
    vals = [c for _, c in pts]
    lo, hi = min(vals), max(vals)
    w, h, pad = 120, 30, 3
    xs = [pad + i * (w - 2 * pad) / (len(pts) - 1) for i in range(len(pts))]
    if hi == lo:
        ys = [h / 2.0] * len(pts)
    else:
        ys = [pad + (hi - v) * (h - 2 * pad) / (hi - lo) for v in vals]
    d = " ".join(f"{'M' if i == 0 else 'L'}{x:.1f} {y:.1f}"
                 for i, (x, y) in enumerate(zip(xs, ys)))
    label = html.escape(
        f"{pts[0][0]}〜{pts[-1][0]} の採用終値 {len(pts)}日"
        "（2ソース照合済みの値のみ。数値は銘柄ページの図で）")
    return (f'<svg class="spark" viewBox="0 0 {w} {h}" role="img" '
            f'aria-label="{label}"><title>{label}</title>'
            f'<path d="{d}" fill="none" stroke="currentColor" stroke-width="1.5" '
            'stroke-linejoin="round" stroke-linecap="round"/>'
            f'<circle cx="{xs[-1]:.1f}" cy="{ys[-1]:.1f}" r="2.4" '
            'fill="currentColor"/></svg>')


def stamp_pill(stamp: str) -> str:
    """一覧に出す判定スタンプ。色だけに意味を持たせない（語が常に見える）。"""
    cls = "stamp"
    if stamp == J.STAMP_BUY:
        cls += " stamp-buy"
    elif stamp == J.STAMP_SELL:
        cls += " stamp-sell"
    elif stamp == J.STAMP_OVERHEAT:
        cls += " stamp-hot"
    # 「判定 」の接頭辞はコンパクト表示では CSS で省く（.st-p）
    return (f'<span class="{cls}" title="src/judge.py の機械判定">'
            f'<span class="st-p">判定 </span>{html.escape(stamp)}</span>')


# 判定スタンプ → 行クラス・絞り込みチップの固定キー（語彙は judge.py が正。
# CSS 側は全キーぶんの規則を静的に持つので、キーをここで増やしたら
# style.py 末尾の絞り込み規則にも同じキーを足すこと）
STAMP_KEYS = {
    J.STAMP_BUY: "buy", J.STAMP_WATCH: "watch", J.STAMP_PROBE: "probe",
    J.STAMP_OVERHEAT: "hot", J.STAMP_SELL: "sell",
    J.STAMP_LIQUIDITY: "liq", J.STAMP_TREND: "trend",
}
VF_LABELS = {"ok": "裏取り済", "part": "裏取り未達あり",
             "stale": "裏取りが古い", "none": "裏取り未実施"}


def render_row(stock: dict, rep: R.Report | None,
               as_of: str = "") -> tuple[str, str | None, str, str]:
    """一覧の1行。(HTML, 判定スタンプ, 判定キー, 裏取りキー) を返す。

    キーは絞り込みチップの件数集計と行クラスに使う（対象外の行は空。
    対象外は f-excluded のトグルだけが束ねる）。
    スマホでは CSS 側でカード状に積み替える（横スクロールさせない）。
    """
    code = stock["code"]
    name = html.escape(stock.get("name", code))
    market = html.escape(str(stock.get("market", "")))
    series = load_adopted_series(code)
    if series:
        date, close = series[-1]
        close_txt = f"{close:,.0f}"
    else:
        date, close_txt = "—", "—"

    # 監視から外した銘柄は**消さずに、外したと分かる形で**残す。
    # 取得も判定も止まっているので、印が無いと「先週と同じ」が
    # 「変わっていない」に読めてしまう（手書き図に「未検証」を必ず出すのと同じ規律）。
    watch_pill = ""
    if not Y.is_watched(stock):
        why = html.escape(" ".join(str(stock.get("watch_reason") or "").split()))
        since = html.escape(str(stock.get("watch_since") or ""))
        watch_pill = ('<span class="pill pill-warn" title="' + why + '">'
                      + "対象外" + (f"（{since}〜）" if since else "")
                      + "・更新を止めている</span>")

    # 判定・裏取りの状態を先に確定する（行クラスと絞り込みチップの材料）。
    # 対象外はキーを付けない——凍った記録をチップの母数に混ぜない
    passed, claims, present, stale = verify_stat(code)
    st: str | None = None
    st_key = vf_key = ""
    if not watch_pill:
        st = load_stamp(code)
        if st:
            st_key = STAMP_KEYS.get(st, "other")
        vf_key = ("none" if not present else "stale" if stale
                  else "ok" if passed == claims else "part")
    classes = " ".join(
        (["row-excluded"] if watch_pill else [])
        + ([f"st-{st_key}"] if st_key else [])
        + ([f"vf-{vf_key}"] if vf_key else []))
    tr_cls = f' class="{classes}"' if classes else ""

    # 終値セル。監視中の銘柄には前週末比・スパークライン・判定も重ねて、
    # 一覧だけで「いま見に行く価値があるか」を判断できるようにする。
    # 対象外は凍った記録なので終値と日付だけにする（動きの表現を足さない）。
    price_cell = (f'<span class="price">{close_txt}</span>'
                  f'<span class="sub">{html.escape(date)}</span>')
    if not watch_pill:
        wc = week_change(series)
        if wc is not None:
            d1, pct = wc
            cls = "chg-pos" if pct >= 0 else "chg-neg"
            price_cell += (f'<span class="wchg {cls}" '
                           f'title="前週末（{html.escape(d1)}）の採用終値比">'
                           f"前週末比 {pct:+.1f}%</span>")
        price_cell += sparkline(series)
        if st:
            price_cell += stamp_pill(st)

    if rep is None:
        row = (
            f'<tr{tr_cls}><td data-l="銘柄"><span class="nm">{name}</span>'
            f'<span class="sub">{html.escape(code)}／{market}／{watch_pill}</span></td>'
            f'<td data-l="終値・判定" class="num">{price_cell}</td>'
            f'<td data-l="状態"><span class="pill">レポート未作成</span></td>'
            f"</tr>"
        )
        return row, st, st_key, vf_key

    flag = '<span class="flag">再調査</span>' if rep.deep_dive else ""
    site = ""
    for lk in rep.links:
        if lk.get("primary"):
            site = ext_link(str(lk.get("url", "")), str(lk.get("label", "公式")))
            break

    # 「一行でいうと」はレポート本文の引用ブロックなので Markdown の強調を含む。
    # 銘柄ページ側は Markdown を通すが、一覧はエスケープしかしていなかったため
    # `**` がそのまま画面に出ていた。**エスケープしたあとに**強調だけ戻す
    # （順序が逆だと `<strong>` ごとエスケープされる／注入経路にもなる）。
    oneline = mark_badges(_STRONG_RE.sub(r"<strong>\1</strong>",
                                         html.escape(R.one_liner(rep))))
    earn = _future_earnings(rep.meta, as_of)   # 過去日付は予定として出さない
    earn_pill = ""
    if earn:
        earn_pill = (f'<span class="pill pill-warn">決算 '
                     f'{html.escape(earn)}</span>')

    latest = rep.latest_week()
    week_txt = "—"
    week_head = ""
    if latest is not None:
        week_head = html.escape(latest[0].split("（")[0])
        week_txt = mark_badges(html.escape(first_sentence(latest[1])))

    # 裏取りの状態を一覧にも出す。銘柄ページを開かないと分からない状態にしない。
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

    # data-href: 行のどこを押しても銘柄ページへ飛ばす（小さな委譲スクリプトが
    # 拾う。リンク・ボタンの上と、文字列選択中は飛ばない）。銘柄名の <a> は
    # 残す——JS が無くても届く経路であり、新しいタブで開く操作も効く
    row = (
        f'<tr{tr_cls} data-href="stock/{html.escape(code)}.html">'
        f'<td data-l="銘柄"><span class="nm">'
        f'<a href="stock/{html.escape(code)}.html">{name}</a>{flag}{site}</span>'
        f'<span class="sub">{html.escape(code)}／{market}／{watch_pill}{earn_pill}'
        f"{verify_pill_html}</span>"
        f'<span class="one">{oneline}</span></td>'
        f'<td data-l="終値・判定" class="num">{price_cell}</td>'
        f'<td data-l="今週"><span class="sub">{week_head}</span>'
        f'<span class="wk-txt">{week_txt}</span></td>'
        f"</tr>"
    )
    return row, st, st_key, vf_key


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
    # 監視対象と対象外を分ける。対象外は**既定で畳む**が、消しはしない。
    # 件数を summary に常時出すので「見えない＝無い」にはならない
    # （閉じた details は Ctrl+F でも当たらないため、件数の明示が生命線）。
    watched = [x for x in stocks if Y.is_watched(x)]
    excluded = [x for x in stocks if not Y.is_watched(x)]
    # 行は**コード順のまま1本**にする。フィルターを外すと対象外が元の位置に戻る。
    row_data = [render_row(x, reports.get(x["code"]), as_of) for x in stocks]
    rows = [r[0] for r in row_data]
    rows_excluded = excluded
    # 絞り込みチップの件数（監視中のみ。対象外はキーを持たない）
    st_counts: dict[str, tuple[str, int]] = {}
    vf_counts: dict[str, int] = {}
    for _, st, st_key, vf_key in row_data:
        if st_key:
            _, n = st_counts.get(st_key, (st, 0))
            st_counts[st_key] = (str(st), n + 1)
        if vf_key:
            vf_counts[vf_key] = vf_counts.get(vf_key, 0) + 1
    scr = master.get("screening", {})
    scr_name = html.escape(str(scr.get("name", "")))
    n_deep = sum(1 for r in reports.values() if r.deep_dive)

    # トップは繰り返し見るページ。説明の長文は about.html に寄せ、ここは短く保つ
    intro = (
        "<h1>銘柄調査台帳</h1>"
        f'<p class="lede">楽天証券スクリーニング「{scr_name}」の通過銘柄を調査し、'
        '週次で記録する。売買の判断は人間が行う。読み方・記号の意味は'
        '<a href="about.html">「読み方」</a>へ。</p>'
    )

    summary = (
        '<div class="kpi">'
        + _tile("監視中", f'{len(watched)}<span class="k-unit">銘柄</span>',
                (f"ほかに対象外 {len(excluded)}銘柄（既定で隠す。一覧のボタンで表示）"
                 if excluded else "対象外は無し"))
        + _tile("レポートあり", f"{len(reports)}", "調査済みの銘柄数")
        + _tile("再調査", f"{n_deep}", "全節を見直しなおす対象")
        + _tile("基準日",
                f'<span class="k-value-sm">{html.escape(as_of)}</span>',
                "確定している最後の営業日")
        + "</div>"
    )

    # 表は1つ。対象外の行は `row-excluded` を持ち、**既定では CSS で隠す**。
    # 先頭のフィルターボタン（チェックボックス＋label・JS なし）を押すと出る。
    #
    # 隠した行は Ctrl+F でも当たらないので、**ボタンに件数を出すのが生命線**。
    # 「見えない＝無い」に見せないという規律は、畳むときも隠すときも同じ。
    # 表示の切り替えは checkbox + label（input は label の直前。行の表示は
    # CSS 末尾の :has() 規則が引く）。下の小さなスクリプトは**選んだ表示を
    # localStorage に覚えるだけ**の上乗せで、JS が無くてもボタン自体は動く
    # （決定論的な固定文字列。D8）。
    def _toggle(cid: str, on: str, off: str, checked: bool = False,
                cls: str = "filter-btn", title: str = "") -> str:
        chk = " checked" if checked else ""
        t = f' title="{html.escape(title)}"' if title else ""
        return (f'<input type="checkbox" id="{cid}" class="filter-toggle"{chk}>'
                f'<label for="{cid}" class="{cls}"{t}>'
                f'<span class="f-on">{html.escape(on)}</span>'
                f'<span class="f-off">{html.escape(off)}</span></label>')

    # 既定はコンパクト（checked）。詳細はボタンで開く
    toolbar = _toggle("v-compact", "コンパクト表示", "詳細表示", checked=True)
    note = ""
    if rows_excluded:
        n = len(rows_excluded)
        toolbar += _toggle("f-excluded", f"対象外 {n}銘柄を表示",
                           f"対象外 {n}銘柄を隠す")
        note = ('<span class="filter-note">対象外は取得も判定も止めている。'
                "数値と判定はその時点で凍ったもの</span>")
    filter_ui = '<div class="list-toolbar">' + toolbar + note + "</div>"

    # 絞り込みチップ（既定=すべて表示。外すとその行を隠す）。
    # 見えない行があっても件数はチップに常時出る（「見えない＝無い」にしない）
    chips = []
    st_order = [STAMP_KEYS[s] for s in J.STAMPS if STAMP_KEYS[s] in st_counts]
    if "other" in st_counts:
        st_order.append("other")
    if st_order:
        chips.append('<span class="fl-cap">判定</span>')
        for key in st_order:
            lbl, n = st_counts[key]
            chips.append(_toggle(f"f-st-{key}", f"{lbl} {n}", f"{lbl} {n}",
                                 checked=True, cls="filter-btn chip",
                                 title="外すと、この判定の行を隠す"))
    vf_order = [k for k in ("ok", "part", "stale", "none") if k in vf_counts]
    if vf_order:
        chips.append('<span class="fl-cap">裏取り</span>')
        for key in vf_order:
            lbl = f"{VF_LABELS[key]} {vf_counts[key]}"
            chips.append(_toggle(f"f-vf-{key}", lbl, lbl,
                                 checked=True, cls="filter-btn chip",
                                 title="外すと、この裏取り状態の行を隠す"))
    if chips:
        filter_ui += ('<div class="list-toolbar list-filters">'
                      + "".join(chips) + "</div>")

    remember = (
        "<script>(function(){try{"
        'document.querySelectorAll(".list-wrap .filter-toggle")'
        ".forEach(function(el){"
        'var k="kabu:"+el.id,v=localStorage.getItem(k);'
        'if(v==="1")el.checked=true;if(v==="0")el.checked=false;'
        'el.addEventListener("change",function(){'
        'localStorage.setItem(k,el.checked?"1":"0")})});'
        # 行のどこを押しても銘柄ページへ（リンクの上と文字列選択中は除く）
        'var t=document.querySelector(".list-table");'
        'if(t)t.addEventListener("click",function(e){'
        'if(e.target.closest("a,label,input"))return;'
        "var s=window.getSelection&&window.getSelection();"
        "if(s&&String(s).length)return;"
        'var tr=e.target.closest("tr[data-href]");'
        'if(tr)location.href=tr.getAttribute("data-href")});'
        "}catch(e){}})();</script>")

    table = (
        '<div class="list-wrap">' + filter_ui
        + '<div class="scroll"><table class="list-table prose-table"><thead><tr>'
        "<th>銘柄</th><th>終値・判定</th><th>今週の動き</th>"
        "</tr></thead><tbody>" + "".join(rows) + "</tbody></table></div></div>"
        + remember
    )

    # 読み方の説明と「見ていない鉄則」は about.html に置く。トップは繰り返し
    # 見るページなので、毎回同じ長文を下に積まない（howto_block / unevaluated_block）。
    body = intro + summary + table
    (DOCS / "index.html").write_text(
        page("銘柄調査台帳", body, as_of, 0, wide=True),
        encoding="utf-8", newline="\n")


def howto_block() -> str:
    """台帳一覧の読み方。about.html に置く（トップには積まない）。"""
    # 節の並びは `report.SECTIONS` が正。ここに順序を手書きすると、並びを変えた
    # 週に案内文だけが取り残される（「週次アップデートを最上部に」で実際に起きた）。
    order = html.escape(section_order_text())
    return (
        "<h2>台帳一覧の読み方</h2>"
        "<ul>"
        "<li>行のどこを押しても、その銘柄の調査レポートが開く"
        "（「IR情報」など小さなリンクの上だけは、そのリンク先へ）</li>"
        "<li>一覧は既定でコンパクト（1行1銘柄）。「詳細表示」ボタンで"
        "概要文とスパークラインが開く</li>"
        "<li>「判定」「裏取り」のチップを外すと、その状態の行を一時的に隠せる"
        "（件数はチップに常時出る）。選んだ表示・絞り込みは次回も覚えている</li>"
        "<li>対象外の銘柄は既定で隠している。「対象外を表示」ボタンで元の位置に出る"
        "（記録は消えない。<a href=\"data.html\">データの出どころ</a>には常に全銘柄が載る）</li>"
        "<li>レポートは<strong>「週次アップデート」と「会社概要」の2つ</strong>に"
        "畳んである。見出しを押すと開く</li>"
        f"<li>節は <strong>{order}</strong> の順に並んでいる</li>"
        "<li>「判定」の札は <code>src/judge.py</code> の機械判定。「買」は実装済みの"
        'ゲートを通過したという意味しかない（下の<a href="#unevaluated">'
        "「この台帳が見ていない鉄則」</a>を併読）。売買の判断は人間が行う</li>"
        "<li>終値の下の小さな線は直近約3か月の採用終値"
        "（2ソース照合済みの値のみ）。傾向の手がかりで、数値は銘柄ページの図が正</li>"
        '<li><span class="flag">再調査</span> が付いた銘柄は毎週すべての項目を'
        "見直している。付いていない銘柄はニュースと値動きだけ追っている</li>"
        "<li>再調査の対象を変えたいときは Claude に「4073 を再調査して」と言えばよい</li>"
        "<li>銘柄名の横の小さなリンクは会社の公式サイト。"
        "各レポートの末尾には、使ったすべての出典 URL を載せている</li>"
        "</ul>"
    )


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


# 週次アップデートを畳まずに見せる週数。それ以前は details に畳む
# （積み上がるほどページが下に伸び、会社概要へ届かなくなるため）。
RECENT_WEEKS = 3


def _update_card(head: str, body_md: str, charts: dict) -> str:
    return ('<div class="upd">'
            f"<h3>{html.escape(head)}</h3>"
            + to_html(body_md, charts) + "</div>")


def render_updates(rep: R.Report, title: str, charts: dict) -> str:
    """週次アップデートは新しい週を上に。過去の記述は残したまま並べる。

    新しい RECENT_WEEKS 件だけ開いて見せ、それ以前は折りたたむ
    （畳んでも中身の存在と件数は summary で必ず伝える。「見えない＝無い」に
    見せない）。見出し（h2）は付けない。銘柄ページでは折りたたみ（fold）の
    summary がグループ名を兼ねるため。
    """
    entries = rep.week_entries()
    if not entries:
        return ""
    parts = ['<p class="lede">新しい週を上に置いている。'
             "過去の記述は書き換えず、そのまま残している。</p>"]
    for head, body_md in entries[:RECENT_WEEKS]:
        parts.append(_update_card(head, body_md, charts))
    older = entries[RECENT_WEEKS:]
    if older:
        parts.append('<details class="upd-old"><summary>'
                     f"それ以前の週次アップデート（{len(older)}件）</summary>")
        for head, body_md in older:
            parts.append(_update_card(head, body_md, charts))
        parts.append("</details>")
    return "".join(parts)


def fold(title: str, hint: str, inner: str, wide: bool = False) -> str:
    """銘柄ページの大分類。既定は閉じておく（開閉は読み手の操作。JSは使わない）。

    閉じていても、中身の存在と量は summary の hint で必ず伝える
    （「見えない＝無い」に見せない）。wide は PC で本文列より広く使う
    （表・タイル主体のグループ用。散文主体のグループには使わない）。
    """
    if not inner.strip():
        return ""
    cls = "sec sec-wide" if wide else "sec"
    return (f'<details class="{cls}">'
            f'<summary><span class="sec-title">{html.escape(title)}</span>'
            f'<span class="sec-hint">{html.escape(hint)}</span></summary>'
            f'<div class="sec-body">{inner}</div></details>')


# --- 検証状況（この銘柄の数値がどこまで確かめられているか） -----------------

ORIGIN_JA = {
    "csv": "検証済みCSVから自動で組み立て",
    "hand": "front matter に人が書き写した値",
    "diagram": "定性図（数値を含まない）",
}


def chart_origin_rows(charts: dict) -> str:
    """図ごとの出どころ。1行1図で、CSV由来と手書きを並べて見せる。"""
    rows = []
    for cid in sorted(charts):
        res = charts[cid]
        origin = ORIGIN_JA.get(res.origin, res.origin)
        if res.origin == "diagram":
            # 数値を持たない構造図。検証の対象になる数値が無いので
            # 「未検証」の警告色ではなく中立のピルで出す（数値が混ざった
            # diagram は chartdata 側が描画を拒否する）
            pill = '<span class="pill">定性図</span>'
            state = "—"
        elif res.origin == "hand":
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
    # 手法の説明は about.html に一元化。ここは1行に留める（このページは銘柄
    # ごとに繰り返し読まれるため。リンクは stock/ 配下からなので ../）
    lede = (
        '<p class="lede">本文の記述を、書いたのとは別の文脈が出典URLを取り直して'
        '1件ずつ判定した結果（<a href="../about.html">読み方</a>）。</p>'
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

    # 手法の説明は about.html に一元化。ここは1行に留める
    lede = (
        '<p class="lede">2つの取得元で一致した値だけを採用し、欠測は0で埋めない。'
        'この銘柄でどこまで確かめられているかの開示'
        '（<a href="../about.html">読み方</a>）。</p>'
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


# --- 次期売上・利益推定（フェルミ。計算は estimate.py＝コードが行う） --------

_BASIS_PILL = {
    "actual": '<span class="pill pill-good">実績</span>',
    "disclosed": '<span class="pill">開示（一次）</span>',
    "assumed": '<span class="pill pill-warn">推定</span>',
}


def _fmt_m(v) -> str:
    return f"{v:,.0f}" if v is not None else "—"


def _rel_pct(value, base):
    """base に対する value の乖離率（%）。比べられないときは None。

    分母は **絶対値**（estimate._delta_pct と同じ規律）。会社計画・市場予想・実績の
    営業利益は赤字＝負になり得て、`value / base - 1` のままだと符号が反転する
    （計画 -50 に対し推定 +100 が「-300%」と、上振れなのに赤く出る）。
    0 と None は「比べない」に倒す（0 除算と、ゼロ計画の無限大表示を出さない）。
    """
    if value is None or base is None or base == 0:
        return None
    return (value - base) / abs(base) * 100.0


def _pct_span(pct) -> str:
    """乖離率を色つきで。None は '—'（符号も色も付けない）。"""
    if pct is None:
        return "—"
    cls = "chg-pos" if pct >= 0 else "chg-neg"
    return f'<span class="{cls}">{pct:+.1f}%</span>'


def _est_sens_row(x: dict) -> str:
    """感度表の1行。estimate.sensitivity の行をそのまま受ける。

    - segment は op_margin（全社）のとき None。`str()` すると "None" と表示される
    - delta_op_pct は +10% で分母が 0 になる端や営業利益 0 のとき None になり得て、
      書式指定（:+.1f）に渡すと TypeError でページ生成ごと落ちる
    - trivial（op_margin）は自明な +10% だと分かる形で出す
    """
    seg = x.get("segment")
    seg_html = html.escape(str(seg)) if seg else "全社"
    d = x.get("delta_op_pct")
    d_html = "—" if d is None else f"{d:+.1f}%"
    if x.get("trivial"):
        seg_html += ' <span class="pill pill-warn">定義上</span>'
        d_html += f'<br><span class="k-sub">{html.escape(EST.TRIVIAL_NOTE)}</span>'
    return (f'<tr><td><code>{html.escape(str(x.get("var", "")))}</code></td>'
            f'<td>{seg_html}</td><td class="num">{d_html}</td></tr>')


def _est_source(src: str) -> str:
    src = str(src or "")
    if src.startswith("http"):
        return ext_link(src.split("（")[0].strip(), "出典")
    return f"<code>{html.escape(src)}</code>" if src else ""


def render_estimate(code: str) -> str:
    """「次期売上・利益推定」の折りたたみ。

    値の選定は人間/LLM（basis を必ず明示）、計算・感度・比較は estimate.py。
    的中を競う頁ではなく、仮定（推定ピル）を疑うための頁。
    """
    data = EST.load_estimate(code, root=ROOT)
    if data is None:
        inner = ('<p class="none">推定モデルは未作成。'
                 "「この銘柄の推定モデルを組んで」と Claude に言えば起案できる"
                 "（手順は <code>.claude/skills/kabu-ledger-estimate/</code>）。</p>")
        return fold("次期売上・利益推定", "まだ作成していない", inner)
    if data.get("errors"):
        errs = "".join(f"<li>{html.escape(e)}</li>" for e in data["errors"])
        inner = ('<p class="none"><span class="pill pill-danger">読めない</span> '
                 f"推定ファイルに形式エラーがある:</p><ul>{errs}</ul>")
        return fold("次期売上・利益推定", "形式エラー", inner)

    models = data["models"]
    m = models[-1]
    period = html.escape(str(m.get("period", "")))
    out = EST.outputs(m)
    sens = EST.sensitivity(m)
    unit = str((m.get("revenue") or {}).get("unit") or "")
    comp = EST.comparisons(code, str(m.get("period", "")), root=ROOT, unit=unit)

    confirmed = str(m.get("status", "draft")) == "confirmed"
    status_pill = ('<span class="pill pill-good">マスター確認済み</span>' if confirmed
                   else '<span class="pill pill-warn">未確定（マスター未確認の下書き）</span>')

    lede = (f'<p class="lede">{status_pill} 対象期 <b>{period}</b>・'
            f'起案 {html.escape(str(m.get("as_of", "")))}。'
            "検証済みデータと<strong>明示した仮定</strong>から機械計算した概算であり、"
            "会社計画でも的中予想でもない。"
            "<span class=\"pill pill-warn\">推定</span> の付いた変数を疑うための頁。</p>")

    # --- タイル ---
    op = out.get("operating_income")
    rev = out.get("revenue_total")
    tiles = [_tile("推定売上", f'{_fmt_m(rev)}<span class="k-unit">百万円</span>',
                   f"対象期 {m.get('period', '')}"),
             _tile("推定営業利益", f'{_fmt_m(op)}<span class="k-unit">百万円</span>',
                   f"営業利益率 {out.get('op_margin', 0) * 100:.1f}%（仮定）")]
    plan_op = (comp.get("plan") or {}).get("operating_income")
    plan_pct = _rel_pct(op, plan_op)
    if plan_pct is not None:
        tiles.append(_tile("会社計画比（営業利益）", _pct_span(plan_pct),
                           f"会社計画 {_fmt_m(plan_op)} 百万円"))
    mf = data.get("market_forecast") or {}
    mf_op = mf.get("operating_income")
    mf_pct = _rel_pct(op, mf_op if isinstance(mf_op, (int, float)) else None)
    if mf_pct is not None:
        tiles.append(_tile("市場予想比（営業利益）", _pct_span(mf_pct),
                           f"{mf.get('name', '市場予想')} {_fmt_m(mf_op)} 百万円"))
    # 最も効く変数は **売上側から** 選ぶ。op_margin は乗法モデルで定義上つねに +10% で
    # 必ず先頭に来るため、そのまま出すと「最も効くのは op_margin」という自明な結論を
    # 発見のように読ませてしまう（estimate.sensitivity の trivial 印で外す）。
    top = next((r for r in sens if not r.get("trivial")
                and r.get("delta_op_pct") is not None), None)
    if top is not None:
        tiles.append(_tile(
            "最も効く変数（売上側）",
            f'<span class="k-value-sm">{html.escape(str(top.get("var", "")))}</span>',
            f"+10%で営業利益 {top['delta_op_pct']:+.1f}%"))
    body = lede + '<div class="kpi">' + "".join(tiles) + "</div>"

    # --- 前期実績・会社計画・推定・実績の比較 ---
    prev = comp.get("prev_actual") or {}
    plan = comp.get("plan") or {}
    act = comp.get("actual") or {}
    has_actual = any(v is not None for v in act.values())
    has_mf_num = any(isinstance(mf.get(k), (int, float))
                     for k in ("revenue", "operating_income"))
    head_cells = ("<th>指標</th><th>前期実績</th><th>会社計画</th>"
                  "<th>市場予想</th><th>当台帳推定</th><th>市場予想比</th>")
    if has_actual:
        head_cells += "<th>実績（答え合わせ）</th><th>推定誤差</th>"
    rows = []
    for label, key, est_v in (("売上", "revenue", rev),
                              ("営業利益", "operating_income", op)):
        mv = mf.get(key)
        mv_num = mv if isinstance(mv, (int, float)) else None
        mf_diff = _pct_span(_rel_pct(est_v, mv_num))
        row = (f"<tr><td>{label}</td><td class=\"num\">{_fmt_m(prev.get(key))}</td>"
               f"<td class=\"num\">{_fmt_m(plan.get(key))}</td>"
               f"<td class=\"num\">{_fmt_m(mv_num)}</td>"
               f"<td class=\"num\"><strong>{_fmt_m(est_v)}</strong></td>"
               f"<td class=\"num\">{mf_diff}</td>")
        if has_actual:
            a = act.get(key)
            err_pct = _rel_pct(est_v, a)
            err = "—" if err_pct is None else f"{err_pct:+.1f}%"
            row += f'<td class="num">{_fmt_m(a)}</td><td class="num">{err}</td>'
        rows.append(row + "</tr>")
    mf_note = ""
    for warn in comp.get("unit_mismatch") or []:
        mf_note += ('<p class="lede"><span class="pill pill-danger">単位不一致</span> '
                    f"{html.escape(warn)}</p>")
    if mf and not has_mf_num:
        mf_note += (f'<p class="lede">市場予想: '
                    f"{html.escape(str(mf.get('note', '')))}</p>")
    elif mf and mf.get("source"):
        mf_note += (f'<p class="lede">市場予想の出所: '
                    f"{html.escape(str(mf.get('name', '')))} "
                    f"{_est_source(mf.get('source'))}</p>")
    body += ("<h3>会社計画・市場予想・実績との比較（百万円）</h3>"
             '<div class="scroll"><table class="prose-table est-mkt"><thead><tr>'
             + head_cells + "</tr></thead><tbody>" + "".join(rows)
             + "</tbody></table></div>" + mf_note)

    # --- 計算の分解（セグメント） ---
    seg_rows = "".join(
        f'<tr><td>{html.escape(str(s.get("name", "")))}</td>'
        f'<td class="num">{html.escape(str(s.get("expr_filled", "")))}</td>'
        f'<td class="num"><strong>{_fmt_m(s.get("value"))}</strong></td></tr>'
        for s in out.get("segments", []))
    body += ("<h3>計算の分解</h3>"
             '<div class="scroll"><table class="prose-table est-vars"><thead><tr>'
             "<th>セグメント</th><th>式（値を代入）</th><th>売上（百万円）</th>"
             "</tr></thead><tbody>" + seg_rows + "</tbody></table></div>")

    # --- 変数と根拠（カード。長文のメモ・出典はクリックで開く） ---
    def _var_card(seg_name: str, vn: str, vd: dict) -> str:
        vd = vd or {}
        unit = html.escape(str(vd.get("unit", "") or ""))
        unit_html = f" {unit}" if unit else ""
        note = html.escape(str(vd.get("note", "")))
        src = _est_source(vd.get("source", ""))
        detail = f"{note} {src}".strip() or "（メモなし）"
        return ('<details class="var">'
                f'<summary><span class="v-seg">{html.escape(seg_name)}</span>'
                f'<code>{html.escape(str(vn))}</code>'
                f'<span class="v-val">{html.escape(str(vd.get("value", "")))}'
                f"{unit_html}</span>"
                f'{_BASIS_PILL.get(str(vd.get("basis", "")), "")}</summary>'
                f"<div>{detail}</div></details>")

    cards = []
    for s in m.get("revenue", {}).get("segments", []):
        for vn, vd in (s.get("vars") or {}).items():
            cards.append(_var_card(str(s.get("name", "")), str(vn), vd))
    om = m.get("profit", {}).get("op_margin") or {}
    cards.append(_var_card("全社", "op_margin", om))
    body += ("<h3>変数と根拠</h3>"
             '<p class="lede">カードを押すと、その値の置き方（メモ・出典）が開く。'
             '<span class="pill pill-warn">推定</span> の変数から疑うこと。</p>'
             '<div class="var-grid">' + "".join(cards) + "</div>")

    # --- 感度（何が重要な変数か） ---
    sens_rows = "".join(_est_sens_row(x) for x in sens[:6])
    body += ("<h3>感度 — この推定を最も動かす変数</h3>"
             '<p class="lede">各変数を +10% したときの営業利益の変化。'
             "上にある変数ほど、根拠を厚く確かめる価値がある。"
             "ただし <code>op_margin</code> だけは別扱い——"
             "営業利益 = 売上合計 × op_margin なので +10% は定義上つねに +10% になり、"
             "売上側の変数と大小を比べても意味が無い。</p>"
             '<div class="scroll"><table class="prose-table"><thead><tr>'
             "<th>変数</th><th>セグメント</th><th>営業利益への影響</th>"
             "</tr></thead><tbody>" + sens_rows + "</tbody></table></div>")

    # --- 変遷（過去版は消さない） ---
    hist_rows = []
    for hm in models:
        try:
            ho = EST.outputs(hm)
            hr, hop = _fmt_m(ho.get("revenue_total")), _fmt_m(ho.get("operating_income"))
        except Exception:  # noqa: BLE001 — 過去版の形式劣化でページを壊さない
            hr = hop = "—"
        hist_rows.append(
            f'<tr><td>{html.escape(str(hm.get("as_of", "")))}</td>'
            f'<td>{html.escape(str(hm.get("period", "")))}</td>'
            f'<td class="num">{hr}</td><td class="num">{hop}</td>'
            f'<td>{html.escape(str(hm.get("note", "")))}</td></tr>')
    body += ("<h3>推定の変遷</h3>"
             '<p class="lede">過去の版は書き換えずに残す。'
             "数値感がどう変わってきたかが学習の履歴になる。</p>"
             '<div class="scroll"><table class="prose-table"><thead><tr>'
             "<th>起案日</th><th>対象期</th><th>売上</th><th>営業利益</th><th>何を変えたか</th>"
             "</tr></thead><tbody>" + "".join(hist_rows) + "</tbody></table></div>")

    status_ja = "確認済み" if confirmed else "未確定"
    hint = f"対象 {m.get('period', '')}・{status_ja}・変数 {len(cards)} 個"
    return fold("次期売上・利益推定", hint, f'<div class="est">{body}</div>',
                wide=True)


def build_stock_page(rep: R.Report, as_of: str,
                     stock: dict | None = None) -> None:
    (DOCS / "stock").mkdir(parents=True, exist_ok=True)
    name = html.escape(rep.name)
    code = html.escape(rep.code)
    market = html.escape(str(rep.meta.get("market", "")))
    flag = '<span class="flag">再調査</span>' if rep.deep_dive else ""

    charts = CD.resolve_charts(rep.code, rep.charts)

    links = "".join(ext_link(str(lk.get("url", "")), str(lk.get("label", "")))
                    for lk in rep.links if lk.get("url"))
    verify_line = verify_headline(rep.code)

    # 監視から外した銘柄は、ページを直接開いた人にも必ず見えるようにする。
    # 一覧の印だけだと、リンクや検索で直接来た読み手が
    # 「これは今の話だ」と読んでしまう。
    watch_note = ""
    if stock is not None and not Y.is_watched(stock):
        why = html.escape(" ".join(str(stock.get("watch_reason") or "").split()))
        since = html.escape(str(stock.get("watch_since") or ""))
        watch_note = (
            '<p class="note note-warn"><strong>この銘柄は監視対象から外している'
            + (f"（{since}〜）" if since else "")
            + "。</strong>株価・財務の取得も判定も止めているので、"
            "以下の数値と判定は<strong>その時点で凍ったもの</strong>であり、"
            "現在の姿ではない。記録は消していない。"
            + (f"<br>外した理由: {why}" if why else "")
            + "</p>")

    head = (
        f"<h1>{name}（{code}）{flag}</h1>"
        f'<p class="lede">{market}／レポート更新 {html.escape(rep.updated)}'
        f"{verify_line}"
        f"<br>{links}</p>"
        + watch_note
    )
    lead_md = strip_title(rep.lead)
    body = head + kpi_tiles(rep.code, rep.meta, as_of) + to_html(lead_md, charts)

    # 大きく2つに畳む（既定は閉）: 週次アップデート／会社概要。
    # 検証の記録（裏取り・数値の検証状況）は会社概要の末尾に含める。
    entries = rep.week_entries()
    if entries:
        latest_key = entries[0][0].split("（")[0]
        upd_hint = f"最新 {latest_key}・全 {len(entries)} 件"
    else:
        upd_hint = "まだ記録なし"
    body += fold("週次アップデート", upd_hint,
                 render_updates(rep, "", charts))
    body += render_estimate(rep.code)

    company = "".join(
        render_section(rep, key, title, charts)
        for key, title in R.SECTIONS if key != "updates")
    company += render_verify(rep)
    company += render_verification(rep, charts)
    sec_titles = "・".join(t for k, t in R.SECTIONS if k != "updates")
    body += fold("会社概要", f"{sec_titles}・検証の記録", company)

    title = f"{rep.name}（{rep.code}）"
    (DOCS / "stock" / f"{rep.code}.html").write_text(
        page(title, body, as_of, 1), encoding="utf-8", newline="\n")


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
        # このページは**出どころの開示**が目的で、合計値もここから出る。
        # 対象外も隠さず載せ、代わりに印を付ける（一覧の方は既定で畳む）。
        if not Y.is_watched(s):
            link += '<span class="pill pill-warn">対象外</span>'
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
        if not Y.is_watched(s):
            name += '<span class="pill pill-warn">対象外</span>'
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
                                    encoding="utf-8", newline="\n")


# --- 読み方（手法の説明はここに一元化する） --------------------------------

def _vm_badge(mark: str) -> str:
    """凡例に出す記号バッジ。本文と同じ見た目（mark_badges と同じクラス）。"""
    cls, title = VM_BADGES[mark]
    return f'<span class="vm {cls}" title="{html.escape(title)}">{mark}</span>'


def build_about_page(as_of: str) -> None:
    """docs/about.html。数値の3段階・記号凡例・裏取り・図の出どころの説明。

    以前は各レポートの出典節に「この節の読み方」表がコピペされていた。
    説明はここに一元化し、レポート本文には手法の説明を書かない
    （書く場所が増えるほど、改訂したときに古い説明が残る）。
    """
    tiers = (
        '<div class="scroll"><table class="prose-table"><thead><tr>'
        "<th>段階</th><th>記号</th><th>意味</th><th>どこで見分けるか</th>"
        "</tr></thead><tbody>"
        "<tr><td><strong>2ソース照合済み</strong></td>"
        f"<td>{_vm_badge('✓')}</td>"
        "<td>運営の異なる2つの取得元から機械で抜き、表示されている桁の範囲で"
        "一致した値だけを採用値にしている。図の数値は原則これで描き、例外（手書き・一次情報のみ）は図の下の注記が明示する</td>"
        "<td>本文では値の直後の ✓。図の下に「出所: data/… の採用値」と出る</td></tr>"
        "<tr><td><strong>一次情報から直接</strong></td>"
        f"<td>{_vm_badge('†')}</td>"
        "<td>決算短信PDF（会社が自分で出した一次情報）から機械で抜いた値。"
        "まとめサイトの値との突き合わせ結果は各銘柄ページの"
        "「数値の検証状況」に併記している</td>"
        "<td>本文では値の直後の †。図の下に「決算短信（一次情報）」と出る</td></tr>"
        "<tr><td><strong>未照合・参考値</strong></td>"
        f"<td>{_vm_badge('※')}</td>"
        "<td>取得元が1つしかない、2つが一致しなかった、または出来高・信用残の"
        "ように照合の仕組みが無い値。<strong>採用値にしていない</strong>。"
        "断定形では書かない</td>"
        "<td>本文では値の直後の ※</td></tr>"
        "</tbody></table></div>"
        '<p class="lede">凡例: ✓=2ソース照合済みの採用値 ／ ※=未照合・参考値 ／ '
        "†=決算短信（一次情報）から直接。記号に指を載せる（マウスを重ねる）と"
        "意味が出る。段階ごとの件数は、各銘柄ページ末尾の"
        "<strong>「数値の検証状況」</strong>に機械が出している。</p>"
    )

    body = (
        "<h1>この台帳の読み方</h1>"
        '<p class="lede">この台帳は、スクリーニングを通過した銘柄について'
        "<strong>その会社が何をしている会社なのか</strong>を調べ、毎週の動きを"
        "記録するためのもの。売買の判断は人間が行い、台帳は候補の提示と記録に徹する。"
        "数値は出所と検証状態をすべて開示し、"
        "調べきれなかったことは「未確認」のまま残す（分かったふりをしない）。</p>"
        + howto_block()
        + "<h2>数値の3段階と記号</h2>"
        + '<p>レポートの数値には検証の段階が3つあり、本文では値の直後の記号で'
        "見分けられるようにしている。</p>"
        + tiers
        + "<h2>「記述の裏取り」とは</h2>"
        + "<p>数値の照合が届かない散文——「導入180社以上」「カバーは0社」のような"
        "記述——は、レポートを書いたのとは<strong>別の文脈</strong>が出典URLを"
        "実際にもう一度取りに行き、その記述がその出典で裏付けられるかを1件ずつ"
        "判定している。書き手が「こう書いてある」と言っていることは根拠にしない。"
        "裏が取れなかった記述は、各銘柄ページの「記述の裏取り」欄に"
        "理由つきで残している（消す方法は用意していない）。</p>"
        + "<h2>図の出どころ</h2>"
        + "<ul>"
        "<li><strong>検証済みCSVから自動</strong>: 図の数値は検証済みデータ"
        "（<code>data/</code> 配下のCSV）からコードが引いて描いている。"
        "照合が成立しなかった点は欠測として抜く（0 で埋めない）</li>"
        "<li><strong>手書き（未検証）</strong>: 人が front matter に書き写した"
        "数値で描いた図には、図そのものに「手書き（未検証）」と表示される。"
        "この表示を消す方法は用意していない</li>"
        "<li><strong>定性図</strong>: ビジネスモデルの構造など、数値を含まない図。"
        "数値が混ざった定性図は描画自体を拒否する（数値は検証済みデータ由来の"
        "図でしか出さない）</li>"
        "</ul>"
        + unevaluated_block()
    )
    (DOCS / "about.html").write_text(page("この台帳の読み方", body, as_of, 0),
                                     encoding="utf-8", newline="\n")


# --- main -----------------------------------------------------------------

def main() -> int:
    DOCS.mkdir(exist_ok=True)
    (DOCS / ".nojekyll").write_text("", encoding="utf-8", newline="\n")

    master = load_master()
    codes = [s["code"] for s in master["stocks"]]
    by_code = {str(s["code"]): s for s in master["stocks"]}
    reports = R.load_all(codes)
    as_of = as_of_date()

    # 判定スタンプを先に書く。銘柄ページの「判定」タイルが stamps.json を
    # 読むため、後に回すと前回の判定が1週遅れで表示される
    write_stamps(master)

    build_index(master, reports, as_of)
    build_data_page(master, reports, as_of)
    build_about_page(as_of)
    for code in sorted(reports):
        build_stock_page(reports[code], as_of, by_code.get(code))

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
    STAMPS.write_text(body, encoding="utf-8", newline="\n")
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
