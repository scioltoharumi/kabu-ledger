"""judge.py のテスト。

方針:
  - **7種のスタンプすべてに到達するケースを作る**（main() で網羅を検証する）。
  - **指標が None のとき「調査」に落ちることを、ゲートごとに確認する**
    （F5-4 / review-findings.md F-02 の回帰テスト。欠測を通過扱いにしない）。
  - 合成データは「手で追える形」で作る。期待値をライブラリ出力から写さない。
  - 実データ（4銘柄）で例外なく判定でき、2回実行しても同一結果になることを確認する。

実行:
  $env:PYTHONIOENCODING = "utf-8"; python tests/test_judge.py
"""
from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import indicators as ind  # noqa: E402
import judge as J  # noqa: E402
import realdata as rd  # noqa: E402


# --- 検証ヘルパ ---------------------------------------------------------------

_STAMPS_SEEN: set[str] = set()


def eq(actual, expected, label=""):
    assert actual == expected, f"{label}: expected {expected!r}, got {actual!r}"


def is_none(actual, label=""):
    assert actual is None, f"{label}: expected None, got {actual!r}"


def close_to(actual, expected, label="", tol=1e-9):
    assert actual is not None, f"{label}: expected {expected!r}, got None"
    assert abs(actual - expected) <= tol, \
        f"{label}: expected {expected!r}, got {actual!r}"


def check(v: J.Verdict, stage: str) -> J.Check:
    for c in v.checks:
        if c.stage == stage:
            return c
    raise AssertionError(f"stage {stage} が checks に無い")


def assert_verdict(v: J.Verdict, stamp: str, stage: str, resolution: str,
                   label: str) -> J.Verdict:
    _STAMPS_SEEN.add(v.stamp)
    eq(v.stamp, stamp, f"{label}: スタンプ")
    eq(v.stage, stage, f"{label}: 確定段階（{v.reason}）")
    eq(v.resolution, resolution, f"{label}: 確定の種類")
    # 確定段階より後ろのゲートは評価されていないこと（F5-1「他の指標を見ない」）
    after = [c for c in v.checks if J._STAGE_NO[c.stage] > J._STAGE_NO[stage]]
    for c in after:
        eq(c.result, J.SKIPPED, f"{label}: {c.stage} は評価しないはず")
    return v


# --- 合成データ ---------------------------------------------------------------

def _weekdays(n: int, start: str = "2025-01-06") -> list[str]:
    """start（月曜）から営業日（月〜祝日無視の平日）を n 日ぶん。"""
    d = date.fromisoformat(start)
    out: list[str] = []
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d += timedelta(days=1)
    return out


def _bars(closes, volume=100_000, band=10.0, dates=None) -> list[ind.Bar]:
    """終値の列から日足を作る。高値・安値は終値±band（一目均衡表の入力）。"""
    ds = dates or _weekdays(len(closes))
    out = []
    for d, c in zip(ds, closes):
        out.append(ind.Bar(date=d, open=c, high=c + band, low=c - band,
                           close=c, volume=volume))
    return out


def _walk(n: int, base: float, pattern) -> list[float]:
    vals = [float(base)]
    for i in range(n - 1):
        vals.append(vals[-1] + pattern[i % len(pattern)])
    return vals


# 上昇トレンド。1サイクル(5日)で +14（+2.8/日）。
# 値上がり幅の平均 24/5=4.8、値下がり幅の平均 10/5=2.0 → RSI は約70で過熱閾値80に届かない。
# 25日乖離率も線形ドリフトぶん（≒2.8×12/1300 ≒ 2.6%）で8%に届かない。
_UP = [10.0, -6.0, 8.0, -4.0, 6.0]
_DOWN = [-10.0, 6.0, -8.0, 4.0, -6.0]
_STALL = [-2.0, 2.0, -2.0, 2.0, -2.0]     # 横ばい〜微減。MA5 の傾きだけを寝かせる
# 加速（+10/日）。**完全な線形ドリフトでは MA5 と MA25 の傾き(%)が一致して "parallel"
# になる**（鉄則が判定材料から除外する状態）。ゴールデンクロス気味を作るには加速が要る。
_ACCEL = [24.0, -10.0, 22.0, -8.0, 22.0]

# 30週（＝150営業日、最終日は金曜）。トレンドゲートは 13週と26週の**両方**を見るので、
# 26週MA＋傾き4週 = 29本ぶんの週足が要る（13週だけなら16本で足りた）。
# 一目均衡表の79本も満たす。最終日が金曜なので「未了週」にはならない。
_N = 150
_ACCEL_BARS = 10


def clean_bars() -> list[ind.Bar]:
    """①〜④をすべて通過する上昇トレンド（最後の10営業日だけ加速する）。

    実測（tests 実行時に test_fixture_clean_passes_gates_1_to_4 が検証する）:
      RSI 76.38 / 25日乖離率 6.34% / 雲の上 / 週足13週MA up / 日足5/25 golden_ish
      → RSI(80)・乖離率(8%)のどちらの過熱閾値にも触れず、④を通過する。
    """
    head = _walk(_N - _ACCEL_BARS, 1000.0, _UP)
    tail = _walk(_ACCEL_BARS + 1, head[-1], _ACCEL)[1:]
    return _bars(head + tail)


def stalling_bars() -> list[ind.Bar]:
    """上昇後に失速。日足5/25 が dead_ish になるが、雲の上・週足は上向きのまま。"""
    closes = _walk(_N - 10, 1000.0, _UP)
    tail = _walk(11, closes[-1], _STALL)[1:]
    return _bars(closes + tail)


def flat_then_breakdown_bars() -> list[ind.Bar]:
    """完全横ばい（雲の上端=下端=1000）＋最終足だけ雲の下（999）。

    下げ幅を1円に留めることで、週足13週MAの傾きは flat のまま
    （＝トレンドゲートを通過し、③の売りシグナルに到達する）。
    """
    closes = [1000.0] * (_N - 1) + [999.0]
    return _bars(closes)


def _config(**over) -> dict:
    cfg = {
        "liquidity_gate": {"min_avg_turnover_20d_jpy": 30_000_000},
        "target_ladder": {1: 0.12, 2: 0.26, 3: 0.41, 4: 0.59, 5: 0.78, 6: 1.00},
        "stop_loss_pct": -0.10,
    }
    cfg.update(over)
    return cfg


def _stock(code="9999", name="テスト", holding=None) -> dict:
    return {"code": code, "name": name,
            "holding": holding or {"status": "none", "buy_price": None,
                                   "buy_date": None, "shares": None}}


def _margin(as_of: str, ratio=1.5, status="OK", long_bal=99.0, short_bal=66.0,
            days_ago=7) -> dict:
    d = (date.fromisoformat(as_of) - timedelta(days=days_ago)).isoformat()
    return {"date": d, "code": "9999", "long_balance": long_bal,
            "short_balance": short_bal, "ratio": "" if ratio is None else ratio,
            "unit": "千株", "status": status,
            "source_url": "https://example.invalid/", "fetched_at": ""}


def _kpi(rev=35.0, ordi=40.0, q1=35.0, periods=1) -> dict:
    history = [{"date": f"2026-0{i + 1}-14", "revenue_yoy_pct": rev,
                "ordinary_income_yoy_pct": ordi, "q1_progress_pct": q1}
               for i in range(periods)][::-1]
    return {"disclosure_date": history[0]["date"],
            "revenue_yoy_pct": rev, "ordinary_income_yoy_pct": ordi,
            "q1_progress_pct": q1, "history": history}


def _months_before(iso: str, months: int) -> str:
    d = date.fromisoformat(iso)
    y, m = d.year, d.month - months
    while m <= 0:
        m += 12
        y -= 1
    return date(y, m, min(d.day, 28)).isoformat()


_OMITTED = object()   # 「省略」と「明示的に None を渡した」を区別するための番兵


def run(bars, stock=None, margin=_OMITTED, kpi=None, config=None) -> J.Verdict:
    cfg = _config() if config is None else config
    m = _margin(bars[-1].date) if margin is _OMITTED else margin
    return J.judge(stock or _stock(), bars, J.compute(bars, cfg), m, kpi, cfg)


# =============================================================================
# 合成データの前提そのものの確認（テストが意図どおりの状況を作れているか）
# =============================================================================

def test_fixture_clean_passes_gates_1_to_4():
    b = clean_bars()
    i = J.compute(b, _config())
    assert i.avg_turnover_20d > 30_000_000, "流動性は通る想定"
    eq(i.weekly_ma_mid_direction, "up", "週足中期MAは上向きの想定")
    eq(i.ichimoku.position, "above", "雲の上の想定")
    assert i.rsi14 < ind.RSI_OVERHEAT, f"RSI は過熱閾値未満の想定: {i.rsi14}"
    assert ind.MA_DEVIATION_SCREEN_PCT < i.ma_deviation_pct < ind.MA_DEVIATION_OVERHEAT_PCT, \
        f"25日乖離率はスクリーニング基準5%超・過熱閾値8%未満の想定: {i.ma_deviation_pct}"
    assert i.daily_cross.kind in ("golden", "golden_ish"), \
        f"日足5/25 はゴールデン側の想定: {i.daily_cross.kind}"


def test_fixture_stalling_is_dead_ish_but_still_above_cloud():
    i = J.compute(stalling_bars(), _config())
    eq(i.daily_cross.kind, "dead_ish", "失速後はデッドクロス気味")
    eq(i.ichimoku.position, "above", "まだ雲の上")
    eq(i.weekly_ma_mid_direction, "up", "週足はまだ上向き")
    assert i.rsi14 < ind.RSI_OVERHEAT, f"RSI: {i.rsi14}"
    assert i.ma_deviation_pct < ind.MA_DEVIATION_OVERHEAT_PCT, \
        f"乖離率: {i.ma_deviation_pct}"


# =============================================================================
# ① 流動性ゲート
# =============================================================================

def test_liquidity_fail():
    # 20日平均売買代金 = 終値約1390 × 出来高100 ≒ 13.9万円 < 3,000万円
    v = run(_bars(_walk(_N, 1000.0, _UP), volume=100))
    assert_verdict(v, J.STAMP_LIQUIDITY, "liquidity", J.FAIL, "流動性不通過")
    assert "流動性ゲート不通過" in v.reason, v.reason


def test_liquidity_unknown_becomes_probe():
    """★F5-4 / review-findings F-02 の回帰テスト。

    出来高が取れないときに流動性ゲートを素通りさせない。
    「流動性が確認できない」は「ゲート通過」ではない。
    """
    b = clean_bars()
    b[-1] = b[-1]._replace(volume=None)     # kabutan が落ちた週を再現
    v = run(b)
    assert_verdict(v, J.STAMP_PROBE, "liquidity", J.UNKNOWN, "出来高欠測")
    eq(check(v, "liquidity").result, J.UNKNOWN, "ゲート結果は unknown（pass ではない）")
    assert "20日平均売買代金" in v.reason, v.reason
    assert "20日平均売買代金" in v.unknowns, v.unknowns
    # 出来高が取れないだけで、他のゲートを見に行っていないこと
    eq(check(v, "trend").result, J.SKIPPED, "以降は評価しない")


def test_liquidity_unknown_when_history_too_short():
    v = run(clean_bars()[-19:])   # 19営業日 < 20
    assert_verdict(v, J.STAMP_PROBE, "liquidity", J.UNKNOWN, "20営業日未満")


def test_liquidity_unknown_when_threshold_missing():
    # 閾値が設定に無いとき、既定値で埋めて通過させない
    v = run(clean_bars(), config={"target_ladder": {}, "stop_loss_pct": -0.10})
    assert_verdict(v, J.STAMP_PROBE, "liquidity", J.UNKNOWN, "閾値未設定")
    assert "閾値" in v.reason, v.reason


# =============================================================================
# ② トレンドゲート（鉄則の第一条）
# =============================================================================

def test_trend_negative_is_reference_only():
    """2026-09-05 改訂: 週足MAの傾きはゲートではない。負でも判定を止めない。

    旧実装の「13週/26週の回帰傾きが負なら見送(トレンド)」は当リポジトリ独自の定義で、
    楽天・株探・SBI のどの上昇トレンド定義とも一致しなかった（BACKLOG.md 改訂履歴）。
    傾きは参考行（n/a）と注意に残し、トレンドの読みはチャート形状（画像判定※）が担う。
    """
    v = run(_bars(_walk(_N, 2000.0, _DOWN)))
    assert v.stamp != J.STAMP_TREND, "見送(トレンド)はもう出ない"
    assert v.stage != "trend", f"②で確定しない: {v.stage}"
    c = check(v, "trend")
    eq(c.result, J.NA, "参考行（pass でも fail でもない）")
    assert "参考" in c.detail, c.detail
    assert any("傾きが負" in x for x in v.cautions), v.cautions
    eq(check(v, "liquidity").result, J.PASS, "流動性は通っている")


def test_trend_unknown_does_not_stop():
    # 30営業日（6週）では13週MAが張れない。以前は「調査」で止めていたが、
    # 参考行に格下げしたので判定は先へ進む（未計算の指標名は unknowns に残る）
    v = run(clean_bars()[-30:])
    assert v.stage != "trend", f"②で確定しない: {v.stage}"
    c = check(v, "trend")
    eq(c.result, J.NA, "参考行")
    assert "算出できない" in c.detail, c.detail
    assert v.stamp != J.STAMP_TREND


def test_trend_reports_both_13w_and_26w():
    """13週と26週の両方を計算し、参考行に両方の値を出す（どちらかが無ければそう書く）。"""
    b = clean_bars()
    i = J.compute(b, _config())
    assert i.weekly_ma_mid_slope_pct is not None, "13週が算出できること"
    assert i.weekly_ma_long_slope_pct is not None, "26週が算出できること"
    eq(sorted(i.weekly_ma_slopes), [13, 26], "両方の期間を持つ")
    v = run(b)
    d = check(v, "trend").detail
    assert "13週" in d and "26週" in d, d
    short = b[-(13 + 4) * 5:]           # 17週ぶん。13週MAは張れるが26週は張れない
    j = J.compute(short, _config())
    assert j.weekly_ma_mid_slope_pct is not None, "13週は算出できる"
    is_none(j.weekly_ma_long_slope_pct, "26週は期間不足")
    v = run(short)
    c = check(v, "trend")
    eq(c.result, J.NA, "参考行")
    assert "26週" in c.detail, c.detail
    assert v.stage != "trend"


def test_trend_negative_inside_flat_band_is_a_caution():
    """不感帯（±0.25%/週）の中の負の傾きも注意には出す（表示ラベルは横ばいのまま）。"""
    closes = [1000.0 * (1.0 - 0.0002) ** i for i in range(_N)]
    b = _bars(closes, volume=100_000)
    i = J.compute(b, _config())
    slope = i.weekly_ma_mid_slope_pct
    assert slope is not None and -tech_flat() < slope < 0, \
        f"不感帯の内側の負の傾きであること: {slope}"
    eq(i.weekly_ma_mid_direction, "flat", "表示ラベルは横ばいのまま")
    v = run(b)
    assert any("傾きが負" in x for x in v.cautions), v.cautions
    assert v.stamp != J.STAMP_TREND


def tech_flat() -> float:
    return ind.SLOPE_FLAT_PCT_PER_WEEK


def test_trend_drops_incomplete_last_week():
    """未了週（金曜で終わっていない最終週）を週足MAの入力から外す。"""
    b = clean_bars()
    eq(J.compute(b, _config()).weekly_last_incomplete, False,
       "金曜で終わっていれば完成週")
    trimmed = b[:-4]                    # 月曜だけを残す（週の途中で切る）
    i = J.compute(trimmed, _config())
    eq(i.weekly_last_incomplete, True, "月曜で終われば未了週")
    eq(i.weekly_bars_used, i.weekly_bars - 1, "未了週を1本落としている")


# =============================================================================
# ③ 売りシグナル（雲の下抜け）
# =============================================================================

def test_cloud_breakdown_sell():
    v = run(flat_then_breakdown_bars())
    assert_verdict(v, J.STAMP_CLOUD, "cloud", J.FAIL, "雲の下抜け（非保有は見送）")
    eq(v.metrics["ichimoku_position"], "below", "雲の下")
    eq(v.metrics["ichimoku_recent_cross"], "breakdown", "下抜けイベント")
    assert "雲の下" in v.reason, v.reason


def test_cloud_unknown_becomes_probe():
    # 雲の算出に使う高値が欠測 → 位置が決まらない。「雲の下ではない」と扱わない
    b = clean_bars()
    b[-30] = b[-30]._replace(high=None)
    v = run(b)
    assert_verdict(v, J.STAMP_PROBE, "cloud", J.UNKNOWN, "雲が張れない")
    is_none(v.metrics["ichimoku_position"], "位置は None")


# =============================================================================
# ④ 過熱チェック
# =============================================================================

def test_overheat_by_margin_ratio():
    b = clean_bars()
    v = run(b, margin=_margin(b[-1].date, ratio=6.0))
    assert_verdict(v, J.STAMP_OVERHEAT, "overheat", J.FAIL, "信用倍率5倍超")
    assert "信用倍率" in v.reason, v.reason


def test_overheat_by_rsi():
    # 単調上昇 → RSI=100。乖離率も跳ねるが、まず RSI が閾値を超えていること
    b = _bars(_walk(_N, 1000.0, [3.0]))
    v = run(b)
    assert_verdict(v, J.STAMP_OVERHEAT, "overheat", J.FAIL, "RSI過熱")
    eq(v.metrics["rsi14"], 100.0, "RSI")
    assert "RSI" in v.reason, v.reason


def test_overheat_margin_ratio_na_is_not_a_pass():
    """RATIO_NA（売り残0）を「過熱していない」と読み替えない。"""
    b = clean_bars()
    v = run(b, margin=_margin(b[-1].date, ratio=None, status="RATIO_NA",
                              long_bal=120.0, short_bal=0.0))
    assert_verdict(v, J.STAMP_OVERHEAT, "overheat", J.FAIL, "RATIO_NA は買い一辺倒")
    is_none(v.metrics["margin_ratio"], "倍率そのものは未計算のまま記録する")


def test_overheat_margin_ratio_na_can_be_configured_as_unknown():
    b = clean_bars()
    cfg = _config(judge={"margin_ratio_na_is_overheat": False})
    v = J.judge(_stock(), b, J.compute(b, cfg),
                _margin(b[-1].date, ratio=None, status="RATIO_NA",
                        long_bal=120.0, short_bal=0.0), _kpi(), cfg)
    assert_verdict(v, J.STAMP_PROBE, "overheat", J.UNKNOWN, "設定次第で調査")


def test_overheat_unknown_when_margin_missing():
    v = run(clean_bars(), margin=None)
    assert_verdict(v, J.STAMP_PROBE, "overheat", J.UNKNOWN, "信用残未取得")
    assert "信用倍率" in v.unknowns, v.unknowns


def test_overheat_unknown_when_margin_is_stale():
    b = clean_bars()
    v = run(b, margin=_margin(b[-1].date, ratio=1.2, days_ago=60))
    assert_verdict(v, J.STAMP_PROBE, "overheat", J.UNKNOWN, "信用残が古い")
    assert "古い" in v.reason, v.reason


def test_overheat_unknown_when_unit_unknown():
    b = clean_bars()
    v = run(b, margin=_margin(b[-1].date, ratio=1.2, status="UNIT_UNKNOWN"))
    assert_verdict(v, J.STAMP_PROBE, "overheat", J.UNKNOWN, "単位不明")


def test_no_margin_positions_is_not_overheat():
    b = clean_bars()
    v = run(b, margin=_margin(b[-1].date, ratio=None, status="RATIO_NA",
                              long_bal=0.0, short_bal=0.0), kpi=_kpi())
    assert_verdict(v, J.STAMP_BUY, "all_clear", J.PASS, "信用残ゼロは過熱ではない")


# =============================================================================
# ⑤ ファンダ確認 / ⑥ すべて通過
# =============================================================================

def test_fundamentals_unknown_becomes_probe():
    v = run(clean_bars(), kpi=None)
    assert_verdict(v, J.STAMP_PROBE, "fundamentals", J.UNKNOWN, "KPI未整備")
    assert "KPI未整備の銘柄に買を出さない" in v.reason, v.reason


def test_fundamentals_partial_kpi_still_probe():
    # 売上だけ揃っていても、経常・進捗率が無ければ通過させない
    k = _kpi()
    k["ordinary_income_yoy_pct"] = None
    v = run(clean_bars(), kpi=k)
    assert_verdict(v, J.STAMP_PROBE, "fundamentals", J.UNKNOWN, "一部欠測")


def test_fundamentals_below_threshold_is_fail_not_unknown():
    v = run(clean_bars(), kpi=_kpi(rev=10.0))
    assert_verdict(v, J.STAMP_PROBE, "fundamentals", J.FAIL, "条件不足")
    # 「未計算」と「条件を満たさない」は同じ調査でも resolution で区別できること
    assert "売上高 +10.0%" in v.reason, v.reason


def test_all_clear_buy():
    v = run(clean_bars(), kpi=_kpi())
    assert_verdict(v, J.STAMP_BUY, "all_clear", J.PASS, "全通過")
    for c in v.checks:
        assert c.result in (J.PASS, J.NA), f"{c.stage} が {c.result}"


# =============================================================================
# 保有管理（F13）
# =============================================================================

def _holding(bars, ret_pct: float, months: int, shares=100) -> dict:
    """現値が買値比 ret_pct% になるように買値を逆算した保有情報を作る。"""
    close = bars[-1].close
    buy = close / (1.0 + ret_pct / 100.0)
    return {"status": "holding", "buy_price": round(buy, 4),
            "buy_date": _months_before(bars[-1].date, months), "shares": shares}


def test_holding_stop_loss_sell():
    b = clean_bars()
    v = run(b, stock=_stock(holding=_holding(b, -20.0, 2)), kpi=_kpi())
    assert_verdict(v, J.STAMP_SELL, "holding_sell", J.FAIL, "逆指値抵触")
    eq(v.holding.stop_loss_hit, True, "抵触")
    close_to(v.holding.stop_loss_price, v.holding.buy_price * 0.9,
             "逆指値ライン = 買値×0.9", tol=1e-6)
    # 損切りは他の何よりも優先される（流動性ゲートより前に確定する）
    eq(check(v, "liquidity").result, J.SKIPPED, "以降は評価しない")


def test_holding_stop_loss_uses_low_not_close():
    """逆指値注文はザラ場で約定する。**安値**が刺さった日を「未抵触」にしない。"""
    b = clean_bars()
    close = b[-1].close
    buy = close / 0.95                       # 現値は買値の -5%（終値では未抵触）
    stop = buy * 0.9
    # 最終足の安値だけを逆指値の下に置く（終値は上に残す）
    b[-1] = b[-1]._replace(low=stop - 1.0)
    h = {"status": "holding", "buy_price": round(buy, 4),
         "buy_date": _months_before(b[-1].date, 2), "shares": 100}
    v = run(b, stock=_stock(holding=h), kpi=_kpi())
    assert_verdict(v, J.STAMP_SELL, "holding_sell", J.FAIL, "安値で抵触")
    eq(v.holding.stop_loss_hit, True, "安値で抵触")
    eq(v.holding.stop_loss_intraday_only, True, "終値では戻している")


def test_holding_sell_is_not_masked_by_upstream_gates():
    """★売りシグナルを上流ゲートに隠さない（フェイルセーフの向きは買いと逆）。

    旧実装では ③雲・H5 が ①流動性・②トレンド・④過熱より下にあったため、
    保有中に雲を下抜けても「見送(流動性)」「調査」で確定し、売りが評価すらされなかった。
    """
    b = flat_then_breakdown_bars()
    h = _holding(b, -1.0, 2)                 # 逆指値には触れていない保有
    stock = _stock(holding=h)

    # (1) 薄商い（流動性ゲート不通過）でも売りが出る
    thin = _bars([x.close for x in b], volume=1)
    thin_h = _holding(thin, -1.0, 2)
    v1 = run(thin, stock=_stock(holding=thin_h), kpi=_kpi())
    assert_verdict(v1, J.STAMP_SELL, "holding_sell", J.FAIL, "薄商い×雲の下")
    eq(check(v1, "liquidity").result, J.SKIPPED, "流動性より先に確定する")

    # (2) 出来高が欠測（流動性が未計算）でも売りが出る
    missing = [x._replace(volume=None) for x in b]
    v2 = run(missing, stock=_stock(holding=_holding(missing, -1.0, 2)), kpi=_kpi())
    assert_verdict(v2, J.STAMP_SELL, "holding_sell", J.FAIL, "出来高欠測×雲の下")

    # (3) 信用倍率が過熱でも売りが出る（旧実装は「様子見(過熱)」になっていた）
    v3 = run(b, stock=stock, kpi=_kpi(),
             margin=_margin(b[-1].date, ratio=9.9))
    assert_verdict(v3, J.STAMP_SELL, "holding_sell", J.FAIL, "過熱×雲の下")
    assert v3.holding.action.startswith("売り"), v3.holding.action

    # (4) 保有していなければ requirements F5 の順序どおり ③ で評価する
    v4 = run(b, kpi=_kpi())
    assert_verdict(v4, J.STAMP_CLOUD, "cloud", J.FAIL, "非保有は③で確定（見送の語）")


def test_holding_status_vocabulary_is_validated():
    """語彙外の holding.status を「保有していない」に倒さない。

    旧実装は `hold` / `保有` / `true` の打ち間違いをすべて「none」と解釈し、
    逆指値ゲートが黙って消えて判定が「買」まで通っていた。
    """
    b = clean_bars()
    buy = b[-1].close / 0.5                  # 現値は買値の半分＝明確に逆指値抵触
    for bad in ("hold", "保有", True, "HOLDING "):
        h = {"status": bad, "buy_price": round(buy, 4),
             "buy_date": _months_before(b[-1].date, 2), "shares": 100}
        v = run(b, stock=_stock(holding=h), kpi=_kpi())
        if str(bad).strip().lower() == "holding":
            assert_verdict(v, J.STAMP_SELL, "holding_sell", J.FAIL,
                           f"{bad!r} は正規化して保有扱い")
        else:
            assert_verdict(v, J.STAMP_PROBE, "holding_sell", J.UNKNOWN,
                           f"{bad!r} は語彙外なので調査")

    # status=none なのに買値だけ入っている（保有登録の書き漏れ）も unknown
    h = {"status": "none", "buy_price": 100.0, "buy_date": None, "shares": None}
    v = run(b, stock=_stock(holding=h), kpi=_kpi())
    assert_verdict(v, J.STAMP_PROBE, "holding_sell", J.UNKNOWN, "none なのに買値あり")


def test_holding_stop_loss_unknown_becomes_probe():
    b = clean_bars()
    h = {"status": "holding", "buy_price": None, "buy_date": None, "shares": None}
    v = run(b, stock=_stock(holding=h), kpi=_kpi())
    assert_verdict(v, J.STAMP_PROBE, "holding_stop_loss", J.UNKNOWN, "買値が無い")


def test_holding_not_reached_is_watch():
    # 経過2か月 → 基準 +26%。現在 +5% なので未到達 → 様子見・損切り確認
    b = clean_bars()
    v = run(b, stock=_stock(holding=_holding(b, 5.0, 2)), kpi=_kpi())
    assert_verdict(v, J.STAMP_WATCH, "holding_target", J.FAIL, "基準未到達")
    eq(v.holding.reached, False, "未到達")
    close_to(v.holding.target_pct, 26.0, "2か月の基準", tol=1e-9)
    # 買値は round(close/1.05, 4) で逆算しているため、騰落率はその丸めぶんだけずれる
    close_to(v.holding.return_pct, 5.0, "現在騰落率", tol=1e-4)
    close_to(v.holding.achievement_ratio, 5.0 / 26.0, "到達率", tol=1e-5)
    eq(v.holding.stop_loss_hit, False, "逆指値には触れていない")
    assert "様子見・損切り確認" in v.reason, v.reason


def test_holding_reached_and_dead_ish_is_sell():
    b = stalling_bars()
    v = run(b, stock=_stock(holding=_holding(b, 60.0, 2)), kpi=_kpi())
    assert_verdict(v, J.STAMP_SELL, "holding_sell", J.FAIL, "到達×デッドクロス気味")
    eq(v.holding.reached, True, "到達")
    eq(v.holding.cross_kind, "dead_ish", "デッドクロス気味")


def test_holding_reached_and_golden_ish_goes_to_fundamentals():
    b = clean_bars()
    stock = _stock(holding=_holding(b, 60.0, 2))
    v = run(b, stock=stock, kpi=_kpi())
    assert_verdict(v, J.STAMP_BUY, "all_clear", J.PASS, "到達×ゴールデン→買い増し可")
    assert v.holding.action.startswith("買い増し可"), v.holding.action
    eq(check(v, "holding_target").result, J.PASS, "H5 は通過")

    # KPI が無ければ「買い増し可」でも買を出さない（KPI未整備に買を出さない）
    v2 = run(b, stock=stock, kpi=None)
    assert_verdict(v2, J.STAMP_PROBE, "fundamentals", J.UNKNOWN, "買い増し可でもKPI必須")


def test_holding_reached_but_parallel_is_watch():
    # 完全横ばい（雲の下には抜けない）→ 日足5/25 は平行。鉄則が除外する状態
    b = _bars([1000.0] * _N)
    v = run(b, stock=_stock(holding=_holding(b, 60.0, 2)), kpi=_kpi())
    assert_verdict(v, J.STAMP_WATCH, "holding_target", J.FAIL, "到達×平行")
    eq(v.holding.cross_kind, "parallel", "平行")


def test_holding_ladder_before_first_month():
    b = clean_bars()
    v = run(b, stock=_stock(holding=_holding(b, 5.0, 0)), kpi=_kpi())
    assert_verdict(v, J.STAMP_WATCH, "holding_target", J.FAIL, "1か月未満")
    is_none(v.holding.target_pct, "基準ラインはまだ立たない")
    eq(v.holding.reached, False, "未到達扱い（データ欠測ではない）")


def test_holding_ladder_caps_at_six_months():
    b = clean_bars()
    v = run(b, stock=_stock(holding=_holding(b, 5.0, 9)), kpi=_kpi())
    close_to(v.holding.target_pct, 100.0, "6か月超は最終段(+100%)で据え置く")


def test_holding_is_reevaluated_flat_every_week():
    """前週の判断を入力に持たない（F13-5）。同じ入力からは常に同じ結論。"""
    b = clean_bars()
    stock = _stock(holding=_holding(b, 5.0, 2))
    a1 = run(b, stock=stock, kpi=_kpi())
    a2 = run(b, stock=stock, kpi=_kpi())
    eq(a1, a2, "同一入力→同一出力")


def test_elapsed_months():
    eq(J._elapsed_months("2026-01-10", "2026-08-10"), 7, "7か月")
    eq(J._elapsed_months("2026-01-10", "2026-08-09"), 6, "日が足りなければ切り捨て")
    eq(J._elapsed_months("2026-08-10", "2026-08-10"), 0, "当日は0か月")
    is_none(J._elapsed_months(None, "2026-08-10"), "買付日なし")
    is_none(J._elapsed_months("2026-01-10", None), "基準日なし")
    # 未来の買付日は入力の破損。0 にクランプして「1か月未満」に化けさせない
    is_none(J._elapsed_months("2027-01-05", "2026-08-10"), "買付日が未来")


def test_future_buy_date_is_probe_not_watch():
    b = clean_bars()
    h = {"status": "holding", "buy_price": float(b[-1].close),
         "buy_date": "2027-01-05", "shares": 100}
    v = run(b, stock=_stock(holding=h), kpi=_kpi())
    assert_verdict(v, J.STAMP_PROBE, "holding_target", J.UNKNOWN, "買付日が未来")
    is_none(v.holding.elapsed_months, "経過月数は算出しない")


# =============================================================================
# スクリーニング5条件（○×）
# =============================================================================

def test_screen_marks():
    b = clean_bars()
    v = run(b, kpi=_kpi(rev=35.0, ordi=10.0))
    marks = {s.key: s.mark for s in v.screen}
    # 5条件＋「（参考）雲の上にあるか」。上抜け（イベント）と雲の上（状態）は別物で、
    # 元のスクリーナーがどちらだったか未確認なので両方出す。
    eq(len(v.screen), 6, "5条件＋参考1行")
    eq(marks["revenue_yoy_pct"], J.MARK_OK, "売上 +35% ≥ 30%")
    eq(marks["ordinary_income_yoy_pct"], J.MARK_NG, "経常 +10% < 30%")
    eq(marks["ma25_deviation_pct"], J.MARK_OK, "乖離率は5%以上の想定")
    # 出来高比は定義が未確認なので○×を出さない（値は detail に出す）
    eq(marks["volume_ratio_3m"], J.MARK_UNKNOWN, "定義未確認なので?（×に丸めない）")
    assert "自社定義" in dict((s.key, s.detail) for s in v.screen)["volume_ratio_3m"]
    eq(marks["ichimoku_breakout"], J.MARK_NG, "既に雲の上で新規の上抜けなし")
    eq(marks["ichimoku_above"], J.MARK_OK, "状態としては雲の上")


def test_screen_unknown_is_not_rounded():
    v = run(clean_bars(), kpi=None)
    marks = {s.key: s.mark for s in v.screen}
    eq(marks["revenue_yoy_pct"], J.MARK_UNKNOWN, "未計算は?（×に丸めない）")
    eq(marks["ordinary_income_yoy_pct"], J.MARK_UNKNOWN, "未計算は?")


def test_screen_breakout_detected_within_lookback():
    # 78本の横ばい（雲=1000）のあと雲の上へ抜け、さらに数日上で推移する。
    # 最終足の遷移だけを見ると取りこぼすが、直近5営業日の走査で捕捉できる。
    closes = [1000.0] * 100 + [1200.0, 1210.0, 1220.0]
    v = run(_bars(closes), kpi=None)
    marks = {s.key: s.mark for s in v.screen}
    eq(marks["ichimoku_breakout"], J.MARK_OK, "直近の上抜けを捕捉")
    eq(v.metrics["ichimoku_cross_last_bar"], None, "最終足の遷移では検出できない")


# =============================================================================
# 信用残・KPI の変換
# =============================================================================

def test_derive_kpi_metrics():
    rows = [
        {"date": "2026-05-14", "metric": "revenue", "value": "1300",
         "unit": "JPY_million", "definition": "FY2027Q1cum|連結|日本基準|売上高"},
        {"date": "2026-05-14", "metric": "revenue_prev_year", "value": "1000",
         "unit": "JPY_million", "definition": "FY2026Q1cum|連結|日本基準|売上高"},
        {"date": "2026-05-14", "metric": "ordinary_income", "value": "200",
         "unit": "JPY_million", "definition": "FY2027Q1cum|連結|日本基準|経常利益"},
        {"date": "2026-05-14", "metric": "ordinary_income_prev_year", "value": "100",
         "unit": "JPY_million", "definition": "FY2026Q1cum|連結|日本基準|経常利益"},
        {"date": "2026-05-14", "metric": "ordinary_income_fy_plan", "value": "500",
         "unit": "JPY_million", "definition": "FY2027Q4cum|連結|日本基準|経常利益"},
    ]
    k = J.derive_kpi_metrics(rows)
    close_to(k["revenue_yoy_pct"], 30.0, "売上 +30%", tol=1e-9)
    close_to(k["ordinary_income_yoy_pct"], 100.0, "経常 +100%", tol=1e-9)
    close_to(k["q1_progress_pct"], 40.0, "1Q進捗率 200/500", tol=1e-9)
    eq(k["disclosure_date"], "2026-05-14", "開示日")


def test_derive_kpi_metrics_guards():
    base = {"date": "2026-05-14", "unit": "JPY_million",
            "definition": "FY2027Q1cum|連結|日本基準|経常利益"}
    # 前年同期が赤字 → 前年同期比は意味を持たない（None。文言で埋めない）
    k = J.derive_kpi_metrics([
        {**base, "metric": "ordinary_income", "value": "200"},
        {**base, "metric": "ordinary_income_prev_year", "value": "-50"},
    ])
    is_none(k["ordinary_income_yoy_pct"], "前年同期が赤字")

    # 単位が違う行どうしで比を作らない（換算しない）
    k = J.derive_kpi_metrics([
        {**base, "metric": "revenue", "value": "1300"},
        {**base, "metric": "revenue_prev_year", "value": "1000000",
         "unit": "JPY_thousand"},
    ])
    is_none(k["revenue_yoy_pct"], "単位不一致")

    # 通期(Q4cum)の開示から1Q進捗率を作らない
    k = J.derive_kpi_metrics([
        {**base, "metric": "ordinary_income", "value": "200",
         "definition": "FY2026Q4cum|連結|日本基準|経常利益"},
        {**base, "metric": "ordinary_income_fy_plan", "value": "500"},
    ])
    is_none(k["q1_progress_pct"], "Q1cum 以外では算出しない")
    eq(k["q1_progress_status"], J.KPI_NOT_APPLICABLE,
       "「該当しない」であって「未計算」ではない")


def test_q1_progress_is_not_applicable_after_2q():
    """★2Q開示が出た瞬間に恒久的に「調査」で固定されないこと。

    旧実装は period が Q1cum でない開示に対して None を返し、judge がそれを
    「未計算」として扱っていたため、他がすべて完璧でも「調査」から動かなかった。
    """
    def rows(date: str, period: str, ordi: str) -> list[dict]:
        base = {"date": date, "unit": "JPY_million"}
        return [
            {**base, "metric": "revenue", "value": "1300",
             "definition": f"{period}|連結|日本基準|売上高"},
            {**base, "metric": "revenue_prev_year", "value": "1000",
             "definition": f"{period}|連結|日本基準|売上高"},
            {**base, "metric": "ordinary_income", "value": ordi,
             "definition": f"{period}|連結|日本基準|経常利益"},
            {**base, "metric": "ordinary_income_prev_year", "value": "100",
             "definition": f"{period}|連結|日本基準|経常利益"},
            {**base, "metric": "ordinary_income_fy_plan", "value": "500",
             "definition": "FY2027Q4cum|連結|日本基準|経常利益"},
        ]

    # 実データの最新営業日とは別の合成日付を使う（べた書きすると
    # test_tests_do_not_hardcode_todays_latest_business_day に引っかかる）。
    q2_date = rd.latest_date()

    b = clean_bars()
    # (1) 2Q累計の開示だけ → 1Q進捗率は n/a。他が条件を満たせば「買」に到達する
    k2 = J.derive_kpi_metrics(rows(q2_date, "FY2027Q2cum", "200"))
    eq(k2["q1_progress_status"], J.KPI_NOT_APPLICABLE, "2Q開示なので該当しない")
    v = run(b, kpi=k2)
    assert_verdict(v, J.STAMP_BUY, "all_clear", J.PASS, "1Q進捗率が n/a でも到達可能")
    assert "該当しない" in check(v, "fundamentals").detail, \
        check(v, "fundamentals").detail

    # (2) 1Q開示が history にあれば、最新が2Qでもそこから拾う
    k3 = J.derive_kpi_metrics(rows("2026-05-14", "FY2027Q1cum", "200")
                              + rows(q2_date, "FY2027Q2cum", "300"))
    eq(k3["q1_progress_status"], J.KPI_OK, "過去の1Q開示から算出する")
    eq(k3["q1_progress_date"], "2026-05-14", "1Q開示の日付")
    close_to(k3["q1_progress_pct"], 40.0, "200/500", tol=1e-9)

    # (3) 1Q開示だが通期計画が無い → **未計算**（n/a ではない）→ 調査で止める
    partial = [r for r in rows("2026-05-14", "FY2027Q1cum", "200")
               if r["metric"] != "ordinary_income_fy_plan"]
    k4 = J.derive_kpi_metrics(partial)
    eq(k4["q1_progress_status"], J.KPI_UNKNOWN, "1Q開示なのに計画が無い＝未計算")
    v4 = run(b, kpi=k4)
    assert_verdict(v4, J.STAMP_PROBE, "fundamentals", J.UNKNOWN, "未計算は調査")


def test_margin_ratio_na_is_not_overheat_for_short_unavailable_stocks():
    """制度信用が買建のみの銘柄で、売り残0を過熱材料にしない。

    旧実装ではこの銘柄が構造的に「様子見(過熱)」から永久に出られなかった。
    master.yaml の flags（固定語彙）を読んで「該当しない」に倒す。
    """
    b = clean_bars()
    margin = _margin(b[-1].date, ratio=None, status="RATIO_NA",
                     long_bal=120.0, short_bal=0.0)
    stock = _stock()
    stock["flags"] = ["制度信用: 買建のみ", "時価総額が極小"]
    v = run(b, stock=stock, margin=margin, kpi=_kpi())
    assert_verdict(v, J.STAMP_BUY, "all_clear", J.PASS, "構造的な RATIO_NA は過熱材料外")
    assert "信用倍率" not in v.unknowns, v.unknowns
    assert any("構造的に定義不能" in c for c in v.cautions), v.cautions

    # flags が無ければ従来どおり過熱側に倒す（「過熱していない」とは読み替えない）
    v2 = run(b, margin=margin, kpi=_kpi())
    assert_verdict(v2, J.STAMP_OVERHEAT, "overheat", J.FAIL, "flags 無しは過熱側")


def test_fundamentals_streak():
    b = clean_bars()
    cfg = _config(judge={"fundamentals_min_streak": 2})
    k1 = _kpi(periods=1)
    v1 = J.judge(_stock(), b, J.compute(b, cfg), _margin(b[-1].date), k1, cfg)
    assert_verdict(v1, J.STAMP_PROBE, "fundamentals", J.FAIL, "継続1期では足りない")
    k2 = _kpi(periods=2)
    v2 = J.judge(_stock(), b, J.compute(b, cfg), _margin(b[-1].date), k2, cfg)
    assert_verdict(v2, J.STAMP_BUY, "all_clear", J.PASS, "継続2期で通過")


# =============================================================================
# 実データ（4銘柄）
# =============================================================================

_REAL_SUMMARY: list[str] = []


def _real_as_of(code: str | None = None) -> str:
    """daily.csv の「確定した」最終営業日。**日付を直書きしない**。

    週次取得で必ず更新されるため、固定値にすると次の実行で CI が落ちる
    （tests は weekly.yml のデプロイ判定に入っている）。

    「確定した」= close（2ソース照合を通った採用値）が入っている日。
    最新営業日は片方の取得元がまだ当日分を出しておらず照合が成立しないことが
    あり、その行は指標の対象外になる（indicators.drop_unconfirmed_tail）。
    judge の基準日もその確定日に揃うので、ここも close のある日で取る。
    """
    return rd.last_confirmed_date(code)


def _has_kpi(code: str) -> bool:
    p = ROOT / "data" / "kpi" / f"{code}.csv"
    return p.exists() and p.stat().st_size > 0


def test_real_data_judges_without_exception():
    master = J.load_master()
    verdicts = J.judge_all(master)
    eq([v.code for v in verdicts], rd.watched_codes(),
       "証券コード順（judge は watch: excluded を判定しない）")
    for v in verdicts:
        assert v.stamp in J.STAMPS, f"{v.code}: 未定義のスタンプ {v.stamp!r}"
        assert v.reason, f"{v.code}: 根拠が空"
        # 基準日は銘柄ごとの「確定した最終足」。全銘柄で同じ日とは限らない
        # （片方の取得元がその日ぶんを出していない銘柄があると1日ずれる）。
        eq(v.as_of, _real_as_of(v.code), f"{v.code}: 基準日は最終足の日付")
        eq(len(v.checks), len(J.STAGE_ORDER), f"{v.code}: 全ゲートを記録")
        eq(len(v.screen), 6, f"{v.code}: スクリーニング5条件＋参考1行")
        # KPI が未整備の間は、ファンダ2条件が必ず「未計算(?)」であること。
        # KPI が入った銘柄では成立しなくなるので、そのときは検証しない。
        if not _has_kpi(v.code):
            marks = {s.key: s.mark for s in v.screen}
            eq(marks["revenue_yoy_pct"], J.MARK_UNKNOWN, f"{v.code}: KPI未整備")
        _STAMPS_SEEN.add(v.stamp)
        _REAL_SUMMARY.append(f"{v.code} {v.name:<12} {v.stamp:<12} "
                             f"{v.stage_label:<26} {v.reason}")


def test_real_data_is_deterministic():
    a = J.judge_all()
    b = J.judge_all()
    eq(a, b, "同一入力に対して同一出力（決定論的生成）")


def test_real_data_no_buy_without_kpi():
    """KPI が無い銘柄に「買」が出ないこと（CLAUDE.md の不変条件）。

    KPI が入った銘柄は「買」に到達しうるので対象から外す。
    """
    for v in J.judge_all():
        if _has_kpi(v.code):
            continue
        assert v.stamp != J.STAMP_BUY, f"{v.code}: KPI未整備なのに買が出ている"


# =============================================================================
# ランナー
# =============================================================================

def main() -> int:
    tests = [(name, fn) for name, fn in sorted(globals().items())
             if name.startswith("test_") and callable(fn)]
    failed = []
    for name, fn in tests:
        try:
            fn()
            print(f"  PASS  {name}")
        except AssertionError as e:
            failed.append((name, str(e)))
            print(f"  FAIL  {name}: {e}")
        except Exception as e:  # noqa: BLE001
            failed.append((name, f"{type(e).__name__}: {e}"))
            print(f"  ERROR {name}: {type(e).__name__}: {e}")

    missing = set(J.STAMPS) - _STAMPS_SEEN
    if missing:
        failed.append(("スタンプ網羅", f"未到達のスタンプ: {sorted(missing)}"))
        print(f"  FAIL  スタンプ網羅: 未到達 {sorted(missing)}")
    else:
        print(f"  PASS  スタンプ網羅（7種すべてに到達）")

    if _REAL_SUMMARY:
        print("\n--- 実データの判定（daily.csv の最終営業日時点・4銘柄） ---")
        for line in _REAL_SUMMARY:
            print(line)

    print(f"\n{len(tests) + 1 - len(failed)}/{len(tests) + 1} passed")
    if failed:
        for name, msg in failed:
            print(f"  - {name}: {msg}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
