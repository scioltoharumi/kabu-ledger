"""鉄則ベースの判定ロジック。

要件: requirements.md F5 / F13、input/investment-rules.md（判定軸の正）、
      gap-analysis.md §6・§7、decisions.md D15/D18。

設計原則（破らないこと）:
  1. **計算はコード、判定もコード**。LLM に数値を計算・判定させない（D3/D9/D14）。
     このモジュールは indicators.py の純関数だけを使い、外部 I/O は main() に閉じている。
  2. **未計算を通過扱いにしない**（F5-4）。指標が None のゲートは pass でも fail でもなく
     `unknown` として扱い、その場で「調査」に落として止める。
     既存実装は `turnover is not None` の条件で出来高欠測時に流動性ゲートごと素通りしていた
     （review-findings.md F-02。「流動性が確認できない」が「ゲート通過」になっていた）。
     **同じ罠を作らない。** 各ゲートは「判定できなかった」と「条件を満たした」を必ず区別する。
  3. **決定論的**。基準日は壁時計ではなく最終足の日付（as_of）を使う。同一入力→同一出力。
  4. 閾値はこのファイル先頭の定数ブロックか data/master.yaml に集約する。
     indicators.py が既に持っている閾値は**再定義せず参照する**（SSoT）。

判定順序（上から評価し、該当した時点で確定する）:

    HS. 保有中の売りシグナル（保有のみ・**買い側のどのゲートよりも先に見る**）
          雲の下 / 逆指値ライン抵触 / 6か月2倍ライン到達×デッドクロス気味 → 売り
    H0. 逆指値ラインの判定可否（保有のみ）  算出できない                  → 調査
     1. 流動性ゲート       20日平均売買代金 < 3,000万円          → 流動性低
     2. 週足MAの傾き       参考表示のみ（2026-09-05 にゲートから格下げ。
                           トレンドの読みは src/shape_chart.py の画像判定※）
     3. 売りシグナル       雲の下                                → 売り（保有）／見送(雲の下)（非保有）
     4. 過熱チェック       RSI>80 / 25日乖離率>8% / 信用倍率>5倍 → 様子見(過熱)
    H5. 6か月2倍ライン     到達×平行 / 未到達（保有のみ）        → 監視
                           到達×ゴールデンクロス気味             → ⑤へ（買い増し可）
     5. ファンダ確認       売上・経常 +30%継続 / 1Q進捗率>30%    → 満たさねば 調査
     6. すべて通過                                                → 買

    どの段階でも、**買い側の**指標が未計算なら → 調査（止める）

**フェイルセーフの向きは買いと売りで逆である**（2026-08-12 改訂）:
  買い側 — 未計算・曖昧は「通過」ではなく「調査」で止める（F5-4）
  売り側 — 上流のゲートが fail / unknown でも売りシグナルを隠さない

旧実装は売りシグナル（③雲・H5の売り分岐）を①流動性・②トレンド・④過熱より下に
置いていたため、保有中に雲を下抜けても、出来高が欠測しているだけで「調査」、
薄商いなら「流動性低」（旧 見送(流動性)）になり、鉄則「雲を下に抜けたらすぐ売る」が
条件付きの指示に化けていた。HS はその修正で、**保有銘柄に限り**売りシグナルを
最上位で評価する。保有していない銘柄では「売り」は行動として意味を持たないので、
requirements.md F5 の順序どおり ③ の位置で評価する。

保有（HS・H0・H5）は毎週フラットに再評価する（F13-5）。前週の判断は入力に持たない。
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from datetime import date as _date
from pathlib import Path
from typing import Any, NamedTuple, Sequence

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))

import indicators as tech  # noqa: E402  （パラメータ名 ind と衝突させないため tech で束縛）
import yamlio as Y


# =============================================================================
# 判定スタンプ（7種。F5-7。ここ以外にリテラルを書かない）
# =============================================================================

STAMP_LIQUIDITY = "流動性低"   # 旧「見送(流動性)」。2026-08-30 改名（表示だけでなく語彙そのもの）
STAMP_TREND = "見送(トレンド)"   # 2026-09-05 以降は出ない（旧 last_stamps.json の読み取り互換のため残す）
STAMP_SELL = "売り"
STAMP_CLOUD = "見送(雲の下)"    # 非保有×雲の下。2026-09-05 追加（旧実装は非保有にも「売り」を出していた）
STAMP_OVERHEAT = "様子見(過熱)"
STAMP_PROBE = "調査"
STAMP_WATCH = "監視"
STAMP_BUY = "買"

STAMPS = (STAMP_LIQUIDITY, STAMP_CLOUD, STAMP_SELL, STAMP_OVERHEAT,
          STAMP_PROBE, STAMP_WATCH, STAMP_BUY)


# =============================================================================
# 定数ブロック（判定閾値。根拠を必ず併記する）
#
# 流動性ゲート閾値・6か月2倍ライン・逆指値率は data/master.yaml が正（SSoT）。
# ここには複製しない。master.yaml に無ければ「未設定」として unknown 扱いにし、
# 既定値で代替しない（設定漏れを通過扱いにしない）。
# =============================================================================

DEFAULT_JUDGE = {
    # --- ④ 過熱チェック -------------------------------------------------------
    # RSI・25日乖離率の閾値は indicators.py の定数を参照する（再定義しない）。
    "rsi_overheat": tech.RSI_OVERHEAT,                        # 80.0 鉄則「RSIが8割超え」
    "ma_deviation_overheat_pct": tech.MA_DEVIATION_OVERHEAT_PCT,  # 8.0 鉄則「7〜8%超は怪しい」
    "margin_ratio_overheat": 5.0,                             # 鉄則「信用倍率5倍超え」
    # 信用残は週次公表。取得も週次なので通常2週以内には更新される。
    # 4週(28日)を超えて古い残高は「現在の需給」を語れないので unknown（＝調査）に落とす。
    "margin_max_age_days": 28,
    # 売り残0（RATIO_NA）の扱い。買残>0 かつ 売残=0 は倍率が定義できないだけで、
    # 経済的には「買い一辺倒」＝5倍超と同じ側にある。**「過熱していない」とは読み替えない**
    # （sources 仕様の注記）。倍率そのものは None のまま記録し、判定だけ過熱側に倒す。
    # False にすると RATIO_NA は unknown（＝調査）になる。どちらでも「通過」にはならない。
    "margin_ratio_na_is_overheat": True,
    # ただし **制度信用が買建のみの銘柄**（貸借銘柄でない）は、売り残0が取引所ルールの
    # 帰結であって需給の情報を持たない。この銘柄で RATIO_NA を過熱側に倒すと
    # 「永久に 様子見(過熱) から出られない」構造ができる（4073 で実際に発生していた）。
    # master.yaml の flags にこのトークンがある銘柄では、信用倍率の条件を
    # **該当しない(n/a)** として扱い、根拠に「構造的に定義不能」と明記する。
    # 過熱チェック自体は RSI・25日乖離率で引き続き行う（ゲートを飛ばすのではない）。
    "margin_short_unavailable_flag": "制度信用: 買建のみ",

    # --- ② トレンドゲート -----------------------------------------------------
    # 鉄則の「週足**中期**移動平均線」がどの期間かは投資ルールに書かれていない
    # （indicators.WEEKLY_MA_MID_PERIODS のコメント参照）。**確定するまで両方を見る**。
    # どちらかが下向きなら不通過（保守側）。マスターが確定したらこの配列を1つにする。
    "weekly_trend_periods": list(tech.WEEKLY_TREND_PERIODS),   # [13, 26]
    # 「下がっている」の閾値。不感帯を置くと年率-12%の下降が横ばい扱いで通過する。
    "trend_negative_tolerance_pct_per_week":
        tech.TREND_GATE_NEGATIVE_TOLERANCE_PCT_PER_WEEK,       # 0.01

    # --- ⑤ ファンダ確認 -------------------------------------------------------
    # スクリーニング基準「売上高変化率 前年同四半期比30%以上」「経常利益変化率 同30%以上」
    "revenue_yoy_min_pct": 30.0,
    "ordinary_income_yoy_min_pct": 30.0,
    # 鉄則「１Q等の進捗率30％超えているか（２Qでの上方修正を狙える）」
    "q1_progress_min_pct": 30.0,
    # 「+30%継続」の実装。直近の開示から遡って連続何本まで条件を満たしているかを数え、
    # この本数以上を要求する。1 = 直近開示のみ。開示が1本しかない現状で構造的に
    # 「調査」固定になるのを避けるため既定は 1。2 に上げるのは設定変更のみで済む。
    "fundamentals_min_streak": 1,

    # --- 雲の上抜け/下抜けイベントの探索幅 -----------------------------------
    # 判定は週次だが日足を見る（鉄則「基本は日足を見る」）。最終足の遷移だけを見ると
    # 週の途中で起きた上抜け/下抜けを取りこぼすため、直近1週間（5営業日）を走査する。
    "ichimoku_cross_lookback_days": 5,

    # --- スクリーニング5条件（○×表示用。ゲートではない） ----------------------
    "screen_revenue_yoy_min_pct": 30.0,
    "screen_ordinary_income_yoy_min_pct": 30.0,
    "screen_ma_deviation_min_pct": tech.MA_DEVIATION_SCREEN_PCT,  # 5.0
    "screen_volume_ratio_min": tech.VOLUME_RATIO_SCREEN,          # 5.0
}

# 判定スタンプの意味（1行ずつ）。**説明文の正はここ**——about.html の凡例と
# 一覧のツールチップは build.py がこの辞書から生成する（案内文の手書きコピペは
# 並び替えで食い違った前例があるため禁止）。閾値は SSoT（indicators.py /
# DEFAULT_JUDGE / master.yaml）を参照し、辞書順＝凡例の表示順（評価順に並べる）。
STAMP_MEANINGS = {
    STAMP_SELL: ("保有銘柄のみ・買い側のどのゲートよりも先に評価する。"
                 "雲の下抜け・逆指値ライン抵触・6か月2倍ライン到達×"
                 "デッドクロス気味のいずれかに該当"),
    STAMP_LIQUIDITY: ("20日平均売買代金が閾値（data/master.yaml の "
                      "liquidity_gate）に満たない。判定が正しくても"
                      "建てられず降りられない"),
    STAMP_CLOUD: ("保有していない銘柄で、終値が一目均衡表の雲の下にある。"
                  "買いで入らない（保有していれば「売り」）"),
    STAMP_OVERHEAT: (f"RSI>{tech.RSI_OVERHEAT:.0f}・25日乖離率>"
                     f"{tech.MA_DEVIATION_OVERHEAT_PCT:.0f}%・信用倍率>"
                     f"{DEFAULT_JUDGE['margin_ratio_overheat']:.0f}倍の"
                     "いずれかに該当。流動性ゲートと雲は通過している"),
    STAMP_PROBE: ("買い側のゲートで指標が未計算・判定不能だった。"
                  "「通過」に見せず、ここで止める"),
    STAMP_WATCH: ("保有銘柄のみ。6か月2倍ラインに未到達、"
                  "または到達したが横ばい"),
    STAMP_BUY: ("実装済みのすべてのゲートを通過した、という意味しかない"
                "（機械が見ていない観点は「この台帳が見ていない鉄則」が正）"),
}

# 保有判定に使う日足MAの組み合わせ（鉄則「基本は日足を見る」）。
HOLDING_CROSS_SHORT = tech.DAILY_MA_SHORT_PERIODS   # 5
HOLDING_CROSS_LONG = tech.DAILY_MA_MID_PERIODS      # 25

# ゲートの並び（表示順・stage_no の根拠）。judge() はこの順に評価する。
STAGE_ORDER: tuple[tuple[str, str], ...] = (
    ("holding_sell", "HS 保有中の売りシグナル（保有のみ・最優先）"),
    ("holding_stop_loss", "H0 逆指値ライン（保有のみ）"),
    ("liquidity", "① 流動性ゲート"),
    ("trend", "② 週足MAの傾き（参考・ゲートではない）"),
    ("cloud", "③ 売りシグナル（雲の下抜け）"),
    ("overheat", "④ 過熱チェック"),
    ("holding_target", "H5 6か月2倍ライン（保有のみ）"),
    ("fundamentals", "⑤ ファンダ確認"),
    ("all_clear", "⑥ すべて通過"),
)
_STAGE_LABEL = dict(STAGE_ORDER)
_STAGE_NO = {sid: i for i, (sid, _) in enumerate(STAGE_ORDER)}

# ゲート結果の語彙。**pass と unknown を混同しないことが本モジュールの要**。
PASS, FAIL, UNKNOWN, SKIPPED, NA = "pass", "fail", "unknown", "skipped", "n/a"

MARK_OK, MARK_NG, MARK_UNKNOWN = "○", "×", "?"


# =============================================================================
# 型
# =============================================================================

class Check(NamedTuple):
    """ゲート1件の評価結果。台帳の「算出根拠の全文開示」に使う。"""
    stage: str
    label: str
    result: str      # PASS / FAIL / UNKNOWN / SKIPPED / NA
    detail: str      # 実値を含む日本語の説明


class ScreenCheck(NamedTuple):
    """スクリーニング5条件の充足状況（台帳で○×表示する）。

    mark は3値。**未計算(?) を × や ○ に丸めない**。
    """
    key: str
    label: str
    mark: str                 # ○ / × / ?
    value: float | str | None
    threshold: float | str | None
    detail: str
    # 表の見出しに出す閾値の文言。数値でない条件（「雲を上抜け」等）や、
    # 定義が未確認で○×を出せない項目のために持つ。
    threshold_label: str = ""


class HoldingView(NamedTuple):
    """保有管理（F13）の算出結果。保有していなくても status="none" で返す。"""
    status: str                       # none / holding / unknown（語彙外・入力矛盾）
    status_note: str | None           # status が unknown になった理由
    buy_price: float | None
    buy_date: str | None
    shares: float | None
    stop_loss_price: float | None     # 買値 × (1 + stop_loss_pct)
    stop_loss_hit: bool | None        # None = 判定不能（安値・買値が無い）
    stop_loss_intraday_only: bool | None  # 安値では抵触したが終値では戻した
    elapsed_months: int | None
    target_pct: float | None          # 経過月数に対応する基準（+12% なら 12.0）
    return_pct: float | None          # 買値からの現在騰落率
    achievement_ratio: float | None   # 基準に対する到達率（1.0 で到達）
    reached: bool | None              # None = 判定不能
    cross_kind: str | None            # 日足5/25 のクロス（golden/dead/parallel/…）
    action: str | None                # 売り / 買い増し可 / 様子見・損切り確認 / 判定不能
    note: str | None


class Verdict(NamedTuple):
    """判定結果。台帳が根拠を全文開示できるだけの情報を持つ。"""
    code: str
    name: str
    as_of: str | None                 # 判定基準日（最終足の日付。壁時計を使わない）
    stamp: str                        # 7種のいずれか
    stage: str                        # どの段階で確定したか（STAGE_ORDER の id）
    stage_no: int                     # STAGE_ORDER 上の位置（0 起点）
    stage_label: str
    resolution: str                   # FAIL=条件に該当して確定 / UNKNOWN=未計算で停止 / PASS=全通過
    reason: str                       # 実値を含む1行の根拠
    checks: tuple[Check, ...]         # 全ゲートの結果（未評価は SKIPPED）
    screen: tuple[ScreenCheck, ...]   # スクリーニング5条件の○×
    metrics: dict[str, Any]           # 判定に使った各指標の実値（順序固定）
    holding: HoldingView
    unknowns: tuple[str, ...]         # 算出できなかった指標名
    # ゲートではないが読み手に伝えるべき注意（鉄則の「注意」水準の項目）。
    # 例: RSI 70超／30以下、20日平均売買代金が単日に集中、週足の未了週を落とした 等。
    cautions: tuple[str, ...] = ()


class PriceIndicators(NamedTuple):
    """株価から決定論的に導出される指標一式（compute() の出力）。"""
    as_of: str | None
    bars: int
    close: float | None
    low: float | None                 # 最終足の安値。逆指値の抵触判定に使う
    ma_short: float | None
    ma_mid: float | None
    ma_deviation_pct: float | None
    rsi14: float | None
    ichimoku: tech.Ichimoku
    cloud_cross_determinable: bool
    recent_cloud_cross: str | None
    recent_cloud_cross_date: str | None
    volume_ratio_3m: float | None
    avg_turnover_20d: float | None
    median_turnover_20d: float | None
    weekly_bars: int                  # 週足の総本数（未了週を含む）
    weekly_bars_used: int             # トレンド判定に使った本数（未了週を除く）
    weekly_last_incomplete: bool      # 最終週が未了で落とされたか
    # 週足MAの傾き（%/週）と向き。**「中期」が13週か26週かは未確定**なので両方持つ。
    weekly_ma_slopes: dict[int, float | None]
    weekly_ma_directions: dict[int, str | None]
    weekly_ma_mid_slope_pct: float | None      # 13週（既存 metric 名の互換）
    weekly_ma_mid_direction: str | None
    weekly_ma_long_slope_pct: float | None     # 26週
    weekly_ma_long_direction: str | None
    daily_cross: tech.CrossSignal


# =============================================================================
# 指標の算出（純関数）
# =============================================================================

def _recent_cloud_cross(highs: Sequence[float | None], lows: Sequence[float | None],
                        closes: Sequence[float | None],
                        lookback: int) -> tuple[str | None, int | None]:
    """直近 lookback 営業日以内に発生した最新の雲の上抜け/下抜けを返す。

    最終足の遷移（ichimoku().cross）だけでは、週の途中に起きた抜けを取りこぼす。
    判定は週次・データは日足なので、直近1週間を新しい順に走査して最初に見つけた
    イベントを採る。見つからなければ (None, None)。
    """
    n = len(closes)
    if n == 0 or lookback is None or lookback < 1:
        return (None, None)
    for i in range(n - 1, max(n - 1 - lookback, -1), -1):
        ich = tech.ichimoku(highs[: i + 1], lows[: i + 1], closes[: i + 1])
        if ich.cross is not None:
            return (ich.cross, i)
    return (None, None)


def compute(bars: Sequence[tech.Bar], cfg: dict | None = None) -> PriceIndicators:
    """日足から判定に必要な指標をすべて算出する。純関数（I/O なし）。

    算出できないものは None のまま返す。ゼロや直近値で代替しない（F11-4）。
    """
    c = merge_config(cfg)
    closes = [b.close for b in bars]
    highs = [b.high for b in bars]
    lows = [b.low for b in bars]
    volumes = [b.volume for b in bars]

    ich = tech.ichimoku(highs, lows, closes)
    lookback = c["ichimoku_cross_lookback_days"]
    kind, idx = _recent_cloud_cross(highs, lows, closes, lookback)

    # 週足。**未了週（最終週が金曜で終わっていない）は落とす**。
    # 営業日1日ぶんの終値が完成週と同じ重みで13週MAの傾きに入ると、
    # 実データで符号が反転する（4073: +0.207 → -0.230 %/週）。
    all_weekly = tech.to_weekly(list(bars))
    weekly, dropped = tech.weekly_for_trend(list(bars))
    wcloses = [w.close for w in weekly]

    periods = [int(p) for p in (c.get("weekly_trend_periods")
                                or tech.WEEKLY_TREND_PERIODS)]
    slopes: dict[int, float | None] = {}
    directions: dict[int, str | None] = {}
    for p in periods:
        series = tech.sma_series(wcloses, p)
        slopes[p] = tech.slope_pct(series, tech.SLOPE_LOOKBACK_WEEKS)
        directions[p] = tech.slope_direction(
            series, tech.SLOPE_LOOKBACK_WEEKS, tech.SLOPE_FLAT_PCT_PER_WEEK)

    mid, long = tech.WEEKLY_MA_MID_PERIODS, tech.WEEKLY_MA_LONG_PERIODS

    return PriceIndicators(
        as_of=bars[-1].date if bars else None,
        bars=len(bars),
        close=closes[-1] if closes else None,
        low=lows[-1] if lows else None,
        ma_short=tech.sma(closes, tech.DAILY_MA_SHORT_PERIODS),
        ma_mid=tech.sma(closes, tech.DAILY_MA_MID_PERIODS),
        ma_deviation_pct=tech.ma_deviation_pct(closes),
        rsi14=tech.rsi(closes),
        ichimoku=ich,
        # 最終足で position と prev_position の両方が出せて初めて「抜けたか」を語れる。
        cloud_cross_determinable=(ich.position is not None
                                  and ich.prev_position is not None),
        recent_cloud_cross=kind,
        recent_cloud_cross_date=(bars[idx].date if idx is not None else None),
        volume_ratio_3m=tech.volume_ratio(volumes),
        avg_turnover_20d=tech.avg_turnover(closes, volumes),
        median_turnover_20d=tech.median_turnover(closes, volumes),
        weekly_bars=len(all_weekly),
        weekly_bars_used=len(weekly),
        weekly_last_incomplete=bool(dropped),
        weekly_ma_slopes=slopes,
        weekly_ma_directions=directions,
        weekly_ma_mid_slope_pct=slopes.get(mid),
        weekly_ma_mid_direction=directions.get(mid),
        weekly_ma_long_slope_pct=slopes.get(long),
        weekly_ma_long_direction=directions.get(long),
        daily_cross=tech.cross_signal(
            tech.sma_series(closes, HOLDING_CROSS_SHORT),
            tech.sma_series(closes, HOLDING_CROSS_LONG)),
    )


# =============================================================================
# 設定
# =============================================================================

def merge_config(config: dict | None) -> dict:
    """judge 固有の閾値に既定値を埋める。

    ここで既定値を持つのは「判定の形」を決める定数だけ。
    流動性ゲート閾値・6か月2倍ライン・逆指値率は master.yaml が正で、
    **無ければ unknown（＝調査）に落とす**。設定漏れを既定値で埋めて通過させない。
    """
    merged = dict(DEFAULT_JUDGE)
    if config:
        for k, v in (config.get("judge") or {}).items():
            merged[k] = v
    return merged


# =============================================================================
# 数値の書式（欠測は必ず「—」。0 と欠測を混同しない）
# =============================================================================

def _f(v, spec: str = ",.2f") -> str:
    if v is None:
        return "—"
    try:
        return format(v, spec)
    except (TypeError, ValueError):
        return str(v)


def _man(v) -> str:
    """円 → 万円表記。"""
    return "—" if v is None else f"{v / 10000:,.0f}万円"


# =============================================================================
# 信用残高
# =============================================================================

def _to_float(text) -> float | None:
    if text is None:
        return None
    s = str(text).strip()
    if s == "":
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _margin_state(margin: dict | None, as_of: str | None, cfg: dict,
                  short_unavailable: bool = False) -> tuple[str, float | None, str]:
    """信用倍率の過熱判定。戻り値は (state, ratio, detail)。

    state:
      FAIL    5倍超（またはRATIO_NAで買い一辺倒）＝過熱
      PASS    5倍以下
      NA      制度信用が買建のみで、倍率が**構造的に**定義できない。
              需給の情報を持たないので過熱材料として使わない（過熱チェック自体は
              RSI・25日乖離率で続行する）。**「過熱していない」と主張してはいない**
      UNKNOWN 取得できていない / 単位不明 / 残高欠測 / 古すぎる
              → **「過熱していない」ではない**。judge は調査で止める
    """
    limit = cfg["margin_ratio_overheat"]
    if not margin:
        return (UNKNOWN, None, "信用残高を取得できていない（data/margin/{code}.csv が無い）")

    status = str(margin.get("status") or "").strip()
    parts = [p for p in status.split("|") if p]
    m_date = str(margin.get("date") or "").strip()
    long_bal = _to_float(margin.get("long_balance"))
    short_bal = _to_float(margin.get("short_balance"))
    ratio = _to_float(margin.get("ratio"))

    if "UNIT_UNKNOWN" in parts:
        return (UNKNOWN, ratio, f"信用残の単位見出しが読めない（{m_date} UNIT_UNKNOWN）")
    if "BALANCE_MISSING" in parts:
        return (UNKNOWN, ratio, f"信用残高が数値として読めない（{m_date} BALANCE_MISSING）")
    if "RATIO_INCONSISTENT" in parts:
        return (UNKNOWN, ratio,
                f"表示倍率と 買残÷売残 が乖離（{m_date} RATIO_INCONSISTENT）")

    # 鮮度。古い残高は「現在の需給」を語れないので通過扱いにしない。
    age = _days_between(m_date, as_of)
    if age is None:
        return (UNKNOWN, ratio, f"信用残の日付が読めない（date={m_date!r}）")
    if age > cfg["margin_max_age_days"]:
        return (UNKNOWN, ratio,
                f"信用残が古い（{m_date}・基準日から{age}日前 > "
                f"{cfg['margin_max_age_days']}日）")

    if ratio is None:
        # RATIO_NA: 売り残0で倍率が定義できない。**「過熱していない」と読み替えない**。
        if long_bal is not None and short_bal is not None and short_bal == 0:
            if long_bal > 0:
                if short_unavailable:
                    # 制度信用が買建のみ＝売り残0は取引所ルールの帰結。需給の材料に
                    # ならないので、この条件だけ「該当しない」にする（判定は続行）。
                    return (NA, None,
                            f"信用倍率は構造的に定義不能（{m_date} 売り残0・"
                            f"買い残{long_bal}／制度信用が買建のみ）。"
                            "需給の材料にならないため過熱の判定材料から外す")
                if cfg["margin_ratio_na_is_overheat"]:
                    return (FAIL, None,
                            f"信用倍率は定義不能（{m_date} 売り残0・買い残{long_bal}）。"
                            f"買い一辺倒のため過熱側として扱う")
                return (UNKNOWN, None,
                        f"信用倍率が定義不能（{m_date} 売り残0・買い残{long_bal}）")
            return (PASS, None,
                    f"信用残なし（{m_date} 買い残0・売り残0）。倍率は定義不能だが過熱ではない")
        return (UNKNOWN, None, f"信用倍率が取得できていない（{m_date} status={status}）")

    if ratio > limit:
        return (FAIL, ratio, f"信用倍率 {ratio:,.2f}倍 > {limit:,.1f}倍（{m_date}）")
    return (PASS, ratio, f"信用倍率 {ratio:,.2f}倍 ≤ {limit:,.1f}倍（{m_date}）")


def _days_between(a: str | None, b: str | None) -> int | None:
    if not a or not b:
        return None
    try:
        return (_date.fromisoformat(str(b)) - _date.fromisoformat(str(a))).days
    except ValueError:
        return None


# =============================================================================
# 決算（KPI）
# =============================================================================

# 比率が出せない理由の語彙。**「該当しない(n/a)」と「未計算(unknown)」を混同しない**。
KPI_OK = "ok"                     # 算出できた
KPI_UNKNOWN = "unknown"           # 材料が無い／読めない → ゲートは「調査」で止める
KPI_NOT_APPLICABLE = "n/a"        # 観点そのものが今は当てはまらない → 条件から外す
KPI_BASE_NOT_POSITIVE = "base_not_positive"   # 前年同期が0以下（比率が意味を持たない）

_KPI_STATUS_NOTE = {
    KPI_UNKNOWN: "未計算（決算行が無い／単位不一致／数値が読めない）",
    KPI_NOT_APPLICABLE: "該当しない（直近の開示が1Q累計ではない）",
    KPI_BASE_NOT_POSITIVE: "前年同期が0以下のため前年同期比を定義できない",
}


def derive_kpi_metrics(rows: Sequence[dict]) -> dict:
    """data/kpi/{code}.csv の実額行から比率を計算する（SKILL.md の derived metric）。

    **比率の計算はコード側の責務**（F8-4・SKILL.md「Claude が引き算しない」）。
    Claude が書くのは実額行だけで、ここが唯一の比率算出箇所。将来 src/kpi.py を
    作る場合はこの関数を import すること（同じ式を再実装しない）。

    返り値:
      {"disclosure_date",
       "revenue_yoy_pct" / "revenue_yoy_status",
       "ordinary_income_yoy_pct" / "ordinary_income_yoy_status",
       "q1_progress_pct" / "q1_progress_status" / "q1_progress_date",
       "history": [開示日ごとの比率, 新しい順]}

    **1Q進捗率の扱い**（2026-08-12 修正）:
      鉄則の「1Q等の進捗率30%超えているか」は 1Q 開示に対する観点である。
      旧実装は period が Q1cum でないと一律 None を返し、judge がそれを「未計算」として
      「調査」に落としていたため、**2Q短信が出た瞬間に全銘柄が恒久的に「調査」から
      動かなくなる**（review-findings F-09 と同型の到達不能）。
      直近開示だけでなく history 全体から最新の 1Q 進捗率を採り、
      1Q 累計の開示が1本も無い場合は n/a（該当しない）として条件から外す。
    """
    by_date: dict[str, dict[str, dict]] = {}
    for r in rows:
        d = str(r.get("date") or "").strip()
        metric = str(r.get("metric") or "").strip()
        if not d or not metric:
            continue
        by_date.setdefault(d, {})[metric] = r

    history: list[dict] = []
    for d in sorted(by_date, reverse=True):
        g = by_date[d]
        rev, rev_st = _yoy(g.get("revenue"), g.get("revenue_prev_year"))
        ordi, ordi_st = _yoy(g.get("ordinary_income"),
                             g.get("ordinary_income_prev_year"))
        q1, q1_st = _progress(g.get("ordinary_income"),
                              g.get("ordinary_income_fy_plan"))
        history.append({
            "date": d,
            "revenue_yoy_pct": rev, "revenue_yoy_status": rev_st,
            "ordinary_income_yoy_pct": ordi, "ordinary_income_yoy_status": ordi_st,
            "q1_progress_pct": q1, "q1_progress_status": q1_st,
        })

    # 1Q進捗率は「最新の 1Q 開示」から採る（直近開示が 2Q/3Q/通期でも遡って拾う）。
    q1_value, q1_status, q1_date = None, KPI_NOT_APPLICABLE, None
    for h in history:
        st = h["q1_progress_status"]
        if st == KPI_NOT_APPLICABLE:
            continue                      # 1Q累計でない開示は素通り
        q1_value, q1_status, q1_date = h["q1_progress_pct"], st, h["date"]
        break

    latest = history[0] if history else {}
    return {
        "disclosure_date": latest.get("date"),
        "revenue_yoy_pct": latest.get("revenue_yoy_pct"),
        "revenue_yoy_status": latest.get("revenue_yoy_status", KPI_UNKNOWN),
        "ordinary_income_yoy_pct": latest.get("ordinary_income_yoy_pct"),
        "ordinary_income_yoy_status": latest.get("ordinary_income_yoy_status",
                                                 KPI_UNKNOWN),
        "q1_progress_pct": q1_value,
        "q1_progress_status": q1_status,
        "q1_progress_date": q1_date,
        "history": history,
    }


def _pair(cur: dict | None, base: dict | None) -> tuple[float, float] | None:
    """2行から (当期, 比較対象) を取り出す。単位が違えば None（換算しない）。"""
    if not cur or not base:
        return None
    a, b = _to_float(cur.get("value")), _to_float(base.get("value"))
    if a is None or b is None:
        return None
    if str(cur.get("unit") or "").strip() != str(base.get("unit") or "").strip():
        return None
    return (a, b)


def _yoy(cur: dict | None, prev: dict | None) -> tuple[float | None, str]:
    """前年同期比(%) と状態。前年同期が0以下なら値は None。

    **前年同期が赤字のとき前年同期比は意味を持たない**（SKILL.md）。
    「黒字転換」等の文言で埋めず、未計算のまま返す。judge は調査で止める。
    黒字転換銘柄が構造的にブロックされるのは承知のうえの扱いで、状態を
    `base_not_positive` として区別し、台帳に理由をそのまま出す。
    """
    p = _pair(cur, prev)
    if p is None:
        return (None, KPI_UNKNOWN)
    a, b = p
    if b <= 0:
        return (None, KPI_BASE_NOT_POSITIVE)
    return ((a / b - 1.0) * 100.0, KPI_OK)


def _progress(cur: dict | None, plan: dict | None) -> tuple[float | None, str]:
    """1Q進捗率(%) と状態。period が Q1cum の開示のときのみ算出する（SKILL.md）。

    period が Q1cum でない開示は **n/a（この観点が当てはまらない）**であって
    「未計算」ではない。ここを混同すると 2Q 以降ずっと「調査」で固定される。
    """
    period = str((cur or {}).get("definition") or "").split("|")[0]
    if not period.endswith("Q1cum"):
        return (None, KPI_NOT_APPLICABLE)
    p = _pair(cur, plan)
    if p is None:
        return (None, KPI_UNKNOWN)       # 1Q開示だが通期計画が無い＝未計算
    a, b = p
    if b <= 0:
        return (None, KPI_UNKNOWN)
    return (a / b * 100.0, KPI_OK)


def _streak(history: Sequence[dict], key: str, threshold: float) -> int:
    """直近の開示から遡って、条件を連続で満たしている開示の本数（「継続」の実装）。"""
    n = 0
    for h in history:
        v = h.get(key)
        if v is None or v < threshold:
            break
        n += 1
    return n


# =============================================================================
# 保有管理（F13）
# =============================================================================

def _elapsed_months(buy_date: str | None, as_of: str | None) -> int | None:
    """買付日から基準日までの完了月数。日が足りなければ切り捨てる。

    買付日が基準日より**未来**なら None（年の打ち間違い等の入力破損）。
    旧実装は 0 にクランプしていたため、`buy_date: 2027-01-05` が
    「買付から1か月未満」という正常な説明文に化け、まだ買っていない値段を基準に
    騰落率と逆指値を計算していた。**入力の壊れを良性の状態に化けさせない。**
    """
    if not buy_date or not as_of:
        return None
    try:
        b = _date.fromisoformat(str(buy_date))
        a = _date.fromisoformat(str(as_of))
    except ValueError:
        return None
    if b > a:
        return None
    m = (a.year - b.year) * 12 + (a.month - b.month)
    if a.day < b.day:
        m -= 1
    return max(m, 0)


def _ladder_target(ladder: dict | None, months: int | None) -> float | None:
    """経過月数に対応する基準騰落率(%)。

    1か月未満は基準ラインがまだ立たない（None）。
    6か月を超えた保有は最終段（+100%）を据え置く（信用の期限が6か月であるため）。
    """
    if not ladder or months is None or months < 1:
        return None
    keys = []
    for k in ladder:
        try:
            keys.append(int(k))
        except (TypeError, ValueError):
            continue
    if not keys:
        return None
    m = min(months, max(keys))
    for k in (m, str(m)):
        if k in ladder:
            v = _to_float(ladder[k])
            return None if v is None else v * 100.0
    return None


HOLDING_STATUSES = ("none", "holding")
HOLDING_UNKNOWN = "unknown"


def _holding_status(h: dict) -> tuple[str, str | None]:
    """holding.status を固定語彙に照合する。語彙外・入力矛盾は unknown。

    master.yaml は人間が手で更新する（F13-1・D18）。`hold` / `保有` / `true` の
    打ち間違いや、`status: none` のまま `buy_price` だけ入れる操作は現実的に起きる。
    旧実装は「holding 以外はすべて保有していない」と解釈していたため、
    **逆指値ゲート（H0）が黙って消え、判定が「買」まで通ってしまった**。
    語彙外は第3の状態にして「調査」で止める。
    """
    raw = h.get("status")
    status = str(raw).strip().lower() if raw is not None else "none"
    if status not in HOLDING_STATUSES:
        return (HOLDING_UNKNOWN,
                f"holding.status が語彙外（{raw!r}）。使えるのは "
                f"{' / '.join(HOLDING_STATUSES)} のみ")
    if status == "none":
        filled = [k for k in ("buy_price", "buy_date", "shares")
                  if h.get(k) not in (None, "")]
        if filled:
            return (HOLDING_UNKNOWN,
                    f"holding.status=none なのに {', '.join(sorted(filled))} が"
                    "入っている（保有登録の書き漏れの疑い）")
    return (status, None)


def evaluate_holding(stock: dict, ind: PriceIndicators,
                     config: dict | None) -> HoldingView:
    """保有情報（F13）を算出する。**前週の判断を入力に持たない**（F13-5）。"""
    h = stock.get("holding") or {}
    status, status_note = _holding_status(h)
    buy_price = _to_float(h.get("buy_price"))
    buy_date = h.get("buy_date")
    buy_date = str(buy_date) if buy_date else None
    shares = _to_float(h.get("shares"))

    stop_pct = _to_float((config or {}).get("stop_loss_pct"))
    ladder = (config or {}).get("target_ladder")
    close = ind.close
    low = ind.low

    stop_price = None if (buy_price is None or stop_pct is None) \
        else buy_price * (1.0 + stop_pct)
    # ★逆指値注文はザラ場で約定する。終値だけを見ると、安値が刺さった日を
    #   「未抵触」と報告してしまう（実データ 4073 で1年に4日発生）。**安値で判定する**。
    #   終値では戻した日は intraday_only で区別して台帳に出す。
    stop_hit = None if (stop_price is None or low is None) else low <= stop_price
    stop_intraday_only = None
    if stop_hit and close is not None:
        stop_intraday_only = close > stop_price

    months = _elapsed_months(buy_date, ind.as_of)
    target = _ladder_target(ladder, months)
    ret = None if (buy_price is None or close is None or buy_price == 0) \
        else (close / buy_price - 1.0) * 100.0
    ach = None if (ret is None or target is None or target == 0) else ret / target

    note = None
    if status != "holding":
        reached: bool | None = None
    elif months is None or buy_price is None or close is None:
        reached = None
        note = "買値・買付日・現値のいずれかが無く、基準ラインを判定できない"
    elif months < 1:
        # 基準ラインがまだ立たない。データ欠測ではないので unknown にはせず、
        # 鉄則の「基準に到達していない＝様子見・損切り確認」に倒す。
        reached = False
        note = "買付から1か月未満のため基準ライン未設定。様子見・損切り確認"
    elif target is None:
        reached = None
        note = "6か月2倍ライン（target_ladder）が master.yaml に無い"
    else:
        reached = ret is not None and ret >= target

    kind = ind.daily_cross.kind
    action = None
    if status == HOLDING_UNKNOWN:
        action = "判定不能（holding の記入を確認する）"
    elif status == "holding":
        # 売りが出る条件を先に並べる（スタンプと action の向きを一致させるため）。
        if stop_hit:
            action = "売り（逆指値ライン抵触）"
        elif ind.ichimoku.position == "below":
            action = "売り（雲の下）"
        elif reached is None:
            action = "判定不能"
        elif not reached:
            action = "様子見・損切り確認"
        elif kind in ("dead", "dead_ish"):
            action = "売り（基準到達×デッドクロス気味）"
        elif kind in ("golden", "golden_ish"):
            action = "買い増し可（基準到達×ゴールデンクロス気味）"
        elif kind == "parallel":
            action = "様子見（基準到達だが平行。鉄則が除外する状態）"
        else:
            action = "判定不能"

    return HoldingView(
        status=status, status_note=status_note,
        buy_price=buy_price, buy_date=buy_date, shares=shares,
        stop_loss_price=stop_price, stop_loss_hit=stop_hit,
        stop_loss_intraday_only=stop_intraday_only,
        elapsed_months=months, target_pct=target, return_pct=ret,
        achievement_ratio=ach, reached=reached, cross_kind=kind,
        action=action, note=note,
    )


# =============================================================================
# スクリーニング5条件（○×。ゲートではない）
# =============================================================================

def _mark(value: float | None, threshold: float, unit: str, label: str,
          key: str) -> ScreenCheck:
    th_label = f"{threshold:,.0f}{unit} 以上"
    if value is None:
        return ScreenCheck(key, label, MARK_UNKNOWN, None, threshold,
                           "未計算（データ不足・取得失敗）", th_label)
    ok = value >= threshold
    return ScreenCheck(key, label, MARK_OK if ok else MARK_NG, value, threshold,
                       f"{value:,.2f}{unit} {'≥' if ok else '<'} {threshold:,.1f}{unit}",
                       th_label)


def evaluate_screening(ind: PriceIndicators, kpi: dict | None,
                       cfg: dict) -> tuple[ScreenCheck, ...]:
    """楽天証券「成長株0606」の5条件の充足状況。**未計算を○にも×にも丸めない**。

    2026-08-12 の修正:
      - 「3か月前出来高増加率」は**当プロジェクト独自の定義**であり、楽天証券の定義を
        再現できていない（indicators.VOLUME_RATIO_DEFINITION_VERIFIED 参照）。
        定義が違うものに○×を付けると台帳が「条件を満たしていない」と断定してしまうので、
        値は出したうえで mark は `?` に留める。
      - 「一目均衡表 上抜け」は**イベント**（直近5営業日に上抜けたか）である。実データでは
        4銘柄中1銘柄しか満たさない一方、「雲の上にある」なら4銘柄とも満たす。
        スクリーナーがどちらの意味だったかは未確認なので、**イベントと状態を2行に分けて**
        両方出す（片方だけを見せて誤読させない）。
    """
    k = kpi or {}
    checks = [
        _mark(k.get("revenue_yoy_pct"), cfg["screen_revenue_yoy_min_pct"], "%",
              "売上高変化率 前年同四半期比", "revenue_yoy_pct"),
        _mark(k.get("ordinary_income_yoy_pct"),
              cfg["screen_ordinary_income_yoy_min_pct"], "%",
              "経常利益変化率 前年同四半期比", "ordinary_income_yoy_pct"),
        _mark(ind.ma_deviation_pct, cfg["screen_ma_deviation_min_pct"], "%",
              "25日移動平均線乖離率", "ma25_deviation_pct"),
    ]

    # 3か月前出来高増加率（定義が未確認なので○×にしない）
    vr_th = cfg["screen_volume_ratio_min"]
    if tech.VOLUME_RATIO_DEFINITION_VERIFIED:
        checks.append(_mark(ind.volume_ratio_3m, vr_th, "倍",
                            "3か月前出来高増加率", "volume_ratio_3m"))
    elif ind.volume_ratio_3m is None:
        checks.append(ScreenCheck(
            "volume_ratio_3m", "3か月前出来高増加率（定義未確認）", MARK_UNKNOWN,
            None, vr_th, "未計算（データ不足・比較先の出来高が0）",
            "楽天証券の定義が未確認"))
    else:
        checks.append(ScreenCheck(
            "volume_ratio_3m", "3か月前出来高増加率（定義未確認）", MARK_UNKNOWN,
            ind.volume_ratio_3m, vr_th,
            f"自社定義で {ind.volume_ratio_3m:,.2f}倍"
            f"（直近{tech.VOLUME_RATIO_WINDOW_DAYS}日平均 ÷ "
            f"{tech.VOLUME_RATIO_LOOKBACK_DAYS}営業日前を終点とする"
            f"{tech.VOLUME_RATIO_WINDOW_DAYS}日平均）。"
            f"楽天証券の定義は未確認のため○×を出さない（基準は {vr_th:,.1f}倍）",
            "楽天証券の定義が未確認"))

    # 一目均衡表 上抜け（イベント）
    look = cfg["ichimoku_cross_lookback_days"]
    ev_label = f"直近{look}営業日以内"
    if not ind.cloud_cross_determinable:
        checks.append(ScreenCheck("ichimoku_breakout", "一目均衡表 上抜け（イベント）",
                                  MARK_UNKNOWN, ind.ichimoku.position, "breakout_up",
                                  "未計算（雲が張れていない／終値が欠測）", ev_label))
    elif ind.recent_cloud_cross == "breakout_up":
        checks.append(ScreenCheck("ichimoku_breakout", "一目均衡表 上抜け（イベント）",
                                  MARK_OK, ind.recent_cloud_cross, "breakout_up",
                                  f"{ind.recent_cloud_cross_date} に雲を上抜け",
                                  ev_label))
    else:
        checks.append(ScreenCheck("ichimoku_breakout", "一目均衡表 上抜け（イベント）",
                                  MARK_NG, ind.ichimoku.position, "breakout_up",
                                  f"直近{look}営業日に上抜けなし"
                                  f"（現在位置 {ind.ichimoku.position}）", ev_label))

    # 雲に対する位置（状態）
    pos = ind.ichimoku.position
    if pos is None:
        checks.append(ScreenCheck("ichimoku_above", "（参考）雲の上にあるか",
                                  MARK_UNKNOWN, None, "above",
                                  "未計算（雲が張れていない／終値が欠測）", "雲の上"))
    else:
        checks.append(ScreenCheck(
            "ichimoku_above", "（参考）雲の上にあるか",
            MARK_OK if pos == "above" else MARK_NG, pos, "above",
            f"現在位置 {pos}（上端 {_f(ind.ichimoku.cloud_top)} / "
            f"下端 {_f(ind.ichimoku.cloud_bottom)}）", "雲の上"))
    return tuple(checks)


# =============================================================================
# 判定本体
# =============================================================================

def _metrics(ind: PriceIndicators, margin_ratio: float | None,
             margin: dict | None, kpi: dict | None,
             margin_state: str = UNKNOWN, margin_detail: str = "") -> dict[str, Any]:
    """判定に使った各指標の実値（順序固定・決定論的）。"""
    k = kpi or {}
    ich = ind.ichimoku
    return {
        "as_of": ind.as_of,
        "bars": ind.bars,
        "weekly_bars": ind.weekly_bars,
        "weekly_bars_used": ind.weekly_bars_used,
        "weekly_last_incomplete": ind.weekly_last_incomplete,
        "close": ind.close,
        "low": ind.low,
        "avg_turnover_20d": ind.avg_turnover_20d,
        "median_turnover_20d": ind.median_turnover_20d,
        "weekly_ma_mid_slope_pct": ind.weekly_ma_mid_slope_pct,
        "weekly_ma_mid_direction": ind.weekly_ma_mid_direction,
        "weekly_ma_long_slope_pct": ind.weekly_ma_long_slope_pct,
        "weekly_ma_long_direction": ind.weekly_ma_long_direction,
        "ichimoku_position": ich.position,
        "ichimoku_prev_position": ich.prev_position,
        "ichimoku_cloud_top": ich.cloud_top,
        "ichimoku_cloud_bottom": ich.cloud_bottom,
        "ichimoku_cross_last_bar": ich.cross,
        "ichimoku_recent_cross": ind.recent_cloud_cross,
        "ichimoku_recent_cross_date": ind.recent_cloud_cross_date,
        "rsi14": ind.rsi14,
        "ma25": ind.ma_mid,
        "ma25_deviation_pct": ind.ma_deviation_pct,
        "volume_ratio_3m": ind.volume_ratio_3m,
        "margin_ratio": margin_ratio,
        # 倍率が None でも「未計算」とは限らない。過熱の判定として決着している
        # （PASS=残高ゼロ / FAIL=買い一辺倒 / NA=構造的に定義不能）かを区別する。
        "margin_state": margin_state,
        "margin_detail": margin_detail,
        "margin_status": (margin or {}).get("status"),
        "margin_date": (margin or {}).get("date"),
        "daily_cross_kind": ind.daily_cross.kind,
        "daily_cross_slope_diff_pct": ind.daily_cross.slope_diff_pct,
        "kpi_disclosure_date": k.get("disclosure_date"),
        "revenue_yoy_pct": k.get("revenue_yoy_pct"),
        "ordinary_income_yoy_pct": k.get("ordinary_income_yoy_pct"),
        "q1_progress_pct": k.get("q1_progress_pct"),
        "q1_progress_status": k.get("q1_progress_status"),
        "q1_progress_date": k.get("q1_progress_date"),
    }


_UNKNOWN_LABELS = {
    "avg_turnover_20d": "20日平均売買代金",
    "weekly_ma_mid_direction": f"週足{tech.WEEKLY_MA_MID_PERIODS}週MAの傾き",
    "weekly_ma_long_direction": f"週足{tech.WEEKLY_MA_LONG_PERIODS}週MAの傾き",
    "ichimoku_position": "雲に対する位置",
    "rsi14": "RSI(14)",
    "ma25_deviation_pct": "25日移動平均乖離率",
    "margin_ratio": "信用倍率",
    "volume_ratio_3m": "3か月前出来高増加率",
    "revenue_yoy_pct": "売上高 前年同四半期比",
    "ordinary_income_yoy_pct": "経常利益 前年同四半期比",
    "q1_progress_pct": "1Q進捗率",
}


# 鉄則に書かれているが、このシステムが**評価していない**項目（F5 の外側）。
# 「①〜⑤をすべて通過した」を「鉄則を全部かけた」と読ませないために台帳へ出す。
# 実装したらここから消す（宣言だけ残さない）。
UNEVALUATED_RULES: tuple[tuple[str, str], ...] = (
    ("季節性の要因を検討したか（I-15）",
     "数年分の日足が要る。現在の保有履歴は1年分で、季節性の推定に足りない"),
    ("同業他社の決算に引っ張られていないか（I-16）",
     "master.yaml の peers が TO_VERIFY のままで、ピアの株価・決算日を持っていない"),
    ("トレンドは明確で出来高を伴っているか（I-06）",
     "出来高は流動性ゲートと3か月前比にしか使っておらず、"
     "「上昇に出来高が伴っているか」は判定していない"),
    ("マクロ（CPI・投資部門別売買動向・海外投資家）（I-18）",
     "外部指標を取得していない。指数は TOPIX / グロース250 の株価のみ"),
)


def _cautions(ind: PriceIndicators, margin_state: str, margin_detail: str,
              holding: HoldingView) -> list[str]:
    """ゲートではないが読み手に伝えるべき注意。**鉄則の「注意」水準の項目**。

    ゲートに昇格させるかはマスターの判断なので、ここでは黙らせずに出すだけにする。
    """
    out: list[str] = []
    if ind.rsi14 is not None:
        if ind.rsi14 > tech.RSI_CAUTION_HIGH and ind.rsi14 <= tech.RSI_OVERHEAT:
            out.append(f"RSI {ind.rsi14:,.2f} が {tech.RSI_CAUTION_HIGH:,.0f} 超"
                       f"（鉄則「70%超え・30%以下は注意」。過熱閾値 "
                       f"{tech.RSI_OVERHEAT:,.0f} には未達）")
        elif ind.rsi14 < tech.RSI_CAUTION_LOW:
            out.append(f"RSI {ind.rsi14:,.2f} が {tech.RSI_CAUTION_LOW:,.0f} 以下"
                       "（鉄則「70%超え・30%以下は注意」）")
    a, m = ind.avg_turnover_20d, ind.median_turnover_20d
    if a is not None and m is not None and a > 0 and m < a / 2:
        out.append(f"20日平均売買代金 {_man(a)} に対し中央値は {_man(m)}。"
                   "売買代金が一部の日に集中しており、平均は流動性を過大評価している"
                   "（ゲートは平均で引いている）")
    if ind.weekly_last_incomplete:
        out.append(f"最終週は未了（{ind.as_of} が週の最終営業日ではない）ため、"
                   "週足MAの傾きの計算から除外した")
    if margin_state == NA:
        out.append(margin_detail)
    if holding.stop_loss_intraday_only:
        out.append("逆指値ラインにザラ場で抵触し、終値では戻している")
    return out


def judge(stock: dict, bars: Sequence[tech.Bar], ind: PriceIndicators | None,
          margin: dict | None, kpi: dict | None, config: dict | None) -> Verdict:
    """鉄則ベースの判定。上から評価し、該当した時点で確定する。

    引数:
      stock  : master.yaml の stocks 要素（holding を含む）
      bars   : 日足 Bar の昇順リスト
      ind    : compute(bars) の結果。None なら内部で算出する
      margin : data/margin/{code}.csv の最新行（dict）。無ければ None
      kpi    : derive_kpi_metrics() の出力。無ければ None
      config : master.yaml 全体（liquidity_gate / target_ladder / stop_loss_pct / judge）

    **未計算は通過ではない**（F5-4）。どの段階でも必要な指標が None なら「調査」で止める。
    """
    cfg = merge_config(config)
    if ind is None:
        ind = compute(bars, config)

    flags = [str(f) for f in (stock.get("flags") or [])]
    short_unavailable = cfg["margin_short_unavailable_flag"] in flags
    margin_state, margin_ratio, margin_detail = _margin_state(
        margin, ind.as_of, cfg, short_unavailable)
    metrics = _metrics(ind, margin_ratio, margin, kpi, margin_state, margin_detail)
    screen = evaluate_screening(ind, kpi, cfg)
    holding = evaluate_holding(stock, ind, config)

    checks: list[Check] = []
    unknowns: list[str] = []
    cautions: list[str] = _cautions(ind, margin_state, margin_detail, holding)
    # 「未計算」に数えない指標（判定として決着がついているもの）。
    # 例: 買残0・売残0 → 倍率は None だが「過熱ではない」と決着している。
    #     制度信用が買建のみ → 倍率は構造的に定義不能で条件から外している。
    resolved: set[str] = set()
    if margin_state != UNKNOWN:
        # PASS（残高ゼロ）/ FAIL（買い一辺倒）/ NA（構造的に定義不能）は
        # 倍率が None でも**判定としては決着している**。「未計算」に数えない。
        resolved.add("margin_ratio")
    if (kpi or {}).get("q1_progress_status") == KPI_NOT_APPLICABLE:
        resolved.add("q1_progress_pct")
    if not tech.VOLUME_RATIO_DEFINITION_VERIFIED:
        # 値は出ているが○×に使っていない。「未計算」ではないので unknowns に入れない。
        resolved.add("volume_ratio_3m")

    def add(stage: str, result: str, detail: str) -> None:
        checks.append(Check(stage, _STAGE_LABEL[stage], result, detail))

    def finish(stamp: str, stage: str, resolution: str, reason: str) -> Verdict:
        seen = {c.stage for c in checks}
        for sid, label in STAGE_ORDER:
            if sid not in seen:
                checks.append(Check(sid, label, SKIPPED,
                                    "上流で確定したため評価していない"))
        for key, label in _UNKNOWN_LABELS.items():
            if metrics.get(key) is None and key not in resolved:
                unknowns.append(label)
        order = {sid: i for i, (sid, _) in enumerate(STAGE_ORDER)}
        return Verdict(
            code=str(stock.get("code", "")),
            name=str(stock.get("name", "")),
            as_of=ind.as_of,
            stamp=stamp,
            stage=stage,
            stage_no=_STAGE_NO[stage],
            stage_label=_STAGE_LABEL[stage],
            resolution=resolution,
            reason=reason,
            checks=tuple(sorted(checks, key=lambda c: order[c.stage])),
            screen=screen,
            metrics=metrics,
            holding=holding,
            unknowns=tuple(unknowns),
            cautions=tuple(cautions),
        )

    is_holding = holding.status == "holding"

    # --- HS. 保有中の売りシグナル（★買い側のどのゲートよりも先に見る） -----------
    #
    # 鉄則の「雲を下に抜けたらすぐ売る」「基準に到達かつデッドクロス気味＝売り」は
    # 無条件の売り指示である。これを①流動性・②トレンド・④過熱より下に置くと、
    # 上流が fail / unknown になった時点で **売りが評価すらされない**。
    # 「調査で止める > 買を出す」は買い側の原則で、売り側に適用してはならない。
    if holding.status == HOLDING_UNKNOWN:
        add("holding_sell", UNKNOWN, holding.status_note or "holding の記入が不正")
        return finish(STAMP_PROBE, "holding_sell", UNKNOWN,
                      f"保有状態を確定できない（{holding.status_note}）。"
                      "master.yaml の holding を確認するまで判定しない")
    if is_holding:
        sells: list[str] = []
        if holding.stop_loss_hit:
            tail = ("／終値は逆指値より上に戻したがザラ場で抵触している"
                    if holding.stop_loss_intraday_only else "")
            sells.append(f"逆指値ライン抵触（安値 {_f(ind.low)} ≤ "
                         f"買値-10% {_f(holding.stop_loss_price)}{tail}）")
        if ind.ichimoku.position == "below":
            when = (f"{ind.recent_cloud_cross_date} に下抜け"
                    if ind.recent_cloud_cross == "breakdown" else "継続して雲の下")
            sells.append(f"雲の下（終値 {_f(ind.close)} < 雲の下端 "
                         f"{_f(ind.ichimoku.cloud_bottom)}・{when}）")
        if holding.reached and holding.cross_kind in ("dead", "dead_ish"):
            sells.append(f"6か月2倍ライン到達×{holding.cross_kind}"
                         f"（現在 {_f(holding.return_pct, '+.2f')}% / "
                         f"基準 {_f(holding.target_pct, '+.0f')}%）")
        if sells:
            add("holding_sell", FAIL, " / ".join(sells))
            return finish(STAMP_SELL, "holding_sell", FAIL,
                          "保有中の売りシグナル: " + " / ".join(sells)
                          + "。鉄則によりすぐ売る・損切設定")
        add("holding_sell", PASS,
            "売りシグナルなし（逆指値抵触・雲の下・基準到達×デッドクロス気味の"
            "いずれにも該当しない）")
    else:
        add("holding_sell", NA, "保有していない（売りは行動として意味を持たない）")

    # --- H0. 逆指値ラインの算出可否（保有のみ） ---------------------------------
    # 抵触していれば HS で確定済み。ここに来るのは「未抵触」か「判定できない」のみ。
    if is_holding:
        if holding.stop_loss_hit is None:
            add("holding_stop_loss", UNKNOWN,
                f"逆指値ラインを判定できない（買値 {_f(holding.buy_price)} / "
                f"安値 {_f(ind.low)} / stop_loss_pct "
                f"{(config or {}).get('stop_loss_pct')!r}）")
            return finish(STAMP_PROBE, "holding_stop_loss", UNKNOWN,
                          "保有中だが逆指値ラインを算出できない（買値・安値・設定を確認）")
        add("holding_stop_loss", PASS,
            f"安値 {_f(ind.low)} > 逆指値 {_f(holding.stop_loss_price)}")
    else:
        add("holding_stop_loss", NA, "保有していない")

    # --- ①. 流動性ゲート（判定の最上位・F5-1） ---------------------------------
    gate = (config or {}).get("liquidity_gate") or {}
    gate_min = _to_float(gate.get("min_avg_turnover_20d_jpy"))
    turnover = ind.avg_turnover_20d
    if gate_min is None:
        add("liquidity", UNKNOWN,
            "liquidity_gate.min_avg_turnover_20d_jpy が設定に無い")
        return finish(STAMP_PROBE, "liquidity", UNKNOWN,
                      "流動性ゲートの閾値が未設定のため判定できない")
    if turnover is None:
        # ★review-findings F-02 の罠。ここを通過扱いにしない。
        add("liquidity", UNKNOWN,
            f"20日平均売買代金が算出できない（終値または出来高の欠測／"
            f"営業日 {ind.bars}日 < {tech.TURNOVER_PERIODS}日）")
        return finish(STAMP_PROBE, "liquidity", UNKNOWN,
                      "20日平均売買代金が算出できず流動性を確認できない"
                      "（欠測を通過扱いにしない）")
    if turnover < gate_min:
        add("liquidity", FAIL, f"20日平均売買代金 {_man(turnover)} < {_man(gate_min)}")
        return finish(STAMP_LIQUIDITY, "liquidity", FAIL,
                      f"流動性ゲート不通過（20日平均売買代金 {_man(turnover)} < "
                      f"{_man(gate_min)}）")
    add("liquidity", PASS,
        f"20日平均売買代金 {_man(turnover)} ≥ {_man(gate_min)}"
        f"（同期間の中央値 {_man(ind.median_turnover_20d)}）")

    # --- ②. 週足MAの傾き（参考・ゲートではない） ------------------------------
    #
    # 2026-09-05 改訂: 「13週/26週MAの回帰傾きが負なら見送」は当リポジトリ独自の定義で、
    # 楽天・株探・SBI のどの「上昇トレンド」定義とも一致しなかった（実データでは
    # 株価が両MAの上にあり日足25/75日線も上向きの銘柄を、26週線がわずかに負という
    # だけで止めていた）。トレンドの読みは楽天「チャート形状検索」の9分類
    # （src/shape_chart.py・画像判定※・ゲートには使わない）に移し、ここは参考表示に
    # 格下げする。**傾きが負でも未計算でも判定を止めない。** 経緯は BACKLOG.md 改訂履歴。
    tol = float(cfg["trend_negative_tolerance_pct_per_week"])
    periods = sorted(ind.weekly_ma_slopes)
    week_note = (f"・最終週は未了（{ind.weekly_bars - ind.weekly_bars_used}本）"
                 "のため傾きの計算から除外" if ind.weekly_last_incomplete else "")
    missing = [p for p in periods if ind.weekly_ma_slopes.get(p) is None]
    if missing:
        add("trend", NA,
            "参考: 週足MAの傾きが算出できない: "
            + " / ".join(f"{p}週" for p in missing)
            + f"（週足 {ind.weekly_bars_used}本・欠測または期間不足{week_note}）"
            "。ゲートではないので判定は止めない")
    else:
        falling = [p for p in periods if ind.weekly_ma_slopes[p] < -tol]
        slopes = " / ".join(
            f"{p}週 {_f(ind.weekly_ma_slopes[p], '+.3f')}%/週"
            f"（{ind.weekly_ma_directions.get(p)}）" for p in periods)
        if falling:
            cautions.append(
                "週足MAの傾きが負（参考・ゲートではない）: "
                + " / ".join(f"{p}週 {_f(ind.weekly_ma_slopes[p], '+.3f')}%/週"
                             for p in falling))
        add("trend", NA, f"参考: 週足MAの傾き {slopes}{week_note}"
            + "。トレンドの読みはチャート形状（画像判定※）を見る")

    # --- ③. 売りシグナル（雲の下抜け・F5-3） -----------------------------------
    pos = ind.ichimoku.position
    if pos is None:
        add("cloud", UNKNOWN,
            f"雲に対する位置が算出できない（日足 {ind.bars}本・"
            f"雲の完成に {tech.ICHIMOKU_SPAN_B_PERIODS + tech.ICHIMOKU_DISPLACEMENT}本必要）")
        return finish(STAMP_PROBE, "cloud", UNKNOWN,
                      "一目均衡表の雲が張れず下抜けを確認できない")
    if pos == "below":
        when = (f"{ind.recent_cloud_cross_date} に下抜け"
                if ind.recent_cloud_cross == "breakdown" else "継続して雲の下")
        add("cloud", FAIL,
            f"終値 {_f(ind.close)} < 雲の下端 {_f(ind.ichimoku.cloud_bottom)}（{when}）")
        if is_holding:
            return finish(STAMP_SELL, "cloud", FAIL,
                          f"雲の下（{when}）。鉄則によりすぐ売る・損切設定")
        # 非保有に「売り」は行動として意味を持たない（2026-09-05。②の格下げで
        # ここに到達する非保有銘柄が増えたため、見送の語で出す）
        return finish(STAMP_CLOUD, "cloud", FAIL,
                      f"雲の下（{when}）。買いで入らない")
    add("cloud", PASS,
        f"雲に対する位置 {pos}（上端 {_f(ind.ichimoku.cloud_top)} / "
        f"下端 {_f(ind.ichimoku.cloud_bottom)}）")

    # --- ④. 過熱チェック -------------------------------------------------------
    unknown_bits: list[str] = []
    if ind.rsi14 is None:
        unknown_bits.append("RSI(14)")
    if ind.ma_deviation_pct is None:
        unknown_bits.append("25日移動平均乖離率")
    if margin_state == UNKNOWN:
        unknown_bits.append(f"信用倍率（{margin_detail}）")
    if unknown_bits:
        add("overheat", UNKNOWN, "未計算: " + " / ".join(unknown_bits))
        return finish(STAMP_PROBE, "overheat", UNKNOWN,
                      "過熱を確認できない（" + " / ".join(unknown_bits) + "）")

    hits: list[str] = []
    if ind.rsi14 > cfg["rsi_overheat"]:
        hits.append(f"RSI {ind.rsi14:,.2f} > {cfg['rsi_overheat']:,.1f}")
    if ind.ma_deviation_pct > cfg["ma_deviation_overheat_pct"]:
        hits.append(f"25日乖離率 {ind.ma_deviation_pct:,.2f}% > "
                    f"{cfg['ma_deviation_overheat_pct']:,.1f}%")
    if margin_state == FAIL:
        hits.append(margin_detail)
    if hits:
        add("overheat", FAIL, " / ".join(hits))
        return finish(STAMP_OVERHEAT, "overheat", FAIL,
                      "過熱（" + " / ".join(hits) + "）")
    margin_text = (f"信用倍率は判定材料から除外（{margin_detail}）"
                   if margin_state == NA else margin_detail)
    add("overheat", PASS,
        f"RSI {ind.rsi14:,.2f} ≤ {cfg['rsi_overheat']:,.1f} / "
        f"25日乖離率 {ind.ma_deviation_pct:,.2f}% ≤ "
        f"{cfg['ma_deviation_overheat_pct']:,.1f}% / {margin_text}")

    # --- H5. 6か月2倍ライン（保有のみ・F13-3/F13-4） ---------------------------
    # 「到達×デッドクロス気味＝売り」は HS で先に確定している（ここには来ない）。
    if is_holding:
        if holding.reached is None:
            add("holding_target", UNKNOWN,
                holding.note or "基準ラインを判定できない")
            return finish(STAMP_PROBE, "holding_target", UNKNOWN,
                          f"6か月2倍ラインを判定できない（{holding.note}）")
        base = (f"経過{holding.elapsed_months}か月・基準 {_f(holding.target_pct, '+.0f')}% / "
                f"現在 {_f(holding.return_pct, '+.2f')}% / "
                f"到達率 {_f(holding.achievement_ratio, '.2f')}")
        if not holding.reached:
            add("holding_target", FAIL, f"基準未到達（{base}）")
            return finish(STAMP_WATCH, "holding_target", FAIL,
                          f"6か月2倍ライン未到達。様子見・損切り確認（{base}／"
                          f"逆指値 {_f(holding.stop_loss_price)}）")
        if holding.cross_kind in ("dead", "dead_ish"):
            add("holding_target", FAIL,
                f"基準到達×{holding.cross_kind}（{base}）")
            return finish(STAMP_SELL, "holding_target", FAIL,
                          f"基準到達かつデッドクロス気味（{holding.cross_kind}）→ 売り（{base}）")
        if holding.cross_kind == "parallel":
            add("holding_target", FAIL, f"基準到達だが両線が平行（{base}）")
            return finish(STAMP_WATCH, "holding_target", FAIL,
                          f"基準到達だが平行（鉄則が除外する状態）。様子見・損切り確認（{base}）")
        if holding.cross_kind not in ("golden", "golden_ish"):
            add("holding_target", UNKNOWN,
                f"日足{HOLDING_CROSS_SHORT}/{HOLDING_CROSS_LONG}のクロスが算出できない（{base}）")
            return finish(STAMP_PROBE, "holding_target", UNKNOWN,
                          "基準到達だがゴールデン/デッドクロスを判定できない")
        add("holding_target", PASS,
            f"基準到達×{holding.cross_kind} → 買い増し可。ファンダ確認へ（{base}）")
    else:
        add("holding_target", NA, "保有していない")

    # --- ⑤. ファンダ確認 -------------------------------------------------------
    #
    # **「該当しない(n/a)」と「未計算(unknown)」を区別する**（2026-08-12 修正）。
    # 1Q進捗率は 1Q 開示に対する観点なので、2Q以降の開示しか無い期間は「該当しない」。
    # 旧実装はこれを未計算として扱い、**2Q短信が出た瞬間に恒久的に「調査」で固定**
    # されていた（他がすべて条件を満たしていても動かない）。
    k = kpi or {}
    rev, ordi, q1 = (k.get("revenue_yoy_pct"), k.get("ordinary_income_yoy_pct"),
                     k.get("q1_progress_pct"))
    # 状態キーが無い入力（テストの手組み dict 等）は値の有無から推定する。
    def _status(key: str, value) -> str:
        st = k.get(key)
        if st:
            return str(st)
        return KPI_OK if value is not None else KPI_UNKNOWN

    rev_st = _status("revenue_yoy_status", rev)
    ordi_st = _status("ordinary_income_yoy_status", ordi)
    q1_st = _status("q1_progress_status", q1)

    missing = []
    for value, status, label in ((rev, rev_st, "売上高 前年同四半期比"),
                                 (ordi, ordi_st, "経常利益 前年同四半期比"),
                                 (q1, q1_st, "1Q進捗率")):
        if status == KPI_NOT_APPLICABLE:
            continue                      # 観点が当てはまらない。条件から外す
        if value is None:
            note = _KPI_STATUS_NOTE.get(status, "未計算")
            missing.append(f"{label}（{note}）")
    if missing:
        add("fundamentals", UNKNOWN, "未計算: " + " / ".join(missing))
        return finish(STAMP_PROBE, "fundamentals", UNKNOWN,
                      "ファンダを確認できない（" + " / ".join(missing)
                      + "）。KPI未整備の銘柄に買を出さない")

    short: list[str] = []
    skipped: list[str] = []
    if rev is not None and rev < cfg["revenue_yoy_min_pct"]:
        short.append(f"売上高 {rev:+,.1f}% < {cfg['revenue_yoy_min_pct']:,.0f}%")
    if ordi is not None and ordi < cfg["ordinary_income_yoy_min_pct"]:
        short.append(f"経常利益 {ordi:+,.1f}% < {cfg['ordinary_income_yoy_min_pct']:,.0f}%")
    if q1_st == KPI_NOT_APPLICABLE:
        skipped.append("1Q進捗率は該当しない（直近の開示が1Q累計ではない）")
    elif q1 is not None and q1 < cfg["q1_progress_min_pct"]:
        short.append(f"1Q進捗率 {q1:,.1f}% < {cfg['q1_progress_min_pct']:,.0f}%")

    history = k.get("history") or []
    need = cfg["fundamentals_min_streak"]
    rev_streak = _streak(history, "revenue_yoy_pct", cfg["revenue_yoy_min_pct"])
    ord_streak = _streak(history, "ordinary_income_yoy_pct",
                         cfg["ordinary_income_yoy_min_pct"])
    if rev_streak < need:
        short.append(f"売上高+30%の継続 {rev_streak}期 < {need}期")
    if ord_streak < need:
        short.append(f"経常利益+30%の継続 {ord_streak}期 < {need}期")

    if short:
        add("fundamentals", FAIL, " / ".join(short + skipped))
        return finish(STAMP_PROBE, "fundamentals", FAIL,
                      "ファンダ条件を満たさない（" + " / ".join(short) + "）")
    add("fundamentals", PASS,
        f"売上高 {_f(rev, '+,.1f')}% / 経常利益 {_f(ordi, '+,.1f')}% / "
        f"1Q進捗率 {_f(q1, ',.1f')}%"
        f"（継続 売上{rev_streak}期・経常{ord_streak}期）"
        + ("／" + " / ".join(skipped) if skipped else ""))

    # --- ⑥. すべて通過 ---------------------------------------------------------
    tail = "（保有・買い増し可）" if is_holding else ""
    add("all_clear", PASS, "①③④⑤のすべてを通過（②は参考）")
    return finish(STAMP_BUY, "all_clear", PASS,
                  f"流動性・雲・過熱・ファンダのすべてを通過{tail}")


# =============================================================================
# I/O（ここから下だけがファイルを読む）
# =============================================================================

def load_master() -> dict:
    import yaml
    return yaml.safe_load((ROOT / "data" / "master.yaml").read_text(encoding="utf-8"))


def load_bars(code: str) -> list[tech.Bar]:
    p = ROOT / "data" / "prices" / "daily.csv"
    if not p.exists():
        return []
    with p.open(encoding="utf-8", newline="") as f:
        return tech.bars_from_rows(list(csv.DictReader(f)), code=code)


def load_margin(code: str) -> dict | None:
    """信用残の最新行。日付昇順の最終行を採る。"""
    p = ROOT / "data" / "margin" / f"{code}.csv"
    if not p.exists():
        return None
    with p.open(encoding="utf-8") as f:
        rows = [r for r in csv.DictReader(f) if str(r.get("date") or "").strip()]
    if not rows:
        return None
    return sorted(rows, key=lambda r: r["date"])[-1]


def load_kpi(code: str) -> dict | None:
    p = ROOT / "data" / "kpi" / f"{code}.csv"
    if not p.exists():
        return None
    with p.open(encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    if not rows:
        return None
    return derive_kpi_metrics(rows)


def judge_all(master: dict | None = None) -> list[Verdict]:
    """**監視対象の**銘柄を証券コード順に判定する（決定論的）。

    `watch: excluded` は判定しない。取得を止めた銘柄のデータは凍るので、
    そこから出した「買」「様子見」は**古い事実に基づく現在形の主張**になる。
    黙って古い判定を出し続けるより、判定を持たない方が読み手を誤らせない
    （台帳には build.py が「対象外」として出す）。
    """
    m = master or load_master()
    out = []
    for stock in sorted(Y.watched_stocks(m), key=lambda s: str(s["code"])):
        code = str(stock["code"])
        bars = load_bars(code)
        out.append(judge(stock, bars, compute(bars, m), load_margin(code),
                         load_kpi(code), m))
    return out


def verdict_to_dict(v: Verdict) -> dict:
    """JSON 化（生成時刻を埋め込まない・キー順固定）。"""
    return {
        "code": v.code, "name": v.name, "as_of": v.as_of, "stamp": v.stamp,
        "stage": v.stage, "stage_no": v.stage_no, "stage_label": v.stage_label,
        "resolution": v.resolution, "reason": v.reason,
        "checks": [c._asdict() for c in v.checks],
        "screen": [s._asdict() for s in v.screen],
        "metrics": v.metrics,
        "holding": v.holding._asdict(),
        "unknowns": list(v.unknowns),
        "cautions": list(v.cautions),
    }


def _print(v: Verdict) -> None:
    print(f"\n{'=' * 78}")
    print(f"{v.code} {v.name}  基準日 {v.as_of}")
    print(f"  判定: {v.stamp}   確定段階: {v.stage_label}（{v.resolution}）")
    print(f"  根拠: {v.reason}")
    print("  --- ゲート ---")
    for c in v.checks:
        print(f"    [{c.result:<7}] {c.label}: {c.detail}")
    print("  --- スクリーニング5条件 ---")
    for s in v.screen:
        print(f"    {s.mark} {s.label}: {s.detail}")
    if v.holding.status == "holding":
        h = v.holding
        print("  --- 保有管理 ---")
        print(f"    買値 {_f(h.buy_price)} / 買付 {h.buy_date} / 数量 {_f(h.shares, ',.0f')}")
        print(f"    逆指値ライン {_f(h.stop_loss_price)}（抵触 {h.stop_loss_hit}）")
        print(f"    経過 {h.elapsed_months}か月 / 基準 {_f(h.target_pct, '+.0f')}% / "
              f"現在 {_f(h.return_pct, '+.2f')}% / 到達率 {_f(h.achievement_ratio, '.2f')}")
        print(f"    クロス {h.cross_kind} → {h.action}")
    if v.unknowns:
        print(f"  --- 未計算の指標 --- {' / '.join(v.unknowns)}")
    if v.cautions:
        print("  --- 注意（ゲートではない） ---")
        for c in v.cautions:
            print(f"    ・{c}")


def codes_from_prices() -> list[str]:
    """`data/prices/daily.csv` に出てくる証券コード（master.yaml を読まない）。"""
    p = ROOT / "data" / "prices" / "daily.csv"
    if not p.exists():
        return []
    with p.open(encoding="utf-8-sig", newline="") as f:
        return sorted({str(r.get("code") or "").strip()
                       for r in csv.DictReader(f) if str(r.get("code") or "").strip()})


def indicators_only() -> dict:
    """**master.yaml を読まずに**指標だけを出す（裏取りの隔離ジョブ用）。

    weekly.yml の verify ジョブは `data/master.yaml` を sparse-checkout から
    意図的に外している（買値・買付日が入っているため・D18）。ところが
    kabu-ledger-verify の SKILL.md は取得元表に `python src/judge.py` を挙げており、
    その構成では `load_master()` が `FileNotFoundError` で落ちる。
    結果、検証者は指示された経路で指標を確かめられず、
    **前回の run の evidence をそのまま持ち越す**（＝前回の検証を根拠にする）
    方向に流れる。ここが隔離を守ったまま指標を出す入口。

    判定（買/見送）は出さない。判定には master.yaml の閾値・保有情報が要る。
    """
    out: dict = {}
    for code in codes_from_prices():
        bars = load_bars(code)
        ind = compute(bars)
        out[code] = {
            "as_of": ind.as_of,
            "bars": ind.bars,
            "close": ind.close,
            "avg_turnover_20d": ind.avg_turnover_20d,
            "median_turnover_20d": ind.median_turnover_20d,
            "ma_short": ind.ma_short,
            "ma_mid": ind.ma_mid,
            "ma_deviation_pct": ind.ma_deviation_pct,
            "rsi14": ind.rsi14,
            "volume_ratio_3m": ind.volume_ratio_3m,
            "weekly_bars_used": ind.weekly_bars_used,
            "weekly_ma_mid_slope_pct": ind.weekly_ma_mid_slope_pct,
            "weekly_ma_mid_direction": ind.weekly_ma_mid_direction,
            "weekly_ma_long_slope_pct": ind.weekly_ma_long_slope_pct,
            "weekly_ma_long_direction": ind.weekly_ma_long_direction,
            "ichimoku_position": ind.ichimoku.position if ind.ichimoku else None,
        }
    return out


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="鉄則ベースの判定を実行する")
    ap.add_argument("--json", action="store_true", help="JSON で出力する")
    ap.add_argument("--stamps", action="store_true",
                    help="{code: stamp} だけを JSON で出力する")
    ap.add_argument("--indicators-only", action="store_true",
                    help="master.yaml を読まずに指標だけを JSON で出す"
                         "（裏取りの隔離ジョブ用。判定は出さない）")
    args = ap.parse_args(argv)

    if args.indicators_only:
        print(json.dumps(indicators_only(), ensure_ascii=False,
                         sort_keys=True, indent=2))
        return 0

    verdicts = judge_all()
    if args.stamps:
        print(json.dumps({v.code: v.stamp for v in verdicts},
                         ensure_ascii=False, sort_keys=True, indent=2))
        return 0
    if args.json:
        print(json.dumps([verdict_to_dict(v) for v in verdicts],
                         ensure_ascii=False, indent=2))
        return 0

    print(f"{'コード':<6}{'銘柄':<14}{'判定':<12}{'確定段階'}")
    for v in verdicts:
        print(f"{v.code:<8}{v.name:<12}{v.stamp:<12}{v.stage_label}")
    for v in verdicts:
        _print(v)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
