"""データ品質検査。すべてコードで判定し、LLM の自己レビューには依存しない。

要件: requirements.md F2 / 不変条件（append-only・欠測を埋めない）、
      review-findings.md F-02（フェイルセーフの向き）、decisions.md D7/D13/D16。

検査対象（存在するものだけを検査する。無いものは「検査した」ことにしない）:

    data/master.yaml          銘柄マスタ（**人間が手で書く唯一の判定入力**）
    data/prices/daily.csv     日足 OHLCV（append-only）
    data/indices/{id}.csv     指数 topix / growth250（append-only・列は daily.csv と同一）
    data/margin/{code}.csv    信用残高（append-only）
    data/kpi/{code}.csv       決算 KPI（append-only・スキーマは SKILL.md が正）
    data/fundamentals/{code}.csv  財務数値の2ソース照合結果（append-only）
    data/link_status.csv      出典URLの死活記録（append-only・`--check-links` のときだけ書く）
    reports/{code}.md         銘柄レポート（v2.0 の主役。**数値は人間が転記している**）

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
レポートの数値（2026-08-13 追加）
--------------------------------------------------------------------------

株価は2ソース照合＋本モジュールの検査で守られているのに、**レポートの財務数値は
人間がまとめサイトの表を目で読んで Markdown に転記しただけ**で、桁を取り違えても
誰も気づかなかった。株価と同じ規律を財務数値にも通すために3つ足している。

1. `check_fundamentals`   — 2ソース照合結果の妥当性（範囲・恒等式・桁・符号・D7）
2. `check_report_numbers` — **レポート本文の数値と採用値の突合**。食い違えば FAIL。
                            突合できなかったものは「未突合」として件数を必ず出す
3. `check_links`          — 出典URLの死活。ネットワークを使うので既定はオフ
                            （`--check-links`。外部要因で CI を止めない）

`data/fundamentals/{code}.csv` が無い場合、2 は「1件も機械照合されていない」と
WARN で言う。**黙って skip しない**（設計原則1。それが現状そのものだから）。

--------------------------------------------------------------------------
使い方
--------------------------------------------------------------------------

    python src/checks.py                      # data/ を検査（CI の既定）
    python src/checks.py --scan-all           # 分割・外れ値を全履歴で走査（初回一括取得の直後）
    python src/checks.py --baseline old_data  # append-only を明示ベースラインで検証
    python src/checks.py --json               # 機械可読出力（build.py の欠測表示用）
    python src/checks.py --data-dir tmp/data  # 別ディレクトリを検査（テスト用）
    python src/checks.py --check-links        # 出典URLに実際にアクセスして死活を見る
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import statistics
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import date as _date
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))

# YAML の読み込みは yamlio に集約する（libyaml があれば C 実装）。
# `import yaml` は残す: 例外型 yaml.YAMLError をこのファイルの3箇所で捕まえている。
import yamlio as Y  # noqa: E402

# holding.status の語彙は judge.py が正（SSoT）。ここで再定義しない。
# 閾値の再計算（MARGIN_RATIO_TOLERANCE 等）は「独立検算」なのであえて複製するが、
# **語彙は複製すると片方だけ増えて検査が素通りになる**ので参照する。
from judge import HOLDING_STATUSES  # noqa: E402

# 裏取り記録の語彙・読み方は verification.py が正（SSoT）。ここで再定義しない。
import verification as VF  # noqa: E402

# =============================================================================
# 定数ブロック（閾値・語彙はすべてここ。根拠を併記する）
# =============================================================================

FAIL, WARN = "FAIL", "WARN"

# --- 検査の並び（出力順を固定するための正。ここに無い check 名は末尾に落ちる） ---
CHECK_ORDER = (
    "schema", "duplicate", "master", "master_schema", "coverage", "append_only",
    "ohlc", "split", "outlier", "volume", "cross_code", "frozen", "no_trade",
    "missing", "freshness", "margin", "index", "kpi",
    "fundamentals", "tanshin", "report", "verify", "links", "stamps",
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

# --- 財務数値（data/fundamentals/{code}.csv） ---------------------------------
# 株価の `close` と同じ規律を財務数値に通す。**2ソース一致で採用した値だけ**が
# `value` に入り、一致しなければ `MISMATCH` として両値を残す（D7）。
#
# 抽出器は別コンテキストで実装されるため、列名は「正規名 + 別名」で解決する。
# **列名が1つ違うだけで検査が丸ごと無効化される**のが最悪の壊れ方なので、
# 解決できなければ「スキップ」ではなく WARN で表に出す（設計原則1）。
FUNDAMENTALS_FIELDS = ("period", "code", "metric", "value", "unit", "tolerance",
                       "status", "source_primary", "value_primary", "raw_primary",
                       "source_secondary", "value_secondary", "raw_secondary",
                       "sources_all", "source_url_primary", "source_url_secondary",
                       "fetched_at")
FUNDAMENTALS_COLUMN_ALIASES = {
    "period": ("period", "fiscal_period", "term", "fy", "date"),
    "code": ("code", "stock_code"),
    "metric": ("metric", "item", "name"),
    "value": ("value", "adopted", "adopted_value"),
    "unit": ("unit",),
    "tolerance": ("tolerance",),
    "status": ("status",),
    "source_primary": ("source_primary", "source_1", "source_a"),
    "value_primary": ("value_primary", "value_1", "value_a"),
    "source_secondary": ("source_secondary", "source_2", "source_b"),
    "value_secondary": ("value_secondary", "value_2", "value_b"),
    "sources_all": ("sources_all",),
    "source_url": ("source_url_primary", "source_url"),
    "fetched_at": ("fetched_at", "fetched", "retrieved_at"),
}
# 照合結果は株価と同じ語彙。付加フラグは語彙を持たない側（抽出器）が増やしうるので、
# **照合結果が1つも無い**ときだけ FAIL にし、知らない付加フラグは WARN で出す。
# ここを FAIL にすると、こちらが知らない正当なフラグ1つで毎週ビルドが止まる。
FUNDAMENTALS_RECONCILE_STATUSES = {"OK", "MISMATCH", "SINGLE_SOURCE", "FETCH_FAILED"}
# 付加フラグの正は `fetch_fundamentals.EXTRA_FLAGS`。**あえて import せず複製する。**
# import すると抽出器が requests / bs4 を持ち込み、checks.py がオフラインで
# 動かなくなる。複製した結果として語彙がずれても、ここでは
# 「知らない status フラグ」WARN として**必ず表に出る**（黙って通らない）ので、
# ずれの検出そのものがこの複製の役目になっている。
FUNDAMENTALS_EXTRA_STATUSES = {
    "ROUNDING",           # 完全一致ではなく表示解像度の範囲で一致
    "UNIT_CONVERTED",     # ソース間で単位が違い、正規化してから照合
    "UNIT_UNCONFIRMED",   # ページ側の単位注記を確認できなかった
    "NONCONSOLIDATED",    # 非連結（単独）決算の期
    "US_GAAP", "IFRS",    # 日本基準以外の期
    "PERIOD_CHANGED",     # 決算期変更のあった期
    "PERIOD_ASTERISK",    # 連結と非連結が混在表記の期
    "ASSUMED",            # 推測で埋めた（D17。根拠の併記が必要）
}
# 抽出器は「26.4億」と「2,638百万」のように**表示解像度が違う**値を、解像度の
# 範囲で一致していれば OK として採用し `ROUNDING` を付ける。採用値は参加した
# 全観測のうち**最も解像度が細かいもの**なので、主・副の列に出ている値とは
# 限らない（第3のソースのことがある）。したがって「OK なら採用値は主副の両方と
# 一致する」は ROUNDING 行には適用できない。代わりに:
#   (1) 採用値は `sources_all` に並ぶ観測値のどれかでなければならない（捏造の防止）
#   (2) 採用値と各観測値の差は、行に記録された `tolerance` 以内でなければならない
#   (3) **その `tolerance` 自体が大きすぎないこと**
# (3) が要。解像度の計算が壊れて許容幅が膨らむと、(2) は何でも通してしまう
# （＝検査が素通りする壊れ方）。解像度の計算を写経して検算するのではなく、
# 「許容幅が採用値の10%を超えたらその行は事実上ノーチェック」と外から縛る。
FUNDAMENTALS_ROUNDING_FLAG = "ROUNDING"
FUND_ROUNDING_MAX_REL = 0.10
FUND_FLOAT_EPS = 1e-9

# metric の正規名 -> 表記ゆれ。CSV 側（英名）とレポート側（日本語）の両方を吸収する。
# **長い別名が勝つ**（「営業利益率」は operating_income ではなく operating_margin_pct）。
# 3文字以下の ASCII 別名（op / roe / eps 等）は部分一致させず、トークン一致のみ。
FUND_METRIC_ALIASES = {
    # 株価の採用終値。**data/prices/daily.csv の close（2ソース一致）と突き合わせる。**
    # 語彙に無かったため、本文が「すべて2つの取得元で一致した終値である」と
    # 断定している6件が「metric を対応づけられない」＝未突合のままだった。
    "close": ("close", "終値", "株価"),
    "revenue": ("revenue", "sales", "net_sales", "売上高", "売上収益", "営業収益", "売上"),
    "cost_of_sales": ("cost_of_sales", "cogs", "売上原価"),
    "gross_profit": ("gross_profit", "売上総利益", "粗利益", "粗利"),
    "sga": ("sga", "sganda", "販売費及び一般管理費", "販管費"),
    "operating_income": ("operating_income", "operating_profit", "op",
                         "営業利益", "営業益"),
    "ordinary_income": ("ordinary_income", "ordinary_profit", "経常利益", "経常益"),
    "net_income": ("net_income", "net_profit", "当期純利益", "最終利益", "最終益",
                   "純利益"),
    "eps": ("eps", "1株益", "一株当たり当期純利益"),
    "equity": ("equity", "自己資本"),
    # 自己資本（equity）とは別の勘定科目。自己資本 = 株主資本 + その他の包括利益
    # 累計額。IR BANK の BS 表はこちらしか持たないので、equity と同一視すると
    # **別々の科目どうしを照合してしまう**（sources.yaml の irbank_bs の note）。
    "shareholders_equity": ("shareholders_equity", "株主資本"),
    "net_assets": ("net_assets", "純資産"),
    "total_assets": ("total_assets", "総資産"),
    "retained_earnings": ("retained_earnings", "利益剰余金", "剰余金"),
    "operating_cf": ("operating_cf", "営業キャッシュフロー",
                     "営業活動によるキャッシュフロー"),
    "operating_margin_pct": ("operating_margin_pct", "operating_margin",
                             "op_margin", "営業利益率", "営利率"),
    "gross_margin_pct": ("gross_margin_pct", "gross_margin", "売上総利益率", "粗利率"),
    "cost_ratio_pct": ("cost_ratio_pct", "cost_ratio", "cost_of_sales_ratio",
                       "売上原価率", "原価率"),
    "sga_ratio_pct": ("sga_ratio_pct", "sga_ratio", "販管費率"),
    "equity_ratio_pct": ("equity_ratio_pct", "equity_ratio", "自己資本比率"),
    "roe_pct": ("roe_pct", "roe", "ROE"),
    "roa_pct": ("roa_pct", "roa", "ROA"),
    # 以下は `data/sources.yaml` の fundamentals が実際に出す metric 名。
    # 語彙に無い metric は範囲・恒等式の対象外になり、突合もされないので、
    # 抽出側が出すものはここに揃えておく（揃っていなければ WARN で出る）。
    "bps": ("bps", "1株純資産"),
    "capex": ("capex", "設備投資"),
    "cash_equivalents": ("cash_equivalents", "現金同等物", "現金及び現金同等物"),
    "investing_cf": ("investing_cf", "投資キャッシュフロー"),
    "financing_cf": ("financing_cf", "財務キャッシュフロー"),
    "free_cf": ("free_cf", "フリーキャッシュフロー"),
    "interest_bearing_debt": ("interest_bearing_debt", "有利子負債"),
    "interest_bearing_debt_ratio": ("interest_bearing_debt_ratio",
                                    "有利子負債倍率", "有利子負債比率"),
}
# 0〜100% を外れたら抽出ミス（比率の定義上ありえない）。
# **営業利益率・ROE・ROA は入れない**。赤字なら負になるし、自己資本が薄い会社では
# ROE が100%を超える。ここに入れると毎週 FAIL する。
FUND_RATIO_0_100_METRICS = ("cost_ratio_pct", "sga_ratio_pct",
                            "equity_ratio_pct", "gross_margin_pct")
# 0〜100% を外れること自体は実在する（赤字期の販管費率は100%を超える。
# 実データ 4937 FY2019-09 の販管費率は 105.12%）。**FAIL にするのは
# 明らかな抽出ミスの水準だけ**にし、0〜100% の外は WARN で表に出す。
# ここを 100% で切ると「採用値を増やす」という目標がビルド停止を招く。
FUND_RATIO_HARD_MIN = -100.0
FUND_RATIO_HARD_MAX = 200.0
# 売上 − 売上原価 − 販管費 ≒ 営業利益 の許容（売上に対する比率）。
# 分母を営業利益にすると、営業利益がゼロ近傍の期に発散して毎週 FAIL する。
FUND_IDENTITY_TOLERANCE = 0.01
# 比率での同じ恒等式（原価率 + 販管費率 + 営業利益率 = 100%）の許容。
# 各サイトが丸めた比率を足すので、表示桁ぶんの丸めに加えて 0.15pt の余裕を持つ。
# 実データ4銘柄では乖離0.000pt だった（緩すぎない値）。
FUND_RATIO_IDENTITY_SLACK_PT = 0.15
# 前期比で桁が飛んだら桁の取り違えを疑う。**利益・EPS・比率には適用しない**
# （小型株の利益は 59 → 386 のように実際に10倍動く。適用すると誤検知が常態化する）。
FUND_SCALE_METRICS = ("revenue", "cost_of_sales", "sga", "total_assets",
                      "equity", "shareholders_equity", "net_assets",
                      "gross_profit")
FUND_SCALE_JUMP_RATIO = 10.0
# 財務数値の鮮度。決算は年4回しか動かないが、**取得そのもの**は毎週走る。
# 株価の最新営業日から離れていたら取得が止まっている（週次 + 余裕）。
FUND_STALE_DAYS = 21
# 銘柄取り違えの検出。別コードの (期, metric, 値) が広範囲に一致するのは、
# 同じページを取っているか code を書き間違えている（実データでの発生は0件）。
FUND_CROSS_CODE_MIN = 20
FUND_CROSS_CODE_RATIO = 0.8

# 単位 -> (族, 円などの基本単位への係数)。族が違う値は比較しない（未突合として数える）。
UNIT_SCALES = {
    "jpy": ("money", 1.0), "円": ("money", 1.0), "yen": ("money", 1.0),
    "jpy_thousand": ("money", 1e3), "千円": ("money", 1e3),
    "jpy_million": ("money", 1e6), "百万円": ("money", 1e6),
    "jpy_billion": ("money", 1e9),
    "億円": ("money", 1e8), "兆円": ("money", 1e12),
    "pct": ("pct", 1.0), "%": ("pct", 1.0), "％": ("pct", 1.0),
    # 倍率と百分率は同じ量の別表記（1倍 = 100%）。同じ族に入れて換算する。
    # 有利子負債倍率は kabutan が「倍」、IR BANK が「%」で出すため、
    # 族を分けると同じ数字が「単位を揃えられない」で未突合になる。
    "x": ("pct", 100.0), "倍": ("pct", 100.0),
    "shares": ("shares", 1.0), "株": ("shares", 1.0),
}
# 単位が書かれていない比率 metric の既定単位。比率に「%」を書き忘れた表を
# 単位不明として捨てると、いちばん転記ミスが起きる列が無検査になる。
FUND_DEFAULT_PCT_METRICS = FUND_RATIO_0_100_METRICS + (
    "operating_margin_pct", "roe_pct", "roa_pct")

# レポートの数値と採用値を突き合わせるときの許容。
# 基本は**表示桁からの丸め幅**（「26.4億円」なら ±0.05億円）で、これに出典側の
# 丸め差ぶんの相対スラックを足す。
#
# **この相対スラックは表示桁の許容を上書きするので、値が大きいほど検出力が落ちる。**
# 実測: 6570 の 2026/3期 売上高 20,729 → 20,792（下2桁の入れ替え）は
# 差 63 に対して 0.005 × 20,729 = 103.6 が勝ち、**FAIL にならない**
# （20,729 → 22,729 まで動かせば FAIL する）。桁違いは値の大小によらず捕まえるが、
# 「0.5% は数字の入れ替えを見逃さない水準」は**万単位の値では成立していない**。
#
# そこで「表示桁の丸めでは説明できないが、相対スラックには収まる」帯を
# WARN で表に出す（黙って通さない・設計原則1）。実データ4銘柄ではこの帯は0件なので、
# 相対スラックを外して FAIL に上げても現状はビルドを止めない。上げるかどうかは
# 閾値の妥当性の判断そのものなので人間に委ねる（`wide_tol` を WARN に留めたのと同じ扱い）。
REPORT_VALUE_REL_SLACK = 0.005

# --- 出典URLの死活監視 ---------------------------------------------------------
LINK_STATUS_FIELDS = ("checked_at", "code", "url", "http_status", "reachable", "note")
# 404/410 は「消えている」。403/429 は「拒否された」であってリンク切れではない。
LINK_DEAD_CODES = (404, 410)
LINK_TIMEOUT_SEC = 15
LINK_INTERVAL_SEC = 1.0

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
        if not p.startswith(f"{prefix}data/"):
            continue
        # 裏取り記録（YAML）も append-only の対象。CSV だけを見ていたため、
        # 過去 run の verdict を書き換えても検出できなかった（不変条件の穴）。
        if p.endswith(".csv") or p.startswith(f"{prefix}data/verification/"):
            files.add(p[len(prefix):])

    def read(rel: str) -> str | None:
        r = _git(["git", "show", f"HEAD:{prefix}{rel}"], toplevel)
        return r.stdout if r.returncode == 0 else None

    return Baseline("git HEAD", files, read)


def _dir_baseline(data_dir: Path, baseline_dir: Path) -> Baseline:
    """`--baseline` で渡した「以前の data/ のコピー」をベースラインにする。"""
    files = {f"data/{p.relative_to(baseline_dir).as_posix()}"
             for p in sorted(baseline_dir.rglob("*.csv"))}
    files |= {f"data/{p.relative_to(baseline_dir).as_posix()}"
              for p in sorted((baseline_dir / "verification").glob("*.yaml"))}

    def read(rel: str) -> str | None:
        p = baseline_dir / Path(rel).relative_to("data")
        return p.read_text(encoding="utf-8-sig") if p.exists() else None

    return Baseline(f"ディレクトリ {baseline_dir}", files, read)


# 期・日付を表す列の別名。**ヘッダに実在するものだけをキーにする。**
# ここに無い名前しか持たないファイルは「鍵を決められない」として WARN で出す
# （鍵が潰れると「追記しただけ」が「過去行が変更されている」に化ける）。
#
# `fetched_at` は**最後**に置く。期を表す列（date / period / disclosed_on）が
# あるファイルではそちらが鍵であり、取得時刻を鍵にすると同じ日を撮り直した行が
# 別行として通ってしまう。逆に**取得時刻しか持たないファイル**——
# `data/verification/fetch_log.csv`（列は fetched_at / code / url / …）——では
# これが無いと鍵が (code, url) に落ち、**同じURLを2回叩いただけで
# 「過去行が変更されている」FAIL になる**（D51 と同じ壊れ方。裏取りは同じ銘柄の
# 同じページを週をまたいで何度も叩くので、必ず起きる）。
KEY_DATE_ALIASES = ("date", "period", "disclosed_on", "checked_at", "as_of",
                    "fiscal_period", "term", "fy", "fetched_at")


def _key_columns(rel: str, cols: list[str] | None = None) -> tuple[str, ...]:
    """そのファイルの append-only 一意キー。

    **実際のヘッダを見て解決する。** 存在しない列名を並べると全行のキーが
    同じ値に潰れ、「追記しただけ」が「過去行が変更されている」として大量の
    FAIL になる（`data/tanshin/fetch_log.csv` を追加した週に実際に起きた。
    このファイルは `date` も `metric` も持たず、全行が (code, "") に潰れていた）。
    ディレクトリ名で分岐すると新しい置き場が増えるたびに壊れるので、
    **ヘッダに実在する列だけで鍵を組み立てる**。1列も当たらなければ空を返し、
    呼び出し側が「追記性を検証できていない」と WARN する（設計原則1）。
    """
    have = [str(c) for c in (cols or [])]
    if not have:
        return ("code", "date")
    if rel.endswith("link_status.csv") or ("url" in have and "checked_at" in have):
        return tuple(c for c in ("url", "checked_at") if c in have)
    day = next((c for c in KEY_DATE_ALIASES if c in have), None)
    keys = [c for c in ("code",) if c in have]
    if day:
        keys.append(day)
    if "metric" in have:
        keys.append("metric")
    # 決算短信の取得ログのように metric も date も無いファイルは、
    # 行を一意にする列（PDF の URL）を鍵に加える。
    if not day and "metric" not in have:
        for extra in ("pdf_url", "url", "source_url"):
            if extra in have:
                keys.append(extra)
                break
    return tuple(keys)


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


# --- 訂正（照合不成立 → 成立）の台帳 -------------------------------------------
#
# append-only は「取れなかったことを後から無かったことにしない」ための規律だが、
# **鍵が期や日付で不変なファイルでは、1回の取得失敗がその行の検証状態を恒久的に
# 固定してしまう**（財務は period が二度と新しくならない。株価も (code,date) は
# 一度きり）。翌週に2ソースが揃っても採用値は空のままで、直す経路が無かった。
#
# そこで「照合不成立 → 成立」の一方向だけ訂正を許し、**訂正したという事実を
# 別の追記専用ファイルに必ず残す**。記録の無い書き換えは従来どおり FAIL。
# 採用値を下げる方向（OK → 空、値の書き換え）は記録があっても FAIL のまま。
REVISIONS_FILE = "data/revisions.csv"
REVISION_FIELDS = ("revised_at", "file", "key", "column", "old_value",
                   "new_value", "kind", "reason")
# ファイル種別ごとの「採用値」の列。訂正はこの列の状態変化として定義する。
ADOPTED_COLUMNS = ("close", "value")
# 訂正の向きは2つだけ。どちらも理由の記載が必須。
#   repair    照合不成立 → 成立。採用値が **空 → 埋まる**（取得の再試行で揃った）
#   withdraw  照合が無効と判明 → 採用の取り下げ。採用値が **埋まる → 空**
#             （例: 別々の勘定科目どうしを突き合わせていたと分かった場合）
# 「採用値を別の値に書き換える」は**どちらでもない**。記録があっても FAIL のまま。
REVISION_KINDS = ("repair", "withdraw")


def load_revisions(data_dir: Path) -> dict[tuple[str, str], list[dict]]:
    """`data/revisions.csv` を (file, key) ごとにまとめて返す。"""
    path = data_dir / "revisions.csv"
    if not path.exists():
        return {}
    out: dict[tuple[str, str], list[dict]] = {}
    for r in load_csv(path):
        key = (str(r.get("file") or ""), str(r.get("key") or ""))
        out.setdefault(key, []).append(r)
    return out


def _adopted_column(header: list[str]) -> str | None:
    for c in ADOPTED_COLUMNS:
        if c in header:
            return c
    return None


def _recorded_revision(old: dict, new: dict, header: list[str],
                       records: list[dict], diffs: list[tuple[str, str, str]]
                       ) -> str | None:
    """訂正として認めてよい変更なら「向き＋理由」を返す。認められないなら None。

    条件（すべて満たすこと）:
      1. `data/revisions.csv` にこの行の記録がある（理由つき・向きが語彙内）
      2. 採用値の列が **空 ⇄ 非空** に変わっている（値の書き換えは認めない）
      3. 向きと status が整合している（repair なら OK が入り、withdraw なら消える）
      4. 記録に無い列の書き換えが混ざっていない
    """
    if not records:
        return None
    col = _adopted_column(header)
    if col is None or col not in {c for c, _, _ in diffs}:
        return None
    was, now = _blank(old.get(col)), _blank(new.get(col))
    flags = [p for p in str(new.get("status") or "").split("|") if p]
    if was and not now:
        want = "repair"
        if "OK" not in flags:
            return None
    elif not was and now:
        want = "withdraw"
        if "OK" in flags:
            return None
    else:
        return None
    kinds = {str(r.get("kind") or "").strip() for r in records}
    if kinds != {want}:
        return None
    recorded = {str(r.get("column") or "") for r in records}
    if sorted(c for c, _, _ in diffs if c not in recorded):
        return None
    reasons = sorted({str(r.get("reason") or "") for r in records if r.get("reason")})
    if not reasons:
        return None
    return want + ": " + "／".join(reasons)


def check_revisions(rep: Report, data_dir: Path) -> None:
    """訂正台帳そのものの検査。**記録だけあって実体が無い**状態を許さない。"""
    path = data_dir / "revisions.csv"
    if not path.exists():
        return
    rows = load_csv(path)
    target = REVISIONS_FILE
    if not rows:
        rep.warn("append_only", target, "行が1つも無い")
        return
    missing = [c for c in REVISION_FIELDS if c not in rows[0]]
    if missing:
        rep.fail("append_only", target, f"必須列が欠落: {missing}")
        return
    bad_time, no_reason, no_file, bad_kind, bad_target = [], [], [], [], []
    kinds: dict[str, int] = {}
    for r in rows:
        label = f"{r.get('file')} {r.get('key')} {r.get('column')}"
        if _d(r.get("revised_at")) is None:
            bad_time.append(f"{label}（revised_at={r.get('revised_at')!r}）")
        if _blank(r.get("reason")):
            no_reason.append(label)
        rel = str(r.get("file") or "")
        if not rel.startswith("data/"):
            bad_target.append(f"{label}（file={rel!r}）")
        elif not (data_dir / Path(rel).relative_to("data")).exists():
            # 実体が無いのは「このツリーにそのファイルが無い」場合もある
            # （テスト用の部分コピー等）。改竄の証拠にはならないので WARN。
            no_file.append(label)
        kind = str(r.get("kind") or "").strip()
        if kind not in REVISION_KINDS:
            bad_kind.append(f"{label}（kind={kind!r}）")
        else:
            kinds[kind] = kinds.get(kind, 0) + 1
    g = rep.group
    g(FAIL, "append_only", target, "revised_at が日付として読めない", bad_time)
    g(FAIL, "append_only", target,
      "reason が空（何を根拠に訂正したのか残っていない）", no_reason)
    g(FAIL, "append_only", target,
      "訂正対象が data/ 配下ではない（記録の書き方が壊れている）", bad_target)
    g(WARN, "append_only", target,
      "訂正対象のファイルがこのツリーに無い（訂正の妥当性を検証できていない）",
      no_file)
    g(FAIL, "append_only", target,
      f"kind が語彙外（正: {'/'.join(REVISION_KINDS)}）", bad_kind)
    detail = "／".join(f"{k} {kinds[k]}件" for k in sorted(kinds))
    rep.warn("append_only", target,
             f"append-only の例外として記録された訂正 {len(rows)}件（{detail}）。"
             "採用値を空⇄非空に動かした記録であり、値の書き換えは含まれない")


def check_verification_append_only(rep: Report, data_dir: Path,
                                   baseline: Baseline) -> None:
    """裏取り記録（YAML）の過去 run が書き換えられていないか。

    `verification.py` と SKILL.md は「過去の run を1行も触らない」を絶対原則に
    挙げているのに、機械の担保が CSV にしか無かった。**run をキーにして
    行単位（run 単位）で突き合わせる**。新しい run の追加だけが許される。
    """
    rels = sorted(r for r in baseline.files
                  if r.startswith("data/verification/") and r.endswith(".yaml"))
    current = sorted(f"data/{p.relative_to(data_dir).as_posix()}"
                     for p in (data_dir / "verification").glob("*.yaml")
                     ) if (data_dir / "verification").exists() else []
    for rel in sorted(set(rels) | set(current)):
        text = baseline.read(rel)
        if text is None:
            continue
        path = data_dir / Path(rel).relative_to("data")
        if not path.exists():
            rep.fail("append_only", rel, "ベースラインに存在したファイルが消えている")
            continue
        try:
            old = Y.safe_load(text) or {}
            new = Y.safe_load(path.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as e:
            rep.fail("append_only", rel, f"YAML を読めない: {e}")
            continue
        old_runs = {str((r or {}).get("run")): r
                    for r in (old.get("runs") or []) if isinstance(r, dict)}
        new_runs = {str((r or {}).get("run")): r
                    for r in (new.get("runs") or []) if isinstance(r, dict)}
        removed = sorted(k for k in old_runs if k not in new_runs)
        changed = sorted(k for k in old_runs
                         if k in new_runs and old_runs[k] != new_runs[k])
        rep.group(FAIL, "append_only", rel, "過去の run が削除されている", removed)
        rep.group(FAIL, "append_only", rel,
                  "過去の run が書き換えられている（裏取り記録は追記のみ）", changed)


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

    revisions = load_revisions(data_dir)
    current = {f"data/{p.relative_to(data_dir).as_posix()}"
               for p in sorted(data_dir.rglob("*.csv"))}
    check_verification_append_only(rep, data_dir, baseline)
    for rel in sorted(baseline.files | current):
        if not rel.endswith(".csv"):
            continue
        text = baseline.read(rel)
        if text is None:
            continue                      # ベースラインに無い＝新規ファイル
        path = data_dir / Path(rel).relative_to("data")
        if not path.exists():
            rep.fail("append_only", rel, "ベースラインに存在したファイルが消えている")
            continue

        old = list(csv.DictReader(text.splitlines()))
        new = load_csv(path)
        header = list(new[0].keys()) if new else (list(old[0].keys()) if old else [])
        keys = _key_columns(rel, header)
        # 鍵が1つも決まらない／ヘッダに無い列を鍵にしている状態で比較すると、
        # 全行のキーが同じ値に潰れて誤検知する。**検証できていないと言う**。
        if not keys or any(k not in header for k in keys):
            rep.warn("append_only", rel,
                     f"追記性の一意キーをヘッダから決められない（列 {header}）。"
                     "**このファイルの追記性は検証できていない**")
            continue
        old_cols = set(old[0].keys()) if old else set()
        new_cols = set(new[0].keys()) if new else set()
        if old and new and old_cols != new_cols:
            rep.warn("append_only", rel,
                     f"列構成が変わっている（追加 {sorted(new_cols - old_cols)} / "
                     f"削除 {sorted(old_cols - new_cols)}）。共通列のみ比較する")
        shared = sorted(old_cols & new_cols) or sorted(old_cols)

        index = {tuple(str(r.get(c) or "") for c in keys): r for r in new}
        removed, changed, repaired = [], [], []
        for r in old:
            k = tuple(str(r.get(c) or "") for c in keys)
            cur = index.get(k)
            if cur is None:
                removed.append("/".join(k))
                continue
            diffs = [(c, str(r.get(c) or ""), str(cur.get(c) or ""))
                     for c in shared if str(r.get(c) or "") != str(cur.get(c) or "")]
            if not diffs:
                continue
            joined = " , ".join(f"{c}: {a!r} → {b!r}" for c, a, b in diffs)
            reason = _recorded_revision(
                r, cur, header, revisions.get((rel, "/".join(k)), []), diffs)
            if reason is None:
                changed.append(f"{'/'.join(k)}（{joined}）")
            else:
                repaired.append(f"{'/'.join(k)}（{reason}）")

        rep.group(FAIL, "append_only", rel, "過去行が削除されている", removed)
        rep.group(FAIL, "append_only", rel, "過去行が変更されている", changed)
        rep.group(WARN, "append_only", rel,
                  "訂正として記録されている過去行（data/revisions.csv に記録あり）",
                  repaired)

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
    doc = Y.safe_load(path.read_text(encoding="utf-8")) or {}
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


def check_source_independence(rep: Report, rows: list[dict], target: str,
                              sources: dict, section: str = "price") -> None:
    """採用終値が「運営の異なる2つの取得元」の一致になっているか。

    株探と みんかぶ は同一運営（ミンカブ・ジ・インフォノイド）の2媒体で、
    その一致は独立した2つの確認ではない。財務側は `_fund_site` が
    「同じサイトの別ページは独立した確認ではない」を FAIL で守っているのに、
    株価側には site / operator の概念すら無く、チェーンの上から2件成功した時点で
    打ち切っていた（実データ1,080行のうち1,036行が同一運営の2媒体の一致）。

    **過去行は append-only なので直さない。** 直すのは取得側（fetch.py）で、
    ここでは「いま台帳に載っている採用終値のうち、独立性が弱いものが何行あるか」
    を数えて表に出す。黙って「2ソース照合済み」と言わないことが目的。
    """
    chain = (sources or {}).get(section, {}).get("chain") or []
    ops = {str(e["id"]): str(e.get("operator") or e["id"]) for e in chain}
    if not ops:
        return
    weak = 0
    strong = 0
    pairs: dict[str, int] = {}
    for r in rows:
        if "OK" not in str(r.get("status") or "").split("|"):
            continue
        a = str(r.get("source_primary") or "")
        b = str(r.get("source_secondary") or "")
        if not a or not b:
            continue
        if ops.get(a, a) == ops.get(b, b):
            weak += 1
            pairs[f"{a}+{b}"] = pairs.get(f"{a}+{b}", 0) + 1
        else:
            strong += 1
    if not weak:
        return
    detail = "／".join(f"{k} {pairs[k]}行" for k in sorted(pairs))
    rep.warn("missing", target,
             f"採用終値のうち {weak}行は**同一運営の2媒体**の一致で採用されている"
             f"（{detail}）。運営の異なる2つの一致は {strong}行。"
             "過去行は append-only なので直さない。"
             "取得側は運営が異なる2件を要求するように直した（sources.yaml の operator）")


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
# 11. 期・単位・metric の正規化（レポートと CSV を同じ土俵に載せるための共通語彙）
# =============================================================================
#
# 「2026/6予」「26/6 3Q」「FY2026Q3cum」「2026-06」を同じ期として扱えないと、
# レポートと検証済み数値の突合は**全件が未突合になって黙って素通りする**。
# 正規化を1箇所に置き、突合できなかったものは必ず件数として出す。

@dataclass(frozen=True)
class Period:
    """会計期。年・月・四半期・累計か・会社計画かで同定する。

    月が書かれない表記（FY2026）があるので月は None を許す。
    `matches` は **片方に月が無いときだけ**月の一致を問わない。
    「3Q累計」と「通期」を取り違えると比較が丸ごと嘘になるので、
    四半期・累計・計画は一致を必須にする。
    """
    year: int
    month: int | None
    quarter: int | None
    cumulative: bool
    plan: bool
    text: str = ""
    standalone: bool = False    # 単独四半期（3か月だけ。累計でも通期でもない）
    day: int | None = None      # 営業日（株価の突合だけが使う）

    def bucket(self) -> tuple:
        return (self.year, self.quarter, self.cumulative, self.plan,
                self.standalone)

    def matches(self, other: "Period") -> bool:
        if self.bucket() != other.bucket():
            return False
        if self.month is None or other.month is None:
            return True
        if self.month != other.month:
            return False
        # 日まで書かれている表記どうしは日で一致すること。
        # 株価（1日ごとの採用終値）を月の粒度で突き合わせると、
        # 同じ月の全営業日が候補になって突合が成立しない。
        if self.day is None or other.day is None:
            return True
        return self.day == other.day

    def label(self) -> str:
        month = "??" if self.month is None else f"{self.month:02d}"
        out = f"{self.year:04d}-{month}"
        if self.quarter:
            out += f"Q{self.quarter}"
        if self.cumulative:
            out += "cum"
        if self.standalone:
            out += "(単独3か月)"
        if self.plan:
            out += "(計画)"
        return out


def _ym_from_index(index: int) -> tuple[int, int]:
    """通し月インデックス（year*12+month）を (年, 月) に戻す。"""
    return ((index - 1) // 12, (index - 1) % 12 + 1)


_PLAN_RE = re.compile(r"(会社予想|会社計画|予想|見通し|計画|予|plan|forecast)", re.I)
_CUM_RE = re.compile(r"(cum|累計|累積)", re.I)
_QUARTER_RE = re.compile(
    r"(?:Q\s*([1-4])(?![0-9])|(?<![0-9])([1-4])\s*Q(?![0-9])|第\s*([1-4])\s*四半期)",
    re.I)
_YEAR_MONTH_RE = re.compile(
    r"(?:FY)?(\d{4})\s*[/\-.年]\s*(\d{1,2})|(?<!\d)(\d{2})\s*[/\-]\s*(\d{1,2})(?!\d)")
_YEAR_ONLY_RE = re.compile(r"(?:FY|fy)\s*(\d{4})|(\d{4})\s*年度")
_PLAN_METRIC_SUFFIX = ("_fy_plan", "_plan", "_forecast", "_guidance")
_TOKEN_RE = re.compile(r"[^0-9a-z぀-ヿ一-龯]+")


_ISO_DAY_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")
_FUND_FY_KEY_RE = re.compile(r"^FY(\d{4})-(\d{2})$")
_FUND_SPAN_KEY_RE = re.compile(r"^([QHC])(\d{4})-(\d{2})_(\d{4})-(\d{2})$")


def _period_from_fund_key(t: str) -> Period | None:
    """`fetch_fundamentals` が出す期間キーを Period にする。

        FY2022-06            通期（2022年6月期）
        C2025-07_2026-03     累計（期首から9か月＝3Q累計。決算期末は 2026-06）
        H2023-01_2023-06     累計（期首から6か月＝2Q累計）
        Q2024-04_2024-06     **単独**四半期（3か月だけ）

    C/H は期首からの累計なので「開始月 + 11か月」が決算期末になる。
    Q は単独期間で、通期にも累計にも対応しないため `standalone` を立てて
    レポートの「3Q」などと**突き合わせない**（別の量なので比べたら嘘になる）。
    """
    m = _FUND_FY_KEY_RE.match(t)
    if m is not None:
        return Period(year=int(m.group(1)), month=int(m.group(2)),
                      quarter=None, cumulative=False, plan=False, text=t)
    m = _FUND_SPAN_KEY_RE.match(t)
    if m is None:
        return None
    tag = m.group(1)
    start = int(m.group(2)) * 12 + int(m.group(3))
    end = int(m.group(4)) * 12 + int(m.group(5))
    months = end - start + 1
    if months <= 0:
        return None
    if tag == "Q":
        ey, em = _ym_from_index(end)
        return Period(year=ey, month=em, quarter=None, cumulative=False,
                      plan=False, text=t, standalone=True)
    fy_year, fy_month = _ym_from_index(start + 11)
    return Period(year=fy_year, month=fy_month, quarter=months // 3,
                  cumulative=True, plan=False, text=t)


def parse_period(text) -> Period | None:
    """期の表記を Period にする。年が読めなければ None（＝突合しない）。"""
    t = str(text or "").strip()
    if not t:
        return None
    fund = _period_from_fund_key(t)
    if fund is not None:
        return fund
    iso = _ISO_DAY_RE.match(t)
    if iso is not None:
        y, mo, d = int(iso.group(1)), int(iso.group(2)), int(iso.group(3))
        if 1 <= mo <= 12 and 1 <= d <= 31:
            return Period(year=y, month=mo, quarter=None, cumulative=False,
                          plan=False, text=t, day=d)
    year: int | None = None
    month: int | None = None
    m = _YEAR_MONTH_RE.search(t)
    if m is not None:
        if m.group(1):
            year, month = int(m.group(1)), int(m.group(2))
        else:
            year, month = 2000 + int(m.group(3)), int(m.group(4))
    else:
        m2 = _YEAR_ONLY_RE.search(t)
        if m2 is not None:
            year = int(m2.group(1) or m2.group(2))
    if year is None or year < 1900 or year > 2199:
        return None
    if month is not None and (month < 1 or month > 12):
        return None
    q = _QUARTER_RE.search(t)
    quarter = None
    if q is not None:
        found = [g for g in q.groups() if g]
        quarter = int(found[0])
    return Period(year=year, month=month, quarter=quarter,
                  cumulative=_CUM_RE.search(t) is not None,
                  plan=_PLAN_RE.search(t) is not None, text=t)


def split_plan_suffix(metric_text: str) -> tuple[str, bool]:
    """`revenue_fy_plan` のように metric 名で計画を表す書き方を分解する。

    KPI のスキーマ（SKILL.md）が `*_fy_plan` を使っているため、fundamentals 側が
    同じ流儀で書いてくる可能性がある。ここで吸収しないと、会社計画と実績を
    同じ期として突き合わせてしまう（＝必ず食い違う）。
    """
    t = str(metric_text or "").strip()
    low = t.lower()
    for suf in _PLAN_METRIC_SUFFIX:
        if low.endswith(suf):
            return t[: len(t) - len(suf)], True
    return t, False


_METRIC_FLAT: list[tuple[str, str]] = sorted(
    ((alias.lower(), canon)
     for canon, aliases in FUND_METRIC_ALIASES.items() for alias in aliases),
    key=lambda x: (-len(x[0]), x[0], x[1]))


def metric_of(text) -> str | None:
    """表記から metric の正規名を引く。曖昧なら None（＝突合しない）。

    **長い別名が勝つ。** 「営業利益率」は operating_income（営業利益）にも
    部分一致するが、より長い別名を持つ operating_margin_pct を採る。
    3文字以下の ASCII 別名（op / roe / eps）は部分一致させずトークン一致のみ。
    同じ長さで2つ以上の metric に当たったら None にする（推測で結び付けない）。
    """
    t = str(text or "").strip().lower()
    if not t:
        return None
    tokens = {x for x in _TOKEN_RE.split(t) if x}
    best_len = 0
    best: set[str] = set()
    for alias, canon in _METRIC_FLAT:
        if len(alias) <= 3 and alias.isascii():
            hit = alias in tokens
        else:
            hit = alias in t
        if not hit:
            continue
        if len(alias) > best_len:
            best_len, best = len(alias), {canon}
        elif len(alias) == best_len:
            best.add(canon)
    if len(best) == 1:
        return best.pop()
    return None


_UNIT_KEYS = sorted(UNIT_SCALES, key=lambda k: (-len(k), k))


def unit_of(text, metric: str | None = None) -> tuple[str, float] | None:
    """単位表記から (族, 基本単位への係数) を引く。分からなければ None。"""
    t = str(text or "").strip().lower()
    if t in UNIT_SCALES:
        return UNIT_SCALES[t]
    for name in _UNIT_KEYS:
        if name in t:
            return UNIT_SCALES[name]
    if metric in FUND_DEFAULT_PCT_METRICS:
        return ("pct", 1.0)          # 比率 metric の単位無記載は % とみなす
    return None


_CELL_NUM_RE = re.compile(
    r"^\s*(?:約|およそ)?\s*([-+−▲△]?)\s*"
    r"([0-9,]+(?:\.[0-9]+)?)\s*([^\s0-9]*)\s*$")
_MD_MARKUP_RE = re.compile(r"[*`~]")


def cell_number(text) -> tuple[float, int, str] | None:
    """**セル全体が1つの数値**のときだけ (値, 小数桁, 単位表記) を返す。

    散文セル（「総資産のうち自前の資本は1割弱」）から数字を拾うと、
    突合できない候補ばかりが増えて「未突合」の件数が意味を失う。
    全体一致に限定し、さらに **後ろに付くのは既知の単位だけ**とする
    （「52週高値」「10年来安値」は数値セルではない）。
    """
    t = _MD_MARKUP_RE.sub("", str(text or "")).strip()
    m = _CELL_NUM_RE.match(t)
    if m is None:
        return None
    sign, digits, unit = m.group(1), m.group(2), m.group(3)
    if unit and unit_of(unit) is None:
        return None
    try:
        value = float(digits.replace(",", ""))
    except ValueError:
        return None
    if sign in ("-", "−", "▲", "△"):
        value = -value
    decimals = len(digits.split(".")[1]) if "." in digits else 0
    return value, decimals, unit


def decimals_of(value) -> int:
    """float の表示桁を推定する（front matter は既にパースされていて原文が無い）。"""
    s = repr(float(value))
    if "e" in s or "E" in s:
        return 0
    if "." not in s:
        return 0
    return len(s.split(".")[1].rstrip("0"))


def half_ulp(decimals: int) -> float:
    """表示桁から丸め幅の半分を出す。「26.4」なら ±0.05。"""
    return 0.5 * (10.0 ** (-decimals))


# =============================================================================
# 12. 財務数値（data/fundamentals/{code}.csv）
# =============================================================================
#
# レポートの数値を裏で支えるファイル。株価の daily.csv と同じ規律で読む。
#   - 採用値が入ってよいのは照合成立行だけ（D7）
#   - 取れなければ null + status。推測で埋めない
#
# **この検査が素通りする壊れ方**を先に潰しておく:
#   (a) 列名が違う      -> 別名で解決し、解決できなければ WARN で表に出す
#   (b) 採用値が0件      -> 「照合が1件も成立していない」を WARN で出す
#   (c) metric 名が未知  -> 範囲・恒等式の対象外になるので、未知 metric を列挙する
# いずれも「検査できなかった」を「検査に通った」にしない（設計原則1）。

@dataclass(frozen=True)
class Fact:
    """fundamentals の1行を、突合できる形に正規化したもの。"""
    code: str
    metric: str
    period: Period
    status: str
    adopted: bool
    value: float | None       # 採用値（照合成立時のみ）
    decimals: int
    family: str | None
    base: float | None        # 基本単位（円 / % / 倍）に揃えた採用値
    unit_text: str
    raw_metric: str


def _resolve_columns(cols, aliases: dict) -> dict[str, str]:
    """実際のヘッダから「正規名 -> 実列名」を作る。"""
    lower = {}
    for c in cols:
        lower.setdefault(str(c).strip().lower(), c)
    out: dict[str, str] = {}
    for canon, names in aliases.items():
        for n in names:
            if n in lower:
                out[canon] = lower[n]
                break
    return out


def _fund_flags(status: str) -> list[str]:
    return [p for p in str(status or "").split("|") if p]


def _observed_values(row: dict, col: dict[str, str],
                     fallback: list[float]) -> list[float]:
    """`sources_all`（"kabutan=29.1|irbank=29.07"）から観測値を取り出す。

    採用値は**参加した全観測**から選ばれるため、主・副の2列だけを見ると
    第3のソースから採った値を「作られた値」と誤検知する。
    列が無い場合は主副にフォールバックする。
    """
    name = col.get("sources_all")
    if not name:
        return fallback
    out: list[float] = []
    for part in str(row.get(name, "") or "").split("|"):
        if "=" not in part:
            continue
        v = _f(part.rsplit("=", 1)[1])
        if v is not None:
            out.append(v)
    return out or fallback


def _fund_sources(row: dict, col: dict[str, str]) -> list[str]:
    """その行の照合に**参加した取得元の名前**（値ではなく名前）。

    `sources_all` は "kabutan_fy=1844|irbank_pl=1844" の形。列が無い／空の行では
    主・副の取得元名にフォールバックする。
    """
    name = col.get("sources_all")
    out: list[str] = []
    if name:
        for part in str(row.get(name, "") or "").split("|"):
            if "=" not in part:
                continue
            s = part.rsplit("=", 1)[0].strip()
            if s:
                out.append(s)
    if out:
        return out
    for key in ("source_primary", "source_secondary"):
        c = col.get(key)
        if not c:
            continue
        s = str(row.get(c, "") or "").strip()
        if s:
            out.append(s)
    return out


def _fund_site(source: str) -> str:
    """取得元名からサイトを取る（`kabutan_ytd3q` → `kabutan`）。

    独立性は**サイト単位**で決まる。同じサイトの別ページが同じ数字を出しても
    それは独立した確認ではない（`fetch_fundamentals.reconcile` と同じ規律）。
    CSV にはページ単位の名前しか残らないので、先頭の区切りまでをサイトとみなす。
    区切りが無い名前はそれ自体がサイト名になる。
    """
    return source.split("_", 1)[0]


def _fact_from_row(code: str, row: dict, col: dict[str, str]) -> Fact | None:
    raw_metric = str(row.get(col["metric"], "") or "").strip()
    metric_text, plan_by_metric = split_plan_suffix(raw_metric)
    metric = metric_of(metric_text)
    period = parse_period(row.get(col.get("period", "period"), ""))
    if metric is None or period is None:
        return None
    if plan_by_metric and not period.plan:
        period = Period(year=period.year, month=period.month,
                        quarter=period.quarter, cumulative=period.cumulative,
                        plan=True, text=period.text)
    status = str(row.get(col["status"], "") or "")
    raw_value = row.get(col["value"], "")
    value = _f(raw_value)
    adopted = value is not None and "OK" in _fund_flags(status)
    unit_text = str(row.get(col.get("unit", "unit"), "") or "")
    scale = unit_of(unit_text, metric)
    family = scale[0] if scale else None
    base = None
    if value is not None and scale is not None:
        base = value * scale[1]
    return Fact(code=code, metric=metric, period=period, status=status,
                adopted=adopted, value=value,
                decimals=len(str(raw_value).split(".")[1])
                if "." in str(raw_value) else 0,
                family=family, base=base, unit_text=unit_text,
                raw_metric=raw_metric)


def _fund_group_key(p: Period) -> tuple:
    return (p.year, p.month, p.quarter, p.cumulative, p.plan)


def _fix_cumulative_periods(facts: list[Fact]) -> tuple[list[Fact], list[str]]:
    """決算期末月と辻褄が合わない「累計」を単独期間に落とす。

    `fetch_fundamentals` の期間キー（C/H/Q）は**期間の長さだけ**でタグを付ける。
    `_period_from_fund_key` はそれを「期首からの累計」と仮定して
    「開始月 + 11か月」を決算期末にするが、6月決算の会社の 1〜6月は**下期**であって
    2Q累計ではない。実測: `H2026-01_2026-06` が「2026年12月期 2Q累計」と読まれ、
    **存在しない決算期**が作られていた。

    その銘柄の通期（FY）キーが持つ月が、その会社の決算期末月そのものなので、
    それと合わない累計は「累計として扱えない期間」＝ standalone に落とす。
    落とした事実は呼び出し側が WARN で出す（黙って捨てない・設計原則1）。
    """
    fy_months = {f.period.month for f in facts
                 if not f.period.cumulative and not f.period.standalone
                 and f.period.quarter is None and f.period.month is not None}
    if not fy_months:
        return facts, []
    out: list[Fact] = []
    demoted: list[str] = []
    for f in facts:
        p = f.period
        if p.cumulative and p.month is not None and p.month not in fy_months:
            demoted.append(f"{p.text}（決算期末 {sorted(fy_months)} と合わない）")
            p2 = Period(year=p.year, month=p.month, quarter=None,
                        cumulative=False, plan=p.plan, text=p.text,
                        standalone=True)
            out.append(Fact(code=f.code, metric=f.metric, period=p2,
                            status=f.status, adopted=f.adopted, value=f.value,
                            decimals=f.decimals, family=f.family, base=f.base,
                            unit_text=f.unit_text, raw_metric=f.raw_metric))
            continue
        out.append(f)
    return out, sorted(set(demoted))


def _check_fund_identity(rep: Report, target: str, groups: dict) -> int:
    """売上 − 売上原価 − 販管費 ≒ 営業利益。検算できた期の数を返す。

    分母は**売上**。営業利益にすると赤字転換期にゼロ近傍で発散して毎週 FAIL する。

    実額（売上・原価・販管費・営業利益）が揃わない期でも、**比率で同じ恒等式が
    立つ**（原価率 + 販管費率 + 営業利益率 = 100%）。取得元が比率しか出していない
    期がある（実データではむしろそちらが多い）ので、両方の経路を持つ。
    片方でも成立すれば検算済みとして数える。
    """
    bad, evaluated = [], 0
    # 期のキーは (年, 月, 四半期, 累計, 計画) で、月・四半期に None が混ざる。
    # そのまま sorted すると None と int の比較で落ちるので文字列で並べる。
    for key in sorted(groups, key=str):
        by_metric = groups[key]
        need = ("revenue", "cost_of_sales", "sga", "operating_income")
        facts = [by_metric.get(m) for m in need]
        if not any(f is None or f.base is None or f.family != "money"
                   for f in facts):
            evaluated += 1
            rev, cos, sga, op = [f.base for f in facts]
            calc = rev - cos - sga
            slack = sum(half_ulp(f.decimals) * abs(f.base / f.value)
                        for f in facts if f.value)
            tol = FUND_IDENTITY_TOLERANCE * abs(rev) + slack
            if abs(calc - op) > tol:
                label = facts[0].period.label()
                bad.append(f"{label}（売上-原価-販管費={calc:,.0f} / "
                           f"営業利益={op:,.0f}）")

        ratios = [by_metric.get(m) for m in
                  ("cost_ratio_pct", "sga_ratio_pct", "operating_margin_pct")]
        if any(f is None or f.base is None or f.family != "pct" for f in ratios):
            continue
        evaluated += 1
        total = sum(f.base for f in ratios)
        tol = sum(half_ulp(f.decimals) for f in ratios) + FUND_RATIO_IDENTITY_SLACK_PT
        if abs(total - 100.0) > tol:
            label = ratios[0].period.label()
            bad.append(f"{label}（原価率+販管費率+営業利益率={total:.2f}%）")
    rep.group(FAIL, "fundamentals", target,
              "売上 − 売上原価 − 販管費 が営業利益と合わない（1%超）", bad)
    return evaluated


def _check_fund_equity_ratio(rep: Report, target: str, groups: dict) -> int:
    """自己資本 ÷ 総資産 ≒ 自己資本比率。検算できた期の数を返す。

    許容は**区間で持つ**（表示桁からの丸め幅を伝播させる）。固定の許容値を置くと、
    桁が違う会社で緩すぎたり厳しすぎたりする。
    `net_assets`（純資産）で代用しない。少数株主持分のぶんだけ必ずずれて、
    正しい抽出まで FAIL にしてしまうため。
    """
    bad, evaluated = [], 0
    for key in sorted(groups, key=str):
        by_metric = groups[key]
        eq = by_metric.get("equity")
        ta = by_metric.get("total_assets")
        ratio = by_metric.get("equity_ratio_pct")
        if eq is None or ta is None or ratio is None:
            continue
        if eq.base is None or ta.base is None or ratio.base is None:
            continue
        if eq.family != "money" or ta.family != "money" or ratio.family != "pct":
            continue
        evaluated += 1
        he = half_ulp(eq.decimals) * abs(eq.base / eq.value) if eq.value else 0.0
        ht = half_ulp(ta.decimals) * abs(ta.base / ta.value) if ta.value else 0.0
        lo_den, hi_den = ta.base - ht, ta.base + ht
        if lo_den <= 0:
            continue
        calc_lo = (eq.base - he) / hi_den * 100.0
        calc_hi = (eq.base + he) / lo_den * 100.0
        hr = half_ulp(ratio.decimals)
        r_lo, r_hi = ratio.base - hr, ratio.base + hr
        if calc_lo > r_hi or r_lo > calc_hi:
            label = ratio.period.label()
            bad.append(f"{label}（自己資本÷総資産={calc_lo:.2f}〜{calc_hi:.2f}% / "
                       f"記載 {ratio.value}%）")
    rep.group(FAIL, "fundamentals", target,
              "自己資本比率が 自己資本÷総資産 と合わない", bad)
    return evaluated


def _check_fund_sign(rep: Report, target: str, groups: dict) -> int:
    """営業利益率の符号は営業利益の符号と一致する（片方だけ符号を落とす転記ミス）。"""
    bad, evaluated = [], 0
    for key in sorted(groups, key=str):
        by_metric = groups[key]
        op = by_metric.get("operating_income")
        margin = by_metric.get("operating_margin_pct")
        if op is None or margin is None:
            continue
        if op.base is None or margin.base is None or op.base == 0 or margin.base == 0:
            continue
        evaluated += 1
        if (op.base > 0) != (margin.base > 0):
            label = op.period.label()
            bad.append(f"{label}（営業利益={op.value} / 営業利益率={margin.value}%）")
    rep.group(FAIL, "fundamentals", target,
              "営業利益と営業利益率の符号が一致しない", bad)
    return evaluated


def _check_fund_scale_jump(rep: Report, target: str, facts: list[Fact]) -> None:
    """前期比で桁が飛んだ項目（桁の取り違え）。

    **利益・EPS・比率には適用しない。** 小型株の利益は 59 → 386 のように実際に
    10倍動く。規模の量（売上・総資産・自己資本など）だけを見る。
    四半期・累計の別が同じもの同士でのみ比較する（通期と1Q累計を比べない）。

    ★系列キーに `standalone`（単独3か月）と `plan`（会社計画）も入れる。
      旧実装は `(metric, quarter, cumulative)` だけだったため、
      **単独四半期・通期・会社計画が同じ系列に混ざって隣接比較されていた**。
      比較そのものが無意味なうえ、実データの最大隣接比が既に 6.26倍
      （閾値10倍）まで来ており、季節性で弱い四半期が1本出れば FAIL していた。
      分けたことで実質は前年同期比になるので、閾値は据え置きでよい。
    """
    series: dict[tuple, list[Fact]] = {}
    for f in facts:
        if f.metric not in FUND_SCALE_METRICS or f.base is None:
            continue
        series.setdefault((f.metric, f.period.quarter, f.period.cumulative,
                           f.period.standalone, f.period.plan), []).append(f)
    bad = []
    for key in sorted(series, key=lambda k: (k[0], str(k[1]), k[2], k[3], k[4])):
        pts = sorted(series[key], key=lambda f: (f.period.year,
                                                 f.period.month or 0))
        for i in range(1, len(pts)):
            prev, cur = pts[i - 1], pts[i]
            if prev.family != cur.family or not prev.base or not cur.base:
                continue
            hi = max(abs(prev.base), abs(cur.base))
            lo = min(abs(prev.base), abs(cur.base))
            if lo <= 0 or hi / lo < FUND_SCALE_JUMP_RATIO:
                continue
            ratio = hi / lo
            bad.append(f"{cur.metric} {prev.period.label()}→{cur.period.label()}"
                       f"（{prev.value}{prev.unit_text} → {cur.value}{cur.unit_text}"
                       f"・{ratio:,.1f}倍）")
    rep.group(FAIL, "fundamentals", target,
              "前期比で桁が飛んでいる（桁・単位の取り違えを疑う）", bad)


def _check_fund_progress(rep: Report, target: str, facts: list[Fact]) -> None:
    """四半期累計 ≦ 通期計画（超えていたら異常か、計画が古い）。

    利益は累計が計画を超えても普通に起こる（上振れ）ので**売上だけ**を見る。
    累計か単独かが読み取れない期は比較しない。読み取れないこと自体を WARN で出す。
    """
    plans: dict[tuple, Fact] = {}
    for f in facts:
        if f.period.plan and f.period.quarter is None and f.base is not None:
            plans[(f.metric, f.period.year)] = f
    over = []
    for f in facts:
        if f.metric != "revenue" or f.base is None:
            continue
        if not f.period.cumulative or f.period.plan or f.period.quarter is None:
            continue
        plan = plans.get((f.metric, f.period.year))
        if plan is None or plan.family != f.family:
            continue
        if f.base > plan.base:
            over.append(f"{f.period.label()}（累計 {f.value}{f.unit_text} > "
                        f"通期計画 {plan.value}{plan.unit_text}）")
    rep.group(WARN, "fundamentals", target,
              "四半期累計が通期計画を超えている（計画が古いか、期の取り違え）", over)

    ambiguous = sorted({f.period.text for f in facts
                        if f.period.quarter is not None and not f.period.cumulative
                        and not f.period.standalone
                        and "単独" not in f.period.text})
    rep.group(WARN, "fundamentals", target,
              "四半期が累計か単独か読み取れない（通期計画との比較を行っていない）",
              ambiguous)


def check_fundamentals(rep: Report, data_dir: Path, master: dict,
                       sources: dict | None = None,
                       price_latest: str | None = None) -> dict[str, list[Fact]]:
    """財務数値の2ソース照合結果を検査し、突合用の Fact を返す。

    ファイルが無い場合は**黙って返さない**。「レポートの数値は1件も機械照合されて
    いない」という現状そのものを WARN で表に出す（設計原則1）。

    `sources` は `data/sources.yaml`。`fundamentals.required_sites`（採用に必要な
    独立サイト数）を読むために使う。渡されなければ2とみなす。
    """
    fdir = data_dir / "fundamentals"
    files = sorted(fdir.glob("*.csv")) if fdir.exists() else []
    if not files:
        rep.warn("fundamentals", "data/fundamentals/",
                 "財務数値の検証済みデータが1件も無い。"
                 "レポートの数値は人間の転記のままで、機械照合されていない")
        return {}

    known = {str(s["code"]) for s in master.get("stocks", [])}
    # ★master を回す（glob した分だけ見ない）。株価側の check_coverage と同じ形。
    #   旧実装はファイルが丸ごと消えても WARN が2件「減る」だけで、
    #   取得漏れが「きれいになった」ように見えていた（設計原則2）。
    have = {p.stem for p in files}
    for code in sorted(known - have):
        rep.warn("fundamentals", f"data/fundamentals/{code}.csv",
                 f"{code}: 財務数値が1件も取れていない（レポートの数値を突合できない）")
    fund_cfg = (sources or {}).get("fundamentals") or {}
    required_sites = fund_cfg.get("required_sites")
    if not isinstance(required_sites, int) or required_sites < 1:
        required_sites = 2
    out: dict[str, list[Fact]] = {}
    for path in files:
        code_file = path.stem
        target = f"data/fundamentals/{code_file}.csv"
        rows = load_csv(path)
        if not rows:
            rep.warn("fundamentals", target, "行が1つも無い")
            continue
        col = _resolve_columns(rows[0].keys(), FUNDAMENTALS_COLUMN_ALIASES)
        lacking = [c for c in ("code", "metric", "value", "status", "period")
                   if c not in col]
        if lacking:
            rep.warn("fundamentals", target,
                     f"列を解決できない: {lacking}（別名も含めて見つからない）。"
                     f"想定スキーマは {list(FUNDAMENTALS_FIELDS)}。"
                     "**この銘柄の財務数値は検査されていない**")
            continue

        check_duplicate(rep, rows, target,
                        (col["code"], col["period"], col["metric"]))

        facts: list[Fact] = []
        bad_code, bad_period, bad_status, unknown_flag = [], [], [], []
        bad_value, no_src, no_unit, unknown_metric = [], [], [], []
        adopted_wrong, ok_no_value, ok_disagree, mismatch_same = [], [], [], []
        mismatch_adopted, out_of_range, ok_invented, ok_far = [], [], [], []
        ok_no_tol, wide_tol = [], []
        ok_one_site, ok_no_sources, single_but_multi = [], [], []
        out_of_range_obs, odd_ratio, unit_unconfirmed = [], [], []
        for r in rows:
            raw_metric = str(r.get(col["metric"], "") or "").strip()
            period_text = str(r.get(col["period"], "") or "")
            key = f"{period_text} {raw_metric}"
            if str(r.get(col["code"], "")) != code_file or code_file not in known:
                bad_code.append(f"{key}（code={r.get(col['code'])!r}）")
            if parse_period(period_text) is None:
                bad_period.append(f"{key}（period={period_text!r}）")
            metric_text, _plan = split_plan_suffix(raw_metric)
            if metric_of(metric_text) is None:
                unknown_metric.append(f"{key}（metric={raw_metric!r}）")

            status = str(r.get(col["status"], "") or "")
            flags = _fund_flags(status)
            reconcile = set(flags) & FUNDAMENTALS_RECONCILE_STATUSES
            if len(reconcile) != 1:
                bad_status.append(f"{key}（status={status!r}）")
            extra = [p for p in flags
                     if p not in FUNDAMENTALS_RECONCILE_STATUSES
                     and p not in FUNDAMENTALS_EXTRA_STATUSES]
            if extra:
                unknown_flag.append(f"{key}（{extra}）")

            raw_value = r.get(col["value"], "")
            value = _f(raw_value)
            if _unreadable(raw_value):
                bad_value.append(f"{key}（value={raw_value!r}）")
            vp = _f(r.get(col.get("value_primary", "value_primary"), ""))
            vs = _f(r.get(col.get("value_secondary", "value_secondary"), ""))
            has_value = not _blank(raw_value)
            if "OK" in flags:
                if not has_value:
                    ok_no_value.append(key)
                else:
                    # ★許容幅の検査は **OK 行すべて**に掛ける。
                    #   旧実装は ROUNDING 分岐の内側にあったため、両サイトが
                    #   同じ粗い表記（ともに「20億」）を出して値が完全一致すると
                    #   ROUNDING が付かず、tolerance が何であっても評価されなかった
                    #   （＝許容幅を膨らませれば ok_far を無効化できた）。
                    tol = _f(r.get(col.get("tolerance", "tolerance"), ""))
                    if (tol is not None and value
                            and abs(tol) > FUND_ROUNDING_MAX_REL * abs(value)):
                        wide_tol.append(f"{key}（許容={tol} / 採用={value}）")
                    if FUNDAMENTALS_ROUNDING_FLAG in flags:
                        observed = _observed_values(
                            r, col, [x for x in (vp, vs) if x is not None])
                        near = [o for o in observed
                                if abs(value - o) <= FUND_FLOAT_EPS]
                        if observed and not near:
                            ok_invented.append(
                                f"{key}（採用={value} / 観測={observed}）")
                        if tol is None:
                            ok_no_tol.append(key)
                        else:
                            outside = [o for o in observed
                                       if abs(value - o) > abs(tol) + FUND_FLOAT_EPS]
                            if outside:
                                ok_far.append(f"{key}（採用={value} / 観測={outside} / "
                                              f"許容={tol}）")
                    elif ((vp is not None and value != vp)
                          or (vs is not None and value != vs)):
                        ok_disagree.append(
                            f"{key}（採用={value} / 主={vp} / 副={vs}）")
            else:
                if has_value:
                    adopted_wrong.append(f"{key}（status={status} なのに value={raw_value}）")
                if "MISMATCH" in flags and has_value:
                    mismatch_adopted.append(f"{key}（value={raw_value}）")
            # ★採用の**根拠の数**を見る。値どうしの一致だけを見ていると、
            #   同じサイトの別ページを2つ数えた行・1ソースしか無い行が
            #   「OK」の顔で採用値を持ったまま素通りする（株価側の
            #   「照合を通っていない値が close に入っている」と同じ壊れ方）。
            sources_named = _fund_sources(r, col)
            sites = sorted({_fund_site(s) for s in sources_named})
            joined = " , ".join(sources_named)
            if "OK" in flags:
                if not sources_named:
                    ok_no_sources.append(key)
                elif len(sites) < required_sites:
                    ok_one_site.append(f"{key}（参加 {joined} / 独立サイト {sites}）")
            elif "SINGLE_SOURCE" in flags and len(sites) >= required_sites:
                single_but_multi.append(f"{key}（独立サイト {sites}）")
            if "MISMATCH" in flags and vp is not None and vp == vs:
                mismatch_same.append(f"{key}（主副とも {vp}）")
            # 数値は必ず出所（URL）と取得時刻を伴う（CLAUDE.md データ層）。
            if "FETCH_FAILED" not in flags and (
                    _blank(r.get(col.get("fetched_at", "fetched_at"), ""))
                    or _blank(r.get(col.get("source_url", "source_url"), ""))):
                no_src.append(key)

            if "UNIT_UNCONFIRMED" in flags:
                unit_unconfirmed.append(key)

            fact = _fact_from_row(code_file, r, col)
            if fact is None:
                continue
            facts.append(fact)
            if fact.family is None and fact.value is not None:
                no_unit.append(f"{key}（unit={fact.unit_text!r}）")
            # 比率の値域。**採用値だけでなく観測値にも掛ける。**
            # 採用値にしか掛けないと、範囲外の値が SINGLE_SOURCE で
            # `value` 空のまま入っているあいだは完全に無検査で、
            # ある週に2ソース一致して採用値になった瞬間に FAIL する
            # （＝「採用値を増やす」という目標がビルド停止を招く）。
            # 上限を 100% で切らないのは、赤字期の販管費率が 105% になるなど
            # **実在する値**を毎週 FAIL にしないため（実データ 4937 FY2019-09）。
            if fact.metric in FUND_RATIO_0_100_METRICS and fact.family == "pct":
                seen = [("採用", fact.base)] if fact.base is not None else []
                scale = unit_of(fact.unit_text, fact.metric)
                mult = scale[1] if scale else 1.0
                for name, raw in (("主", vp), ("副", vs)):
                    if raw is not None:
                        seen.append((name, raw * mult))
                for name, b in seen:
                    if b < FUND_RATIO_HARD_MIN or b > FUND_RATIO_HARD_MAX:
                        item = f"{key}（{name} {b:.2f}%）"
                        (out_of_range if name == "採用" else
                         out_of_range_obs).append(item)
                    elif b < 0.0 or b > 100.0:
                        odd_ratio.append(f"{key}（{name} {b:.2f}%）")

        g = rep.group
        g(FAIL, "fundamentals", target, "code がファイル名またはマスタと一致しない", bad_code)
        g(FAIL, "fundamentals", target, "period が期として読めない", bad_period)
        g(FAIL, "fundamentals", target,
          "照合結果（OK/MISMATCH/SINGLE_SOURCE/FETCH_FAILED）が1つ入っていない",
          bad_status)
        g(FAIL, "fundamentals", target, "value が数値として読めない", bad_value)
        g(FAIL, "fundamentals", target,
          "照合を通っていない値が採用値の列に入っている（D7）", adopted_wrong)
        g(FAIL, "fundamentals", target,
          "status=MISMATCH の行に採用値が入っている（D7）", mismatch_adopted)
        g(FAIL, "fundamentals", target, "status=OK だが採用値が空", ok_no_value)
        g(FAIL, "fundamentals", target,
          "status=OK だが採用値が照合値と食い違う", ok_disagree)
        g(FAIL, "fundamentals", target,
          "採用値がどの観測値とも一致しない（値が作られている）", ok_invented)
        g(FAIL, "fundamentals", target,
          "採用値が記録された許容幅を超えて観測値と離れている", ok_far)
        g(WARN, "fundamentals", target,
          "照合の許容幅が採用値の10%を超えている（この行は事実上ノーチェック）",
          wide_tol)
        g(WARN, "fundamentals", target,
          "ROUNDING だが tolerance 列が無く、許容幅を検算できない", ok_no_tol)
        g(FAIL, "fundamentals", target,
          f"status=OK だが独立した取得元が {required_sites}サイトに満たない"
          "（同じサイトの別ページは独立した確認ではない・D7）", ok_one_site)
        # 株価側では「status=OK だが第2ソースの記録が無い」は FAIL（check_ohlcv_schema）。
        # 財務側だけ WARN だったため、**観測の痕跡を消すだけで採用値が無検査で
        # 通る**穴が開いていた。同じ扱いに揃える（D7）。
        g(FAIL, "fundamentals", target,
          "status=OK だが参加した取得元の名前が記録されていない"
          "（独立性を検査できない値を採用値に格上げしない・D7）", ok_no_sources)
        g(WARN, "fundamentals", target,
          "status=SINGLE_SOURCE だが独立した取得元が複数参加している"
          "（照合が成立しているのに採用されていない）", single_but_multi)
        g(FAIL, "fundamentals", target,
          "status=MISMATCH だが主副の値が一致している", mismatch_same)
        g(FAIL, "fundamentals", target,
          "source_url または fetched_at が空（出所不明の数値は記録しない）", no_src)
        g(FAIL, "fundamentals", target,
          f"採用値の比率が {FUND_RATIO_HARD_MIN:.0f}〜{FUND_RATIO_HARD_MAX:.0f}% の"
          "外にある（抽出ミス）", out_of_range)
        g(WARN, "fundamentals", target,
          f"観測値の比率が {FUND_RATIO_HARD_MIN:.0f}〜{FUND_RATIO_HARD_MAX:.0f}% の"
          "外にある（採用値ではないが抽出ミスの疑い）", out_of_range_obs)
        g(WARN, "fundamentals", target,
          "比率が 0〜100% の外にある（赤字期なら実在しうるが、要確認）", odd_ratio)
        g(WARN, "fundamentals", target,
          "ページ側の単位注記を確認できていない（UNIT_UNCONFIRMED）。"
          "取得元が注記の文言を変えると、単位未確認のまま採用値が積まれる",
          unit_unconfirmed)
        g(WARN, "fundamentals", target,
          "unit を解釈できない（この行は突合・恒等式の対象外）", no_unit)
        g(WARN, "fundamentals", target,
          "metric が語彙に無い（範囲・恒等式の検査対象外。突合もされない）",
          unknown_metric)
        g(WARN, "fundamentals", target, "知らない status フラグ", unknown_flag)

        facts, demoted = _fix_cumulative_periods(facts)
        rep.group(WARN, "fundamentals", target,
                  "累計として読めない期間キー（決算期末月と辻褄が合わない）。"
                  "単独期間として扱い、通期計画との比較・同一期のグループから外した",
                  demoted)

        groups: dict[tuple, dict[str, Fact]] = {}
        for f in facts:
            if f.adopted:
                groups.setdefault(_fund_group_key(f.period), {})[f.metric] = f
        adopted_facts = [f for f in facts if f.adopted]
        ran = {
            "売上−原価−販管費≒営業利益": _check_fund_identity(rep, target, groups),
            "自己資本÷総資産≒自己資本比率":
                _check_fund_equity_ratio(rep, target, groups),
            "営業利益と営業利益率の符号": _check_fund_sign(rep, target, groups),
        }
        _check_fund_scale_jump(rep, target, adopted_facts)
        _check_fund_progress(rep, target, adopted_facts)

        # **「検算できなかった」を「検算に通った」にしない**（設計原則1）。
        # 材料（同じ期の関連 metric）が揃わないと整合検査は1件も走らないが、
        # 結果だけ見ると「FAIL 0 = 検証済み」に見えてしまう。
        idle = sorted(name for name, n in ran.items() if n == 0)
        if adopted_facts and idle:
            rep.warn("fundamentals", target,
                     f"材料が揃わず1期も検算できていない整合検査: {' / '.join(idle)}"
                     "（通ったのではない）")

        if not adopted_facts:
            rep.warn("fundamentals", target,
                     f"照合が1件も成立していない（{len(rows)}行すべて採用値なし）。"
                     "レポートの数値を突合できる材料が無い")

        # 鮮度。壁時計を使わず CSV 内の値だけで見る（決定論的）。
        fetched = sorted(str(r.get(col.get("fetched_at", "fetched_at"), "") or "")[:10]
                         for r in rows
                         if not _blank(r.get(col.get("fetched_at", "fetched_at"), "")))
        if not fetched:
            rep.warn("fundamentals", target,
                     "fetched_at が1行も無い（いつ取得したものか分からない）")
        else:
            newest = max(fetched)
            span = _days(newest, price_latest) if price_latest else None
            if span is not None and span > FUND_STALE_DAYS:
                rep.warn("fundamentals", target,
                         f"最後に取得したのが {newest}・株価の最新営業日 "
                         f"{price_latest} から {span}日前"
                         f"（{FUND_STALE_DAYS}日超）。取得が止まっている疑い")

        out[code_file] = facts

    # 銘柄取り違え・同一ページの取得。株価側 check_cross_code_identity と同じ問い。
    # 件数を数える検査は「全銘柄が同じ値で埋まっている」に感度がゼロ（設計原則2）。
    _check_fund_cross_code(rep, out)
    return out


def _check_fund_cross_code(rep: Report, by_code: dict[str, list[Fact]]) -> None:
    """別の銘柄と (期, metric, 値) が丸ごと一致していないか（取り違え）。"""
    codes = sorted(by_code)
    if len(codes) < 2:
        return
    sig: dict[str, set] = {}
    for code in codes:
        sig[code] = {(f.period.label(), f.metric, f.base)
                     for f in by_code[code] if f.base is not None}
    bad = []
    for i in range(len(codes)):
        for j in range(i + 1, len(codes)):
            a, b = codes[i], codes[j]
            shared = sig[a] & sig[b]
            small = min(len(sig[a]), len(sig[b]))
            if not small:
                continue
            ratio = len(shared) / small
            if len(shared) >= FUND_CROSS_CODE_MIN and ratio >= FUND_CROSS_CODE_RATIO:
                bad.append(f"{a} と {b}: (期, 指標, 値) が {len(shared)}件一致"
                           f"（重なり {ratio:.0%}）")
    rep.group(FAIL, "fundamentals", "data/fundamentals/",
              "別の銘柄と財務数値が広範囲に一致している（銘柄取り違え・"
              "同一ページの取得）", bad)


# =============================================================================
# 12.5 決算短信（一次情報）— data/tanshin/{code}.csv
# =============================================================================
#
# このファイルは**決算短信PDFから直接抜いた一次情報**で、レポートの見出し級の
# 数字がここから来る（4073 の「3Q時点の営業利益 −78百万円」「自己資本比率 9.9%」）。
# それなのに検査が1つも無く、値を10倍にしても・見出しだけにしても・
# unit を BANANA にしても、`run_checks` の出力は1文字も変わらなかった。
#
# 語彙は `fetch_tanshin` が正だが、**あえて import せず複製する**
# （import すると checks.py が requests / pypdf を持ち込みオフラインで動かなくなる。
# fundamentals の EXTRA_FLAGS と同じ判断）。ずれは「知らない status フラグ」
# WARN として必ず表に出る。

TANSHIN_FIELDS = ("date", "code", "metric", "value", "value_extracted", "unit",
                  "definition", "assumed", "source", "tier", "status",
                  "source_url", "fetched_at")
TANSHIN_UNITS = {"JPY", "JPY_thousand", "JPY_million", "JPY_billion", "pct", "x",
                 "shares"}
TANSHIN_TIERS = {"primary", "secondary"}
# 採用を止めるフラグ（立っていたら value が空でなければならない）
TANSHIN_BLOCKING = {"YOY_MISMATCH", "SCALE_SUSPECT", "OUT_OF_RANGE",
                    "SIGN_SUSPECT", "PERIOD_MISMATCH", "LABEL_UNSAFE"}
TANSHIN_INFO = {"OK", "NOT_CROSS_CHECKED", "YOY_CHECK_NA",
                "EPS_CROSS_OK", "EPS_CROSS_NA", "EPS_CROSS_FAILED",
                "EQUITY_CROSS_OK", "EQUITY_CROSS_NA", "EQUITY_CROSS_FAILED"}
# 「文書のなかで自己検算に失敗した」を表すフラグ。値は残るが、
# **図や本文で断定形に使ってよい値ではない**ので必ず表に出す。
TANSHIN_SELF_CHECK_FAILED = ("EPS_CROSS_FAILED", "EQUITY_CROSS_FAILED")
TANSHIN_LOG_FIELDS = ("disclosed_on", "code", "pdf_url", "status", "pages",
                      "text_chars", "metrics_written", "note", "fetched_at")
TANSHIN_LOG_STATUSES = {"OK", "DOWNLOAD_FAILED", "NOT_PDF", "PDF_ENCRYPTED",
                        "PDF_UNREADABLE", "PDF_IMAGE_ONLY", "SUMMARY_UNPARSED",
                        "NOT_FOUND"}
TANSHIN_VALUE_ABS_MAX = 1e9
# 短信の値と、まとめサイトの観測値を突き合わせる許容の下限。
# fundamentals 側の tolerance（表示解像度）を使い、無い行はここを使う。
TANSHIN_CROSS_MIN_TOL = 0.0


def _front_matter(path: Path) -> tuple[dict, str]:
    """レポートの front matter と本文。読めなければ ({}, 全文)。"""
    text = path.read_text(encoding="utf-8")
    m = _FRONT_MATTER_RE.match(text)
    if m is None:
        return {}, text
    try:
        meta = Y.safe_load(m.group(1)) or {}
    except yaml.YAMLError:
        return {}, m.group(2)
    return (meta if isinstance(meta, dict) else {}), m.group(2)


def _tanshin_cross(rep: Report, target: str, data_dir: Path, code: str,
                   rows: list[dict], cross_period: str) -> None:
    """決算短信（一次情報）と まとめサイト（二次情報）の突き合わせ（D33）。

    chartdata が同じ照合を**表示のためだけ**に行っていて、結果が
    「食い違い2件」に変わっても FAIL も WARN も出なかった。
    出所が独立した2つなので、食い違いは株価の MISMATCH と同格に扱う。

    突き合わせる相手は fundamentals の**観測値**（`sources_all`）であって
    採用値ではない。まとめサイト側が1サイトしか持たない期でも、
    一次情報と一致すればそれ自体が独立した2つの出所の一致になる。
    """
    if not parse_period(cross_period):
        rep.warn("tanshin", target,
                 f"tanshin_cross_period を期として読めない（{cross_period!r}）。"
                 "一次情報と二次情報の突き合わせを行っていない")
        return
    fpath = data_dir / "fundamentals" / f"{code}.csv"
    if not fpath.exists():
        rep.warn("tanshin", target,
                 "まとめサイト側の財務数値が無く、突き合わせられない")
        return
    # (metric, 単位, 許容) と観測値。**採用値ではなく観測値を見る。**
    obs: dict[str, dict] = {}
    found_period = False
    for r in load_csv(fpath):
        if str(r.get("period") or "").strip() != cross_period:
            continue
        found_period = True
        metric = str(r.get("metric") or "").strip()
        values = []
        for part in str(r.get("sources_all") or "").split("|"):
            if "=" in part:
                v = _f(part.rsplit("=", 1)[1])
                if v is not None:
                    values.append((part.rsplit("=", 1)[0].strip(), v))
        if values:
            obs[metric] = {"unit": r.get("unit"), "tol": _f(r.get("tolerance")),
                           "values": values}
    if not found_period:
        rep.warn("tanshin", target,
                 f"tanshin_cross_period に指定された期 {cross_period} が "
                 f"data/fundamentals/{code}.csv に無い（突き合わせていない）")
        return

    agree, disagree, nopair = [], [], []
    for r in rows:
        raw_metric = str(r.get("metric") or "").strip()
        # 前年同期・前期末・通期計画は**別の期の数値**。この期の照合に混ぜない。
        if raw_metric.endswith(("_prev_year", "_prev_fy", "_fy_plan")):
            continue
        value = _f(r.get("value"))
        if value is None:
            continue
        mine = unit_of(r.get("unit"), metric_of(raw_metric))
        pair = obs.get(raw_metric)
        theirs = unit_of(pair["unit"], metric_of(raw_metric)) if pair else None
        if pair is None or mine is None or theirs is None or mine[0] != theirs[0]:
            nopair.append(raw_metric)
            continue
        base = value * mine[1]
        tol = max(abs(pair["tol"] or 0.0) * theirs[1], TANSHIN_CROSS_MIN_TOL)
        got = [(name, v * theirs[1]) for name, v in pair["values"]]
        hit = [name for name, b in got if abs(b - base) <= tol + FUND_FLOAT_EPS]
        if hit:
            agree.append(raw_metric)
        else:
            shown = " / ".join(f"{n}={v}" for n, v in pair["values"])
            disagree.append(f"{raw_metric}: 短信 {value}{r.get('unit')} / "
                            f"まとめサイト {shown}{pair['unit']}")
    rep.group(FAIL, "tanshin", target,
              f"決算短信（一次情報）とまとめサイト（二次情報）が食い違う"
              f"[{cross_period}]", disagree)
    rep.group(WARN, "tanshin", target,
              f"決算短信にあるがまとめサイト側に相手が無い項目[{cross_period}]",
              sorted(set(nopair)))
    rep.warn("tanshin", target,
             f"一次情報と二次情報の突き合わせ[{cross_period}]: "
             f"一致 {len(agree)}件 / 食い違い {len(disagree)}件 / "
             f"相手なし {len(set(nopair))}件")


def check_tanshin(rep: Report, data_dir: Path, master: dict, reports_dir: Path,
                  facts_by_code: dict[str, list[Fact]]) -> None:
    """data/tanshin/{code}.csv（決算短信PDFから抜いた一次情報）の検査。"""
    tdir = data_dir / "tanshin"
    if not tdir.exists():
        return
    known = {str(s["code"]) for s in master.get("stocks", [])}
    files = sorted(p for p in tdir.glob("*.csv") if p.stem != "fetch_log")

    for path in files:
        code = path.stem
        target = f"data/tanshin/{code}.csv"
        rows = load_csv(path)
        if not rows:
            rep.warn("tanshin", target, "行が1つも無い")
            continue
        missing_cols = [c for c in TANSHIN_FIELDS if c not in rows[0]]
        if missing_cols:
            rep.fail("tanshin", target, f"必須列が欠落: {missing_cols}")
            continue

        check_duplicate(rep, rows, target, ("code", "date", "metric"))

        bad_date, bad_code, bad_unit, bad_tier, bad_status = [], [], [], [], []
        unknown_flag, blocked_with_value, no_def, bad_assumed = [], [], [], []
        no_src, oversize, self_failed, bad_value = [], [], [], []
        for r in rows:
            metric = str(r.get("metric") or "").strip()
            key = f"{r.get('date')} {metric}"
            if _d(r.get("date")) is None:
                bad_date.append(f"{key}（date={r.get('date')!r}）")
            if str(r.get("code")) != code or code not in known:
                bad_code.append(f"{key}（code={r.get('code')!r}）")
            if str(r.get("unit") or "").strip() not in TANSHIN_UNITS:
                bad_unit.append(f"{key}（unit={r.get('unit')!r}）")
            if str(r.get("tier") or "").strip() not in TANSHIN_TIERS:
                bad_tier.append(f"{key}（tier={r.get('tier')!r}）")
            flags = [p for p in str(r.get("status") or "").split("|") if p]
            if not flags:
                bad_status.append(f"{key}（status が空）")
            extra = [p for p in flags
                     if p not in TANSHIN_BLOCKING and p not in TANSHIN_INFO]
            if extra:
                unknown_flag.append(f"{key}（{extra}）")
            value = _f(r.get("value"))
            if _unreadable(r.get("value")):
                bad_value.append(f"{key}（value={r.get('value')!r}）")
            blocking = [p for p in flags if p in TANSHIN_BLOCKING]
            if blocking and value is not None:
                blocked_with_value.append(f"{key}（{blocking} なのに value={value}）")
            if value is not None and abs(value) > TANSHIN_VALUE_ABS_MAX:
                oversize.append(f"{key}（value={value}）")
            hit = [p for p in flags if p in TANSHIN_SELF_CHECK_FAILED]
            if hit:
                self_failed.append(f"{key}（{'/'.join(hit)}）")
            if _blank(r.get("definition")):
                no_def.append(key)
            if str(r.get("assumed") or "").strip() not in ("true", "false"):
                bad_assumed.append(f"{key}（assumed={r.get('assumed')!r}）")
            if _blank(r.get("source_url")) or _blank(r.get("fetched_at")):
                no_src.append(key)

        g = rep.group
        g(FAIL, "tanshin", target, "date が読めない", bad_date)
        g(FAIL, "tanshin", target, "code がファイル名またはマスタと一致しない", bad_code)
        g(FAIL, "tanshin", target, "unit が定義外", bad_unit)
        g(FAIL, "tanshin", target, "tier が定義外（primary / secondary のみ）", bad_tier)
        g(FAIL, "tanshin", target, "status が空", bad_status)
        g(FAIL, "tanshin", target, "value が数値として読めない", bad_value)
        g(FAIL, "tanshin", target,
          "検算に落ちたフラグが立っているのに値が残っている", blocked_with_value)
        g(FAIL, "tanshin", target, "definition が空（何の数字か特定できない）", no_def)
        g(FAIL, "tanshin", target, "assumed が true/false でない", bad_assumed)
        g(FAIL, "tanshin", target,
          "source_url または fetched_at が空（数値は必ず出所を伴う）", no_src)
        g(FAIL, "tanshin", target, "桁が大きすぎる（単位の取り違えの疑い）", oversize)
        g(WARN, "tanshin", target, "知らない status フラグ", unknown_flag)
        g(WARN, "tanshin", target,
          "PDF 内の自己検算に落ちている（値は残るが断定形で使わない）", self_failed)

        # レポートが宣言した突き合わせ期があれば、一次情報と二次情報を照合する。
        report = reports_dir / f"{code}.md"
        if report.exists():
            meta, _body = _front_matter(report)
            cross = str(meta.get("tanshin_cross_period") or "")
            if cross:
                _tanshin_cross(rep, target, data_dir, code, rows, cross)
            else:
                rep.warn("tanshin", target,
                         "レポートに tanshin_cross_period が無い"
                         "（一次情報と二次情報の突き合わせを行っていない）")

    # 取得ログ（1本の短信 = 1行）
    log = tdir / "fetch_log.csv"
    if not log.exists():
        if files:
            rep.warn("tanshin", "data/tanshin/fetch_log.csv",
                     "取得ログが無い（どの PDF をいつ読んだのか残っていない）")
        return
    rows = load_csv(log)
    target = "data/tanshin/fetch_log.csv"
    if not rows:
        rep.warn("tanshin", target, "行が1つも無い")
        return
    missing_cols = [c for c in TANSHIN_LOG_FIELDS if c not in rows[0]]
    if missing_cols:
        rep.fail("tanshin", target, f"必須列が欠落: {missing_cols}")
        return
    bad_status, bad_day, no_url, unreadable = [], [], [], []
    for r in rows:
        key = f"{r.get('code')} {r.get('disclosed_on')}"
        if str(r.get("status") or "").strip() not in TANSHIN_LOG_STATUSES:
            bad_status.append(f"{key}（status={r.get('status')!r}）")
        if _d(r.get("disclosed_on")) is None:
            bad_day.append(f"{key}（disclosed_on={r.get('disclosed_on')!r}）")
        if _blank(r.get("pdf_url")):
            no_url.append(key)
        if str(r.get("status") or "").strip() not in ("OK", ""):
            unreadable.append(f"{key}（{r.get('status')} {r.get('note')}）")
    g = rep.group
    g(FAIL, "tanshin", target, "status が定義外", bad_status)
    g(FAIL, "tanshin", target, "disclosed_on が読めない", bad_day)
    g(FAIL, "tanshin", target, "pdf_url が空（出所不明の数値は記録しない）", no_url)
    g(WARN, "tanshin", target,
      "PDF を読めなかった開示（黙って二次情報に落とさない）", unreadable)


# =============================================================================
# 13. レポートの数値 と 検証済み数値 の突合
# =============================================================================
#
# ここが今回の主眼。**レポートの財務数値は人間が目で転記したもの**で、
# 桁を取り違えても誰も気づかない状態だった。突合できたものは食い違えば FAIL、
# 突合できなかったものは「未突合」として件数を必ず出す（黙って許さない）。

@dataclass(frozen=True)
class Claim:
    """レポートが読者に見せている1つの数値。"""
    where: str
    metric: str | None
    period: Period | None
    value: float
    decimals: int
    family: str | None
    base: float | None
    unit_text: str


_FRONT_MATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n(.*)$", re.DOTALL)
_SEP_CELL_RE = re.compile(r"^:?-{2,}:?$")
_PROSE_NUM_RE = re.compile(
    r"[0-9][0-9,]*(?:\.[0-9]+)?\s*(?:億円|百万円|千円|兆円|円|%|％|倍)")
_URL_RE = re.compile(r"https?://[^\s<>\"'\)\]|｜]+")


def _claim(where: str, metric, period, value, decimals, unit_text) -> Claim:
    scale = unit_of(unit_text, metric)
    family = scale[0] if scale else None
    base = value * scale[1] if scale else None
    return Claim(where=where, metric=metric, period=period, value=float(value),
                 decimals=decimals, family=family, base=base,
                 unit_text=str(unit_text or ""))


def _claims_from_charts(meta: dict) -> list[Claim]:
    """front matter の charts。**グラフに描かれる数字はレポート本文と同格**。

    metric の対応づけは `charts.<id>.metric` の明示が最優先。無ければ chart id
    から引く（revenue_10y -> revenue）。引けなければ metric=None のまま
    「未突合」に数える。id からの推測で誤った metric に結び付けない。

    棒・折れ線は各点の `label` が期を表すが、progress / range 型の
    `done` `target` 等は期を持たない。突合したい場合は
    `done_period: "26/6 3Q累計"` のように `<key>_period` を書く
    （書かなければ「期を特定できない」として未突合に数える）。
    """
    out: list[Claim] = []
    charts = meta.get("charts") or {}
    if not isinstance(charts, dict):
        return out
    for cid in sorted(charts):
        chart = charts[cid] or {}
        if not isinstance(chart, dict):
            continue
        metric = metric_of(chart.get("metric") or "") or metric_of(cid)
        unit_text = chart.get("unit") or ""
        points = chart.get("data") or []
        if isinstance(points, list):
            for p in points:
                if not isinstance(p, dict):
                    continue
                v = p.get("value")
                if not isinstance(v, (int, float)) or isinstance(v, bool):
                    continue
                label = str(p.get("label") or "")
                where = f"charts.{cid}[{label}]"
                out.append(_claim(where, metric, parse_period(label),
                                  v, decimals_of(v), unit_text))
        default_period = parse_period(chart.get("period") or "")
        for key in ("done", "target", "low", "high", "current"):
            v = chart.get(key)
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                period = parse_period(chart.get(f"{key}_period") or "") or default_period
                out.append(_claim(f"charts.{cid}.{key}", metric, period,
                                  v, decimals_of(v), unit_text))
        markers = chart.get("markers") or []
        if isinstance(markers, list):
            for mk in markers:
                if not isinstance(mk, dict):
                    continue
                v = mk.get("value")
                if isinstance(v, (int, float)) and not isinstance(v, bool):
                    label = str(mk.get("label") or "")
                    out.append(_claim(f"charts.{cid}.markers[{label}]", metric,
                                      parse_period(label), v, decimals_of(v),
                                      unit_text))
    return out


def _chart_prose(meta: dict) -> list[str]:
    """front matter の説明文（caption / notes / *_label）を集める。

    D30 は「図の数値を front matter に手書きしない」を実現したが、
    **同じ front matter の説明文に埋まった数値は素通り**していた。
    caption は図の直下に、notes は図の中に描画される（「2020/6: 26.4億円 — 過去最高」）
    ので読者から見れば本文と同格。少なくとも散文の分母に入れる。
    """
    out: list[str] = []
    charts = meta.get("charts") or {}
    if not isinstance(charts, dict):
        return out
    for cid in sorted(charts):
        chart = charts[cid] or {}
        if not isinstance(chart, dict):
            continue
        for key, value in sorted(chart.items()):
            if key == "caption" or key.endswith("_label"):
                if isinstance(value, str):
                    out.append(value)
            elif key == "notes" and isinstance(value, dict):
                out += [str(v) for v in value.values()]
            elif key in ("data", "markers") and isinstance(value, list):
                for p in value:
                    if isinstance(p, dict) and isinstance(p.get("note"), str):
                        out.append(p["note"])
    return out


def _chart_overlay_problems(cid: str, chart: dict) -> list[str]:
    """図に重ねた装飾（`band` / `markers`）の書き方を見る。

    値そのものは CSV から引けない手書きなので、正しさは検査できない。
    検査できるのは **「書いたものが実際に描かれるか」** と
    **「読者に手書きだと分かる形になっているか」** の2つだけ。
    `chartdata._overlay_notes` が注記に「手書き（未検証）」を必ず出すので、
    ここでは黙って落ちる書き方（描かれない band・帯を描けない type）を拾う。
    """
    out: list[str] = []
    band = chart.get("band")
    if band is None:
        return out
    if not isinstance(band, (list, tuple)) or len(band) != 2:
        out.append(f"charts.{cid}.band が [下限, 上限] の2要素でない: {band!r}"
                   "（chart.render が黙って帯を落とす）")
        return out
    try:
        lo, hi = float(band[0]), float(band[1])
    except (TypeError, ValueError):
        out.append(f"charts.{cid}.band に数値でない値がある: {band!r}")
        return out
    if lo > hi:
        out.append(f"charts.{cid}.band の上下が逆: {band!r}")
    if str(chart.get("type", "")) != "line":
        out.append(f"charts.{cid}.band は type: line でしか描かれない"
                   f"（この図は type: {chart.get('type')!r}）")
    if not str(chart.get("band_label", "") or "").strip():
        out.append(f"charts.{cid}.band に band_label が無い"
                   "（何を表す帯か読者に分からない）")
    return out


def _chart_source_problems(meta: dict) -> list[str]:
    """`charts.<id>.source.periods` の並びと、手書き図の書き方の検査。

    数値そのものは CSV から引くので嘘にはならないが、**どの期をどのラベルで
    見せるか**は無検査だった。期を1つ差し替えると図の意味が変わる。
    手書き図に `metric:` が無いと、chartdata の注記が
    「矛盾するデータが2件ある」から「比べる相手が無い」に化けるので、それも拾う。

    `band`（参考帯）も見る。**帯の数値は CSV から引けない手書き**なのに、
    ここも chartdata も一切検査していなかった。`chart.render` は
    `len(band) == 2` でないと黙って帯を落とすので、書いたつもりで描かれていない
    ことに誰も気づけない。帯を描けるのは `type: line` だけである点も同じ。
    """
    out: list[str] = []
    charts = meta.get("charts") or {}
    if not isinstance(charts, dict):
        return out
    for cid in sorted(charts):
        chart = charts[cid] or {}
        if not isinstance(chart, dict):
            continue
        out += _chart_overlay_problems(cid, chart)
        source = chart.get("source")
        if not isinstance(source, dict):
            if chart.get("data") and not chart.get("metric"):
                out.append(f"charts.{cid}: 手書きの data: に metric: が無い"
                           "（検証状況を突き合わせられない）")
            continue
        periods = source.get("periods")
        if not isinstance(periods, list):
            continue
        keys = []
        for item in periods:
            if isinstance(item, str):
                keys.append(item)
            elif isinstance(item, dict) and item.get("period"):
                keys.append(str(item["period"]))
        dups = sorted({k for k in keys if keys.count(k) > 1})
        if dups:
            out.append(f"charts.{cid}.source.periods に同じ期が2回ある: {dups}")
        if keys != sorted(keys):
            out.append(f"charts.{cid}.source.periods が昇順でない: {keys}")
        for item in periods:
            if not isinstance(item, dict):
                continue
            label, period = item.get("label"), item.get("period")
            if not label or not period:
                continue
            lp, pp = parse_period(str(label)), parse_period(str(period))
            if lp is None or pp is None:
                continue
            # 「2026/6予」のように**ラベル側だけが会社計画**を表す書き方は正常
            # （期は実績と同じ FY キーで、計画かどうかは metric で分かれる）。
            # 見たいのは年・月がずれている場合だけ。
            if lp.plan or pp.plan:
                continue
            if lp.year != pp.year or (
                    lp.month is not None and pp.month is not None
                    and lp.month != pp.month):
                out.append(f"charts.{cid}: ラベル「{label}」と期 {period} が"
                           "食い違う（図の意味が変わる）")
    return out


def _price_facts(code: str, price_rows: list[dict]) -> list[Fact]:
    """採用終値（`close` が入っている行だけ）を突合できる Fact にする。

    本文は「上の表とレンジの図は、すべて2つの取得元で一致した終値である」と
    断定しているのに、`checks.py` から見ると「metric を対応づけられない」＝
    未突合だった。材料は `data/prices/daily.csv` にある。
    """
    out: list[Fact] = []
    for r in price_rows:
        if str(r.get("code") or "").strip() != code:
            continue
        close = _f(r.get("close"))
        if close is None:
            continue
        day = str(r.get("date") or "")
        period = parse_period(day)
        if period is None:
            continue
        raw = str(r.get("close"))
        out.append(Fact(code=code, metric="close", period=period,
                        status=str(r.get("status") or ""), adopted=True,
                        value=close,
                        decimals=len(raw.split(".")[1]) if "." in raw else 0,
                        family="money", base=close, unit_text="円",
                        raw_metric="close"))
    return out


def _markdown_tables(body: str) -> list[list[list[str]]]:
    """本文中の Markdown 表を (行 x セル) で取り出す。"""
    tables: list[list[list[str]]] = []
    cur: list[list[str]] = []
    for line in body.splitlines():
        s = line.strip()
        if s.startswith("|") and s.count("|") >= 2:
            cur.append([c.strip() for c in s.strip("|").split("|")])
            continue
        if len(cur) >= 2:
            tables.append(cur)
        cur = []
    if len(cur) >= 2:
        tables.append(cur)
    return tables


def _claims_from_tables(body: str) -> list[Claim]:
    """本文の表からセル単位で数値を拾う。

    metric は「行の先頭セル」または「列見出し」から、期も同じく行・列から引く。
    **両方が解決したセルだけを突合対象にする。** 行と列で別々の metric に
    当たった場合は曖昧として metric=None（未突合に数える）。
    """
    out: list[Claim] = []
    for table in _markdown_tables(body):
        sep = None
        for i, row in enumerate(table):
            if row and all(_SEP_CELL_RE.match(c or "-") for c in row):
                sep = i
                break
        if sep is None or sep == 0:
            continue
        header = table[sep - 1]
        for row in table[sep + 1:]:
            if not row:
                continue
            row_label = row[0]
            row_metric = metric_of(row_label)
            row_period = parse_period(row_label)
            for i in range(1, len(row)):
                num = cell_number(row[i])
                if num is None:
                    continue
                head = header[i] if i < len(header) else ""
                col_metric = metric_of(head)
                col_period = parse_period(head)
                metric = row_metric or col_metric
                if row_metric and col_metric and row_metric != col_metric:
                    metric = None
                period = row_period or col_period
                if metric is None and period is None:
                    continue          # 数値の入った散文セル。データではない
                value, decimals, unit_text = num
                if not unit_text:
                    unit_text = str(head or "")
                label = row_label or head
                out.append(_claim(f"表「{label}」", metric, period,
                                  value, decimals, unit_text))
    return out


def _quarter_alternatives(p: Period) -> list[Period]:
    """レポートの「26/6期 3Q」が、抽出側でどう表記されうるかの候補。

    レポートは「3Q」としか書かないが、抽出側は期間で持つ:
      - 累計（`C2025-07_2026-03`）… 3Qまでの累計。損益はこれ。
        **残高（自己資本比率など）も同じ期間キーの行に入る**
      - 四半期末の時点（`FY2026-03`）… 決算期末表記の表から来た残高

    どちらも「3Q時点の同じ数字」なので候補にする。決算期末が6月の会社に
    「3月期の通期」は存在しないため、同一銘柄の中でこの読み替えが
    別の期と衝突することはない。単独3か月（standalone）は別の量なので混ぜない。
    """
    if p.quarter is None or p.month is None or p.cumulative or p.standalone:
        return []
    out = [Period(year=p.year, month=p.month, quarter=p.quarter,
                  cumulative=True, plan=p.plan, text=p.text)]
    end = p.year * 12 + p.month - (4 - p.quarter) * 3
    year, month = _ym_from_index(end)
    out.append(Period(year=year, month=month, quarter=None, cumulative=False,
                      plan=p.plan, text=p.text))
    return out


def _match_fact(claim: Claim, facts: list[Fact]) -> tuple[str, Fact | None]:
    """claim に対応する Fact を1つに絞る。戻り値は (理由, Fact)。"""
    if claim.metric is None:
        return ("metric を対応づけられない", None)
    if claim.period is None:
        return ("期を特定できない", None)
    cands = [f for f in facts
             if f.metric == claim.metric and f.period.matches(claim.period)]
    for alt in _quarter_alternatives(claim.period) if not cands else []:
        cands = [f for f in facts
                 if f.metric == claim.metric and f.period.matches(alt)]
        if cands:
            break
    if not cands:
        return ("対応する検証済み数値が無い", None)
    adopted = [f for f in cands if f.adopted]
    if len(adopted) == 1:
        return ("", adopted[0])
    if len(adopted) > 1:
        return ("検証済み数値の候補が複数ある", None)
    return (f"検証が成立していない（status={cands[0].status}）", None)


def _check_handwritten_ranges(rep: Report, target: str, meta: dict) -> None:
    """手書き `data:` の値そのものに値域検査を掛ける。

    0〜100% の検査は `data/fundamentals` の採用値にしか効いていなかったため、
    手書き図の比率を 68.4 → 684.0 に書き換えても何も出なかった。
    手書きは「未検証」と表示されるが、**明らかな桁違いは表示だけで済ませない**。
    """
    charts = meta.get("charts") or {}
    if not isinstance(charts, dict):
        return
    bad = []
    for cid in sorted(charts):
        chart = charts[cid] or {}
        if not isinstance(chart, dict) or isinstance(chart.get("source"), dict):
            continue
        scale = unit_of(chart.get("unit"), metric_of(chart.get("metric") or ""))
        if scale is None or scale[0] != "pct":
            continue
        for p in chart.get("data") or []:
            if not isinstance(p, dict):
                continue
            v = p.get("value")
            if not isinstance(v, (int, float)) or isinstance(v, bool):
                continue
            base = v * scale[1]
            if base < FUND_RATIO_HARD_MIN or base > FUND_RATIO_HARD_MAX:
                bad.append(f"charts.{cid}[{p.get('label')}] {v}"
                           f"{chart.get('unit')}")
    rep.group(FAIL, "report", target,
              f"手書きの比率が {FUND_RATIO_HARD_MIN:.0f}〜{FUND_RATIO_HARD_MAX:.0f}% の"
              "外にある（桁違い）", bad)


_WEEK_ENTRY_RE = re.compile(r"^###\s+(\d{4}-W\d{2})\b", re.MULTILINE)


def _week_entries(text: str) -> list[tuple[str, str]]:
    """`## 週次アップデート` 節の `### YYYY-Www` を [(週, 本文)] に分解する。

    **dict にしない。** 同じ週に2回書く（「2026-W33」と「2026-W33（続報）」）ことが
    あり、dict だと後勝ちで潰れて、消えた側の書き換えを検出できなくなる。

    節の切り出しは report.py と同じ規則（`##` 見出しで分割）に揃える。
    ここで report をインポートしないのは、検査対象のパーサを検査に流用すると
    **両方が同じ勘違いをしたときに素通りする**ため（独立検算）。
    """
    body = ""
    for chunk in re.split(r"^##\s+(.+?)\s*$", text, flags=re.MULTILINE)[1:]:
        if "週次アップデート" in chunk:
            body = None            # 次のチャンクが本文
            continue
        if body is None:
            body = chunk
            break
    if not body:
        return []
    parts = _WEEK_ENTRY_RE.split(body)
    out: list[tuple[str, str]] = []
    for i in range(1, len(parts) - 1, 2):
        out.append((parts[i], parts[i + 1].strip()))
    return out


def check_report_updates_append_only(rep: Report, reports_dir: Path,
                                     repo_root: Path) -> None:
    """週次アップデートが append-only であることを git HEAD と突き合わせて確かめる。

    なぜ要るか
    ----------
    「読み返したときに、この会社をどう理解してきたかの軌跡が残る」ことが
    週次アップデートの目的（requirements §3-④）。上書きするとその目的が消える。
    `report.py` は「並べ替えたり削ったりしない」と不変条件を書いているが、
    **それは表示側の約束であって、書き手を縛るものが1つも無かった**。

    人間が毎週レポートを承認していた間はそこが歯止めだった。
    自動生成に切り替えるなら、歯止めを機械に移さないと外れたままになる。

    見るもの: HEAD にあった週の見出しが (a) 消えていないか (b) 本文が変わっていないか。
    新しい週の追加は当然許す。**新規レポート（HEAD に無い）は対象外**。
    """
    if not reports_dir.exists():
        return
    paths = sorted(reports_dir.glob("*.md"))
    if not paths:
        return
    top = _git(["git", "rev-parse", "--show-toplevel"], repo_root)
    if top.returncode != 0 or not top.stdout.strip():
        # 「検証できていない」を黙って通さない（追記性検査と同じ方針）。
        rep.warn("report", "reports/",
                 "git 管理下でないため週次アップデートの追記性を検証できていない")
        return
    toplevel = Path(top.stdout.strip())
    try:
        prefix = reports_dir.resolve().relative_to(toplevel.resolve()).as_posix()
    except ValueError:
        rep.warn("report", "reports/",
                 "reports/ がリポジトリの外にあり追記性を検証できていない")
        return

    checked = 0
    for path in paths:
        target = f"reports/{path.stem}.md"
        old = _git(["git", "show", f"HEAD:{prefix}/{path.name}"], toplevel)
        if old.returncode != 0:
            continue                      # HEAD に無い＝新規レポート
        before = _week_entries(old.stdout)
        if not before:
            continue
        checked += 1
        after = _week_entries(path.read_text(encoding="utf-8"))
        # HEAD にあった (週, 本文) の組が、そのまま残っているか。
        # 追加は自由・並び替えも許す（表示順は report.week_entries が決める）。
        remaining = list(after)
        for week, body in before:
            if (week, body) in remaining:
                remaining.remove((week, body))
                continue
            if any(w == week for w, _ in after):
                rep.fail("report", target,
                         f"週次アップデート {week} の本文が書き換わっている。"
                         "訂正は過去週を直すのではなく、"
                         f"新しい見出し（例 ### {week}（続報））を足して書く")
            else:
                rep.fail("report", target,
                         f"週次アップデート {week} が消えている。"
                         "過去の週は消さない（append-only）")
    if checked == 0:
        rep.warn("report", "reports/",
                 "HEAD に週次アップデートを持つレポートが無く、追記性を検証していない")


def check_report_numbers(rep: Report, reports_dir: Path,
                         facts_by_code: dict[str, list[Fact]],
                         master: dict | None = None,
                         price_rows: list[dict] | None = None) -> None:
    """reports/{code}.md の数値と data/fundamentals/{code}.csv の採用値を突き合わせる。

    食い違えば FAIL（人間の転記ミスか抽出ミスのどちらかが必ずある）。
    突合できなかったものは「未突合」として理由別に件数を出す。
    **1件も突合できていない状態を「異常なし」と表示しない**のがこの検査の要点。
    """
    if not reports_dir.exists():
        rep.warn("report", "reports/", "レポートのディレクトリが無い")
        return
    paths = sorted(reports_dir.glob("*.md"))
    if not paths:
        rep.warn("report", "reports/", "レポートが1件も無い")
        return

    # master の全銘柄を回してレポート不在を名指しする（株価側と同じ形）。
    # 4銘柄に対してレポートは1件だが、旧実装はそれを誰も指摘しなかった。
    have = {p.stem for p in paths}
    for code in sorted({str(s["code"]) for s in (master or {}).get("stocks", [])}
                       - have):
        rep.warn("report", f"reports/{code}.md",
                 f"{code}: レポートが無い（この銘柄の数値は1件も突合されていない）")

    for path in paths:
        code = path.stem
        target = f"reports/{code}.md"
        text = path.read_text(encoding="utf-8")
        m = _FRONT_MATTER_RE.match(text)
        if m is None:
            meta, body = {}, text
        else:
            try:
                meta = Y.safe_load(m.group(1)) or {}
            except yaml.YAMLError as e:
                rep.fail("report", target, f"front matter を読めない: {e}")
                continue
            body = m.group(2)
        if not isinstance(meta, dict):
            meta = {}

        claims = _claims_from_charts(meta) + _claims_from_tables(body)
        facts = list(facts_by_code.get(code, []))
        facts += _price_facts(code, price_rows or [])
        # 散文の分母は本文だけでなく **front matter の説明文も**含める。
        # caption / notes は図の中と直下に描画されるので読者から見れば本文と同格。
        caption_text = "\n".join(_chart_prose(meta))
        prose = len(_PROSE_NUM_RE.findall(body))
        caption_nums = len(_PROSE_NUM_RE.findall(caption_text))

        rep.group(WARN, "report", target,
                  "図の指定が読者の見え方と食い違う", _chart_source_problems(meta))
        _check_handwritten_ranges(rep, target, meta)

        if not claims:
            rep.warn("report", target,
                     "数値を1件も抽出できていない（抽出が壊れている疑い）")
            continue

        mismatched: list[str] = []
        # 表示桁の丸めでは説明できないが、相対スラックに隠れて FAIL にならない帯。
        near_miss: list[str] = []
        unmatched: dict[str, list[str]] = {}
        matched = 0
        for c in claims:
            reason, fact = _match_fact(c, facts)
            if fact is None:
                unmatched.setdefault(reason, []).append(c.where)
                continue
            if c.family is None or fact.family is None or c.family != fact.family:
                unmatched.setdefault("単位を揃えられない", []).append(c.where)
                continue
            matched += 1
            claim_h = half_ulp(c.decimals) * (abs(c.base / c.value) if c.value else 1.0)
            fact_h = half_ulp(fact.decimals) * (abs(fact.base / fact.value)
                                                if fact.value else 1.0)
            slack = max(fact_h, REPORT_VALUE_REL_SLACK * abs(fact.base))
            lo_c, hi_c = c.base - claim_h, c.base + claim_h
            lo_f, hi_f = fact.base - slack, fact.base + slack
            if lo_c > hi_f or lo_f > hi_c:
                period_label = fact.period.label()
                mismatched.append(
                    f"{c.where} {fact.metric} {period_label}: "
                    f"レポート {c.value}{c.unit_text} / "
                    f"検証済み {fact.value}{fact.unit_text}")
            elif abs(c.base - fact.base) > claim_h + fact_h:
                # 両者の表示桁を足しても届かない差。相対スラックが勝っているだけで、
                # 「一致した」とは言えない（万単位の値の数字の入れ替えがここに落ちる）。
                period_label = fact.period.label()
                near_miss.append(
                    f"{c.where} {fact.metric} {period_label}: "
                    f"レポート {c.value}{c.unit_text} / "
                    f"検証済み {fact.value}{fact.unit_text}")

        rep.group(FAIL, "report", target,
                  "レポートの数値が検証済みの採用値と食い違う"
                  "（転記ミスか抽出ミスのどちらか）", mismatched)
        rep.group(WARN, "report", target,
                  "レポートの数値が採用値と表示桁のぶんだけ食い違う"
                  f"（相対許容 {REPORT_VALUE_REL_SLACK:.1%} に隠れて FAIL にならない水準。"
                  "数字の入れ替えがここに落ちる）", near_miss)
        for reason in sorted(unmatched):
            rep.group(WARN, "report", target, f"未突合（{reason}）",
                      unmatched[reason])
        total_numbers = len(claims) + prose + caption_nums
        rep.warn("report", target,
                 f"レポートに出る数値のうち機械照合されているのは "
                 f"{matched}/{total_numbers}件。"
                 f"内訳: 突合 {matched} / 未突合 {len(claims) - matched}"
                 f"（抽出した数値 {len(claims)}件）/ 本文の散文 {prose} / "
                 f"front matter の説明文（caption・notes・ラベル） {caption_nums}。"
                 "散文と説明文は突合が届かないので data/verification/ が受け持つ")


# =============================================================================
# 14. 裏取り記録（別コンテキストの検証・F3）
# =============================================================================
#
# `check_report_numbers` が守れるのは**数値**だけである。レポートの大半は散文で、
# 4073 の場合その散文の中に85件の数値と、それより多い言明が入っている。
# 「導入180社以上」「アナリストのカバーは0社」「大型案件は来期計上」は、
# どれも CSV に対応する行を持たないので、突合検査からは完全に見えない。
#
# そこを埋めるのが `data/verification/{code}.yaml`（手順は
# `.claude/skills/kabu-ledger-verify/SKILL.md`）。本検査はその記録が
# **本当に裏取りとして成立しているか**を見る。記録があること自体は合格ではない。
#
#   FAIL にするもの（記録が裏取りとして成立していない／黙って誤りを残している）
#     - 出典と食い違うと判定した記述が、本文にそのまま残っている
#     - 再取得した記録が無いのに `supported` と判定している（判子だけ押した状態）
#     - 判定の根拠（evidence）が空
#     - レポートに書かれていないURLを叩いている（取得先を自律的に増やした）
#   WARN にするもの（人間が読んで直すもの。台帳にも印が出る）
#     - 裏が取れていない記述が本文に残っている（台帳に「未確認」と出る）
#     - 検証後にレポートが書き換えられている（記録が古い）
#     - そもそも裏取り記録が無い

def _verify_body_urls(text: str) -> set:
    return {u.rstrip(".,;:、。>）)") for u in _URL_RE.findall(text)}


def _fetch_log_urls(data_dir: Path, code: str) -> set:
    """`fetch_source.py` が残した取得ログのうち、その銘柄で到達できたURL。

    裏取り記録の自己申告（`http_status: 200`）を裏付ける唯一の材料。
    ログが無い＝**照合できない**（「異常なし」ではない）ので、
    呼び出し側は「裏付けが取れていない」と明示する。
    """
    path = data_dir / "verification" / "fetch_log.csv"
    if not path.exists():
        return set()
    out = set()
    for r in load_csv(path):
        if str(r.get("code") or "").strip() != code:
            continue
        status = _f(r.get("http_status"))
        if status is not None and 200 <= status < 400:
            out.add(str(r.get("url") or ""))
    return out


def check_verification(rep: Report, reports_dir: Path, data_dir: Path,
                       repo_root: Path | None = None) -> None:
    """裏取り記録の妥当性。**記録が無いことを「異常なし」と表示しない**。

    `repo_root` は `sources` に書かれたリポジトリ内ファイル（`data/…` `src/…`）を
    探す基点。旧実装は `data_dir.parent` 固定だったため、`--data-dir` を別の場所に
    向けると実在するファイルまで FAIL していた。
    """
    if not reports_dir.exists():
        return
    paths = sorted(reports_dir.glob("*.md"))
    if not paths:
        return
    root = repo_root if repo_root is not None else data_dir.parent

    for path in paths:
        code = path.stem
        source = f"reports/{code}.md"
        target = f"data/verification/{code}.yaml"
        text = path.read_text(encoding="utf-8")

        rec = VF.load(code, data_dir)
        if rec is None:
            rep.warn("verify", source,
                     "裏取り記録が無い（本文の記述を出典に当て直していない。"
                     "台帳には『未検証』と出る）")
            continue
        for p in rec.problems:
            rep.fail("verify", target, f"記録が壊れている: {p}")
        run = rec.latest
        if run is None:
            continue

        ids = [r.run for r in rec.runs]
        if ids != sorted(ids):
            rep.fail("verify", target,
                     "runs が実行時刻の昇順に並んでいない（append-only が崩れている）")
        if len(set(ids)) != len(ids):
            rep.fail("verify", target, "同じ run が2つある")
        if not run.run:
            rep.fail("verify", target, "最新の run に実行時刻が無い")

        # 最新 run が前 run の claim を拾い直しているか（SKILL.md「全量スナップショット」）。
        # 拾い直されなかった指摘は `folded()` が持ち越すので消えはしないが、
        # **消えかけたこと自体**を出す。
        if len(rec.runs) >= 2:
            prev_ids = {c.id for c in rec.runs[-2].claims}
            now_ids = {c.id for c in run.claims}
            lost = sorted(prev_ids - now_ids)
            rep.group(WARN, "verify", target,
                      "最新の run が前回の claim を拾い直していない"
                      "（最新 run は本文の全量スナップショットであること）", lost)

        # --- 記録と本文の対応 -------------------------------------------------
        now_sha = VF.sha256(text)
        if not run.report_sha256:
            rep.fail("verify", target,
                     "report_sha256 が無い（どの本文を検証したのか特定できない）")
        elif run.report_sha256 != now_sha:
            rep.warn("verify", target,
                     f"検証後にレポートが書き換えられている"
                     f"（記録 {run.report_sha256[:12]} / 現在 {now_sha[:12]}）。"
                     "この記録は現在の本文を保証しない")

        # --- 再取得の実体 -----------------------------------------------------
        body_urls = _verify_body_urls(text)
        fetched = run.fetched_ok()
        # 出典URLを1件も叩いていない run は、原則として裏取りではない。
        # ただし「本文の修正を検証済みデータに当て直しただけ」の部分再検証は
        # 正当にあり得るので、**全 claim が dataset 由来のときだけ** WARN に落とす。
        # 外向きの出典に依る claim が1件でもあれば FAIL のまま。
        if not run.urls:
            web_backed = [c.id for c in run.claims if c.tier != "dataset"]
            if web_backed:
                rep.fail("verify", target,
                         "urls_refetched が空なのに、出典に依る判定がある"
                         f"（{len(web_backed)}件）。出典を取りに行っていない")
            else:
                rep.warn("verify", target,
                         "出典URLを1件も取り直していない"
                         "（検証済みデータだけの部分再検証）")
        # 「記録の再取得URLが本文に無い」は **WARN**。実際に起きるのは
        # 「人がレポートを編集して出典を差し替えた」であって「取得先を勝手に増やした」
        # ではない。裏取りは翌週まで走らない（weekly.yml は data → verify）ので、
        # FAIL にすると **編集した週から次の verify まで data ジョブが落ち続ける**。
        # 取得先の制限は取得時点（fetch_source.py が本文記載URL以外を拒否する）で担保する。
        outside = sorted(u for u, _ in run.urls if u and u not in body_urls)
        rep.group(WARN, "verify", target,
                  "記録の再取得URLが現在の本文に無い（出典が差し替えられたか、記録が古い）。"
                  "取得先の制限は fetch_source.py が取得時点で担保している",
                  outside)
        unreachable = sorted(f"{u}（HTTP {s}）" for u, s in run.urls
                             if u and u not in fetched)
        rep.group(WARN, "verify", target,
                  "再取得したが到達できなかった出典", unreachable)

        # ★「実際に叩いた」ことの機械的な裏付け。
        #   `urls_refetched[].http_status: 200` は検証者が YAML に書いた文字列で
        #   あって取得の痕跡ではない。ネットワークに一切触れずに run を捏造できる。
        #   `fetch_source.py` が残す追記ログと突き合わせる。
        #   （ログ導入前の run は照合できないので WARN に留める）
        logged = _fetch_log_urls(data_dir, code)
        if logged:
            unlogged = sorted(u for u, _ in run.urls
                              if u and u not in logged)
            rep.group(WARN, "verify", target,
                      "再取得したと記録されているが、fetch_source.py の取得ログに"
                      "その URL が無い（叩いた痕跡が無い）", unlogged)
        elif run.urls:
            rep.warn("verify", target,
                     "取得ログ（data/verification/fetch_log.csv）にこの銘柄の行が無い。"
                     "**再取得したという記録は自己申告のままで、裏付けが取れていない**")

        delegated = {u for u, _ in run.delegated if u}
        # 委譲先は**リポジトリ内の検証済みデータ**でなければならない。
        # レポート本文や存在しないファイルへの委譲を通すと、
        # 「機械抽出が担当している」と言うだけで出典の再取得を免れる。
        missing_to = sorted(
            f"{u} → {to or '（空）'}" for u, to in run.delegated
            if u and (not to or not to.startswith("data/")
                      or not (root / to).exists()))
        rep.group(FAIL, "verify", target,
                  "委譲先（urls_delegated.to）が data/ 配下の実在ファイルでない",
                  missing_to)
        untouched = sorted(u for u in body_urls
                           if u not in fetched and u not in delegated)
        rep.group(WARN, "verify", target,
                  "本文の出典URLのうち、再取得も委譲もされていないもの", untouched)

        # --- 判定1件ずつ ------------------------------------------------------
        #
        # ★claim は **id で畳んで最新の判定**を見る（`Record.folded`）。
        #   最新 run しか見ないと、claim 1件だけの run を足すだけで過去の指摘が
        #   全部消える。判定の根拠（sources が再取得済みか）は、
        #   **その判定を出した run** の再取得記録で見る。
        bad_verdict, bad_tier, no_evidence, no_quote = [], [], [], []
        no_source, unfetched_source, missing_local = [], [], []
        dropped, kept_fatal, kept_marked, unresolved_fatal = [], [], [], []
        bad_resolution = []
        resolutions = {r.id: r for r in rec.resolutions}
        folded = rec.folded()
        for c, owner in folded:
            if c.verdict not in VF.VERDICTS:
                bad_verdict.append(f"{c.id} verdict={c.verdict or '空'}")
                continue
            if c.tier not in VF.TIERS:
                bad_tier.append(f"{c.id} tier={c.tier or '空'}")
            if not c.evidence:
                no_evidence.append(c.id)
            if not c.quote:
                no_quote.append(c.id)
            elif c.verdict in VF.FATAL_IF_KEPT:
                # ★`contradicted` は「本文から消えた」だけでは解除しない。
                #   quote の完全一致に乗せていたため、主張を残したまま一語変えれば
                #   FAIL が消えていた（「直した」と「言い回しを変えた」を機械が
                #   区別できていなかった）。解除には次のどちらかが要る:
                #     (a) 後続の run が同じ id を非 fatal に再判定した（folded が拾う）
                #     (b) resolutions に始末の記録があり、かつそれが本文と整合する
                res = resolutions.get(c.id)
                if c.quote in text:
                    kept_fatal.append(f"{c.id}「{c.quote[:32]}」{c.action or ''}")
                elif res is None:
                    unresolved_fatal.append(
                        f"{c.id}「{c.quote[:32]}」"
                        "（本文から消えているが始末の記録が無い）")
                elif res.how not in VF.RESOLUTION_HOWS:
                    bad_resolution.append(f"{c.id} how={res.how or '空'}")
                elif res.how == "rewritten" and (
                        not res.quote or res.quote not in text):
                    bad_resolution.append(
                        f"{c.id}: how=rewritten だが、書き直した本文"
                        "（quote）が現在の本文に無い")
            elif c.quote not in text:
                dropped.append(f"{c.id}「{c.quote[:24]}」")
            elif c.verdict in VF.MARKED_IF_KEPT:
                kept_marked.append(f"{c.id}「{c.quote[:32]}」({c.verdict})")

            if c.verdict == "unverifiable":
                continue                     # 出典が無いこと自体が判定内容
            if not c.sources:
                no_source.append(f"{c.id}（{c.verdict}）")
                continue
            owner_fetched = owner.fetched_ok()
            for s in c.sources:
                if VF.is_local_source(s):
                    if not (root / s).exists():
                        missing_local.append(f"{c.id} {s}")
                elif s not in owner_fetched:
                    unfetched_source.append(f"{c.id} {s}")

        stray = sorted(r.id for r in rec.resolutions
                       if r.id and r.id not in {c.id for c, _ in folded})
        rep.group(FAIL, "verify", target,
                  f"verdict が語彙外（正: {'/'.join(sorted(VF.VERDICTS))}）", bad_verdict)
        rep.group(FAIL, "verify", target,
                  f"tier が語彙外（正: {'/'.join(sorted(VF.TIERS))}）", bad_tier)
        rep.group(FAIL, "verify", target,
                  "evidence が空（何を見て判定したのかが残っていない）", no_evidence)
        rep.group(FAIL, "verify", target,
                  "quote が空（本文のどこを検証したのか特定できない）", no_quote)
        rep.group(FAIL, "verify", target,
                  "根拠（sources）が無いのに判定を出している", no_source)
        rep.group(FAIL, "verify", target,
                  "再取得できていない出典を根拠にしている"
                  "（叩いていないURLで裏付けたことにしない）", unfetched_source)
        rep.group(FAIL, "verify", target,
                  "根拠に挙げたリポジトリ内のファイルが無い", missing_local)
        rep.group(FAIL, "verify", target,
                  "出典と食い違うと判定した記述が本文にそのまま残っている"
                  "（落とすか直すこと）", kept_fatal)
        rep.group(FAIL, "verify", target,
                  "出典と食い違うと判定された記述の始末が記録されていない"
                  "（resolutions に how: removed / rewritten を書く。"
                  "言い回しを変えただけで解除しない）", unresolved_fatal)
        rep.group(FAIL, "verify", target,
                  "resolutions の書き方が本文と整合しない", bad_resolution)
        rep.group(WARN, "verify", target,
                  "resolutions に、対応する claim が無い id がある", stray)
        rep.group(WARN, "verify", source,
                  "裏が取れていない記述が本文に残っている（台帳に『未確認』と出る）",
                  kept_marked)
        rep.group(WARN, "verify", target,
                  "記録の quote が本文に無い（記述が落とされたか、記録が古い）", dropped)

        counts: dict[str, int] = {k: 0 for k in VF.VERDICTS}
        for c, _owner in folded:
            if c.verdict in counts:
                counts[c.verdict] += 1
        passed = sum(1 for c, _ in folded if c.passed)
        total = len(folded)
        if total == 0:
            rep.fail("verify", target, "claims が空（1件も検証していない）")
            continue
        detail = "／".join(f"{VF.VERDICTS[k]} {counts[k]}"
                           for k in VF.VERDICTS if counts[k])
        stale_note = ("。**本文が書き換わっているのでこの件数は現在の本文に"
                      "適用できない**" if run.report_sha256 and
                      run.report_sha256 != now_sha else "")
        rep.warn("verify", target,
                 f"裏取り {passed}/{total}件が裏付けあり（{detail}）"
                 f"・再取得 {len(fetched)}URL・委譲 {len(delegated)}URL"
                 f"{stale_note}")


# =============================================================================
# 15. 出典URLの死活監視
# =============================================================================
#
# ネットワークを使うので **既定はオフ**。checks.py は CI の停止判定に使われるため、
# 外部サイトの一時的な不調でビルドを止めない（`--check-links` で明示的に有効化）。
# オフのときは「確認していない」ことと、前回の記録で死んでいたURLを表に出す。

def collect_report_urls(reports_dir: Path) -> list[tuple[str, str]]:
    """reports/*.md から (code, url) を集める。front matter の links も本文も見る。"""
    out: set[tuple[str, str]] = set()
    if not reports_dir.exists():
        return []
    for path in sorted(reports_dir.glob("*.md")):
        text = path.read_text(encoding="utf-8")
        for raw in _URL_RE.findall(text):
            url = raw.rstrip(".,;:、。>）)")
            out.add((path.stem, url))
    return sorted(out)


def _load_link_status(data_dir: Path) -> dict[str, dict]:
    """URLごとの**最後の**記録を返す（append-only なので checked_at の最大）。"""
    path = data_dir / "link_status.csv"
    if not path.exists():
        return {}
    last: dict[str, dict] = {}
    for r in load_csv(path):
        url = str(r.get("url") or "")
        cur = last.get(url)
        if cur is None or str(r.get("checked_at") or "") >= str(cur.get("checked_at") or ""):
            last[url] = r
    return last


def _append_link_status(data_dir: Path, rows: list[dict]) -> None:
    path = data_dir / "link_status.csv"
    write_header = not path.exists()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(LINK_STATUS_FIELDS))
        if write_header:
            w.writeheader()
        w.writerows(rows)


def _probe(requests, url: str, user_agent: str) -> tuple[int | None, str]:
    """URL を1件叩いて (HTTPステータス, 備考) を返す。例外は握って備考に残す。"""
    headers = {"User-Agent": user_agent}
    try:
        r = requests.head(url, headers=headers, timeout=LINK_TIMEOUT_SEC,
                          allow_redirects=True)
        if r.status_code >= 400:
            # HEAD を受けないサイトがある（405/403）。GET で確かめ直す
            r = requests.get(url, headers=headers, timeout=LINK_TIMEOUT_SEC,
                             allow_redirects=True, stream=True)
            r.close()
        return r.status_code, ""
    except Exception as e:                                    # noqa: BLE001
        return None, f"{type(e).__name__}: {e}"


def check_links(rep: Report, reports_dir: Path, data_dir: Path,
                sources: dict, enabled: bool) -> None:
    """出典URLの到達性。404・接続失敗は WARN（記述は残す。リンクだけ死ぬことがある）。"""
    pairs = collect_report_urls(reports_dir)
    if not pairs:
        return
    last = _load_link_status(data_dir)

    if not enabled:
        dead = []
        for code, url in pairs:
            rec = last.get(url)
            if rec is None:
                continue
            if str(rec.get("reachable") or "").strip().lower() != "true":
                note = str(rec.get("note") or "")
                status = str(rec.get("http_status") or "")
                dead.append(f"{code} {url}（前回 {rec.get('checked_at')} / "
                            f"HTTP {status} {note}）")
        rep.group(WARN, "links", "data/link_status.csv",
                  "前回の記録で到達できなかった出典URL", dead)
        never = [f"{c} {u}" for c, u in pairs if u not in last]
        rep.group(WARN, "links", "data/link_status.csv",
                  "一度も死活確認していない出典URL（--check-links で確認する）", never)
        return

    # requests は既定の経路では読み込まない（checks.py をオフラインでも動かす）。
    try:
        import requests
    except ImportError:
        rep.warn("links", "reports/",
                 "requests が無いため死活確認を実行できていない（未確認）")
        return
    try:
        import truststore
        truststore.inject_into_ssl()
    except ImportError:
        pass
    from datetime import datetime, timedelta, timezone

    pol = (sources or {}).get("fetch_policy") or {}
    user_agent = pol.get("user_agent") or "kabu-ledger/1.0"
    now = datetime.now(timezone(timedelta(hours=9))).isoformat()

    records, broken, unreachable = [], [], []
    for code, url in pairs:
        status, note = _probe(requests, url, user_agent)
        reachable = status is not None and status < 400
        records.append({"checked_at": now, "code": code, "url": url,
                        "http_status": "" if status is None else status,
                        "reachable": "true" if reachable else "false",
                        "note": note})
        if status in LINK_DEAD_CODES:
            broken.append(f"{code} {url}（HTTP {status}）")
        elif not reachable:
            label = f"HTTP {status}" if status is not None else note
            unreachable.append(f"{code} {url}（{label}）")
        time.sleep(LINK_INTERVAL_SEC)

    _append_link_status(data_dir, records)
    rep.group(WARN, "links", "reports/",
              "出典URLがリンク切れ（404/410）", broken)
    rep.group(WARN, "links", "reports/",
              "出典URLに到達できない（403・タイムアウト等。記述は残す）", unreachable)


# =============================================================================
# 16. 判定スタンプ（notify.py の唯一の入力）
# =============================================================================
#
# `scoring/stamps.json` は notify.py が「先週と何が変わったか」を見る唯一の入力。
# ところが v2.0 改稿で **書く側（build.py）が丸ごと落ちていた**。
# ファイルは誰かが置いた値のまま凍り、`changed = {}` が永久に続いて
# 判定が「見送」から「買」に変わっても Issue が出ない状態だった。
# しかも非空なので notify.py の警告経路にも入らず、`起票 0 件 / 変化 0 件` が
# 「今週は変化なし」に見えていた。**同じ黙り方を再発させないために検査を置く。**

def check_stamps(rep: Report, data_dir: Path, master: dict) -> None:
    path = data_dir.parent / "scoring" / "stamps.json"
    target = "scoring/stamps.json"
    known = {str(s["code"]) for s in master.get("stocks", [])}
    if not path.exists():
        rep.warn("stamps", target,
                 "判定スタンプが無い（notify.py は状態を更新せずに終了する＝"
                 "判定が変わっても Issue が出ない）")
        return
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError) as e:
        rep.fail("stamps", target, f"JSON として読めない: {e}")
        return
    if not isinstance(data, dict) or not data:
        rep.fail("stamps", target,
                 "判定スタンプが空（build.py が出力していない）。"
                 "notify.py はこの状態で「変化なし」と表示する")
        return
    got = {str(k) for k in data}
    extra = sorted(got - known)
    lacking = sorted(known - got)
    if extra:
        rep.fail("stamps", target, f"マスタに無い銘柄がある: {extra}")
    if lacking:
        rep.fail("stamps", target,
                 f"マスタにあるのにスタンプが無い銘柄: {lacking}"
                 "（判定が落ちている）")
    empty = sorted(k for k, v in data.items() if not str(v or "").strip())
    if empty:
        rep.fail("stamps", target, f"スタンプが空の銘柄: {empty}")


# =============================================================================
# 実行
# =============================================================================

def run_checks(data_dir: Path, baseline: Baseline | None,
               scan_all: bool = False, reports_dir: Path | None = None,
               check_links_flag: bool = False,
               repo_root: Path | None = None) -> Report:
    """全検査を実行して Report を返す。I/O はここと各 check の入口に閉じる。"""
    rep = Report()
    master = Y.safe_load((data_dir / "master.yaml").read_text(encoding="utf-8"))
    sources = Y.safe_load((data_dir / "sources.yaml").read_text(encoding="utf-8"))

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
    check_source_independence(rep, rows, target, sources)
    check_freshness(rep, rows, target)

    price_dates = {str(r.get("date")) for r in rows}
    price_latest = max(price_dates) if price_dates else None
    check_margin(rep, data_dir, master, price_latest)
    check_indices(rep, data_dir, sources, price_dates, price_latest, scan_all)
    check_kpi(rep, data_dir, master)

    reports = reports_dir if reports_dir is not None else data_dir.parent / "reports"
    facts = check_fundamentals(rep, data_dir, master, sources, price_latest)
    check_tanshin(rep, data_dir, master, reports, facts)
    check_report_numbers(rep, reports, facts, master, rows)
    check_report_updates_append_only(
        rep, reports, repo_root if repo_root is not None else ROOT)
    check_verification(rep, reports, data_dir,
                       repo_root if repo_root is not None else ROOT)
    check_links(rep, reports, data_dir, sources, check_links_flag)
    check_stamps(rep, data_dir, master)
    check_revisions(rep, data_dir)
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
    ap.add_argument("--reports-dir", type=Path, default=None,
                    help="銘柄レポートのディレクトリ（既定: <data-dir>/../reports）")
    ap.add_argument("--repo-root", type=Path, default=None,
                    help="裏取り記録の sources に書かれたリポジトリ内ファイル"
                         "（data/… src/…）を探す基点（既定: このファイルの上位）")
    ap.add_argument("--check-links", action="store_true",
                    help="出典URLに実際にアクセスして死活を確認する"
                         "（既定オフ。外部要因でビルドを止めないため）")
    ap.add_argument("--verify-only", action="store_true",
                    help="裏取り記録（data/verification/）だけを検査する。"
                         "master.yaml を読まないので、保有情報を渡さない"
                         "隔離ジョブ（weekly.yml の verify）から自己点検に使える")
    ap.add_argument("--json", action="store_true", help="機械可読出力")
    args = ap.parse_args(argv)

    data_dir = args.data_dir.resolve()
    baseline = None
    if args.verify_only:
        # 隔離ジョブ用。`data/master.yaml` には買値・買付日が入る（D18）ので、
        # 裏取りの文脈には渡さない。ここは master.yaml を一切読まない経路。
        rep = Report()
        reports = (args.reports_dir if args.reports_dir is not None
                   else data_dir.parent / "reports")
        check_verification(rep, reports, data_dir,
                           args.repo_root or ROOT)
    else:
        baseline = resolve_baseline(data_dir, args.baseline, not args.no_git)
        rep = run_checks(data_dir, baseline, args.scan_all,
                         args.reports_dir, args.check_links, args.repo_root)

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
    if not Y.USING_LIBYAML:
        print("[WARN] pyyaml が libyaml 無しで入っている。YAML の解析が"
              "純 Python 経路になり、検査もテストも約2倍遅い")
    print(f"\nFAIL {rep.fails} / WARN {rep.warns}")
    return 1 if rep.fails else 0


if __name__ == "__main__":
    sys.exit(main())
