"""レポート用の図。純関数で SVG 文字列を返す（外部ライブラリなし）。

色は dataviz の検証済みパレットに従う:
  プラス/黒字  #2a78d6（light）/ #3987e5（dark）
  マイナス/赤字 #d03b3b（両モード）
  → この2色は light/dark 両方で全チェック通過（CVD ΔE 23.8 / 25.7）。
     「黒字=緑・赤字=赤」は赤緑色覚で ΔE 4.1 と区別できないため採らない。

規約（dataviz）:
  - 1つの図に y 軸は1本だけ。2軸グラフは作らない
  - 系列が1つなら凡例は置かない（タイトルが系列名を兼ねる）
  - グリッドと軸は控えめ。値は選択的に直接ラベルする（全点に数字を置かない）
  - 色だけに意味を持たせない。符号は必ずラベル/位置でも分かるようにする
  - スマホ優先。viewBox で可変幅にし、固定 px 幅にしない
"""
from __future__ import annotations

import html
from dataclasses import dataclass

# --- 色（CSS 変数名。実体は style.py 側で light/dark を切り替える） ---
POS = "var(--viz-pos)"
NEG = "var(--viz-neg)"
GRID = "var(--viz-grid)"
AXIS = "var(--viz-axis)"
INK = "var(--viz-ink)"
MUTED = "var(--viz-muted)"


@dataclass
class Point:
    label: str
    value: float | None
    note: str = ""          # ツールチップに出す補足
    emphasis: bool = False   # 直接ラベルを付ける点


def _pts(data: list[dict]) -> list[Point]:
    out = []
    for d in data:
        out.append(Point(
            label=str(d.get("label", "")),
            value=None if d.get("value") is None else float(d["value"]),
            note=str(d.get("note", "")),
            emphasis=bool(d.get("emphasis")),
        ))
    return out


def _fmt(v: float, unit: str) -> str:
    if abs(v) >= 100:
        s = f"{v:,.0f}"
    elif abs(v) >= 10:
        s = f"{v:,.1f}"
    else:
        s = f"{v:,.2f}".rstrip("0").rstrip(".")
    return f"{s}{unit}"


def _wrap(inner: str, vb_w: int, vb_h: int, caption: str = "",
          source_note: str = "") -> str:
    """図を figure で包む。

    source_note は「この図の数値がどこから来たか」。**必ず図と一緒に出す**。
    2ソース照合済みの値と、人が転記した未検証の値を同じ見た目で並べない
    （CLAUDE.md データ層／D7）。
    """
    cap = ""
    if caption:
        cap = f'<figcaption>{html.escape(caption)}</figcaption>'
    src = ""
    if source_note:
        cls = "viz-src"
        if source_note.startswith("手書き"):
            cls = "viz-src viz-src-hand"
        src = f'<p class="{cls}">{html.escape(source_note)}</p>'
    return (
        f'<figure class="viz">'
        f'<svg viewBox="0 0 {vb_w} {vb_h}" role="img" '
        f'preserveAspectRatio="xMidYMid meet">{inner}</svg>{cap}{src}</figure>'
    )


# --- 棒グラフ（正負対応・単一系列） ---------------------------------------

def bar(data: list[dict], unit: str = "", caption: str = "",
        zero_label: str = "0", source_note: str = "") -> str:
    """縦棒。値が負なら赤で下向きに描く。y 軸は1本のみ。"""
    pts = _pts(data)
    vals = [p.value for p in pts if p.value is not None]
    if not vals:
        return ""

    W, H = 720, 300
    pad_l, pad_r, pad_t, pad_b = 54, 12, 18, 52
    plot_w = W - pad_l - pad_r
    plot_h = H - pad_t - pad_b

    vmax = max(vals + [0])
    vmin = min(vals + [0])
    span = (vmax - vmin) or 1.0
    # 0 の位置
    y0 = pad_t + plot_h * (vmax / span)

    n = len(pts)
    slot = plot_w / n
    bw = min(slot * 0.62, 46)

    parts: list[str] = []

    # ゼロ線
    parts.append(f'<line x1="{pad_l}" y1="{y0:.1f}" x2="{W - pad_r}" '
                 f'y2="{y0:.1f}" stroke="{AXIS}" stroke-width="1.5"/>')
    parts.append(f'<text x="{pad_l - 8}" y="{y0 + 4:.1f}" text-anchor="end" '
                 f'class="viz-tick">{html.escape(zero_label)}</text>')

    # 目盛（上下の端）
    for v in (vmax, vmin):
        if abs(v) < 1e-9:
            continue
        y = pad_t + plot_h * ((vmax - v) / span)
        parts.append(f'<line x1="{pad_l}" y1="{y:.1f}" x2="{W - pad_r}" '
                     f'y2="{y:.1f}" stroke="{GRID}" stroke-width="1" '
                     f'stroke-dasharray="2 3"/>')
        parts.append(f'<text x="{pad_l - 8}" y="{y + 4:.1f}" text-anchor="end" '
                     f'class="viz-tick">{html.escape(_fmt(v, unit))}</text>')

    for i, p in enumerate(pts):
        cx = pad_l + slot * i + slot / 2
        x = cx - bw / 2
        if p.value is None:
            parts.append(f'<text x="{cx:.1f}" y="{y0 - 6:.1f}" '
                         f'text-anchor="middle" class="viz-tick">—</text>')
        else:
            y = pad_t + plot_h * ((vmax - max(p.value, 0)) / span)
            h = plot_h * (abs(p.value) / span)
            color = POS if p.value >= 0 else NEG
            tip = f"{p.label}: {_fmt(p.value, unit)}"
            if p.note:
                tip += f" — {p.note}"
            parts.append(
                f'<g class="viz-bar"><title>{html.escape(tip)}</title>'
                f'<rect x="{x:.1f}" y="{y:.1f}" width="{bw:.1f}" '
                f'height="{max(h, 1.5):.1f}" fill="{color}" rx="3"/></g>'
            )
            if p.emphasis:
                ly = y - 6 if p.value >= 0 else y + h + 15
                parts.append(
                    f'<text x="{cx:.1f}" y="{ly:.1f}" text-anchor="middle" '
                    f'class="viz-value">{html.escape(_fmt(p.value, unit))}</text>')

        # x ラベル（多いときは間引く）
        step = 1 if n <= 12 else 2
        if i % step == 0:
            parts.append(
                f'<text x="{cx:.1f}" y="{H - pad_b + 20:.1f}" '
                f'text-anchor="middle" class="viz-tick">'
                f'{html.escape(p.label)}</text>')

    return _wrap("".join(parts), W, H, caption, source_note)


# --- 折れ線（単一系列） ---------------------------------------------------

def line(data: list[dict], unit: str = "", caption: str = "",
         band: tuple[float, float] | None = None,
         band_label: str = "", source_note: str = "") -> str:
    """折れ線。band を渡すと参考帯（注意水準など）を敷く。"""
    pts = _pts(data)
    vals = [p.value for p in pts if p.value is not None]
    if not vals:
        return ""

    W, H = 720, 280
    pad_l, pad_r, pad_t, pad_b = 54, 14, 20, 48
    plot_w = W - pad_l - pad_r
    plot_h = H - pad_t - pad_b

    lo, hi = min(vals), max(vals)
    if band:
        lo, hi = min(lo, band[0]), max(hi, band[1])
    pad_v = (hi - lo) * 0.15 or 1
    lo, hi = lo - pad_v, hi + pad_v
    span = (hi - lo) or 1.0

    n = len(pts)
    step_x = plot_w / max(n - 1, 1)

    def xy(i: int, v: float) -> tuple[float, float]:
        return pad_l + step_x * i, pad_t + plot_h * ((hi - v) / span)

    parts: list[str] = []

    if band:
        by1 = pad_t + plot_h * ((hi - band[1]) / span)
        by2 = pad_t + plot_h * ((hi - band[0]) / span)
        parts.append(f'<rect x="{pad_l}" y="{by1:.1f}" width="{plot_w}" '
                     f'height="{max(by2 - by1, 1):.1f}" fill="{NEG}" '
                     f'opacity="0.08"/>')
        if band_label:
            parts.append(f'<text x="{W - pad_r - 4}" y="{by1 + 14:.1f}" '
                         f'text-anchor="end" class="viz-tick">'
                         f'{html.escape(band_label)}</text>')

    for v in (hi - pad_v, lo + pad_v):
        y = pad_t + plot_h * ((hi - v) / span)
        parts.append(f'<line x1="{pad_l}" y1="{y:.1f}" x2="{W - pad_r}" '
                     f'y2="{y:.1f}" stroke="{GRID}" stroke-width="1"/>')
        parts.append(f'<text x="{pad_l - 8}" y="{y + 4:.1f}" text-anchor="end" '
                     f'class="viz-tick">{html.escape(_fmt(v, unit))}</text>')

    coords = [(i, p.value) for i, p in enumerate(pts) if p.value is not None]
    d = " ".join(
        ("M" if k == 0 else "L") + f"{xy(i, v)[0]:.1f},{xy(i, v)[1]:.1f}"
        for k, (i, v) in enumerate(coords))
    parts.append(f'<path d="{d}" fill="none" stroke="{POS}" stroke-width="2" '
                 f'stroke-linejoin="round" stroke-linecap="round"/>')

    for i, v in coords:
        x, y = xy(i, v)
        p = pts[i]
        tip = f"{p.label}: {_fmt(v, unit)}"
        if p.note:
            tip += f" — {p.note}"
        parts.append(f'<g class="viz-dot"><title>{html.escape(tip)}</title>'
                     f'<circle cx="{x:.1f}" cy="{y:.1f}" r="4.5" fill="{POS}" '
                     f'stroke="var(--viz-surface)" stroke-width="2"/></g>')
        if p.emphasis:
            parts.append(f'<text x="{x:.1f}" y="{y - 12:.1f}" '
                         f'text-anchor="middle" class="viz-value">'
                         f'{html.escape(_fmt(v, unit))}</text>')

    step = 1 if n <= 12 else 2
    for i, p in enumerate(pts):
        if i % step:
            continue
        x = pad_l + step_x * i
        parts.append(f'<text x="{x:.1f}" y="{H - pad_b + 20:.1f}" '
                     f'text-anchor="middle" class="viz-tick">'
                     f'{html.escape(p.label)}</text>')

    return _wrap("".join(parts), W, H, caption, source_note)


# --- 進捗（実績 vs 必要量） -----------------------------------------------

def progress(done: float, target: float, unit: str = "",
             done_label: str = "実績", rest_label: str = "残り",
             caption: str = "", source_note: str = "") -> str:
    """目標に対する到達と残りを1本のバーで見せる。

    done が負（＝赤字）の場合、残りは目標との差になり、バーは
    「マイナスからの出発」として左端に赤い区間を描く。
    """
    W, H = 720, 132
    pad_l, pad_r = 12, 12
    bar_w = W - pad_l - pad_r
    y, bh = 42, 34

    lo = min(done, 0.0)
    hi = max(target, done, 0.0)
    span = (hi - lo) or 1.0

    def px(v: float) -> float:
        return pad_l + bar_w * ((v - lo) / span)

    x_zero, x_done, x_target = px(0.0), px(done), px(target)
    parts: list[str] = []

    parts.append(f'<rect x="{pad_l}" y="{y}" width="{bar_w}" height="{bh}" '
                 f'rx="4" fill="{GRID}" opacity="0.55"/>')

    if done < 0:
        parts.append(f'<g class="viz-bar"><title>{html.escape(done_label)}: '
                     f'{html.escape(_fmt(done, unit))}</title>'
                     f'<rect x="{x_done:.1f}" y="{y}" '
                     f'width="{max(x_zero - x_done, 2):.1f}" height="{bh}" '
                     f'rx="4" fill="{NEG}"/></g>')
    else:
        parts.append(f'<g class="viz-bar"><title>{html.escape(done_label)}: '
                     f'{html.escape(_fmt(done, unit))}</title>'
                     f'<rect x="{x_zero:.1f}" y="{y}" '
                     f'width="{max(x_done - x_zero, 2):.1f}" height="{bh}" '
                     f'rx="4" fill="{POS}"/></g>')

    # 目標線
    parts.append(f'<line x1="{x_target:.1f}" y1="{y - 10}" x2="{x_target:.1f}" '
                 f'y2="{y + bh + 10}" stroke="{INK}" stroke-width="2"/>')
    parts.append(f'<text x="{x_target:.1f}" y="{y - 16}" text-anchor="middle" '
                 f'class="viz-value">目標 {html.escape(_fmt(target, unit))}</text>')

    # ゼロ線
    if lo < 0:
        parts.append(f'<line x1="{x_zero:.1f}" y1="{y - 4}" x2="{x_zero:.1f}" '
                     f'y2="{y + bh + 4}" stroke="{AXIS}" stroke-width="1.5" '
                     f'stroke-dasharray="3 3"/>')
        parts.append(f'<text x="{x_zero:.1f}" y="{y + bh + 22}" '
                     f'text-anchor="middle" class="viz-tick">0</text>')

    parts.append(f'<text x="{pad_l}" y="{y - 16}" class="viz-tick">'
                 f'{html.escape(done_label)} {html.escape(_fmt(done, unit))}</text>')
    gap = target - done
    parts.append(f'<text x="{pad_l}" y="{y + bh + 22}" class="viz-value">'
                 f'{html.escape(rest_label)} {html.escape(_fmt(gap, unit))}</text>')

    return _wrap("".join(parts), W, H, caption, source_note)


# --- レンジ内の位置（52週高安の中で今どこか） -----------------------------

def range_pos(low: float, high: float, current: float, unit: str = "円",
              low_label: str = "安値", high_label: str = "高値",
              markers: list[dict] | None = None, caption: str = "",
              source_note: str = "") -> str:
    W, H = 720, 118
    pad_l, pad_r = 44, 44
    bar_w = W - pad_l - pad_r
    y, bh = 52, 12
    span = (high - low) or 1.0

    def px(v: float) -> float:
        return pad_l + bar_w * ((v - low) / span)

    parts: list[str] = []
    parts.append(f'<rect x="{pad_l}" y="{y}" width="{bar_w}" height="{bh}" '
                 f'rx="6" fill="{GRID}"/>')
    parts.append(f'<rect x="{pad_l}" y="{y}" width="{px(current) - pad_l:.1f}" '
                 f'height="{bh}" rx="6" fill="{POS}" opacity="0.35"/>')

    for m in (markers or []):
        mx = px(float(m["value"]))
        parts.append(f'<g class="viz-dot"><title>'
                     f'{html.escape(str(m.get("label", "")))}: '
                     f'{html.escape(_fmt(float(m["value"]), unit))}</title>'
                     f'<line x1="{mx:.1f}" y1="{y - 6}" x2="{mx:.1f}" '
                     f'y2="{y + bh + 6}" stroke="{MUTED}" stroke-width="1.5"/></g>')
        parts.append(f'<text x="{mx:.1f}" y="{y - 12}" text-anchor="middle" '
                     f'class="viz-tick">{html.escape(str(m.get("label", "")))}</text>')

    cx = px(current)
    parts.append(f'<g class="viz-dot"><title>現在: '
                 f'{html.escape(_fmt(current, unit))}</title>'
                 f'<circle cx="{cx:.1f}" cy="{y + bh / 2:.1f}" r="9" '
                 f'fill="{POS}" stroke="var(--viz-surface)" stroke-width="2.5"/></g>')
    parts.append(f'<text x="{cx:.1f}" y="{y + bh + 30:.1f}" text-anchor="middle" '
                 f'class="viz-value">現在 {html.escape(_fmt(current, unit))}</text>')

    parts.append(f'<text x="{pad_l}" y="{y + bh + 30:.1f}" text-anchor="middle" '
                 f'class="viz-tick">{html.escape(low_label)}<tspan x="{pad_l}" '
                 f'dy="14">{html.escape(_fmt(low, unit))}</tspan></text>')
    parts.append(f'<text x="{W - pad_r}" y="{y + bh + 30:.1f}" '
                 f'text-anchor="middle" class="viz-tick">'
                 f'{html.escape(high_label)}<tspan x="{W - pad_r}" dy="14">'
                 f'{html.escape(_fmt(high, unit))}</tspan></text>')

    return _wrap("".join(parts), W, H, caption, source_note)


# --- ディスパッチ ---------------------------------------------------------

def render(spec: dict) -> str:
    """front matter の charts エントリ1件（chartdata で解決済み）を SVG にする。

    `source_note` は chartdata.resolve_chart が入れる出所の説明。描く値が
    足りなければ空文字を返す（呼び手が「描けなかった」と表示する）。
    """
    kind = spec.get("type")
    caption = spec.get("caption", "")
    unit = spec.get("unit", "")
    note = spec.get("source_note", "")
    if kind == "bar":
        return bar(spec.get("data", []), unit, caption,
                   spec.get("zero_label", "0"), note)
    if kind == "line":
        band = spec.get("band")
        band_t = tuple(band) if band and len(band) == 2 else None
        return line(spec.get("data", []), unit, caption, band_t,
                    spec.get("band_label", ""), note)
    if kind == "progress":
        if spec.get("done") is None or spec.get("target") is None:
            return ""
        return progress(float(spec["done"]), float(spec["target"]), unit,
                        spec.get("done_label", "実績"),
                        spec.get("rest_label", "残り"), caption, note)
    if kind == "range":
        need = ("low", "high", "current")
        if any(spec.get(k) is None for k in need):
            return ""
        return range_pos(float(spec["low"]), float(spec["high"]),
                         float(spec["current"]), unit,
                         spec.get("low_label", "安値"),
                         spec.get("high_label", "高値"),
                         spec.get("markers"), caption, note)
    return ""
