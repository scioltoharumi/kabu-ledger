"""indicators.py のテスト。

方針:
  - 期待値は「手計算できる小さい系列」で固定する。ライブラリの出力を写さない。
    一目均衡表と RSI は定義から手で追える形の系列を使い、途中値もコメントに残す。
  - 期間不足・欠測で None が返ることを、指標ごとに確認する（通過扱いにしない担保）。
  - 実データ（data/prices/daily.csv）で全指標が算出できることを確認する。銘柄数・営業日数は
    書かない（週次で増えるうえ、新規登録した銘柄は履歴が短い）。

実行:
  $env:PYTHONIOENCODING = "utf-8"; python tests/test_indicators.py
"""
from __future__ import annotations

import csv
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import indicators as ind  # noqa: E402
import realdata as rd  # noqa: E402


# --- 検証ヘルパ ---------------------------------------------------------------

def eq(actual, expected, label=""):
    assert actual == expected, f"{label}: expected {expected!r}, got {actual!r}"


def close_to(actual, expected, label="", tol=1e-9):
    assert actual is not None, f"{label}: expected {expected!r}, got None"
    assert abs(actual - expected) <= tol, \
        f"{label}: expected {expected!r}, got {actual!r} (diff {actual - expected})"


def is_none(actual, label=""):
    assert actual is None, f"{label}: expected None, got {actual!r}"


def bar(date, o, h, lo, c, v):
    return ind.Bar(date=date, open=o, high=h, low=lo, close=c, volume=v)


# =============================================================================
# sma / sma_series / highest / lowest / midpoint
# =============================================================================

def test_sma():
    close_to(ind.sma([1, 2, 3, 4, 5], 3), 4.0, "sma(last3 of 1..5)")   # (3+4+5)/3
    close_to(ind.sma([1, 2, 3, 4, 5], 5), 3.0, "sma(all)")
    close_to(ind.sma([0, 0, 0], 3), 0.0, "sma(zeros)")                 # 0 は有効値
    is_none(ind.sma([1, 2, 3], 5), "sma 期間不足")
    is_none(ind.sma([1, None, 3], 3), "sma 欠測混入")
    is_none(ind.sma([], 3), "sma 空")
    is_none(ind.sma([1, 2, 3], 0), "sma n=0")


def test_sma_series():
    eq(ind.sma_series([1, 2, 3, 4], 2), [None, 1.5, 2.5, 3.5], "sma_series")
    eq(ind.sma_series([1, None, 3, 4], 2), [None, None, None, 3.5],
       "sma_series 欠測は前後2点ぶんが None")


def test_highest_lowest_midpoint():
    close_to(ind.highest([1, 5, 3], 3), 5.0, "highest")
    close_to(ind.lowest([1, 5, 3], 3), 1.0, "lowest")
    # (3期間高値5 + 3期間安値0) / 2
    close_to(ind.midpoint([1, 5, 3], [0, 2, 1], 3), 2.5, "midpoint")
    is_none(ind.midpoint([1, 5, 3], [0, None, 1], 3), "midpoint 欠測")


# =============================================================================
# to_weekly
# =============================================================================
# 2026-08-03(月)〜08-07(金) が ISO 2026-W32、08-10(月) が 2026-W33。
_WEEK_BARS = [
    bar("2026-08-03", 100.0, 105.0, 99.0, 104.0, 1000),
    bar("2026-08-04", 104.0, 110.0, 103.0, 108.0, 2000),
    bar("2026-08-05", 108.0, 109.0, 95.0, 97.0, 1500),
    bar("2026-08-06", 97.0, 100.0, 96.0, 99.0, 0),      # NO_TRADE 相当（欠測ではない）
    bar("2026-08-07", 99.0, 102.0, 98.0, 101.0, 500),
    bar("2026-08-10", 101.0, 103.0, 100.0, 102.0, 700),
]


def test_to_weekly():
    wk = ind.to_weekly(_WEEK_BARS)
    eq(len(wk), 2, "週数")
    w32, w33 = wk

    eq(w32.week, "2026-W32", "W32 week")
    eq(w32.date, "2026-08-07", "W32 date=週内最終営業日")
    close_to(w32.open, 100.0, "W32 始値=週初の始値")
    close_to(w32.high, 110.0, "W32 高値=週内最高")
    close_to(w32.low, 95.0, "W32 安値=週内最安")
    close_to(w32.close, 101.0, "W32 終値=週末終値")
    eq(w32.volume, 5000, "W32 出来高=合計（0の日を含む）")
    eq(w32.days, 5, "W32 営業日数")

    eq(w33.week, "2026-W33", "W33 week")
    eq(w33.days, 1, "W33 は部分週")
    close_to(w33.close, 102.0, "W33 終値")
    eq(w33.volume, 700, "W33 出来高")


def test_to_weekly_is_order_independent():
    eq(ind.to_weekly(list(reversed(_WEEK_BARS))), ind.to_weekly(_WEEK_BARS),
       "入力順に依存しない（決定論的生成）")


def test_to_weekly_keeps_missing_as_none():
    bars = [
        bar("2026-08-03", 100.0, None, 99.0, 104.0, 1000),   # 高値のみ欠測
        bar("2026-08-04", 104.0, 110.0, 103.0, None, 2000),  # 終値のみ欠測（SINGLE_SOURCE）
    ]
    w = ind.to_weekly(bars)[0]
    is_none(w.high, "週高値: 1本でも欠測なら None（最大値で隠さない）")
    close_to(w.low, 99.0, "週安値は算出できる")
    is_none(w.close, "週末終値が欠測なら週足終値も None")
    eq(w.volume, 3000, "出来高は算出できる")


# =============================================================================
# slope / slope_pct / slope_direction
# =============================================================================

def test_slope():
    close_to(ind.slope([1, 2, 3, 4, 5], 5), 1.0, "slope 上昇")
    close_to(ind.slope([5, 4, 3, 2, 1], 5), -1.0, "slope 下降")
    close_to(ind.slope([3, 3, 3], 3), 0.0, "slope 平ら")
    # 最小二乗なので端点のノイズで符号が反転しない: 1,2,3,4,0 は依然として…
    # x平均2, y平均2 → Σ(x-2)(y-2) = (-2)(-1)+(-1)(0)+0+1*2+2*(-2) = 2+0+0+2-4 = 0
    close_to(ind.slope([1, 2, 3, 4, 0], 5), 0.0, "slope 末尾急落で0")
    is_none(ind.slope([1, 2, 3], 5), "slope 期間不足")
    is_none(ind.slope([1, 2, 3], 1), "slope n<2")
    is_none(ind.slope([1, None, 3], 3), "slope 欠測")


def test_slope_pct():
    # slope=1.0, 平均=102 → 0.980392...%/期間
    close_to(ind.slope_pct([100, 101, 102, 103, 104], 5), 100.0 / 102.0,
             "slope_pct", tol=1e-12)
    is_none(ind.slope_pct([0, 0, 0], 3), "slope_pct 水準0")


def test_slope_direction_thresholds():
    up = [100, 101, 102, 103, 104]        # 約 +0.98%/期間
    down = [104, 103, 102, 101, 100]
    flat = [100.0, 100.02, 100.04, 100.06, 100.08]   # 約 +0.02%/期間
    eq(ind.slope_direction(up, 5), "up", "up")
    eq(ind.slope_direction(down, 5), "down", "down")
    eq(ind.slope_direction(flat, 5), "flat", "flat（平行）")

    # 同じ系列でも日足閾値と週足閾値で結論が変わる（定数の効き目の確認）
    mid = [100.0, 100.1, 100.2, 100.3]    # 約 +0.0999%/期間
    eq(ind.slope_direction(mid, 4, ind.SLOPE_FLAT_PCT_PER_DAY), "up",
       "日足閾値0.05%では up")
    eq(ind.slope_direction(mid, 4, ind.SLOPE_FLAT_PCT_PER_WEEK), "flat",
       "週足閾値0.25%では平行")
    is_none(ind.slope_direction([1, 2], 5), "slope_direction 期間不足")


# =============================================================================
# RSI（Wilder）
# =============================================================================

def test_rsi_hand_calculated():
    # n=2, closes = [10, 11, 10.5, 11.5]
    #   値幅   : +1.0, -0.5, +1.0
    #   初期(2本): avg_gain=(1.0+0)/2=0.5, avg_loss=(0+0.5)/2=0.25
    #   3本目   : avg_gain=(0.5*1+1.0)/2=0.75, avg_loss=(0.25*1+0)/2=0.125
    #   RS=6.0 → RSI=100-100/7=85.714285714...
    close_to(ind.rsi([10, 11, 10.5, 11.5], 2), 100.0 - 100.0 / 7.0,
             "RSI(2) 手計算", tol=1e-12)


def test_rsi_extremes():
    eq(ind.rsi(list(range(1, 17)), 14), 100.0, "全上昇 → 100")
    eq(ind.rsi(list(range(16, 0, -1)), 14), 0.0, "全下降 → 0")
    # 完全横ばい。100 を返すと NO_TRADE 続きの薄商い銘柄が過熱扱いになるため 50。
    eq(ind.rsi([100.0] * 20, 14), 50.0, "無変動 → 50")


def test_rsi_wilder_not_simple_average():
    # 単純平均版とWilder版で値が分かれる系列。
    # closes: 14本ぶんの値幅が全て +1、最後の1本だけ -14。
    closes = [float(i) for i in range(15)] + [0.0]
    #   単純平均(直近14本の値幅) : gains=13/14≒0.9286, losses=14/14=1.0 → RSI≒48.15
    #   Wilder                   : 初期 avg_gain=1.0, avg_loss=0.0
    #                              最終 avg_gain=(1.0*13+0)/14=0.928571...
    #                                   avg_loss=(0.0*13+14)/14=1.0
    #                              RS=0.928571... → RSI=48.148148...
    got = ind.rsi(closes, 14)
    expected_gain = (1.0 * 13 + 0.0) / 14
    expected_loss = (0.0 * 13 + 14.0) / 14
    expected = 100.0 - 100.0 / (1.0 + expected_gain / expected_loss)
    close_to(got, expected, "RSI(14) Wilder 平滑", tol=1e-12)


def test_rsi_guards():
    is_none(ind.rsi(list(range(1, 15)), 14), "RSI 期間不足（n+1本未満）")
    is_none(ind.rsi([None] + list(range(1, 17)), 14), "RSI 窓に欠測")
    is_none(ind.rsi([], 14), "RSI 空")


def test_rsi_warmup_truncation():
    # 系列先頭に欠測があっても、暖機本数(n*10+1=141)より後ろが揃っていれば算出できる。
    seq = [None] * 5 + [100.0 + (i % 3) for i in range(200)]
    got = ind.rsi(seq, 14)
    assert got is not None, "暖機打ち切りにより先頭の欠測に引きずられない"
    assert 0.0 <= got <= 100.0, f"RSI が定義域外: {got}"


# =============================================================================
# 一目均衡表
# =============================================================================

def _ramp(n):
    """close = 100+i、high = close+1、low = close-1 の単調増加系列。"""
    highs = [101.0 + i for i in range(n)]
    lows = [99.0 + i for i in range(n)]
    closes = [100.0 + i for i in range(n)]
    return highs, lows, closes


def test_ichimoku_hand_calculated():
    highs, lows, closes = _ramp(100)   # index 0..99
    ich = ind.ichimoku(highs, lows, closes)
    # 転換線(9)  = (high[99]=200 + low[91]=190) / 2
    close_to(ich.tenkan, 195.0, "転換線")
    # 基準線(26) = (high[99]=200 + low[74]=173) / 2
    close_to(ich.kijun, 186.5, "基準線")
    # 先行スパンA = (195 + 186.5) / 2
    close_to(ich.span_a, 190.75, "先行スパンA（当日算出値）")
    # 先行スパンB(52) = (high[99]=200 + low[48]=147) / 2
    close_to(ich.span_b, 173.5, "先行スパンB（当日算出値）")
    # 当日に掛かる雲は 26本前(index 73)の算出値
    #   転換@73 = (high[73]=174 + low[65]=164)/2 = 169
    #   基準@73 = (high[73]=174 + low[48]=147)/2 = 160.5
    #   スパンA@73 = 164.75
    #   スパンB@73 = (high[73]=174 + low[22]=121)/2 = 147.5
    close_to(ich.cloud_top, 164.75, "雲の上端")
    close_to(ich.cloud_bottom, 147.5, "雲の下端")
    eq(ich.position, "above", "close=199 は雲の上")
    eq(ich.prev_position, "above", "前日も雲の上")
    is_none(ich.cross, "既に上にいるので上抜けではない")


def _flat_then(last_high, last_low, last_close, flat_bars=78):
    """flat_bars 本の完全横ばい（high110/low90/close100）＋最終1本。"""
    highs = [110.0] * flat_bars + [last_high]
    lows = [90.0] * flat_bars + [last_low]
    closes = [100.0] * flat_bars + [last_close]
    return highs, lows, closes


def test_ichimoku_breakout_up():
    # 横ばい区間の雲は上端=下端=100（転換・基準・スパンBすべて (110+90)/2）。
    # 前日 close=100 は雲の中(in)、当日 close=120 は雲の上(above) → 上抜け。
    ich = ind.ichimoku(*_flat_then(125.0, 100.0, 120.0))
    close_to(ich.cloud_top, 100.0, "横ばい区間の雲上端")
    close_to(ich.cloud_bottom, 100.0, "横ばい区間の雲下端")
    eq(ich.prev_position, "in", "前日は雲の中")
    eq(ich.position, "above", "当日は雲の上")
    eq(ich.cross, "breakout_up", "スクリーニング基準の「一目均衡表 上抜け」")


def test_ichimoku_breakdown():
    # 鉄則「雲を下に抜けたらすぐ売る」に対応。
    ich = ind.ichimoku(*_flat_then(100.0, 75.0, 80.0))
    eq(ich.prev_position, "in", "前日は雲の中")
    eq(ich.position, "below", "当日は雲の下")
    eq(ich.cross, "breakdown", "雲の下抜け")


def test_ichimoku_insufficient_history():
    # 雲の完成には 52 + 26 = 78本、上抜け/下抜け判定にはさらに1本必要。
    highs, lows, closes = _ramp(77)
    ich = ind.ichimoku(highs, lows, closes)
    is_none(ich.cloud_top, "77本では雲が張れない")
    is_none(ich.position, "位置も判定できない")
    is_none(ich.cross, "交差も判定できない")

    highs, lows, closes = _ramp(78)
    ich = ind.ichimoku(highs, lows, closes)
    assert ich.cloud_top is not None, "78本で雲は張れる"
    assert ich.position is not None, "位置は判定できる"
    is_none(ich.prev_position, "前日の雲はまだ無い")
    is_none(ich.cross, "78本では上抜け/下抜けを判定しない（未計算）")


def test_ichimoku_missing_close():
    highs, lows, closes = _ramp(100)
    closes = closes[:-1] + [None]      # 最終終値が欠測（SINGLE_SOURCE 等）
    ich = ind.ichimoku(highs, lows, closes)
    assert ich.cloud_top is not None, "雲は高値安値から張れる"
    is_none(ich.position, "終値が無ければ位置を決めない（推定しない）")
    is_none(ich.cross, "交差も判定しない")


def test_ichimoku_length_mismatch():
    highs, lows, closes = _ramp(100)
    is_none(ind.ichimoku(highs, lows[:-1], closes).position, "系列長不一致")


# =============================================================================
# 移動平均乖離率
# =============================================================================

def test_ma_deviation_pct():
    close_to(ind.ma_deviation_pct([100.0] * 25, 25), 0.0, "乖離率0")
    # sma = (100*24 + 105)/25 = 100.2 → (105 - 100.2)/100.2*100
    close_to(ind.ma_deviation_pct([100.0] * 24 + [105.0], 25),
             (105.0 - 100.2) / 100.2 * 100.0, "乖離率 手計算", tol=1e-12)
    close_to(ind.ma_deviation_pct([100.0] * 24 + [95.0], 25),
             (95.0 - 99.8) / 99.8 * 100.0, "乖離率（下方向）", tol=1e-12)
    is_none(ind.ma_deviation_pct([100.0] * 24, 25), "乖離率 期間不足")
    is_none(ind.ma_deviation_pct([100.0] * 24 + [None], 25), "乖離率 欠測")


# =============================================================================
# 出来高
# =============================================================================

def test_volume_ratio():
    # 直近5日平均500 ÷ 60営業日前を終点とする5日平均100 = 5.0（スクリーニング基準ちょうど）
    volumes = [100] * 5 + [0] * 55 + [500] * 5
    eq(len(volumes), 65, "必要本数 = 60 + 5")
    close_to(ind.volume_ratio(volumes), 5.0, "出来高増加率 5倍")
    eq(ind.volume_ratio(volumes) >= ind.VOLUME_RATIO_SCREEN, True,
       "スクリーニング基準の判定に使える")


def test_volume_ratio_zero_base():
    # 比較先が全て0（NO_TRADE 連続）→ 倍率として定義できないので None。
    # 便宜的に大きな値を返すと、薄商い銘柄が常に条件を満たしてしまう。
    volumes = [0] * 5 + [1] * 55 + [500] * 5
    is_none(ind.volume_ratio(volumes), "比較先が0 → None")


def test_volume_ratio_counts_zero_volume():
    # 直近5日のうち1日が NO_TRADE(0)。0 を欠測として除外せず平均に算入する。
    volumes = [100] * 5 + [100] * 55 + [500, 500, 500, 500, 0]
    close_to(ind.volume_ratio(volumes), (500 * 4 + 0) / 5 / 100.0,
             "0 を除外せず平均に算入", tol=1e-12)


def test_volume_ratio_guards():
    is_none(ind.volume_ratio([100] * 64), "本数不足（65本未満）")
    is_none(ind.volume_ratio([100] * 60 + [None] + [100] * 4), "欠測混入")


def test_avg_turnover():
    close_to(ind.avg_turnover([100.0] * 20, [1000] * 20, 20), 100_000.0,
             "20日平均売買代金")
    # NO_TRADE の日は 0 円として算入する（除外すると流動性を過大評価する）
    close_to(ind.avg_turnover([100.0] * 20, [1000] * 19 + [0], 20),
             100_000.0 * 19 / 20, "出来高0の日を0円として算入")
    is_none(ind.avg_turnover([100.0] * 19, [1000] * 19, 20), "期間不足")
    is_none(ind.avg_turnover([100.0] * 19 + [None], [1000] * 20, 20), "終値欠測")
    is_none(ind.avg_turnover([100.0] * 19 + [100.0], [1000] * 19 + [None], 20),
            "出来高欠測（0 と区別する）")
    is_none(ind.avg_turnover([100.0] * 20, [1000] * 19, 20), "系列長不一致")


# =============================================================================
# ゴールデン/デッドクロス
# =============================================================================

def test_cross_golden():
    long_ma = [100.0] * 6
    short_ma = [97.0, 98.0, 99.0, 100.5, 101.5, 102.5]   # 4本目で上抜け
    sig = ind.cross_signal(short_ma, long_ma)
    eq(sig.crossed, "up", "実際の上抜け")
    eq(sig.kind, "golden", "ゴールデンクロス")
    close_to(sig.spread_pct, 2.5, "乖離率", tol=1e-12)


def test_cross_dead():
    long_ma = [100.0] * 6
    short_ma = [103.0, 102.0, 101.0, 99.5, 98.5, 97.5]
    sig = ind.cross_signal(short_ma, long_ma)
    eq(sig.crossed, "down", "実際の下抜け")
    eq(sig.kind, "dead", "デッドクロス")


def test_cross_parallel():
    # 短期線が長期線のちょうど1.02倍。%で見た傾きが完全に等しい＝平行。
    long_ma = [100.0, 101.0, 102.0, 103.0, 104.0, 105.0]
    short_ma = [v * 1.02 for v in long_ma]
    sig = ind.cross_signal(short_ma, long_ma)
    eq(sig.parallel, True, "平行")
    eq(sig.kind, "parallel", "鉄則が除外する「平行」を明示的に返す")
    eq(sig.crossed, None, "交差なし")
    assert abs(sig.slope_diff_pct) < ind.CROSS_PARALLEL_SLOPE_DIFF_PCT, \
        f"傾き差が閾値未満のはず: {sig.slope_diff_pct}"


def test_cross_golden_ish():
    # 未交差だが短期線が相対的に強く上向き＝「ゴールデンクロス気味(平行ではない)」
    short_ma = [90.0, 92.0, 94.0, 96.0, 98.0, 100.0]
    long_ma = [110.0, 110.2, 110.4, 110.6, 110.8, 111.0]
    sig = ind.cross_signal(short_ma, long_ma)
    eq(sig.crossed, None, "まだ交差していない")
    eq(sig.parallel, False, "平行ではない")
    eq(sig.kind, "golden_ish", "ゴールデンクロス気味")
    assert sig.slope_diff_pct > 0, "傾き差は正"


def test_cross_dead_ish():
    # 短期線が上にあるが縮小方向＝「デッドクロス気味(平行ではない)」
    short_ma = [110.0, 108.0, 106.0, 104.0, 102.0, 100.0]
    long_ma = [90.0, 90.2, 90.4, 90.6, 90.8, 91.0]
    sig = ind.cross_signal(short_ma, long_ma)
    eq(sig.crossed, None, "まだ交差していない")
    eq(sig.kind, "dead_ish", "デッドクロス気味")
    assert sig.slope_diff_pct < 0, "傾き差は負"


def test_cross_guards():
    is_none(ind.cross_signal([1.0, 2.0], [1.0, 2.0]).kind, "点数不足")
    is_none(ind.cross_signal([1.0] * 6, [1.0] * 5).kind, "系列長不一致")
    is_none(ind.cross_signal([1.0] * 5 + [None], [1.0] * 6).kind, "欠測混入")
    is_none(ind.cross_signal(ind.sma_series([1.0] * 3, 25),
                             ind.sma_series([1.0] * 3, 75)).kind,
            "MA が張れていない（全 None）")


# =============================================================================
# bars_from_rows
# =============================================================================

def test_bars_from_rows_does_not_fill_close():
    rows = [
        {"date": "2026-08-04", "code": "4073", "open": "1.0", "high": "2.0",
         "low": "0.5", "close": "", "volume": "0", "status": "SINGLE_SOURCE",
         "value_primary": "1.5"},
        {"date": "2026-08-03", "code": "4073", "open": "1.0", "high": "2.0",
         "low": "0.5", "close": "1.2", "volume": "300", "status": "OK",
         "value_primary": "1.2"},
        {"date": "2026-08-03", "code": "9999", "open": "1.0", "high": "2.0",
         "low": "0.5", "close": "9.9", "volume": "10", "status": "OK",
         "value_primary": "9.9"},
    ]
    # このテストは「close を埋めないこと」を見るので、末尾の未確定行を
    # 落とさない生の並びで取る（confirmed_only の既定は True）
    bars = ind.bars_from_rows(rows, code="4073", confirmed_only=False)
    eq(len(bars), 2, "code で絞り込む")
    eq([b.date for b in bars], ["2026-08-03", "2026-08-04"], "日付昇順に並べ替える")
    is_none(bars[1].close, "close が空なら None（value_primary で埋めない）")
    eq(bars[1].volume, 0, "出来高0 は 0 のまま（None にしない）")
    close_to(bars[0].close, 1.2, "OK 行の終値")


# =============================================================================
# 実データ（data/prices/daily.csv）
# =============================================================================

_CSV = ROOT / "data" / "prices" / "daily.csv"
_REAL_SUMMARY: list[str] = []


def _load_real_rows():
    with _CSV.open(encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _min_bars_needed() -> int:
    """全指標を算出するのに必要な最小の営業日数（**定数から導く**）。

    ここに実データの行数（1076 等）を書くと、次の週次取得で意味を失ううえ、
    「何本あれば足りるのか」がテストから読み取れなくなる。要求は指標側の
    定数で決まっているので、そこから引く。
    """
    return max(
        ind.ICHIMOKU_SPAN_B_PERIODS + ind.ICHIMOKU_DISPLACEMENT + 1,
        ind.VOLUME_RATIO_LOOKBACK_DAYS + ind.VOLUME_RATIO_WINDOW_DAYS,
        ind.WEEKLY_MA_LONG_PERIODS * 5,      # 26週ぶんの営業日
    )


def test_real_data_all_indicators():
    rows = _load_real_rows()
    need = _min_bars_needed()
    codes = sorted({r["code"] for r in rows})
    eq(codes, rd.codes(), "銘柄（master.yaml と一致する）")

    header = (f"{'code':>5} {'bars':>5} {'wk':>4} {'close':>8} {'sma25':>9} "
              f"{'dev%':>7} {'rsi14':>6} {'雲':>6} {'交差':>12} "
              f"{'vol比':>7} {'20日代金(万円)':>15} {'週MA13傾き':>11} {'週MA向き':>8} "
              f"{'日足5/25':>10}")
    _REAL_SUMMARY.append(header)

    # 営業日の網羅は**生の行**で見る。確定足（bars_from_rows の既定）で数えると、
    # 「最新営業日の照合が一部の銘柄でだけ成立した」ふつうの状態で本数がずれ、
    # データが1日進むたびに落ちる。取得漏れの検出は checks.py の coverage の担当。
    #
    # ★本数の一致は要求しない。**新しく登録した銘柄は履歴が短い**（intake の
    #   `fetch.py --historical` は登録日から遡るので、先に登録済みの銘柄より
    #   始まりが遅い）。2026-08-21 に 150A を登録した時点で、本数の一致を
    #   求めるこの検査は落ちた。ここで確かめたいのは「途中が抜けていないこと」
    #   なので、**全銘柄の日付が全体の営業日の連続した末尾になっている**ことを見る。
    #   途中の穴（＝取得漏れ）はこの形を必ず壊す。
    all_days = sorted({r["date"] for r in rows})
    for c in codes:
        days = sorted({r["date"] for r in rows if r["code"] == c})
        eq(days, all_days[-len(days):],
           f"{c}: 営業日が全体の連続した末尾になっていない"
           f"（{len(days)}日 / 全体 {len(all_days)}日・途中に穴がある疑い）")

    for code in codes:
        bars = ind.bars_from_rows(rows, code=code)
        assert len(bars) >= need, \
            f"{code}: 確定足が {len(bars)}本しかなく、全指標には {need}本必要"

        closes = [b.close for b in bars]
        highs = [b.high for b in bars]
        lows = [b.low for b in bars]
        volumes = [b.volume for b in bars]

        # CSV で close が空の行は、Bar でも None のまま（value_primary で埋めない）。
        # 特定の行番号を当てにせず、**空だった行すべて**について確かめる。
        empty_days = {r["date"] for r in rows
                      if r["code"] == code and not str(r["close"] or "").strip()}
        assert empty_days, f"{code}: close が空の行が1つも無い（前提が変わっている）"
        for b in bars:
            if b.date in empty_days:
                assert b.close is None, \
                    f"{code} {b.date}: 照合を通っていない値が close に入っている"

        # 確定足の末尾は必ず採用値を持つ（drop_unconfirmed_tail の担保）。
        # 「最終行に終値がある」を生の行に対して仮定すると、最新営業日が
        # 未照合になったふつうの週に落ちる。
        assert closes[-1] is not None, \
            f"{code}: 確定足の末尾に採用値が無い（未確定行が残っている）"

        ma25 = ind.sma(closes, ind.DAILY_MA_MID_PERIODS)
        dev = ind.ma_deviation_pct(closes)
        r = ind.rsi(closes)
        ich = ind.ichimoku(highs, lows, closes)
        vr = ind.volume_ratio(volumes)
        turn = ind.avg_turnover(closes, volumes)

        weekly = ind.to_weekly(bars)
        wcloses = [w.close for w in weekly]
        wma_mid = ind.sma_series(wcloses, ind.WEEKLY_MA_MID_PERIODS)
        wslope = ind.slope_pct(wma_mid, ind.SLOPE_LOOKBACK_WEEKS)
        wdir = ind.slope_direction(wma_mid, ind.SLOPE_LOOKBACK_WEEKS,
                                   ind.SLOPE_FLAT_PCT_PER_WEEK)

        sig = ind.cross_signal(ind.sma_series(closes, ind.DAILY_MA_SHORT_PERIODS),
                               ind.sma_series(closes, ind.DAILY_MA_MID_PERIODS))

        for name, val in [("sma25", ma25), ("ma_deviation_pct", dev), ("rsi14", r),
                          ("cloud_top", ich.cloud_top), ("cloud_bottom", ich.cloud_bottom),
                          ("ichimoku_position", ich.position),
                          ("prev_position", ich.prev_position),
                          ("tenkan", ich.tenkan), ("kijun", ich.kijun),
                          ("span_a", ich.span_a), ("span_b", ich.span_b),
                          ("volume_ratio", vr), ("avg_turnover_20d", turn),
                          ("weekly_ma13_slope_pct", wslope),
                          ("weekly_ma13_direction", wdir),
                          ("cross_kind", sig.kind)]:
            assert val is not None, f"{code}: {name} が算出できていない"

        assert 0.0 <= r <= 100.0, f"{code}: RSI 定義域外 {r}"
        assert ich.cloud_top >= ich.cloud_bottom, f"{code}: 雲の上下が逆転"
        assert ich.position in ("above", "in", "below"), f"{code}: position 不正"
        assert turn > 0, f"{code}: 平均売買代金が0以下"
        assert len(weekly) >= ind.WEEKLY_MA_LONG_PERIODS, \
            f"{code}: 週足 {len(weekly)}本では26週MAが張れない"

        _REAL_SUMMARY.append(
            f"{code:>5} {len(bars):>5} {len(weekly):>4} {closes[-1]:>8.1f} "
            f"{ma25:>9.2f} {dev:>7.2f} {r:>6.2f} {ich.position:>6} "
            f"{str(ich.cross):>12} {vr:>7.2f} {turn / 10000:>15,.0f} "
            f"{wslope:>11.3f} {wdir:>8} {str(sig.kind):>10}"
        )


def test_real_data_no_trade_rows_are_zero_volume():
    rows = _load_real_rows()
    no_trade = [r for r in rows
                if "NO_TRADE" in str(r["status"] or "").split("|")]
    # 件数はべた書きしない（append-only なので増える）。「1件も無い＝この検査が
    # 空回りしている」ことだけを見て、あとは全件について不変条件を確かめる。
    assert no_trade, "NO_TRADE の行が1つも無く、この検査が空回りしている"
    quoted = 0
    for r in no_trade:
        eq(int(r["volume"]), 0, f"{r['date']} {r['code']} の出来高")
        # NO_TRADE は照合結果に**付加**されるフラグ（D24）。売買不成立の日に
        # 終値（気配値）が載るかは、その日にどの取得元が値を返したかで変わる。
        # 「NO_TRADE なら必ず終値がある」と決めつけると、片側の取得元しか
        # 当日分を出していない週に落ちる。**入っているなら主ソースと一致する**
        # ことだけを全件で見て、気配値の記録が生きていることは総数で見る。
        if r["close"]:
            quoted += 1
            eq(r["close"], r["value_primary"],
               f"{r['date']} {r['code']}: 終値と主ソースの値が食い違う")
    assert quoted, "NO_TRADE の日に終値（気配値）が1件も残っていない"

    # NO_TRADE を含む銘柄でも 20日平均売買代金は算出できる（0を含めて平均する）
    codes = sorted({r["code"] for r in no_trade})
    for code in codes:
        bars = ind.bars_from_rows(rows, code=code)
        # 採用値のある行だけで計算する（最新日は照合が成立せず空のことがある）
        priced = [b for b in bars if b.close is not None]
        turn = ind.avg_turnover([b.close for b in priced], [b.volume for b in priced])
        assert turn is not None and turn > 0, \
            f"{code}: NO_TRADE を含んでも売買代金は算出できる"


def test_real_data_is_deterministic():
    rows = _load_real_rows()
    bars = ind.bars_from_rows(rows, code="6570")
    closes = [b.close for b in bars]
    a = (ind.sma(closes, 25), ind.rsi(closes), ind.ichimoku(
        [b.high for b in bars], [b.low for b in bars], closes))
    b = (ind.sma(closes, 25), ind.rsi(closes), ind.ichimoku(
        [b.high for b in bars], [b.low for b in bars], closes))
    eq(a, b, "同一入力に対して同一出力（決定論的）")


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

    if _REAL_SUMMARY:
        print("\n--- 実データの算出結果（daily.csv の最終営業日時点・4銘柄） ---")
        for line in _REAL_SUMMARY:
            print(line)

    print(f"\n{len(tests) - len(failed)}/{len(tests)} passed")
    if failed:
        for name, msg in failed:
            print(f"  - {name}: {msg}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
