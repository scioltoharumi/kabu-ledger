"""候補を名指しする前に通す検査。**利益が何％増えているか**を機械で出す。

## なぜこのファイルがあるか（2026-09-01）

高砂熱学工業を「特に見込みがある」と推した。根拠は通期の営業利益 +47.3% と
ROE 計画 19.33%。**しかし直近2四半期は営業減益（-24.7%・-16.2%）だった。**
四半期の値は `data/fundamentals/1969.csv` に採用値（status OK）として
**すでに入っていた**のに、見ずに通期だけで判断していた。
同じ日、日新電機を候補に挙げたが、2023年4月に上場廃止済みだった。

どちらも「判断が難しかった」のではなく **手元にある安い確認を飛ばした**ミス。
人間のレビューを厚くするのではなく、機械が先に見る。

## 何を出すか

銘柄ごとに、判断の優先順（segments の growth_axis）で並べた数字:

  1. 直近四半期の営業利益 前年同期比 ← 最速で変調が出る
  2. その前の四半期の前年同期比      ← 1四半期の振れと区別する
  3. 通期の営業利益 伸び率
  4. 会社計画の伸び率
  5. 株価の位置（52週高値比・6か月前比）＝ 織り込みの一次近似

## 警告（machine flags）

  NO_PRICE_DATA      株価の採用値が無い。**土俵に乗っていない＝語れない**
  NO_QUARTERLY       四半期の採用値が無い。伸びを測れない
  MOMENTUM_REVERSED  通期はプラスだが直近四半期がマイナス（勢いが終わっている）
  MOMENTUM_NEGATIVE  2四半期続けてマイナス（振れではなく減速）
  PLAN_FLAT          会社計画の伸びが小さい（会社が「ここまで」と見ている）

флаг が付いた銘柄を「有望」と書かない。判断そのものは人間が行う。

実行:
  python src/screen.py            # 監視中の全銘柄
  python src/screen.py 1944 6622  # 銘柄を指定
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import chartdata as CD  # noqa: E402
import yamlio as Y  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]

# 会社計画の伸びがこれ未満なら「会社が『ここまで』と見ている」と扱う。
# 好決算の直後に横ばい計画が出ているときの合図（knowledge/segment-method.html）。
PLAN_FLAT_PCT = 5.0


def _fy_series(code: str, metric: str) -> dict[str, float]:
    """通期（FY…）の採用値。status が OK で始まる行だけ（D53 と同じ数え方）。"""
    out: dict[str, float] = {}
    for f in CD.fundamentals(code)["facts"]:
        if (f.metric == metric and f.period.startswith("FY")
                and f.value is not None and str(f.status).startswith("OK")):
            out[f.period] = f.value
    return out


def _q_series(code: str) -> dict[str, float]:
    """四半期（Q…）の営業利益の採用値。"""
    out: dict[str, float] = {}
    for f in CD.fundamentals(code)["facts"]:
        if (f.metric == "operating_income" and f.period.startswith("Q")
                and f.value is not None and str(f.status).startswith("OK")):
            out[f.period] = f.value
    return out


def _prev_year_key(key: str) -> str:
    """Q2026-04_2026-06 → Q2025-04_2025-06（前年の同じ四半期）。"""
    body = key[1:]
    a, b = body.split("_", 1)
    def back(d: str) -> str:
        y, m = d.split("-", 1)
        return f"{int(y) - 1}-{m}"
    return "Q" + back(a) + "_" + back(b)


def _pct(cur: float | None, prev: float | None) -> float | None:
    """前年同期比（％）。前年が 0 以下なら比率が意味を持たないので出さない。"""
    if cur is None or prev is None or prev <= 0:
        return None
    return (cur / prev - 1.0) * 100.0


def quarter_growth(code: str, back: int = 0) -> tuple[str | None, float | None]:
    """新しい方から back 番目の四半期の (期間キー, 前年同期比%)。"""
    q = _q_series(code)
    keys = sorted(q)
    if len(keys) <= back:
        return None, None
    key = keys[-1 - back]
    return key, _pct(q.get(key), q.get(_prev_year_key(key)))


def full_year_growth(code: str) -> tuple[str | None, float | None]:
    """直近通期の営業利益 前年比。"""
    fy = _fy_series(code, "operating_income")
    keys = sorted(fy)
    if len(keys) < 2:
        return (keys[-1] if keys else None), None
    return keys[-1], _pct(fy[keys[-1]], fy[keys[-2]])


def plan_growth(code: str) -> tuple[str | None, float | None]:
    """会社計画の営業利益 伸び率（直近通期の実績と比べる）。"""
    plan = _fy_series(code, "operating_income_plan")
    fy = _fy_series(code, "operating_income")
    if not plan or not fy:
        return None, None
    pkey = sorted(plan)[-1]
    base = [k for k in sorted(fy) if k < pkey]
    if not base:
        return pkey, None
    return pkey, _pct(plan[pkey], fy[base[-1]])


def price_position(code: str) -> dict:
    """採用値の終値だけで見る株価の位置。無ければ空 dict。"""
    series = CD.prices(code)
    if not series:
        return {}
    series = sorted(series)
    last_day, last = series[-1]
    year_ago = f"{int(last_day[:4]) - 1}{last_day[4:]}"
    window = [v for d, v in series if d >= year_ago]
    values = [v for _d, v in series]
    m6 = values[-120] if len(values) >= 120 else values[0]
    return {
        "date": last_day,
        "close": last,
        "high_52w": max(window) if window else None,
        "vs_high_pct": (last / max(window) * 100.0) if window else None,
        "chg_6m_pct": (last / m6 - 1.0) * 100.0 if m6 else None,
        "days": len(series),
    }


def screen_one(code: str) -> dict:
    """1銘柄ぶんの検査結果。警告は flags に入れる。"""
    q0_key, q0 = quarter_growth(code, 0)
    q1_key, q1 = quarter_growth(code, 1)
    fy_key, fy = full_year_growth(code)
    plan_key, plan = plan_growth(code)
    price = price_position(code)

    flags: list[str] = []
    if not price:
        flags.append("NO_PRICE_DATA")
    if q0 is None:
        flags.append("NO_QUARTERLY")
    else:
        if q0 < 0 and q1 is not None and q1 < 0:
            flags.append("MOMENTUM_NEGATIVE")
        elif q0 < 0 and fy is not None and fy > 0:
            flags.append("MOMENTUM_REVERSED")
    if plan is not None and plan < PLAN_FLAT_PCT:
        flags.append("PLAN_FLAT")

    return {
        "code": code,
        "q0_period": q0_key, "q0_yoy_pct": q0,
        "q1_period": q1_key, "q1_yoy_pct": q1,
        "fy_period": fy_key, "fy_yoy_pct": fy,
        "plan_period": plan_key, "plan_yoy_pct": plan,
        "price": price,
        "flags": flags,
    }


FLAG_JA = {
    "NO_PRICE_DATA": "株価の採用値が無い（土俵に乗っていない）",
    "NO_QUARTERLY": "四半期の採用値が無く伸びを測れない",
    "MOMENTUM_REVERSED": "通期はプラスだが直近四半期がマイナス（勢いが終わっている）",
    "MOMENTUM_NEGATIVE": "2四半期続けてマイナス（振れではなく減速）",
    "PLAN_FLAT": f"会社計画の伸びが +{PLAN_FLAT_PCT:.0f}% 未満（会社が「ここまで」と見ている）",
}


def _f(v: float | None, suffix: str = "%") -> str:
    return "—" if v is None else f"{v:+.1f}{suffix}"


def render(rows: list[dict], names: dict[str, str]) -> str:
    """人が読む表。伸びの大きい順（測れないものは末尾）。"""
    def key(r):
        return (r["q0_yoy_pct"] is None, -(r["q0_yoy_pct"] or 0.0))
    out = ["利益の勢い（営業利益・すべて台帳の採用値）",
           "",
           "{:<16}{:>10}{:>10}{:>10}{:>10}  {:>9}{:>9}".format(
               "銘柄", "直近Q", "その前Q", "通期", "会社計画", "高値比", "6か月")]
    for r in sorted(rows, key=key):
        p = r["price"]
        out.append("{:<5}{:<11}{:>10}{:>10}{:>10}{:>10}  {:>9}{:>9}".format(
            r["code"], names.get(r["code"], "")[:10],
            _f(r["q0_yoy_pct"]), _f(r["q1_yoy_pct"]),
            _f(r["fy_yoy_pct"]), _f(r["plan_yoy_pct"]),
            "—" if not p or p.get("vs_high_pct") is None
            else f"{p['vs_high_pct']:.0f}%",
            "—" if not p or p.get("chg_6m_pct") is None
            else f"{p['chg_6m_pct']:+.0f}%"))
    warned = [r for r in rows if r["flags"]]
    if warned:
        out += ["", "警告 — この印が付いた銘柄を「有望」と書かない:"]
        for r in sorted(warned, key=lambda x: x["code"]):
            for fl in r["flags"]:
                out.append(f"  [{fl}] {r['code']} {names.get(r['code'], '')}"
                           f" — {FLAG_JA.get(fl, '')}")
    else:
        out += ["", "警告なし"]
    return "\n".join(out)


def main() -> int:
    master = Y.safe_load((ROOT / "data" / "master.yaml").read_text(encoding="utf-8"))
    names = {str(s["code"]): s.get("name", "") for s in master["stocks"]}
    codes = sys.argv[1:] or [str(s["code"]) for s in Y.watched_stocks(master)]
    rows = [screen_one(c) for c in codes]
    print(render(rows, names))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
