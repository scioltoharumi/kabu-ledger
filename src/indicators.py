"""テクニカル指標の計算エンジン。

要件: requirements.md F11 / gap-analysis.md I-01〜I-09 / input/investment-rules.md（判定軸の正）

設計原則（破らないこと）:
  1. すべて純関数。副作用なし。標準ライブラリのみ（外部依存ゼロ）。
     計算は必ずコードで行う。LLM に数値を計算させない（D3/D9/D14）。
  2. 計算に必要な期間が満たない場合、または窓に欠測が含まれる場合は None を返す（F11-4）。
     ゼロや直近値で代替しない。**None は「未計算」であって「条件クリア」ではない**。
     呼び出し側（judge）は None を通過扱いにせず「調査」で止めること（F5-4 / review-findings F-02）。
  3. 出来高 0（NO_TRADE＝売買不成立）は欠測ではない。0 として正しく集計する。
     真偽値判定（`if volume:`）で 0 を欠測に潰さないよう、すべて `is None` で判定している。
  4. 係数・閾値はファイル先頭の定数ブロックに集約する。マジックナンバーを散在させない。

このモジュールは I/O を持たない。CSV の読み込みは呼び出し側の責務。
`bars_from_rows()` は「読み込み済みの行」を Bar に正規化するだけの純関数。
"""
from __future__ import annotations

from datetime import date as _date
from typing import NamedTuple, Sequence

# =============================================================================
# 定数ブロック（閾値・期間はすべてここ。根拠を必ず併記する）
# =============================================================================

# --- 移動平均の期間 -----------------------------------------------------------
# 日足 5/25/75 は日本の証券会社チャートの標準。25日は楽天証券スクリーニング
# 「25日前移動平均線乖離率5%」が参照している線でもある。
DAILY_MA_SHORT_PERIODS = 5
DAILY_MA_MID_PERIODS = 25
# 75日は日本の証券会社チャートの標準セットの一員だが、鉄則が参照していないため
# **判定にも表示にも使っていない**。使っていない定数を「あるように見せない」ため、
# 定義は置かない（必要になった時点で判定への使い道と一緒に足す）。

# 週足 13/26。**「中期」がどちらを指すかは判定軸の正（input/investment-rules.md）に
# 書かれていない**。日本の週足チャートの慣行は 13週=短期 / 26週=中期 / 52週=長期 だが、
# 鉄則の枠組み全体（6か月2倍ライン）が信用の期限＝26週を軸にしているため、
# 13週を「中期」と読む余地もある。実データでは 4073 の結論が 13週(flat) と
# 26週(down) で反転する（2026-08-10 時点）。
#
# → **どちらか一方を勝手に選ばない。** judge は 13週と26週の両方を評価し、
#    どちらかが下向きならトレンドゲートを不通過にする（保守側・「見送 > 買」）。
#    マスターが「中期」を確定したら master.yaml の judge.weekly_trend_periods を
#    その1つだけにする。
WEEKLY_MA_MID_PERIODS = 13
WEEKLY_MA_LONG_PERIODS = 26
# 週足MAの完成に必要な週足本数（傾きの回帰4週ぶんを含む）。
WEEKLY_TREND_PERIODS = (WEEKLY_MA_MID_PERIODS, WEEKLY_MA_LONG_PERIODS)

# --- 傾き＝「平行ではない」の数値化 -------------------------------------------
# 鉄則は「デッドクロス気味(平行ではない)」「ゴールデンクロス気味(平行ではない)」と
# 平行の除外を要求する。基準を鉄則自身の「6か月後2倍の線」に置く。
#   26週で +100% は複利で週あたり +2.70%（2**(1/26)-1 = 0.0270）。
#   その約1/10 未満（週 ±0.25%）の傾きは、目標ラインに対して実質的に横ばい＝「平行」。
#   日足はこれを営業日に割り戻す（0.25% / 5営業日 = 0.05%/日）。
SLOPE_FLAT_PCT_PER_WEEK = 0.25
SLOPE_FLAT_PCT_PER_DAY = 0.05

# --- トレンドゲートの「下がっている」の閾値 -----------------------------------
# 鉄則は「トレンドが**下がっていたら**決して買いで入らない」であり、
# 不感帯（平行の除外）を要求しているのは **ゴールデン/デッドクロスの側だけ**。
# 上の SLOPE_FLAT_PCT_PER_WEEK（0.25%/週 ≒ 年率13%）をトレンドゲートに流用すると、
# 週足MAが年率-12%で下降していても "flat" としてゲートを通過する（実測: 銘柄により
# 判定可能週の 5〜31%）。**曖昧を通過扱いにする**のは F5-4 と向きが逆なので、
# トレンドゲートは「傾きが負なら不通過」とする。
# ここで許容するのは浮動小数の丸めと1円刻みによる符号の揺れだけ（0.01%/週 ≒ 年率0.5%）。
# up/flat/down の**表示ラベル**には従来どおり SLOPE_FLAT_PCT_PER_WEEK を使う。
TREND_GATE_NEGATIVE_TOLERANCE_PCT_PER_WEEK = 0.01

# 傾きを見る点数。週足は直近4週（1か月）、日足は直近5営業日（1週間）。
# 短すぎると1本のノイズで向きが反転し、長すぎると転換の検出が遅れる。
SLOPE_LOOKBACK_WEEKS = 4
# 日足の傾きは cross_signal（CROSS_SLOPE_PERIODS）が持つ。
# ここに別名の定数を置くと SSoT が割れるため定義しない。

# --- RSI ----------------------------------------------------------------------
RSI_PERIODS = 14
# Wilder 平滑は再帰的で理論上は全履歴に依存する。初期シードの重みは ((n-1)/n)**k で
# 減衰し、n=14・k=140 で (13/14)**140 ≒ 4.5e-5。10n 本を暖機に取れば全履歴版と
# 実質的に一致するため、直近 n*RSI_WARMUP_MULTIPLE+1 本に打ち切る。
# 実測（daily.csv 4銘柄・有効履歴259〜265本）で全履歴版との差は
#   最終足のみ    最大 4.8e-4pt
#   全時点で比較  最大 8.2e-3pt（理論上界 (13/14)**126 × 100 ≒ 8.9e-3pt）
# （RSI は 0〜100 スケール）。過熱閾値80の判定を動かさない。
# （打ち切らないと、系列先頭の欠測1つで RSI 全体が None になり実用に耐えない）
RSI_WARMUP_MULTIPLE = 10
RSI_OVERHEAT = 80.0        # 鉄則「RSIが8割超えになっていないか」→ 過熱チェック
# 鉄則「70%超え、30%以下は注意。一番ではないが、決め手にかけるときに見る」。
# ゲートではなく**注意喚起**。judge が Verdict.cautions に載せ、台帳に出す。
RSI_CAUTION_HIGH = 70.0
RSI_CAUTION_LOW = 30.0

# --- 一目均衡表 ---------------------------------------------------------------
ICHIMOKU_TENKAN_PERIODS = 9      # 転換線 = (9日高値 + 9日安値) / 2
ICHIMOKU_KIJUN_PERIODS = 26      # 基準線 = (26日高値 + 26日安値) / 2
ICHIMOKU_SPAN_B_PERIODS = 52     # 先行スパンB = (52日高値 + 52日安値) / 2
# 先行スパンは 26本先に記入する（原著「26日先行」／主要チャートの標準）。
# 「当日を含めて26日目」と読む流儀では 25 になるが、当プロジェクトは 26 に固定する。
# → 当日に掛かっている雲は、26本前に算出された先行スパンA/B。
ICHIMOKU_DISPLACEMENT = 26

# --- 移動平均乖離率 -----------------------------------------------------------
MA_DEVIATION_PERIODS = 25
MA_DEVIATION_SCREEN_PCT = 5.0     # スクリーニング基準「25日前移動平均線乖離率5%」
MA_DEVIATION_OVERHEAT_PCT = 8.0   # 鉄則「7〜8％超えてたら怪しい」→ 過熱チェックは上限側を採る

# --- 出来高 -------------------------------------------------------------------
# スクリーニング基準「3か月前出来高増加率5倍」。3か月 ≒ 60営業日。
VOLUME_RATIO_LOOKBACK_DAYS = 60
# 単日どうしの比は薄商い銘柄でノイズが支配的になり、比較先が NO_TRADE(0) だと
# 定義できない。両端を5営業日（1週間）平均にして比べる。
#
# ★**楽天証券の定義は未確認。これは本プロジェクト独自の定義である。**
#   実データ（4銘柄・2026-08-10）で解釈を4通り試したが、スクリーニング通過4銘柄すべてが
#   「5倍以上」を満たす定義は見つからなかった:
#     実装(5日/5日)  47.45 / 107.59 /  0.37 /  0.84
#     単日/単日      29.9  /  21.91 /  1.30 /  5.84
#     5日/60日平均    3.17 /  30.00 /  0.96 /  2.80
#     単日/60日平均   3.21 /   3.85 /  4.59 / 15.56
#   したがって台帳では **○×ではなく「自社定義の実測値」として出す**（judge の
#   evaluate_screening が MARK_UNKNOWN を返し、定義未確認である旨を detail に書く）。
#   楽天証券の定義が確定したらここを直し、○×に戻す。
VOLUME_RATIO_WINDOW_DAYS = 5
VOLUME_RATIO_SCREEN = 5.0
# 定義が未確認であることを judge / build が参照するためのフラグ（SSoT）。
VOLUME_RATIO_DEFINITION_VERIFIED = False

# --- 売買代金（流動性ゲート） --------------------------------------------------
TURNOVER_PERIODS = 20
# 閾値 min_avg_turnover_20d_jpy は data/master.yaml が正（SSoT）。ここに複製しない。

# --- 相対パフォーマンス（F9 / I-17） -------------------------------------------
# 「市場要因と個社要因を分離しないと仮説検証にならない」。分母は指数（TOPIX）。
# 週次判定だが日足で持つため、4週=20営業日・12週=60営業日で数える。
RELATIVE_PERF_4W_PERIODS = 20
RELATIVE_PERF_12W_PERIODS = 60

# --- ゴールデン/デッドクロス ---------------------------------------------------
CROSS_LOOKBACK_PERIODS = 5   # 「直近1週間以内に交差したか」を実際の交差とみなす
CROSS_SLOPE_PERIODS = 5      # 両線の傾きを見る点数（日足1週間分）
# 両線の傾き（%/期間）の差がこれ未満なら「平行」。
# ★根拠は「1本の線が水平か」の閾値（SLOPE_FLAT_PCT_PER_DAY）の流用であって、
#   「2本の線が平行か」の閾値として導いたものではない。実データでは 6570 の
#   傾き差が 0.0686（閾値の1.37倍）で golden_ish 判定になっており、保有していて
#   基準到達なら「買い増し可」と「監視」がこの差で分かれる。
#   **感度分析かマスターへの確認が要る（未了）。** 値を変えるときは
#   CLAUDE.md の「マスターに確認したい論点」に追記してから変えること。
CROSS_PARALLEL_SLOPE_DIFF_PCT = SLOPE_FLAT_PCT_PER_DAY


# =============================================================================
# 型
# =============================================================================

class Bar(NamedTuple):
    """1営業日分の四本値。date は YYYY-MM-DD。取得できなかった値は None。"""
    date: str
    open: float | None
    high: float | None
    low: float | None
    close: float | None
    volume: int | None


class WeeklyBar(NamedTuple):
    """週足1本。date は週内最終営業日（日足系列と突き合わせられるようにするため）。

    days は週内の営業日数。最終週は未了のことがある（days < 5）。
    高値・安値・出来高は「その時点までの週内集計」であり、週が終わると値が変わる。
    """
    week: str          # ISO週 "2026-W33"
    date: str          # 週内最終営業日
    open: float | None
    high: float | None
    low: float | None
    close: float | None
    volume: int | None
    days: int


class Ichimoku(NamedTuple):
    """一目均衡表。span_a / span_b は「当日算出値」で、ICHIMOKU_DISPLACEMENT 本先に描かれる。

    cloud_top / cloud_bottom は **当日に掛かっている雲**（26本前に算出された先行スパン）。
    position / cross の判定はこちらを使う。
    """
    tenkan: float | None
    kijun: float | None
    span_a: float | None
    span_b: float | None
    cloud_top: float | None
    cloud_bottom: float | None
    position: str | None       # "above" / "in" / "below"
    prev_position: str | None
    cross: str | None          # "breakout_up"（雲を上抜け）/ "breakdown"（雲を下抜け）


class CrossSignal(NamedTuple):
    """ゴールデン/デッドクロス。鉄則の「気味（平行ではない）」を傾き付きで判定する。

    kind:
      "golden"     直近 lookback 本で短期線が長期線を上抜けた（確定のゴールデンクロス）
      "dead"       直近 lookback 本で短期線が長期線を下抜けた（確定のデッドクロス）
      "parallel"   両線の傾き差が閾値未満＝平行。**鉄則が除外する状態**。判定材料にしない
      "golden_ish" 平行でなく、短期線が長期線より相対的に上向き＝ゴールデンクロス気味
      "dead_ish"   平行でなく、短期線が長期線より相対的に下向き＝デッドクロス気味
      None         データ不足・欠測（未計算。通過扱いにしない）
    """
    kind: str | None
    crossed: str | None            # "up" / "down" / None（実際の交差の有無）
    spread_pct: float | None       # (短期 - 長期) / 長期 * 100
    short_slope_pct: float | None  # 短期線の傾き（%/期間）
    long_slope_pct: float | None   # 長期線の傾き（%/期間）
    slope_diff_pct: float | None   # short_slope_pct - long_slope_pct
    parallel: bool | None


_NO_CROSS = CrossSignal(None, None, None, None, None, None, None)
_NO_ICHIMOKU = Ichimoku(None, None, None, None, None, None, None, None, None)


# =============================================================================
# 内部ヘルパ
# =============================================================================

def _clean_tail(values: Sequence[float | None] | None, n: int) -> list[float] | None:
    """末尾 n 点を float のリストで返す。

    長さ不足、または窓に欠測（None）が1つでもあれば None。欠測を埋めない（F11-4）。
    0 は有効な値として通す（NO_TRADE の出来高 0 を欠測に潰さない）。
    """
    if n is None or n <= 0:
        return None
    if values is None or len(values) < n:
        return None
    tail = list(values[-n:])
    for v in tail:
        if v is None:
            return None
    return [float(v) for v in tail]


def _agg(values: Sequence[float | None], fn) -> float | None:
    """欠測が1つでもあれば None。空も None。"""
    vals = list(values)
    if not vals:
        return None
    for v in vals:
        if v is None:
            return None
    return fn(vals)


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


def _to_int(text) -> int | None:
    v = _to_float(text)
    return int(v) if v is not None else None


# =============================================================================
# 入力の正規化
# =============================================================================

def drop_unconfirmed_tail(bars: list[Bar]) -> list[Bar]:
    """末尾の未確定行（close が空）を落とす。

    なぜ要るか: 取得元によって当日分が載る時刻が違う（minkabu は翌日）。
    そのため **最新営業日は照合が成立せず close が空になるのが普通**である。
    その行を含めたまま指標を計算すると、`sma` 等は末尾の欠測ひとつで
    すべて None を返すため、**実データが1日進むたびに全指標が消える**。

    ここで落とすのは「値を捏造する」こととは違う。**確定している最後の日
    までで計算する**だけであり、照合を通っていない値を採用値に格上げしない
    という原則（D7）はそのまま守られる。「いつ時点の指標か」は
    `bars[-1].date` で分かるので、呼び出し側はそれを表示すること。

    途中の欠測は落とさない（系列の連続性が壊れ、n日移動平均の意味が変わるため）。
    """
    out = list(bars)
    while out and out[-1].close is None:
        out.pop()
    return out


def bars_from_rows(rows, code: str | None = None,
                   confirmed_only: bool = True) -> list[Bar]:
    """data/prices/daily.csv の行（csv.DictReader の出力等）を Bar に正規化する純関数。

    重要: `close` が空の行（SINGLE_SOURCE / MISMATCH / FETCH_FAILED）は close=None のまま
    にする。**value_primary で埋めない**。2ソース照合を通っていない値を採用値に格上げ
    すると、記録の意味（status の担保）が壊れるため（D7）。

    `confirmed_only=True`（既定）のとき、**末尾**の未確定行だけを落とす
    （`drop_unconfirmed_tail` を参照）。系列の途中にある欠測はそのまま残すので、
    その区間を含む指標が None になる挙動は変わらない。生の並びが要る場合は
    `confirmed_only=False` を渡す。

    日付昇順に並べ替えて返す（指標はすべて時系列順を前提にする）。
    """
    bars: list[Bar] = []
    for r in rows:
        if code is not None and str(r.get("code", "")).strip() != str(code):
            continue
        d = str(r.get("date", "")).strip()
        if not d:
            continue
        # 日付が ISO でない行は落とす。to_weekly が date.fromisoformat で
        # ValueError を投げてパイプラインごと止まるのを防ぐ（本モジュールの作法は
        # 「読めない入力は None／除外」であって例外送出ではない）。
        # 落としたこと自体は checks.py の schema 検査が FAIL として表に出す。
        try:
            _date.fromisoformat(d)
        except ValueError:
            continue
        bars.append(Bar(
            date=d,
            open=_to_float(r.get("open")),
            high=_to_float(r.get("high")),
            low=_to_float(r.get("low")),
            close=_to_float(r.get("close")),
            volume=_to_int(r.get("volume")),
        ))
    bars.sort(key=lambda b: b.date)
    return drop_unconfirmed_tail(bars) if confirmed_only else bars


def to_weekly(bars: Sequence[Bar]) -> list[WeeklyBar]:
    """日足を週足に変換する。

    週の区切りは ISO 週（月曜始まり）。
      始値 = 週初営業日の始値 / 高値 = 週内最高 / 安値 = 週内最安
      終値 = 週末営業日の終値 / 出来高 = 週内合計
    高値・安値・出来高は週内に1つでも欠測があれば None（合計や最大で欠測を隠さない）。
    入力が未ソートでも結果は日付順に確定する（決定論的生成）。
    """
    groups: dict[tuple[int, int], list[Bar]] = {}
    for b in bars:
        y, w, _ = _date.fromisoformat(b.date).isocalendar()
        groups.setdefault((y, w), []).append(b)

    out: list[WeeklyBar] = []
    for key in sorted(groups):
        days = sorted(groups[key], key=lambda b: b.date)
        out.append(WeeklyBar(
            week=f"{key[0]:04d}-W{key[1]:02d}",
            date=days[-1].date,
            open=days[0].open,
            high=_agg([d.high for d in days], max),
            low=_agg([d.low for d in days], min),
            close=days[-1].close,
            volume=_agg([d.volume for d in days], sum),
            days=len(days),
        ))
    return out


def last_week_is_incomplete(bars: Sequence[Bar]) -> bool | None:
    """最終週が「まだ終わっていない週」か。判定できなければ None。

    週足MAの入力から未了週を落とすために使う。実データの最終週は営業日1日
    （2026-08-10 月曜）しかなく、その1本が13週MAの傾きを +0.207%/週 と -0.230%/週 の
    あいだで振らせていた（4073。符号が反転する）。**完成週と同じ重みで入れない。**

    判定は「最終営業日が金曜か」だけで行う（決定論的で、祝日表を持たなくてよい）。
      金曜で終わっている → その週は完成（祝日で days<5 でも完成週）
      月〜木で終わっている → 未了
    金曜が祝日で木曜が週の最終営業日だった場合は「未了」と誤判定するが、
    その向きは保守側（1本古いMAを使う）なので許容する。
    """
    if not bars:
        return None
    try:
        return _date.fromisoformat(bars[-1].date).isoweekday() != 5
    except ValueError:
        return None


def weekly_for_trend(bars: Sequence[Bar]) -> tuple[list[WeeklyBar], bool]:
    """トレンド判定に使う週足系列と、「未了週を落としたか」を返す。

    落とすのは**最終週だけ**。系列途中の days<5 は祝日で短くなった完成週であり、
    落とすと週の連続性が崩れる。
    """
    weekly = to_weekly(bars)
    incomplete = last_week_is_incomplete(bars)
    if weekly and incomplete:
        return (weekly[:-1], True)
    return (weekly, False)


# =============================================================================
# 移動平均
# =============================================================================

def sma(values: Sequence[float | None], n: int) -> float | None:
    """単純移動平均。直近 n 点。期間不足・欠測混入なら None。"""
    tail = _clean_tail(values, n)
    if tail is None:
        return None
    return sum(tail) / n


def sma_series(values: Sequence[float | None], n: int) -> list[float | None]:
    """各時点の SMA を入力と同じ長さの系列で返す。算出できない時点は None。

    cross_signal / slope に渡す MA 系列を作るための補助。
    """
    return [sma(values[: i + 1], n) for i in range(len(values))]


def highest(values: Sequence[float | None], n: int) -> float | None:
    tail = _clean_tail(values, n)
    return None if tail is None else max(tail)


def lowest(values: Sequence[float | None], n: int) -> float | None:
    tail = _clean_tail(values, n)
    return None if tail is None else min(tail)


def midpoint(highs: Sequence[float | None], lows: Sequence[float | None],
             n: int) -> float | None:
    """(n期間高値 + n期間安値) / 2。一目均衡表の転換線・基準線・先行スパンBの共通形。"""
    h = highest(highs, n)
    lo = lowest(lows, n)
    if h is None or lo is None:
        return None
    return (h + lo) / 2.0


# =============================================================================
# 傾き（鉄則の「トレンドが下がっている」「平行ではない」を数値化する）
# =============================================================================

def slope(values: Sequence[float | None], n: int) -> float | None:
    """直近 n 点の最小二乗回帰の傾き。単位は「価格 / 1期間」。

    2点の差分ではなく回帰にするのは、端点1本のノイズで向きが反転しないため。
    """
    if n is None or n < 2:
        return None
    tail = _clean_tail(values, n)
    if tail is None:
        return None
    mean_x = (n - 1) / 2.0
    mean_y = sum(tail) / n
    num = sum((i - mean_x) * (y - mean_y) for i, y in enumerate(tail))
    den = sum((i - mean_x) ** 2 for i in range(n))
    if den == 0:
        return None
    return num / den


def slope_pct(values: Sequence[float | None], n: int) -> float | None:
    """傾きを水準で正規化した値（%/1期間）。銘柄間・時点間で比較できる形にする。"""
    tail = _clean_tail(values, n)
    s = slope(values, n)
    if tail is None or s is None:
        return None
    level = sum(tail) / n
    if level == 0:
        return None
    return s / level * 100.0


def slope_direction(values: Sequence[float | None], n: int,
                    flat_pct: float = SLOPE_FLAT_PCT_PER_DAY) -> str | None:
    """"up" / "flat" / "down" / None。

    flat_pct は「平行」とみなす帯の半幅（%/期間）。週足には SLOPE_FLAT_PCT_PER_WEEK を渡す。
    鉄則の第一条（週足中期MAが下向きなら買わない）は down を見る。
    None（未計算）は flat でも down でもない。**通過扱いにしないこと**。
    """
    p = slope_pct(values, n)
    if p is None:
        return None
    if p > flat_pct:
        return "up"
    if p < -flat_pct:
        return "down"
    return "flat"


# =============================================================================
# RSI（Wilder 方式）
# =============================================================================

def rsi(closes: Sequence[float | None], n: int = RSI_PERIODS,
        warmup_multiple: int = RSI_WARMUP_MULTIPLE) -> float | None:
    """RSI(n)。Wilder の平滑平均を使う（単純平均版ではない）。

      初期値: 最初の n 本の値幅の単純平均
      以降  : avg = (前avg * (n-1) + 当期値) / n
      RSI   = 100 - 100 / (1 + 平均上昇幅 / 平均下降幅)

    値幅は n 本の終値差から作るため、最低 n+1 本の終値が要る。
    直近 n*warmup_multiple+1 本に打ち切る（定数ブロック RSI_WARMUP_MULTIPLE の根拠を参照）。

    平均上昇幅・平均下降幅がともに 0（＝期間中まったく動いていない）のときは 50.0 を返す。
    100 を返す実装もあるが、値動きゼロを「買われ過ぎ」と読ませると
    NO_TRADE が続く薄商い銘柄で過熱判定が誤作動するため採らない。
    """
    if n is None or n < 1:
        return None
    if closes is None or len(closes) < n + 1:
        return None

    limit = n * max(1, warmup_multiple) + 1
    series = list(closes[-limit:]) if len(closes) > limit else list(closes)
    for v in series:
        if v is None:
            return None
    series = [float(v) for v in series]
    if len(series) < n + 1:
        return None

    deltas = [series[i] - series[i - 1] for i in range(1, len(series))]
    gains = [d if d > 0 else 0.0 for d in deltas]
    losses = [-d if d < 0 else 0.0 for d in deltas]

    avg_gain = sum(gains[:n]) / n
    avg_loss = sum(losses[:n]) / n
    for i in range(n, len(deltas)):
        avg_gain = (avg_gain * (n - 1) + gains[i]) / n
        avg_loss = (avg_loss * (n - 1) + losses[i]) / n

    if avg_loss == 0 and avg_gain == 0:
        return 50.0
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - 100.0 / (1.0 + rs)


# =============================================================================
# 一目均衡表
# =============================================================================

def _span_pair_at(highs: Sequence[float | None], lows: Sequence[float | None],
                  i: int, tenkan_n: int, kijun_n: int,
                  span_b_n: int) -> tuple[float | None, float | None]:
    """index i 時点で算出される先行スパン (A, B)。i は 0-origin。"""
    if i < 0 or i >= len(highs):
        return (None, None)
    h = highs[: i + 1]
    lo = lows[: i + 1]
    tenkan = midpoint(h, lo, tenkan_n)
    kijun = midpoint(h, lo, kijun_n)
    span_a = None if (tenkan is None or kijun is None) else (tenkan + kijun) / 2.0
    span_b = midpoint(h, lo, span_b_n)
    return (span_a, span_b)


def _position(close: float | None, top: float | None,
              bottom: float | None) -> str | None:
    if close is None or top is None or bottom is None:
        return None
    if close > top:
        return "above"
    if close < bottom:
        return "below"
    return "in"


def ichimoku(highs: Sequence[float | None], lows: Sequence[float | None],
             closes: Sequence[float | None],
             tenkan_n: int = ICHIMOKU_TENKAN_PERIODS,
             kijun_n: int = ICHIMOKU_KIJUN_PERIODS,
             span_b_n: int = ICHIMOKU_SPAN_B_PERIODS,
             displacement: int = ICHIMOKU_DISPLACEMENT) -> Ichimoku:
    """最終足時点の一目均衡表。雲に対する位置と、前足からの上抜け・下抜けを含む。

    雲の完成には span_b_n + displacement 本（既定 52+26 = 78本）、
    上抜け・下抜けの判定にはさらに1本（79本）の日足が要る。

    上抜け(breakout_up): 前足が below/in → 当足が above。スクリーニング基準「一目均衡表 上抜け」
    下抜け(breakdown)  : 前足が above/in → 当足が below。鉄則「雲を下に抜けたらすぐ売る」

    「above → in」は下抜けではない（雲の中に入っただけ）。判定材料として position を別途返す。
    """
    n = len(highs)
    if n == 0 or len(lows) != n or len(closes) != n:
        return _NO_ICHIMOKU

    last = n - 1
    h = highs
    lo = lows
    tenkan = midpoint(h, lo, tenkan_n)
    kijun = midpoint(h, lo, kijun_n)
    span_a = None if (tenkan is None or kijun is None) else (tenkan + kijun) / 2.0
    span_b = midpoint(h, lo, span_b_n)

    def cloud_at(i: int) -> tuple[float | None, float | None]:
        a, b = _span_pair_at(h, lo, i - displacement, tenkan_n, kijun_n, span_b_n)
        if a is None or b is None:
            return (None, None)
        return (max(a, b), min(a, b))

    cloud_top, cloud_bottom = cloud_at(last)
    position = _position(closes[last], cloud_top, cloud_bottom)

    prev_position = None
    if last - 1 >= 0:
        p_top, p_bottom = cloud_at(last - 1)
        prev_position = _position(closes[last - 1], p_top, p_bottom)

    cross = None
    if position is not None and prev_position is not None:
        if position == "above" and prev_position in ("below", "in"):
            cross = "breakout_up"
        elif position == "below" and prev_position in ("above", "in"):
            cross = "breakdown"

    return Ichimoku(
        tenkan=tenkan,
        kijun=kijun,
        span_a=span_a,
        span_b=span_b,
        cloud_top=cloud_top,
        cloud_bottom=cloud_bottom,
        position=position,
        prev_position=prev_position,
        cross=cross,
    )


# =============================================================================
# 移動平均乖離率
# =============================================================================

def ma_deviation_pct(closes: Sequence[float | None],
                     n: int = MA_DEVIATION_PERIODS) -> float | None:
    """(最終終値 - n日移動平均) / n日移動平均 * 100。

    スクリーニング基準は 5%、鉄則の過熱ラインは 7〜8%（MA_DEVIATION_OVERHEAT_PCT）。
    """
    ma = sma(closes, n)
    if ma is None or ma == 0:
        return None
    last = closes[-1]
    if last is None:
        return None
    return (float(last) - ma) / ma * 100.0


# =============================================================================
# 出来高
# =============================================================================

def volume_ratio(volumes: Sequence[int | None],
                 n: int = VOLUME_RATIO_LOOKBACK_DAYS,
                 window: int = VOLUME_RATIO_WINDOW_DAYS) -> float | None:
    """3か月前比の出来高増加率（スクリーニング基準「3か月前出来高増加率5倍」）。

    直近 window 日平均 ÷ 「n営業日前を終点とする window 日平均」。
    n + window 本の出来高が要る（既定 65本）。

    比較先の平均が 0（NO_TRADE 連続）の場合は None。0 からの増加は倍率で表せない。
    ここで便宜的に大きな値を返すと、薄商い銘柄が常にスクリーニング条件を満たしてしまう。
    出来高 0 の日そのものは欠測ではないので、平均には 0 として算入する。
    """
    if volumes is None or window is None or window <= 0 or n is None or n <= 0:
        return None
    if len(volumes) < n + window:
        return None
    recent = _clean_tail(volumes, window)
    past = _clean_tail(volumes[: len(volumes) - n], window)
    if recent is None or past is None:
        return None
    past_avg = sum(past) / window
    if past_avg == 0:
        return None
    return (sum(recent) / window) / past_avg


def avg_turnover(closes: Sequence[float | None], volumes: Sequence[int | None],
                 n: int = TURNOVER_PERIODS) -> float | None:
    """n日平均売買代金（円）。流動性ゲート（判定の最上位）の入力。

    NO_TRADE の日は出来高 0 → 売買代金 0 として平均に算入する（除外しない）。
    取引がなかった事実こそが流動性の低さであり、除外すると流動性を過大評価する。
    閾値は data/master.yaml の liquidity_gate.min_avg_turnover_20d_jpy が正。
    """
    if closes is None or volumes is None or len(closes) != len(volumes):
        return None
    c = _clean_tail(closes, n)
    v = _clean_tail(volumes, n)
    if c is None or v is None:
        return None
    return sum(ci * vi for ci, vi in zip(c, v)) / n


def median_turnover(closes: Sequence[float | None], volumes: Sequence[int | None],
                    n: int = TURNOVER_PERIODS) -> float | None:
    """n日**中央値**売買代金（円）。平均の補助として台帳に出す（ゲートには使わない）。

    ゲートの根拠は「建てられず降りられない」ことなのに、平均は1日の突出で持ち上がる。
    実測（2026-08-10・20営業日）:
        3851 平均 7,807万 / 中央値 3,045万（最大寄与日が28.0%）
        4073 平均 6,596万 / 中央値   470万（最大寄与日が41.6%）
        4937 平均   268万 / 中央値    61万（最大寄与日が59.9%）
        6570 平均 2,624万 / 中央値   931万（最大寄与日が54.4%）
    4073 は中央値470万円の流動性で平均6,596万円としてゲートを通過している。
    **ゲートを中央値にも掛けるかはマスターの判断事項**（勝手に厳しくしない）。
    ここでは値を出して台帳に載せ、判断材料を隠さないことに留める。
    """
    if closes is None or volumes is None or len(closes) != len(volumes):
        return None
    c = _clean_tail(closes, n)
    v = _clean_tail(volumes, n)
    if c is None or v is None:
        return None
    vals = sorted(ci * vi for ci, vi in zip(c, v))
    mid = len(vals) // 2
    if len(vals) % 2:
        return vals[mid]
    return (vals[mid - 1] + vals[mid]) / 2.0


# =============================================================================
# ゴールデン/デッドクロス
# =============================================================================

def cross_signal(short_ma_series: Sequence[float | None],
                 long_ma_series: Sequence[float | None],
                 lookback: int = CROSS_LOOKBACK_PERIODS,
                 slope_periods: int = CROSS_SLOPE_PERIODS,
                 parallel_threshold_pct: float = CROSS_PARALLEL_SLOPE_DIFF_PCT
                 ) -> CrossSignal:
    """短期MA系列と長期MA系列からクロスを判定する。

    鉄則の「デッドクロス気味(平行ではない)＝売り」「ゴールデンクロス気味(平行ではない)
    ＝買い増しオーケー」を実装するため、交差の有無だけでなく **両線の傾き差** を返す。
    傾き差が parallel_threshold_pct 未満なら "parallel"（鉄則が除外する状態）とし、
    "気味" の判定を出さない。

    判定順: 実際の交差 → 平行 → 傾き差の符号。
    """
    if short_ma_series is None or long_ma_series is None:
        return _NO_CROSS
    if len(short_ma_series) != len(long_ma_series):
        return _NO_CROSS
    if lookback is None or lookback < 1 or slope_periods is None or slope_periods < 2:
        return _NO_CROSS

    need = max(lookback + 1, slope_periods)
    s = _clean_tail(short_ma_series, need)
    lg = _clean_tail(long_ma_series, need)
    if s is None or lg is None:
        return _NO_CROSS

    spread = [a - b for a, b in zip(s, lg)]
    spread_pct = None if lg[-1] == 0 else spread[-1] / lg[-1] * 100.0

    # 直近 lookback 本ぶんの遷移で符号が変わったか（最後の交差を採る）。
    # 判定の向きを対称にするため、境界の 0 は「まだ抜けていない」側に置く
    # （一致した状態から離れた瞬間を交差とみなす）。旧実装の `< 0 <=` / `> 0 >=` は
    # 非対称で、両線が一致した状態からの上抜けを拾えなかった。
    crossed = None
    for i in range(len(spread) - lookback, len(spread)):
        if i <= 0:
            continue
        if spread[i - 1] <= 0 < spread[i]:
            crossed = "up"
        elif spread[i - 1] >= 0 > spread[i]:
            crossed = "down"

    short_slope = slope_pct(s, slope_periods)
    long_slope = slope_pct(lg, slope_periods)
    if short_slope is None or long_slope is None:
        return CrossSignal(None, crossed, spread_pct, short_slope, long_slope,
                           None, None)

    diff = short_slope - long_slope
    parallel = abs(diff) < parallel_threshold_pct

    if crossed == "up":
        kind = "golden"
    elif crossed == "down":
        kind = "dead"
    elif parallel:
        kind = "parallel"
    elif diff > 0:
        kind = "golden_ish"
    else:
        kind = "dead_ish"

    return CrossSignal(
        kind=kind,
        crossed=crossed,
        spread_pct=spread_pct,
        short_slope_pct=short_slope,
        long_slope_pct=long_slope,
        slope_diff_pct=diff,
        parallel=parallel,
    )
