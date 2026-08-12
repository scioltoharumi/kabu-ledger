"""データ品質検査。すべてコードで判定し、LLM の自己レビューには依存しない。

要件: requirements.md F2 / 不変条件（append-only・欠測を埋めない）、
      review-findings.md F-02（フェイルセーフの向き）、decisions.md D7/D13/D16。

検査対象（存在するものだけを検査する。無いものは「検査した」ことにしない）:

    data/master.yaml          銘柄マスタ（**人間が手で書く唯一の判定入力**）
    data/prices/daily.csv     日足 OHLCV（append-only）
    data/indices/{id}.csv     指数 topix / growth250（append-only・列は daily.csv と同一）
    data/margin/{code}.csv    信用残高（append-only）
    data/kpi/{code}.csv       決算 KPI（append-only・スキーマは SKILL.md が正）

    FAIL -> ビルドを止める（weekly.yml の data ジョブが停止し、後続を実行しない）
    WARN -> 台帳に表示して続行する（F2-6）。build.py が run_checks() を直接呼んで
            data.html に一覧を出す。`--json` は人間・CI 向けの機械可読出力。

--------------------------------------------------------------------------
設計原則（破らないこと）
--------------------------------------------------------------------------

1. **欠測を通過扱いにしない。** 「検査できなかった」は「検査に通った」ではない。
   出来高が取れないと流動性ゲートごとスキップされていた review-findings.md F-02 と
   同じ罠を作らない。検査に必要な材料が無い場合は、黙って skip せず WARN で表に出す。

2. **感度のない検査を書かない。** 各検査は「この指標が動かない壊れ方は何か」に答える。
   件数だけを数える検査は「全銘柄が同じ値で埋まっている」に対して感度がゼロなので、
   別途 `check_cross_code_identity`（銘柄間の完全一致＝取り違え・同一ページ取得）と
   `check_frozen`（値が動かない＝更新の停止）を持つ。
   同様に「最新営業日が揃っているか」は `check_coverage` が持つ。行数の合計を見ても、
   1銘柄だけ取得漏れした週は検出できない。

3. **決定論的。** 出力に壁時計を持ち込まない（鮮度の検査も CSV の `fetched_at` と
   `date` の差で見る）。結果の並び順を固定する。同じ入力なら同じ出力になる。

4. **append-only の検証は git に依存し切らない。** CI では git HEAD を、ローカルや
   テストでは `--baseline`（以前の data/ のコピー）をベースラインにする。
   どちらも無い場合は「検証をスキップ」ではなく WARN として表に出す（原則1）。

--------------------------------------------------------------------------
参照値の定義（重要）
--------------------------------------------------------------------------

`close` 列は「2ソース一致で採用した値」であり、`SINGLE_SOURCE` / `MISMATCH` の行は空。
判定と指標計算はこの `close` だけを使う（`indicators.bars_from_rows` は
`value_primary` で埋めない。照合を通っていない値を採用値に格上げしないため）。

**一方、本モジュールの整合性検査は `close` が空なら `value_primary` を参照値に使う。**
検査の問いが「採用に足る値か」ではなく「取得したものが壊れていないか」だからである。
`close` だけを見ると、SINGLE_SOURCE の33行と `growth250`（第2ソースが無く全行 `close` 空）
の269行が完全に無検査になる。参照値は必ず「主ソースが返した4本値と同じ行」から取るため、
OHLC の内部整合性は `value_primary` を使っても崩れない。

--------------------------------------------------------------------------
分割・権利落ちの確認記録（任意ファイル）
--------------------------------------------------------------------------

`check_split` は整数比の下落を FAIL にする（F2-4）。一次情報で確認したものは
`data/corporate_actions.yaml` に記録すると WARN に落ちる。**過去行は書き換えない**。

    actions:
      - code: "4073"          # 指数なら "topix" 等
        date: "2026-09-01"    # 比率が変わった日（下落として現れた日）
        kind: split           # split / reverse_split / rights / not_corporate_action
        ratio: "1:2"
        source_url: "https://www.release.tdnet.info/..."   # 空なら FAIL のまま

`kind: not_corporate_action` は「TDnet で確認した結果、分割ではなく実際の下落だった」の意。
`source_url` が空の記録は「確認していない」として FAIL のままにする（F8-6 と同じ扱い）。

--------------------------------------------------------------------------
使い方
--------------------------------------------------------------------------

    python src/checks.py                      # data/ を検査（CI の既定）
    python src/checks.py --scan-all           # 分割・外れ値を全履歴で走査（初回一括取得の直後）
    python src/checks.py --baseline old_data  # append-only を明示ベースラインで検証
    python src/checks.py --json               # 機械可読出力（build.py の欠測表示用）
    python src/checks.py --data-dir tmp/data  # 別ディレクトリを検査（テスト用）
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import statistics
import subprocess
import sys
from dataclasses import dataclass
from datetime import date as _date
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))

# holding.status の語彙は judge.py が正（SSoT）。ここで再定義しない。
# 閾値の再計算（MARGIN_RATIO_TOLERANCE 等）は「独立検算」なのであえて複製するが、
# **語彙は複製すると片方だけ増えて検査が素通りになる**ので参照する。
from judge import HOLDING_STATUSES  # noqa: E402

# =============================================================================
# 定数ブロック（閾値・語彙はすべてここ。根拠を併記する）
# =============================================================================

FAIL, WARN = "FAIL", "WARN"

# --- 検査の並び（出力順を固定するための正。ここに無い check 名は末尾に落ちる） ---
CHECK_ORDER = (
    "schema", "duplicate", "master", "master_schema", "coverage", "append_only",
    "ohlc", "split", "outlier", "volume", "cross_code", "frozen", "no_trade",
    "missing", "freshness", "margin", "index", "kpi",
)
_CHECK_INDEX = {name: i for i, name in enumerate(CHECK_ORDER)}

# --- 株価・指数 CSV -----------------------------------------------------------
PRICE_FIELDS = (
    "date", "code", "open", "high", "low", "close", "volume", "status",
    "source_primary", "value_primary", "source_secondary", "value_secondary",
    "fetched_at",
)
# status の語彙は fetch.py / sources.yaml が正。ここに無い値はスキーマ変更＝FAIL。
# status は `|` 区切りで複数のフラグを持つ（例: `SINGLE_SOURCE|NO_TRADE`）。
# 照合結果（OK / MISMATCH / SINGLE_SOURCE / FETCH_FAILED）は必ず1つ入り、
# NO_TRADE・VOLUME_MISMATCH は付加情報として並ぶ。
PRICE_RECONCILE_STATUSES = {"OK", "MISMATCH", "SINGLE_SOURCE", "FETCH_FAILED"}
PRICE_EXTRA_STATUSES = {"NO_TRADE", "VOLUME_MISMATCH"}
PRICE_STATUSES = PRICE_RECONCILE_STATUSES | PRICE_EXTRA_STATUSES
# `close`（＝採用値）が埋まってよいのは **照合が成立した行だけ**（OK を含む status）。
# 旧 fetch.py は NO_TRADE のとき照合結果を潰して主ソース値を close に入れていたため、
# 2026-08-12 以前に書かれた `NO_TRADE` 単独の行（実データ7行）は close が埋まっている。
# append-only なので過去行は直さない。**その7行だけを例外として通す**（新規に
# `NO_TRADE` 単独が現れることは、修正後の fetch.py では起こらない）。
LEGACY_NO_TRADE_STATUS = "NO_TRADE"
# 欠測として台帳冒頭に出す status（F2-6）
MISSING_STATUSES = ("FETCH_FAILED", "MISMATCH", "SINGLE_SOURCE")

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# --- 分割・権利落ち（F2-4） ----------------------------------------------------
SPLIT_RATIOS = {0.5: "1:2", 1 / 3: "1:3", 0.25: "1:4", 0.2: "1:5", 2 / 3: "2:3"}
SPLIT_TOLERANCE = 0.02
CORPORATE_ACTION_KINDS = {"split", "reverse_split", "rights", "not_corporate_action"}

# --- 外れ値（F2-5。絶対値閾値は小型株で誤検知だらけになるため σ ベース） --------
OUTLIER_SIGMA = 4.0
OUTLIER_MIN_POINTS = 12          # σ を推定するのに必要な変化率の点数
# 既定では直近 RECENT_BARS 本だけを走査する。週次で追記されるのは直近5営業日分（D13）で、
# 全履歴を毎週走査すると同じ WARN を延々と出し続けることになる。
# 初回の一括取得（D16・1年分）の直後は `--scan-all` で全履歴を見る。
RECENT_BARS = 5

# --- 出来高の桁飛び（列の取り違え・単位違い） ---------------------------------
# 出来高は2ソース照合を通らないまま流動性ゲートの入力になる唯一の数値。
# **主たる防御は fetch.py の VOLUME_MISMATCH（2ソースの出来高が2倍以上食い違う）**
# であり、この統計検査は補助にすぎない。監視銘柄は超小型株で出来高の自然な変動幅が
# 大きく（実データで中央値の 0.01〜249倍）、閾値を下げると誤検知だらけになるため、
# **桁が3つ飛ぶ水準（1000倍）**だけを拾う。単位違い（株↔千株↔売買代金）はここで鳴る。
VOLUME_JUMP_RATIO = 1000.0
VOLUME_JUMP_MIN_POINTS = 20

# --- 値が動かない壊れ方（キャッシュ固着・同一ページの取得） --------------------
# 実データの最長は3営業日連続（4銘柄・269営業日）。10 は十分に上の閾値。
FROZEN_MIN_RUN = 10
# 別コードが同一日に OHLCV 完全一致 = 同じページを取っている疑い。
# 実データでの発生は0件。1〜2日は偶然を許容して WARN、3日以上は取り違えとして FAIL。
CROSS_CODE_FAIL_DAYS = 3

# --- 鮮度（壁時計を使わず CSV 内の値だけで見る） ------------------------------
# 週次実行なので通常は 1〜3 日。年末年始・GW を跨いでも 14 日は超えない。
STALE_FETCH_DAYS = 14

# --- 信用残高 -----------------------------------------------------------------
MARGIN_FIELDS = ("date", "code", "long_balance", "short_balance", "ratio",
                 "unit", "status", "source_url", "fetched_at")
MARGIN_STATUSES = {"OK", "RATIO_NA", "UNIT_UNKNOWN", "BALANCE_MISSING",
                   "RATIO_INCONSISTENT"}
# 倍率 = 買い残 ÷ 売り残 の再計算許容。fetch_margin.py と同じ値を**あえて複製する**。
# これは fetch の計算を検算する独立検査であり、import して同じ実装を共有すると
# fetch 側のバグをそのまま引き継いで感度がゼロになる。
MARGIN_RATIO_TOLERANCE = 0.15
MARGIN_RATIO_CHECK_MIN_SHORT = 1.0
# judge.py の margin_max_age_days（28日）と同じ。これを超えると judge は unknown に
# 落として「調査」で止める。止まる前に台帳で見えるように WARN を出す。
MARGIN_MAX_AGE_DAYS = 28

# --- 決算 KPI（スキーマの正は .claude/skills/kabu-ledger/SKILL.md） -----------
KPI_FIELDS = ("date", "code", "metric", "value", "unit", "definition",
              "assumed", "source_url", "fetched_at")
KPI_UNITS = {"JPY", "JPY_thousand", "JPY_million", "JPY_billion", "pct", "x",
             "shares"}
KPI_METRICS = {
    "revenue", "revenue_prev_year", "revenue_fy_plan",
    "operating_income", "operating_income_prev_year", "operating_income_fy_plan",
    "ordinary_income", "ordinary_income_prev_year", "ordinary_income_fy_plan",
}
KPI_SEGMENT_RE = re.compile(r"^segment_revenue:[a-z0-9_]+$")
# **コードが計算する derived metric**。CSV に書かれていたら LLM が比率を計算している
# （F8-4・SKILL.md「Claude が引き算しない」の違反）。名前で検出できる唯一の防波堤。
KPI_DERIVED_METRICS = {"revenue_yoy_pct", "ordinary_income_yoy_pct",
                       "q1_progress_pct", "stock_revenue_ratio"}
KPI_VALUE_ABS_MAX = 1e9          # SKILL.md「上限点検」。百万円単位で1000兆円
KPI_NON_NEGATIVE_PREFIX = ("revenue",)   # SKILL.md「符号点検」: 売上高が負なら誤読

SAMPLE_LIMIT = 5                  # 1メッセージに載せる実例の最大数


# =============================================================================
# 結果
# =============================================================================

@dataclass(frozen=True)
class Result:
    level: str      # FAIL / WARN
    check: str      # CHECK_ORDER のいずれか
    target: str     # 対象ファイル（data/ からの相対パス）または "-"
    message: str

    def line(self) -> str:
        return f"[{self.level}] {self.check} {self.target}: {self.message}"


class Report:
    """検査結果の集約。順序は emit() で固定する（決定論的生成）。"""

    def __init__(self) -> None:
        self._results: list[Result] = []

    def fail(self, check: str, target: str, message: str) -> None:
        self._results.append(Result(FAIL, check, target, message))

    def warn(self, check: str, target: str, message: str) -> None:
        self._results.append(Result(WARN, check, target, message))

    def group(self, level: str, check: str, target: str, rule: str,
              items: list[str], limit: int = SAMPLE_LIMIT) -> None:
        """同種の違反をまとめて1件にする。件数を必ず出し、実例を limit 件まで載せる。"""
        if not items:
            return
        ordered = sorted(items)
        body = " / ".join(ordered[:limit])
        more = f" ほか{len(ordered) - limit}件" if len(ordered) > limit else ""
        msg = f"{rule}: {len(ordered)}件 — {body}{more}"
        (self.fail if level == FAIL else self.warn)(check, target, msg)

    @property
    def results(self) -> list[Result]:
        return sorted(self._results, key=lambda r: (
            _CHECK_INDEX.get(r.check, len(CHECK_ORDER)),
            r.level, r.target, r.message))

    @property
    def fails(self) -> int:
        return sum(1 for r in self._results if r.level == FAIL)

    @property
    def warns(self) -> int:
        return sum(1 for r in self._results if r.level == WARN)


# =============================================================================
# 小さな部品
# =============================================================================

def load_csv(path: Path) -> list[dict]:
    """CSV を dict の列で読む。BOM 付き（Excel で開いて保存した場合）も受ける。"""
    with path.open(encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def _blank(v) -> bool:
    return v is None or str(v).strip() == ""


def _f(v) -> float | None:
    """数値として読めれば float、空・非数値なら None。"""
    if _blank(v):
        return None
    try:
        return float(str(v).strip())
    except ValueError:
        return None


def _unreadable(v) -> bool:
    """空ではないのに数値として読めない（＝パース事故）。"""
    return not _blank(v) and _f(v) is None


def _d(v) -> _date | None:
    if _blank(v):
        return None
    try:
        return _date.fromisoformat(str(v).strip()[:10])
    except ValueError:
        return None


def _days(a: str | None, b: str | None) -> int | None:
    da, db = _d(a), _d(b)
    return None if (da is None or db is None) else (db - da).days


def ref_close(row: dict) -> float | None:
    """整合性検査に使う参照値。`close`（採用値）が空なら主ソースの終値を使う。

    モジュール冒頭「参照値の定義」を参照。判定・指標計算では絶対にこの代替をしない。
    """
    c = _f(row.get("close"))
    return c if c is not None else _f(row.get("value_primary"))


def build_series(rows: list[dict]) -> dict[str, list[tuple[str, float, int | None]]]:
    """コード別の (日付, 参照終値, 出来高) 系列を日付昇順で返す。参照値が無い行は落とす。"""
    out: dict[str, list[tuple[str, float, int | None]]] = {}
    for r in sorted(rows, key=lambda x: (str(x.get("code")), str(x.get("date")))):
        c = ref_close(r)
        if c is None:
            continue
        v = _f(r.get("volume"))
        out.setdefault(str(r.get("code")), []).append(
            (str(r.get("date")), c, None if v is None else int(v)))
    return out


# =============================================================================
# 1. スキーマ（株価・指数で共通）
# =============================================================================

def check_ohlcv_schema(rep: Report, rows: list[dict], target: str) -> None:
    """列・status 語彙・状態と値の整合を見る（F2-1）。

    「status=OK なのに close が空」だけでは、逆向きの壊れ方（照合していない値が
    採用値の列に入る／不一致のはずが同値）を拾えないので、両方向を検査する。
    """
    if not rows:
        return
    missing = [c for c in PRICE_FIELDS if c not in rows[0]]
    if missing:
        rep.fail("schema", target, f"必須列が欠落: {missing}")
        return

    bad_date, bad_code, bad_status, no_fetched = [], [], [], []
    ok_no_close, adopted_wrong, ok_disagree, ok_no_secondary = [], [], [], []
    mismatch_same, unreadable, non_positive, neg_volume, no_value = [], [], [], [], []
    nt_disagree, nt_volume = [], []

    for r in rows:
        key = f"{r.get('code')} {r.get('date')}"
        date, status = str(r.get("date") or ""), str(r.get("status") or "")
        flags = [p for p in status.split("|") if p]
        if not DATE_RE.match(date) or _d(date) is None:
            bad_date.append(f"{key}（date={date!r}）")
        if _blank(r.get("code")):
            bad_code.append(f"{key}")
        # 照合結果はちょうど1つ入っていること。
        # 旧 fetch.py が書いた `NO_TRADE` 単独（実データ7行）だけを例外にする。
        if status != LEGACY_NO_TRADE_STATUS and (
                not flags or any(p not in PRICE_STATUSES for p in flags)
                or len(set(flags) & PRICE_RECONCILE_STATUSES) != 1):
            bad_status.append(f"{key}（status={status!r}）")
        if _blank(r.get("fetched_at")):
            no_fetched.append(key)

        close, vp, vs = _f(r.get("close")), _f(r.get("value_primary")), _f(r.get("value_secondary"))
        if "OK" in flags:
            if _blank(r.get("close")):
                ok_no_close.append(key)
            if _blank(r.get("source_secondary")) or vs is None:
                ok_no_secondary.append(key)
            elif close is not None and (close != vp or close != vs):
                ok_disagree.append(f"{key}（close={close} / 主={vp} / 副={vs}）")
        # 採用値が入ってよいのは照合成立行だけ。旧 fetch.py が書いた
        # `NO_TRADE` 単独の行のみ例外（上の LEGACY_NO_TRADE_STATUS の注記を参照）。
        if (not _blank(r.get("close")) and "OK" not in flags
                and status != LEGACY_NO_TRADE_STATUS):
            adopted_wrong.append(f"{key}（status={status} なのに close={r.get('close')}）")
        # NO_TRADE でも主副が食い違っていれば「売買不成立だった」とは言えない。
        # 旧実装は NO_TRADE が MISMATCH を握り潰していたため、この検査が無いと
        # 照合していない主ソース値が採用値のまま台帳に載る。
        if "NO_TRADE" in flags and vs is not None and vp is not None and vp != vs:
            nt_disagree.append(f"{key}（主={vp} / 副={vs}）")
        vol_raw = _f(r.get("volume"))
        if "NO_TRADE" in flags and (vol_raw is None or vol_raw != 0):
            nt_volume.append(f"{key}（volume={r.get('volume')!r}）")
        if "MISMATCH" in flags and vp is not None and vp == vs:
            mismatch_same.append(f"{key}（主副とも {vp}）")
        if "FETCH_FAILED" not in flags and vp is None:
            no_value.append(f"{key}（status={status}）")

        for col in ("open", "high", "low", "close", "volume",
                    "value_primary", "value_secondary"):
            if _unreadable(r.get(col)):
                unreadable.append(f"{key} {col}={r.get(col)!r}")
        for col in ("open", "high", "low", "close", "value_primary", "value_secondary"):
            v = _f(r.get(col))
            if v is not None and v <= 0:
                non_positive.append(f"{key} {col}={v}")
        vol = _f(r.get("volume"))
        if vol is not None and vol < 0:
            neg_volume.append(f"{key} volume={vol}")

    g = rep.group
    g(FAIL, "schema", target, "date が YYYY-MM-DD として読めない", bad_date)
    g(FAIL, "schema", target, "code が空", bad_code)
    g(FAIL, "schema", target, "status が定義外（スキーマ変更の疑い）", bad_status)
    g(FAIL, "schema", target, "fetched_at が空（出所不明の数値は記録しない）", no_fetched)
    g(FAIL, "schema", target, "status=OK だが close が空", ok_no_close)
    g(FAIL, "schema", target, "status=OK だが第2ソースの記録が無い", ok_no_secondary)
    g(FAIL, "schema", target, "status=OK だが close と照合値が食い違う", ok_disagree)
    g(FAIL, "schema", target,
      "照合を通っていない値が close（採用値）に入っている", adopted_wrong)
    g(FAIL, "schema", target,
      "NO_TRADE だが主副の終値が食い違っている（売買不成立とは言えない）", nt_disagree)
    g(FAIL, "schema", target, "NO_TRADE だが出来高が0でない", nt_volume)
    g(FAIL, "schema", target, "status=MISMATCH だが主副の値が一致している", mismatch_same)
    g(FAIL, "schema", target, "数値として読めない値", unreadable)
    g(FAIL, "schema", target, "価格が 0 以下", non_positive)
    g(FAIL, "schema", target, "出来高が負", neg_volume)
    g(FAIL, "schema", target, "取得できているはずの行に主ソースの値が無い", no_value)


def check_duplicate(rep: Report, rows: list[dict], target: str,
                    keys: tuple[str, ...]) -> None:
    """append-only の一意キーが重複していないか（重複＝追記処理の破綻）。"""
    seen: dict[tuple, int] = {}
    for r in rows:
        k = tuple(str(r.get(c) or "") for c in keys)
        seen[k] = seen.get(k, 0) + 1
    dups = [f"{'/'.join(k)}（{n}行）" for k, n in seen.items() if n > 1]
    rep.group(FAIL, "duplicate", target,
              f"一意キー {'+'.join(keys)} が重複", dups)


# =============================================================================
# 2. マスタ突合・カバレッジ
# =============================================================================

def check_master(rep: Report, rows: list[dict], master: dict, target: str) -> None:
    known = {str(s["code"]) for s in master.get("stocks", [])}
    unknown = sorted({str(r.get("code")) for r in rows if str(r.get("code")) not in known})
    for code in unknown:
        rep.fail("master", target, f"マスタ未登録のコード: {code}（銘柄取り違えの疑い）")


def check_master_schema(rep: Report, master: dict) -> None:
    """master.yaml そのものの検査。**人間が手で書く唯一の判定入力**（F13-1・D18）。

    `holding.status` は judge が固定語彙として読む。旧実装は語彙外の値をすべて
    「保有していない」と解釈していたため、`hold` / `保有` / `true` の打ち間違い、
    あるいは `status: none` のまま `buy_price` だけ入れる操作で、
    **逆指値ゲートが黙って消えて判定が「買」まで通っていた**。
    judge 側は語彙外を unknown（＝調査）に落とすように直したが、
    どこにも出ないと気づけないのでここで FAIL にする。
    """
    target = "data/master.yaml"
    for key in ("liquidity_gate", "target_ladder", "stop_loss_pct"):
        if master.get(key) in (None, {}, []):
            rep.fail("master_schema", target,
                     f"{key} が無い（judge は既定値で代替せず「調査」で止まる）")
    gate = master.get("liquidity_gate") or {}
    if gate and _f(gate.get("min_avg_turnover_20d_jpy")) is None:
        rep.fail("master_schema", target,
                 "liquidity_gate.min_avg_turnover_20d_jpy が数値として読めない")
    if _f(master.get("stop_loss_pct")) is None and master.get("stop_loss_pct") is not None:
        rep.fail("master_schema", target, "stop_loss_pct が数値として読めない")

    for s in master.get("stocks") or []:
        code = str(s.get("code"))
        h = s.get("holding")
        if h is None:
            rep.fail("master_schema", target, f"{code}: holding が無い（D18）")
            continue
        status = h.get("status")
        text = str(status).strip().lower() if status is not None else "none"
        if text not in HOLDING_STATUSES:
            rep.fail("master_schema", target,
                     f"{code}: holding.status が語彙外（{status!r}）。"
                     f"使えるのは {' / '.join(sorted(HOLDING_STATUSES))} のみ")
            continue
        filled = sorted(k for k in ("buy_price", "buy_date", "shares")
                        if h.get(k) not in (None, ""))
        if text == "none" and filled:
            rep.fail("master_schema", target,
                     f"{code}: holding.status=none なのに {', '.join(filled)} が"
                     "入っている（保有登録の書き漏れ）")
        if text == "holding":
            lacking = sorted(k for k in ("buy_price", "buy_date")
                             if h.get(k) in (None, ""))
            if lacking:
                rep.fail("master_schema", target,
                         f"{code}: holding.status=holding なのに "
                         f"{', '.join(lacking)} が無い（逆指値・基準ラインを算出できない）")
            if h.get("buy_date") is not None and _d(h.get("buy_date")) is None:
                rep.fail("master_schema", target,
                         f"{code}: holding.buy_date が日付として読めない"
                         f"（{h.get('buy_date')!r}）")


def check_coverage(rep: Report, rows: list[dict], master: dict, target: str) -> None:
    """最新営業日のデータが全銘柄そろっているか（取得漏れの検出）。

    行数の合計を見ても1銘柄だけの取得漏れは分からない。fetch.py は全ソース失敗時に
    **行を1つも書かない**ため、欠測が「行の不在」として現れる。不在は status で
    表現されないので、ここで見なければどこにも出てこない（review-findings F-02 と同型）。
    """
    if not rows:
        rep.fail("coverage", target, "株価データが1行も無い")
        return

    by_code: dict[str, set[str]] = {}
    for r in rows:
        by_code.setdefault(str(r.get("code")), set()).add(str(r.get("date")))
    all_dates = sorted({d for ds in by_code.values() for d in ds})
    latest = all_dates[-1]

    holes: list[str] = []
    for stock in sorted(master.get("stocks", []), key=lambda s: str(s["code"])):
        code = str(stock["code"])
        dates = by_code.get(code)
        if not dates:
            rep.fail("coverage", target, f"{code}: 行が1つも無い（取得漏れ）")
            continue
        if latest not in dates:
            last = max(dates)
            gap = sum(1 for d in all_dates if d > last)
            rep.fail("coverage", target,
                     f"{code}: 最新営業日 {latest} の行が無い"
                     f"（最終行 {last}・{gap}営業日ぶん欠落）")
        # 自分の履歴の範囲内にある穴（初回一括取得の取りこぼし）
        lo, hi = min(dates), max(dates)
        gaps = [d for d in all_dates if lo < d < hi and d not in dates]
        holes += [f"{code} {d}" for d in gaps]

    rep.group(WARN, "coverage", target,
              "他銘柄には行があるのにこの銘柄には無い営業日", holes)


# =============================================================================
# 3. 追記性（append-only）
# =============================================================================

class Baseline:
    """比較元の提供者。git HEAD かディレクトリ。無ければ None を返す。"""

    def __init__(self, kind: str, files: set[str], reader) -> None:
        self.kind = kind
        self.files = files
        self._read = reader

    def read(self, rel: str) -> str | None:
        return self._read(rel)


def _git(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    """git を UTF-8 固定で呼ぶ。

    text=True の既定はロケール（Windows なら cp932）で復号するため、
    UTF-8 の CSV を `git show` すると復号に失敗し、**空文字が返って比較が黙って
    素通りする**。追記性の検査が無言で無効化されるのが最悪なので encoding を固定する。
    """
    return subprocess.run(args, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", cwd=cwd, check=False)


def _git_baseline(root: Path) -> Baseline | None:
    """git 管理下なら HEAD をベースラインにする（CI はこちらを使う）。"""
    try:
        top = _git(["git", "rev-parse", "--show-toplevel"], root)
    except (FileNotFoundError, OSError):
        return None
    if top.returncode != 0 or not top.stdout.strip():
        return None
    toplevel = Path(top.stdout.strip())
    ls = _git(["git", "ls-files", "-z", "--", "data"], toplevel)
    if ls.returncode != 0:
        return None
    prefix = ""
    try:
        prefix = root.resolve().relative_to(toplevel.resolve()).as_posix()
    except ValueError:
        return None
    prefix = "" if prefix == "." else f"{prefix}/"
    files = set()
    for p in ls.stdout.split("\0"):
        if p.startswith(f"{prefix}data/") and p.endswith(".csv"):
            files.add(p[len(prefix):])

    def read(rel: str) -> str | None:
        r = _git(["git", "show", f"HEAD:{prefix}{rel}"], toplevel)
        return r.stdout if r.returncode == 0 else None

    return Baseline("git HEAD", files, read)


def _dir_baseline(data_dir: Path, baseline_dir: Path) -> Baseline:
    """`--baseline` で渡した「以前の data/ のコピー」をベースラインにする。"""
    files = {f"data/{p.relative_to(baseline_dir).as_posix()}"
             for p in sorted(baseline_dir.rglob("*.csv"))}

    def read(rel: str) -> str | None:
        p = baseline_dir / Path(rel).relative_to("data")
        return p.read_text(encoding="utf-8-sig") if p.exists() else None

    return Baseline(f"ディレクトリ {baseline_dir}", files, read)


def _key_columns(rel: str) -> tuple[str, ...]:
    return ("code", "date", "metric") if "/kpi/" in rel else ("code", "date")


def _fetch_gap(rows: list[dict]) -> int | None:
    """直近の取得実行が持ち帰った最新営業日と、その実行日との差（日）。

    壁時計を使わず CSV 内の `fetched_at` と `date` だけで測る（決定論的）。
    check_freshness と同じ計算。**同じ問いを2箇所から見るためにあえて共有する**
    （片方は「日付が古い」、もう片方は「行が増えていない」ときの重み付けに使う）。
    """
    fetch_days = [str(r.get("fetched_at") or "")[:10] for r in rows
                  if not _blank(r.get("fetched_at"))]
    if not fetch_days:
        return None
    last_fetch_day = max(fetch_days)
    if _d(last_fetch_day) is None:
        return None
    batch = [str(r.get("date")) for r in rows
             if str(r.get("fetched_at") or "")[:10] == last_fetch_day]
    return _days(max(batch), last_fetch_day) if batch else None


def check_append_only(rep: Report, data_dir: Path, baseline: Baseline | None) -> None:
    """過去行の変更・削除を検出する（F2-3・不変条件）。

    行を突き合わせて比較する。git diff の行数を数えるだけだと、どのキーの
    どの列が書き換わったのかが分からず、列構成の変更と値の改変も区別できない。
    """
    if baseline is None:
        rep.warn("append_only", "-",
                 "ベースラインが無いため追記性を検証できていない"
                 "（git 管理下で実行するか --baseline を渡す）。"
                 "**検証済みではない**")
        return

    current = {f"data/{p.relative_to(data_dir).as_posix()}"
               for p in sorted(data_dir.rglob("*.csv"))}
    for rel in sorted(baseline.files | current):
        text = baseline.read(rel)
        if text is None:
            continue                      # ベースラインに無い＝新規ファイル
        path = data_dir / Path(rel).relative_to("data")
        if not path.exists():
            rep.fail("append_only", rel, "ベースラインに存在したファイルが消えている")
            continue

        old = list(csv.DictReader(text.splitlines()))
        new = load_csv(path)
        keys = _key_columns(rel)
        old_cols = set(old[0].keys()) if old else set()
        new_cols = set(new[0].keys()) if new else set()
        if old and new and old_cols != new_cols:
            rep.warn("append_only", rel,
                     f"列構成が変わっている（追加 {sorted(new_cols - old_cols)} / "
                     f"削除 {sorted(old_cols - new_cols)}）。共通列のみ比較する")
        shared = sorted(old_cols & new_cols) or sorted(old_cols)

        index = {tuple(str(r.get(c) or "") for c in keys): r for r in new}
        removed, changed = [], []
        for r in old:
            k = tuple(str(r.get(c) or "") for c in keys)
            cur = index.get(k)
            if cur is None:
                removed.append("/".join(k))
                continue
            diffs = [f"{c}: {r.get(c)!r} → {cur.get(c)!r}"
                     for c in shared if str(r.get(c) or "") != str(cur.get(c) or "")]
            if diffs:
                changed.append(f"{'/'.join(k)}（{' , '.join(diffs)}）")

        rep.group(FAIL, "append_only", rel, "過去行が削除されている", removed)
        rep.group(FAIL, "append_only", rel, "過去行が変更されている", changed)

        # 取得が止まっている壊れ方の検出。セレクタが外れても例外は出ず、
        # ファイルは前週のまま静かに残る（行数も status も変化しない）。
        #
        # 「同じ週に2回流した」だけでも 0件になるので WARN が基本。ただし
        # **取得は動いているのに古い日付しか返ってこない**（＝ fetched_at が進んでいるのに
        # 最終営業日が離れている）状態と重なったら、それは休場ではなく壊れているので FAIL。
        # ここで止めないと、台帳は3か月前の値を「今週の判定」として毎週掲示し続ける。
        if rel == "data/prices/daily.csv" and len(new) == len(old) and not removed:
            gap = _fetch_gap(new)
            if gap is not None and gap > STALE_FETCH_DAYS:
                rep.fail("append_only", rel,
                         f"追記が0件（{len(new)}行のまま）で、かつ直近の取得実行が"
                         f"持ち帰った最新営業日が {gap}日前（{STALE_FETCH_DAYS}日超）。"
                         "取得が壊れている（セレクタ外れ・キャッシュ）。"
                         "先週と同じ判定を「今週の判定」として掲示しない")
            else:
                rep.warn("append_only", rel,
                         f"追記が0件（{len(new)}行のまま）。"
                         "取得が空振りしている可能性がある（セレクタ外れ・休場・再実行）")


# =============================================================================
# 4. OHLC の整合性
# =============================================================================

def check_ohlc(rep: Report, rows: list[dict], target: str) -> None:
    """高値・安値が四本値として成立しているか。

    一目均衡表（転換線・基準線・先行スパンB）は高値・安値だけで作られる。
    高安が入れ替わった行が1本混ざると、雲がずれたまま「売り」も「上抜け」も
    静かに誤判定される。値そのものの妥当性はここでしか見られない。
    """
    hl, ho, hc, lo_, lc, missing = [], [], [], [], [], []
    for r in rows:
        key = f"{r.get('code')} {r.get('date')}"
        h, l = _f(r.get("high")), _f(r.get("low"))
        o, c = _f(r.get("open")), ref_close(r)
        if h is None or l is None:
            missing.append(key)
            continue
        if h < l:
            hl.append(f"{key}（high={h} < low={l}）")
        if o is not None and h < o:
            ho.append(f"{key}（high={h} < open={o}）")
        if c is not None and h < c:
            hc.append(f"{key}（high={h} < close={c}）")
        if o is not None and l > o:
            lo_.append(f"{key}（low={l} > open={o}）")
        if c is not None and l > c:
            lc.append(f"{key}（low={l} > close={c}）")

    g = rep.group
    g(FAIL, "ohlc", target, "high < low", hl)
    g(FAIL, "ohlc", target, "high < open", ho)
    g(FAIL, "ohlc", target, "high < close", hc)
    g(FAIL, "ohlc", target, "low > open", lo_)
    g(FAIL, "ohlc", target, "low > close", lc)
    g(WARN, "ohlc", target,
      "高値・安値が空（一目均衡表を計算できない日）", missing)


# =============================================================================
# 5. 分割・権利落ち / 外れ値
# =============================================================================

def load_corporate_actions(data_dir: Path) -> dict[tuple[str, str], dict]:
    """確認済みの資本異動記録（任意ファイル）。無ければ空。"""
    path = data_dir / "corporate_actions.yaml"
    if not path.exists():
        return {}
    doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    out: dict[tuple[str, str], dict] = {}
    for a in doc.get("actions") or []:
        out[(str(a.get("code")), str(a.get("date")))] = a
    return out


def check_split(rep: Report, series: dict, target: str,
                actions: dict[tuple[str, str], dict], scan_all: bool) -> None:
    """整数比に近い下落は暴落ではなく分割を第一に疑う（F2-4）。

    確認は TDnet の一次情報で行い、結果を data/corporate_actions.yaml に記録すると
    WARN に落ちる。**過去行は書き換えない**（生値を保持する F10-1）。
    """
    for code in sorted(series):
        pts = series[code]
        span = range(1, len(pts)) if scan_all else \
            range(max(1, len(pts) - RECENT_BARS), len(pts))
        for i in span:
            prev, cur = pts[i - 1][1], pts[i][1]
            date = pts[i][0]
            if not prev:
                continue
            ratio = cur / prev
            for r, label in sorted(SPLIT_RATIOS.items()):
                if abs(ratio - r) >= SPLIT_TOLERANCE:
                    continue
                act = actions.get((code, date))
                if act and not _blank(act.get("source_url")) \
                        and str(act.get("kind")) in CORPORATE_ACTION_KINDS:
                    rep.warn("split", target,
                             f"{code} {date}: 前値比 {ratio:.3f}（{label} 相当）。"
                             f"確認済み: {act.get('kind')} {act.get('ratio')} "
                             f"{act.get('source_url')}")
                else:
                    why = "確認記録が無い" if not act else \
                        "確認記録に source_url または kind が無い"
                    rep.fail("split", target,
                             f"{code} {date}: 前値比 {ratio:.3f}（{label} 分割の可能性・"
                             f"{why}）。TDnet で確認し data/corporate_actions.yaml に"
                             "記録するまで時系列に接続しない")


def check_volume_jump(rep: Report, series: dict, target: str,
                      scan_all: bool) -> None:
    """出来高が桁で飛んでいないか（列の取り違え・単位違いの検出）。

    **出来高は close と違って2ソース照合を通らない**（`fetch.py` は主ソースの値を
    そのまま書く）。ところが流動性ゲート（判定の最上位）の唯一の入力は
    `avg_turnover(close, volume)` である。`sources.yaml` の volume 列インデックスは
    取得元ごとに違う（kabutan=7 / minkabu=6 / yahoo=5）ので、ページに1列挿入された
    だけで別の列（売買代金など）を読む。review-findings F-02 は「出来高が**取れない**と
    素通り」だったが、これは「出来高が**間違っている**と素通り」で向きは同じ。

    比較先は同一銘柄の中央値。σ ではなく中央値比にするのは、出来高の分布が
    対数正規に近く裾が厚いためで、σ ベースだと薄商い銘柄で誤検知が出る。
    """
    for code in sorted(series):
        pts = series[code]
        vols = [(d, v) for d, _, v in pts if v is not None and v > 0]
        if len(vols) < VOLUME_JUMP_MIN_POINTS:
            rep.warn("volume", target,
                     f"{code}: 出来高が {len(vols)}点しかなく桁の検定ができない"
                     f"（{VOLUME_JUMP_MIN_POINTS}点必要）")
            continue
        med = statistics.median([v for _, v in vols])
        if med <= 0:
            continue
        window = vols if scan_all else vols[-RECENT_BARS:]
        for date, v in window:
            ratio = v / med
            if ratio > VOLUME_JUMP_RATIO or ratio < 1 / VOLUME_JUMP_RATIO:
                rep.warn("volume", target,
                         f"{code} {date}: 出来高 {v:,} が中央値 {med:,.0f} の "
                         f"{ratio:,.1f}倍（{VOLUME_JUMP_RATIO:,.0f}倍を超える桁の乖離）。"
                         "列の取り違え・単位違いを疑う")


def check_outlier(rep: Report, series: dict, target: str, scan_all: bool) -> None:
    """変化率が 4σ 超（F2-5）。絶対値閾値は小型株で誤検知だらけになるため σ ベース。

    既定（`scan_all=False`）では σ を **走査対象を除いた過去**の変化率から推定する。
    走査対象を含めると、大きな異常ほど自分で σ を押し上げて自分を隠すため。
    `--scan-all` は全履歴を走査対象にするので σ の母集団と走査対象が一致する
    （＝上の自己隠蔽が起きうる）。実データ269点では leave-one-out σ と全期間 σ の
    差は小数第5位までゼロで検出数も同じだったが、点数が少ない系列では
    感度が落ちうることを承知して使うこと。
    """
    for code in sorted(series):
        pts = series[code]
        rets = [(pts[i][0], pts[i][1] / pts[i - 1][1] - 1)
                for i in range(1, len(pts)) if pts[i - 1][1]]
        if len(rets) < OUTLIER_MIN_POINTS:
            rep.warn("outlier", target,
                     f"{code}: 変化率が {len(rets)}点しかなく外れ値を検定できない"
                     f"（{OUTLIER_MIN_POINTS}点必要）")
            continue
        window = rets if scan_all else rets[-RECENT_BARS:]
        base = rets if scan_all else rets[:-RECENT_BARS]
        sigma = statistics.pstdev([x for _, x in base]) if len(base) >= 2 else 0.0
        if not sigma:
            rep.warn("outlier", target,
                     f"{code}: 変化率のばらつきが 0 で外れ値を検定できない"
                     "（値が動いていない可能性）")
            continue
        for date, x in window:
            if abs(x) > OUTLIER_SIGMA * sigma:
                rep.warn("outlier", target,
                         f"{code} {date}: 変化率 {x:+.1%}（{abs(x) / sigma:.1f}σ）")


# =============================================================================
# 6. 「値が動かない」壊れ方（件数を数える検査では感度がゼロになる領域）
# =============================================================================

def check_cross_code_identity(rep: Report, rows: list[dict], target: str) -> None:
    """別コードが同一日に OHLCV 完全一致 ＝ 同じページを取っている疑い。

    行数・件数の検査は「全銘柄が同じ値で埋まっている」に対して感度がゼロ。
    URL のフォーマット文字列が固定されている等の壊れ方はここでしか出ない。
    """
    by_date: dict[str, dict[str, tuple]] = {}
    for r in rows:
        vals = (_f(r.get("open")), _f(r.get("high")), _f(r.get("low")),
                ref_close(r), _f(r.get("volume")))
        if any(v is None for v in vals):
            continue
        by_date.setdefault(str(r.get("date")), {})[str(r.get("code"))] = vals

    pairs: dict[tuple[str, str], list[str]] = {}
    for date in sorted(by_date):
        m = by_date[date]
        codes = sorted(m)
        for i in range(len(codes)):
            for j in range(i + 1, len(codes)):
                if m[codes[i]] == m[codes[j]]:
                    pairs.setdefault((codes[i], codes[j]), []).append(date)

    for (a, b), dates in sorted(pairs.items()):
        head = " / ".join(dates[:SAMPLE_LIMIT])
        more = f" ほか{len(dates) - SAMPLE_LIMIT}日" if len(dates) > SAMPLE_LIMIT else ""
        msg = (f"{a} と {b} が OHLCV 完全一致: {len(dates)}日（{head}{more}）。"
               "同じページを取得している疑い")
        if len(dates) >= CROSS_CODE_FAIL_DAYS:
            rep.fail("cross_code", target, msg)
        else:
            rep.warn("cross_code", target, msg)


def check_frozen(rep: Report, series: dict, target: str) -> None:
    """同一終値が連続 ＝ 値が更新されていない（キャッシュ固着・同一行の複製）。"""
    for code in sorted(series):
        pts = series[code]
        run_start, best = 0, (0, "", "")
        for i in range(1, len(pts) + 1):
            if i < len(pts) and pts[i][1] == pts[run_start][1]:
                continue
            length = i - run_start
            if length > best[0]:
                best = (length, pts[run_start][0], pts[i - 1][0])
            run_start = i
        if best[0] >= FROZEN_MIN_RUN:
            rep.warn("frozen", target,
                     f"{code}: 同一終値が {best[0]}営業日連続"
                     f"（{best[1]}〜{best[2]}）。値が更新されていない可能性")


# =============================================================================
# 7. 欠測・売買不成立の可視化（F2-6）
# =============================================================================

def check_no_trade(rep: Report, rows: list[dict], target: str) -> None:
    """出来高0（売買不成立）。**異常ではないが、隠すと流動性を読み違える**。

    NO_TRADE の日は始値・高値・安値が存在せず、終値欄には気配値が入る。
    fetch.py は OHLC を終値で揃えて記録するため、値だけ見ると通常の同値足と
    区別できない。ここで台帳に出さないと「薄商い」が数字から消える。
    """
    by_code: dict[str, list[str]] = {}
    inconsistent_zero, inconsistent_nt = [], []
    for r in rows:
        status = str(r.get("status") or "")
        flags = [p for p in status.split("|") if p]
        vol = _f(r.get("volume"))
        key = f"{r.get('code')} {r.get('date')}"
        if "NO_TRADE" in flags:
            by_code.setdefault(str(r.get("code")), []).append(str(r.get("date")))
            if vol is None or vol != 0:
                inconsistent_nt.append(f"{key}（volume={r.get('volume')!r}）")
        elif vol == 0:
            inconsistent_zero.append(f"{key}（status={status}）")

    for code in sorted(by_code):
        dates = by_code[code]
        head = " / ".join(dates[:SAMPLE_LIMIT])
        more = f" ほか{len(dates) - SAMPLE_LIMIT}日" if len(dates) > SAMPLE_LIMIT else ""
        rep.warn("no_trade", target,
                 f"{code}: 出来高0（売買不成立）が {len(dates)}日 — {head}{more}。"
                 "始値・高値・安値は終値で代替されている")

    rep.group(FAIL, "no_trade", target,
              "status=NO_TRADE だが出来高が0でない", inconsistent_nt)
    rep.group(WARN, "no_trade", target,
              "出来高0だが status が NO_TRADE でない", inconsistent_zero)


def check_missing(rep: Report, rows: list[dict], target: str) -> None:
    """FETCH_FAILED / MISMATCH / SINGLE_SOURCE を台帳冒頭に可視化する（F2-6）。"""
    agg: dict[tuple[str, str], list[str]] = {}
    for r in rows:
        flags = [p for p in str(r.get("status") or "").split("|") if p]
        for status in flags:
            if status in MISSING_STATUSES:
                agg.setdefault((str(r.get("code")), status), []).append(
                    str(r.get("date")))

    for (code, status), dates in sorted(agg.items()):
        dates.sort()
        note = {
            "FETCH_FAILED": "取得失敗（推定値で埋めない）",
            "MISMATCH": "2ソース不一致（判定は前週値を据え置き）",
            "SINGLE_SOURCE": "1ソースのみ（照合不成立・close は空のまま）",
        }[status]
        rep.warn("missing", target,
                 f"{code} {status}: {len(dates)}日（{dates[0]}〜{dates[-1]}）— {note}")

    # 不一致は件数だけでなく実値を出す。列の取り違えか調整後終値の混入かを見分けるため。
    detail = [f"{r.get('code')} {r.get('date')}: "
              f"{r.get('source_primary')}={r.get('value_primary')} vs "
              f"{r.get('source_secondary')}={r.get('value_secondary')}"
              for r in rows
              if "MISMATCH" in str(r.get("status") or "").split("|")]
    rep.group(WARN, "missing", target, "不一致の実値", detail)

    # 出来高の2ソース不一致（fetch.py が VOLUME_MISMATCH を立てた行）。
    # 出来高は close と違って照合を通らないまま採用される唯一の数値であり、
    # かつ流動性ゲート（判定の最上位）の唯一の入力なので、必ず表に出す。
    vol_bad = [f"{r.get('code')} {r.get('date')}（volume={r.get('volume')}）"
               for r in rows
               if "VOLUME_MISMATCH" in str(r.get("status") or "").split("|")]
    rep.group(WARN, "missing", target,
              "出来高が2ソースで食い違う（列の取り違え・単位違いの疑い）", vol_bad)


def check_freshness(rep: Report, rows: list[dict], target: str) -> None:
    """**直近の取得実行が持ち帰った最新営業日**が、その実行日から離れていないか。

    壁時計を使わず CSV 内の値だけで見る（同じ入力なら同じ出力になる）。
    見ているのは「取得は動いたのに、返ってきたのが古い日付だった」壊れ方
    （キャッシュされたページ・日付列の誤パース・上場廃止後の据え置き）。

    「取得が動いたが1行も増えなかった」壊れ方はここでは見えない。
    それはベースライン比較（check_append_only の「追記が0件」）の担当。
    どちらか片方だけでは穴が残るので、両方を持つ。
    """
    if not rows:
        return
    fetch_days = [str(r.get("fetched_at") or "")[:10] for r in rows
                  if not _blank(r.get("fetched_at"))]
    if not fetch_days:
        return
    last_fetch_day = max(fetch_days)
    if _d(last_fetch_day) is None:
        rep.warn("freshness", target,
                 f"fetched_at を日付として読めない（{last_fetch_day!r}）")
        return
    batch = [str(r.get("date")) for r in rows
             if str(r.get("fetched_at") or "")[:10] == last_fetch_day]
    gap = _fetch_gap(rows)
    if gap is not None and gap > STALE_FETCH_DAYS:
        rep.warn("freshness", target,
                 f"直近の取得実行 {last_fetch_day} が持ち帰った最新営業日は {max(batch)} で "
                 f"{gap}日前（{STALE_FETCH_DAYS}日超）。"
                 "古いページを取得している可能性")


# =============================================================================
# 8. 信用残高
# =============================================================================

def check_margin(rep: Report, data_dir: Path, master: dict,
                 price_latest: str | None) -> None:
    """data/margin/{code}.csv。無い銘柄は「取れていない」ことを WARN で出す。

    judge は信用倍率が unknown なら「調査」で止める（過熱していない、とは読まない）。
    止まる理由が台帳から見えるように、ここで欠測を明示する。
    """
    margin_dir = data_dir / "margin"
    known = {str(s["code"]) for s in master.get("stocks", [])}
    files = {p.stem: p for p in sorted(margin_dir.glob("*.csv"))} if margin_dir.exists() else {}

    for code in sorted(known):
        if code not in files:
            rep.warn("margin", f"data/margin/{code}.csv",
                     f"{code}: 信用残高が取得できていない"
                     "（信用倍率は unknown → judge は「調査」で止まる）")

    for code, path in sorted(files.items()):
        target = f"data/margin/{code}.csv"
        if code not in known:
            rep.fail("margin", target, f"マスタ未登録のコード: {code}（取り違えの疑い）")
        rows = load_csv(path)
        if not rows:
            rep.warn("margin", target, "行が1つも無い")
            continue
        missing_cols = [c for c in MARGIN_FIELDS if c not in rows[0]]
        if missing_cols:
            rep.fail("margin", target, f"必須列が欠落: {missing_cols}")
            continue

        check_duplicate(rep, rows, target, ("code", "date"))

        bad_date, bad_status, bad_code, no_src, neg = [], [], [], [], []
        ratio_na_wrong, unit_wrong, recalc = [], [], []
        units: set[str] = set()
        for r in rows:
            key = f"{code} {r.get('date')}"
            if _d(r.get("date")) is None:
                bad_date.append(f"{key}（date={r.get('date')!r}）")
            flags = [p for p in str(r.get("status") or "").split("|") if p]
            if not flags or any(p not in MARGIN_STATUSES for p in flags):
                bad_status.append(f"{key}（status={r.get('status')!r}）")
            if str(r.get("code")) != code:
                bad_code.append(f"{key}（code={r.get('code')!r} / ファイル名 {code}）")
            if _blank(r.get("source_url")) or _blank(r.get("fetched_at")):
                no_src.append(key)

            long_b, short_b = _f(r.get("long_balance")), _f(r.get("short_balance"))
            ratio = _f(r.get("ratio"))
            for name, v in (("long_balance", long_b), ("short_balance", short_b)):
                if v is not None and v < 0:
                    neg.append(f"{key} {name}={v}")
            # 状態と値の対応。status を見ずに値だけ読むと「倍率が無い」を
            # 「過熱していない」と読み替える事故が起きる（sources.yaml の注記）。
            if (ratio is None) != ("RATIO_NA" in flags):
                ratio_na_wrong.append(f"{key}（ratio={r.get('ratio')!r} / status={flags}）")
            if _blank(r.get("unit")) != ("UNIT_UNKNOWN" in flags):
                unit_wrong.append(f"{key}（unit={r.get('unit')!r} / status={flags}）")
            if not _blank(r.get("unit")):
                units.add(str(r.get("unit")).strip())
            # 倍率を独立に再計算して突き合わせる（列の取り違えの検出）。
            # ★真偽値ではなく `is not None` で判定する。買い残 0 は falsy なので、
            #   `if ratio and long_b and ...` だと「買残0・売残100 なのに倍率5.0」
            #   のような明白な矛盾を検査ごとスキップしてしまう。
            if (ratio is not None and ratio != 0
                    and short_b is not None and long_b is not None
                    and short_b >= MARGIN_RATIO_CHECK_MIN_SHORT
                    and "RATIO_INCONSISTENT" not in flags):
                calc = long_b / short_b
                if abs(calc - ratio) / ratio > MARGIN_RATIO_TOLERANCE:
                    recalc.append(f"{key}（表示 {ratio} / 買残÷売残 {calc:.2f}）")

        g = rep.group
        g(FAIL, "margin", target, "date が読めない", bad_date)
        g(FAIL, "margin", target, "status が定義外", bad_status)
        g(FAIL, "margin", target, "code がファイル名と一致しない", bad_code)
        g(FAIL, "margin", target, "source_url または fetched_at が空", no_src)
        g(FAIL, "margin", target, "残高が負", neg)
        g(FAIL, "margin", target, "ratio の有無と RATIO_NA が対応していない", ratio_na_wrong)
        g(FAIL, "margin", target, "unit の有無と UNIT_UNKNOWN が対応していない", unit_wrong)
        g(FAIL, "margin", target,
          "表示倍率と 買残÷売残 が乖離しているのに RATIO_INCONSISTENT が立っていない",
          recalc)

        if len(units) > 1:
            rep.warn("margin", target,
                     f"単位が途中で変わっている: {sorted(units)}。"
                     "残高の時系列比較が成立しないため、解釈を人間が確認する")
        flagged = sorted({p for r in rows
                          for p in str(r.get("status") or "").split("|")
                          if p and p != "OK"})
        if flagged:
            rep.warn("margin", target,
                     f"要注意 status: {flagged}（RATIO_NA は「過熱していない」ではない）")

        dates = sorted(str(r.get("date")) for r in rows)
        age = _days(dates[-1], price_latest)
        if age is not None and age > MARGIN_MAX_AGE_DAYS:
            rep.warn("margin", target,
                     f"最新の信用残が {dates[-1]}・株価の最新営業日 {price_latest} から "
                     f"{age}日前（{MARGIN_MAX_AGE_DAYS}日超）。"
                     "judge は古い残高を unknown として「調査」で止める")


# =============================================================================
# 9. 指数
# =============================================================================

def check_indices(rep: Report, data_dir: Path, sources: dict,
                  price_dates: set[str], price_latest: str | None,
                  scan_all: bool = False) -> None:
    """data/indices/{id}.csv。相対パフォーマンス（F9・I-17）の分母。

    `growth250` は第2ソースが無く全行 SINGLE_SOURCE（`close` が全行空）。
    これは既知の仕様であり、close だけを見る検査では丸ごと無検査になる。
    整合性検査は value_primary を参照値に使う（冒頭「参照値の定義」）。
    """
    idx_dir = data_dir / "indices"
    targets = {str(t["id"]): t for t in (sources.get("index") or {}).get("targets", [])}
    files = {p.stem: p for p in sorted(idx_dir.glob("*.csv"))} if idx_dir.exists() else {}

    for iid in sorted(targets):
        if iid not in files:
            rep.warn("index", f"data/indices/{iid}.csv",
                     f"{iid}: 指数データが無い（相対パフォーマンスを算出できない）")

    for iid, path in sorted(files.items()):
        target = f"data/indices/{iid}.csv"
        if iid not in targets:
            rep.warn("index", target,
                     f"{iid}: sources.yaml の index.targets に定義が無い")
        rows = load_csv(path)
        if not rows:
            rep.warn("index", target, "行が1つも無い")
            continue

        check_ohlcv_schema(rep, rows, target)
        check_duplicate(rep, rows, target, ("code", "date"))
        check_ohlc(rep, rows, target)
        check_missing(rep, rows, target)
        series = build_series(rows)
        check_split(rep, series, target, load_corporate_actions(data_dir), scan_all)
        # 指数の 4σ は多くが実際の相場変動だが、小数点の桁ずれ・別指数の混入も
        # ここにしか現れない（比率が整数比でないため check_split では拾えない）。
        check_outlier(rep, series, target, scan_all)
        check_frozen(rep, series, target)

        bad_code = sorted({str(r.get("code")) for r in rows if str(r.get("code")) != iid})
        for c in bad_code:
            rep.fail("index", target,
                     f"code 列が指数ID と一致しない: {c}（ファイル名 {iid}）")

        dates = {str(r.get("date")) for r in rows}
        if price_latest and price_latest not in dates:
            rep.warn("index", target,
                     f"株価の最新営業日 {price_latest} の行が無い（取得漏れ）")
        gaps = sorted(d for d in price_dates if d not in dates and d <= max(dates))
        if gaps:
            head = " / ".join(gaps[:SAMPLE_LIMIT])
            more = f" ほか{len(gaps) - SAMPLE_LIMIT}日" if len(gaps) > SAMPLE_LIMIT else ""
            rep.warn("index", target,
                     f"株価にあって指数に無い営業日 {len(gaps)}日（{head}{more}）。"
                     "その日の相対パフォーマンスは算出できない")
        # 参照値が1つも取れない指数は、行があっても使えない
        usable = sum(1 for r in rows if ref_close(r) is not None)
        if usable == 0:
            rep.fail("index", target, "参照できる終値が1行も無い（close も value_primary も空）")


# =============================================================================
# 10. 決算 KPI
# =============================================================================

def check_kpi(rep: Report, data_dir: Path, master: dict) -> None:
    """data/kpi/{code}.csv。スキーマの正は .claude/skills/kabu-ledger/SKILL.md。

    ここは **LLM が書く唯一のデータファイル**に対する検査であり、
    「比率を書かせない」「推測を隠させない」「出典を伴わせる」の防波堤を兼ねる。
    ファイルが無い場合は何も言わない（未整備であることは judge が unknowns で出す）。
    """
    kpi_dir = data_dir / "kpi"
    if not kpi_dir.exists():
        return
    known = {str(s["code"]) for s in master.get("stocks", [])}

    for path in sorted(kpi_dir.glob("*.csv")):
        code_file = path.stem
        target = f"data/kpi/{code_file}.csv"
        rows = load_csv(path)
        if not rows:
            rep.warn("kpi", target, "行が1つも無い")
            continue
        missing_cols = [c for c in KPI_FIELDS if c not in rows[0]]
        if missing_cols:
            rep.fail("kpi", target,
                     f"必須列が欠落: {missing_cols}（SKILL.md のスキーマが正）")
            continue

        check_duplicate(rep, rows, target, ("code", "date", "metric"))

        bad_date, bad_code, bad_metric, derived = [], [], [], []
        bad_value, bad_unit, no_def, bad_assumed = [], [], [], []
        no_src, no_basis, oversize, negative, assumed_rows = [], [], [], [], []
        for r in rows:
            metric = str(r.get("metric") or "").strip()
            key = f"{r.get('date')} {metric}"
            if _d(r.get("date")) is None:
                bad_date.append(f"{key}（date={r.get('date')!r}）")
            if str(r.get("code")) != code_file or str(r.get("code")) not in known:
                bad_code.append(f"{key}（code={r.get('code')!r}）")
            if metric in KPI_DERIVED_METRICS:
                derived.append(f"{key}")
            elif metric not in KPI_METRICS and not KPI_SEGMENT_RE.match(metric):
                bad_metric.append(f"{key}（metric={metric!r}）")

            value = _f(r.get("value"))
            if value is None:
                bad_value.append(f"{key}（value={r.get('value')!r}）")
            else:
                if abs(value) > KPI_VALUE_ABS_MAX:
                    oversize.append(f"{key}（value={value}）")
                if metric.startswith(KPI_NON_NEGATIVE_PREFIX) and value < 0:
                    negative.append(f"{key}（value={value}）")
            if str(r.get("unit") or "").strip() not in KPI_UNITS:
                bad_unit.append(f"{key}（unit={r.get('unit')!r}）")
            definition = str(r.get("definition") or "").strip()
            if not definition:
                no_def.append(key)

            assumed = str(r.get("assumed") or "").strip()
            if assumed not in ("true", "false"):
                bad_assumed.append(f"{key}（assumed={r.get('assumed')!r}）")
            elif assumed == "true":
                assumed_rows.append(key)
                if "assumed:" not in definition:
                    no_basis.append(key)
            elif _blank(r.get("source_url")):
                no_src.append(key)
            if _blank(r.get("fetched_at")):
                no_src.append(key)

        g = rep.group
        g(FAIL, "kpi", target, "date が読めない", bad_date)
        g(FAIL, "kpi", target, "code がファイル名またはマスタと一致しない", bad_code)
        g(FAIL, "kpi", target,
          "比率が CSV に書かれている（比率はコードが計算する。F8-4）", derived)
        g(FAIL, "kpi", target, "metric が定義外（SKILL.md の固定語彙のみ）", bad_metric)
        g(FAIL, "kpi", target, "value が数値として読めない", bad_value)
        g(FAIL, "kpi", target, "unit が定義外", bad_unit)
        g(FAIL, "kpi", target, "definition が空（何の数字か特定できない行は無価値）", no_def)
        g(FAIL, "kpi", target, "assumed が true/false でない", bad_assumed)
        g(FAIL, "kpi", target,
          "assumed=true だが definition に根拠（assumed:）が無い（推測を隠さない）", no_basis)
        g(FAIL, "kpi", target,
          "source_url または fetched_at が空（数値は必ず出所を伴う）", no_src)
        g(FAIL, "kpi", target, "桁が大きすぎる（単位の取り違えの疑い）", oversize)
        g(FAIL, "kpi", target, "売上高系が負（符号の読み違えの疑い）", negative)
        g(WARN, "kpi", target, "推測で埋めた値（assumed=true）", assumed_rows)


# =============================================================================
# 実行
# =============================================================================

def run_checks(data_dir: Path, baseline: Baseline | None,
               scan_all: bool = False) -> Report:
    """全検査を実行して Report を返す。I/O はここと各 check の入口に閉じる。"""
    rep = Report()
    master = yaml.safe_load((data_dir / "master.yaml").read_text(encoding="utf-8"))
    sources = yaml.safe_load((data_dir / "sources.yaml").read_text(encoding="utf-8"))

    prices_path = data_dir / "prices" / "daily.csv"
    target = "data/prices/daily.csv"
    rows = load_csv(prices_path) if prices_path.exists() else []
    if not prices_path.exists():
        rep.fail("schema", target, "株価ファイルが存在しない")

    check_ohlcv_schema(rep, rows, target)
    check_duplicate(rep, rows, target, ("code", "date"))
    check_master(rep, rows, master, target)
    check_master_schema(rep, master)
    check_coverage(rep, rows, master, target)
    check_append_only(rep, data_dir, baseline)
    check_ohlc(rep, rows, target)

    series = build_series(rows)
    actions = load_corporate_actions(data_dir)
    check_split(rep, series, target, actions, scan_all)
    check_outlier(rep, series, target, scan_all)
    check_volume_jump(rep, series, target, scan_all)
    check_cross_code_identity(rep, rows, target)
    check_frozen(rep, series, target)
    check_no_trade(rep, rows, target)
    check_missing(rep, rows, target)
    check_freshness(rep, rows, target)

    price_dates = {str(r.get("date")) for r in rows}
    price_latest = max(price_dates) if price_dates else None
    check_margin(rep, data_dir, master, price_latest)
    check_indices(rep, data_dir, sources, price_dates, price_latest, scan_all)
    check_kpi(rep, data_dir, master)
    return rep


def resolve_baseline(data_dir: Path, baseline_dir: Path | None,
                     use_git: bool) -> Baseline | None:
    if baseline_dir is not None:
        return _dir_baseline(data_dir, baseline_dir)
    return _git_baseline(data_dir.parent) if use_git else None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="データ品質検査（FAIL でビルドを止める）")
    ap.add_argument("--data-dir", type=Path, default=ROOT / "data",
                    help="検査対象の data ディレクトリ（既定: <repo>/data）")
    ap.add_argument("--baseline", type=Path, default=None,
                    help="追記性の比較元にする「以前の data/ のコピー」")
    ap.add_argument("--no-git", action="store_true",
                    help="git HEAD をベースラインに使わない")
    ap.add_argument("--scan-all", action="store_true",
                    help="分割・外れ値を全履歴で走査する（初回一括取得の直後）")
    ap.add_argument("--json", action="store_true", help="機械可読出力")
    args = ap.parse_args(argv)

    data_dir = args.data_dir.resolve()
    baseline = resolve_baseline(data_dir, args.baseline, not args.no_git)
    rep = run_checks(data_dir, baseline, args.scan_all)

    if args.json:
        print(json.dumps({
            "fail": rep.fails,
            "warn": rep.warns,
            "baseline": baseline.kind if baseline else None,
            "results": [{"level": r.level, "check": r.check,
                         "target": r.target, "message": r.message}
                        for r in rep.results],
        }, ensure_ascii=False, indent=2))
        return 1 if rep.fails else 0

    print(f"対象: {data_dir}")
    print(f"追記性のベースライン: {baseline.kind if baseline else 'なし'}")
    for r in rep.results:
        print(r.line())
    print(f"\nFAIL {rep.fails} / WARN {rep.warns}")
    return 1 if rep.fails else 0


if __name__ == "__main__":
    sys.exit(main())
