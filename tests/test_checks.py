"""checks.py のテスト。

方針:
  - **「検査を書いた」ことと「その検査が効いている」ことは別**。全 FAIL/WARN について
    「その検査だけが反応する壊し方」を実データのコピーに作り、反応することを確認する。
  - 壊していない実データ（4銘柄×269営業日・1076行）で FAIL 0 になることを確認する。
    誤検知する検査は、毎週 FAIL を出してビルドを止めるので実質的に使えない。
  - 壊し方は「起こりうる形」で作る。存在しない列を消すのではなく、
    セレクタがずれて高安が入れ替わる・過去行が書き換わる・1銘柄だけ取得漏れる、など。

実行:
  $env:PYTHONIOENCODING = "utf-8"; python tests/test_checks.py
"""
from __future__ import annotations

import csv
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import checks  # noqa: E402

_TMPDIRS: list[Path] = []
_REAL_SUMMARY: list[str] = []


# =============================================================================
# ヘルパ
# =============================================================================

def make_data(mutate=None) -> Path:
    """実データの data/ を一時ディレクトリに複製し、mutate で壊す。"""
    base = Path(tempfile.mkdtemp(prefix="kabu-checks-"))
    _TMPDIRS.append(base)
    dst = base / "data"
    shutil.copytree(ROOT / "data", dst)
    if mutate is not None:
        mutate(dst)
    return dst


def read_csv(path: Path):
    with path.open(encoding="utf-8-sig", newline="") as f:
        r = csv.DictReader(f)
        return list(r.fieldnames or []), list(r)


def write_csv(path: Path, fields, rows) -> None:
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)


def edit_prices(data_dir: Path, fn) -> None:
    """daily.csv を読み、fn(rows) の戻り値（または破壊的変更）を書き戻す。"""
    p = data_dir / "prices" / "daily.csv"
    fields, rows = read_csv(p)
    out = fn(rows)
    write_csv(p, fields, rows if out is None else out)


def run(data_dir: Path, baseline_dir: Path | None = None,
        scan_all: bool = False) -> checks.Report:
    baseline = None if baseline_dir is None \
        else checks._dir_baseline(data_dir, baseline_dir)
    return checks.run_checks(data_dir, baseline, scan_all)


def hits(rep: checks.Report, level: str, check: str, needle: str) -> list:
    return [r for r in rep.results
            if r.level == level and r.check == check
            and (needle in r.message or needle in r.target)]


def expect(rep: checks.Report, level: str, check: str, needle: str) -> None:
    got = hits(rep, level, check, needle)
    assert got, (f"{level}/{check} に {needle!r} を含む結果が無い。"
                 f"実際: {[r.line() for r in rep.results]}")


def expect_none(rep: checks.Report, level: str, check: str, needle: str = "") -> None:
    got = hits(rep, level, check, needle)
    assert not got, f"{level}/{check} が出てはいけない: {[r.line() for r in got]}"


def expect_no_fail(rep: checks.Report) -> None:
    fails = [r.line() for r in rep.results if r.level == checks.FAIL]
    assert not fails, f"FAIL が出てはいけない: {fails}"


def price_rows(rows, code):
    return sorted([r for r in rows if r["code"] == code], key=lambda r: r["date"])


def latest_date(code: str | None = None) -> str:
    """実データの最新営業日を返す。

    このテスト群は実データを複製して壊す方式なので、期待値に日付を
    べた書きすると **データが1行増えるたびに落ちる**（週次で必ず壊れる）。
    期待値はここから引いてデータに追随させる。
    """
    _, rows = read_csv(ROOT / "data" / "prices" / "daily.csv")
    if code:
        rows = [r for r in rows if r["code"] == code]
    return max(r["date"] for r in rows)


def last_ok(rows, code):
    """status が OK（2ソース照合が成立）の最後の行を返す。

    最新営業日は、片方の取得元がまだ当日分を出していないために
    SINGLE_SOURCE になることがある（minkabu は翌日に載る）。
    「照合が成立した行」を前提にする検査は、最終行ではなくここから取る。
    """
    rs = [r for r in price_rows(rows, code) if r["status"] == "OK"]
    assert rs, f"{code} に status=OK の行が無い"
    return rs[-1]


def close_of(row) -> float:
    """行の価格を取る。

    `close` は「2ソースが一致したときだけ入る採用値」なので、照合が
    成立しなかった日は空になる。最終行がたまたまその状態でも壊れないよう
    `value_primary` にフォールバックする。
    """
    return float(row["close"] or row["value_primary"])


def set_price(row, value: float) -> None:
    """1行の価格系をすべて value に揃える（他の検査を巻き込まずに値だけ動かす）。"""
    for col in ("open", "high", "low", "close", "value_primary", "value_secondary"):
        if row.get(col) not in (None, ""):
            row[col] = f"{value}"


# =============================================================================
# 0. 実データ（壊していない）
# =============================================================================

def test_real_data_has_no_fail():
    """実データ 1076行で FAIL 0。誤検知する検査は毎週ビルドを止めるので使えない。"""
    rep = run(ROOT / "data")
    expect_no_fail(rep)
    _REAL_SUMMARY.append(f"既定走査   : FAIL {rep.fails} / WARN {rep.warns}")
    for r in rep.results:
        _REAL_SUMMARY.append(f"  {r.line()}")


def test_real_data_scan_all_has_no_fail():
    """全履歴を走査しても FAIL 0（分割相当の下落が履歴に無いことの確認）。"""
    rep = run(ROOT / "data", scan_all=True)
    expect_no_fail(rep)
    _REAL_SUMMARY.append(f"--scan-all : FAIL {rep.fails} / WARN {rep.warns}")


def test_real_data_outlier_is_actually_evaluated():
    """外れ値検定が「12点未満で検定不能」に落ちず、実際に評価されている。"""
    rep = run(ROOT / "data")
    expect_none(rep, checks.WARN, "outlier", "検定できない")
    expect(rep, checks.WARN, "outlier", "σ")


def test_real_data_no_trade_is_visible():
    """出来高0の7日が WARN として見えている（異常にはしない）。"""
    rep = run(ROOT / "data")
    expect(rep, checks.WARN, "no_trade", "4937: 出来高0（売買不成立）が 6日")
    expect(rep, checks.WARN, "no_trade", "3851: 出来高0（売買不成立）が 1日")


def test_real_data_index_all_single_source_is_not_fail():
    """growth250 は全行 close 空。既知の仕様であり FAIL にしない（が WARN で見える）。"""
    rep = run(ROOT / "data")
    expect_none(rep, checks.FAIL, "index")
    expect(rep, checks.WARN, "missing", "growth250 SINGLE_SOURCE: 269日")


# =============================================================================
# 1. append-only（過去行の改変・削除）
# =============================================================================

def test_append_only_detects_modified_past_row():
    baseline = make_data()
    target = make_data(lambda d: edit_prices(
        d, lambda rows: [dict(r, close="99999.0") if
                         (r["code"] == "4073" and r["date"] == "2025-09-01") else r
                         for r in rows]))
    rep = run(target, baseline_dir=baseline)
    expect(rep, checks.FAIL, "append_only", "過去行が変更されている")
    expect(rep, checks.FAIL, "append_only", "4073/2025-09-01")


def test_append_only_detects_deleted_past_row():
    baseline = make_data()
    target = make_data(lambda d: edit_prices(
        d, lambda rows: [r for r in rows
                         if not (r["code"] == "4073" and r["date"] == "2025-09-01")]))
    rep = run(target, baseline_dir=baseline)
    expect(rep, checks.FAIL, "append_only", "過去行が削除されている")


def test_append_only_detects_margin_rewrite():
    """検査対象は daily.csv だけではない。信用残の過去行も守る。"""
    def mutate(d: Path) -> None:
        p = d / "margin" / "3851.csv"
        fields, rows = read_csv(p)
        rows[0]["long_balance"] = "1.0"
        write_csv(p, fields, rows)

    baseline = make_data()
    rep = run(make_data(mutate), baseline_dir=baseline)
    expect(rep, checks.FAIL, "append_only", "過去行が変更されている")
    assert hits(rep, checks.FAIL, "append_only", "margin/3851.csv")


def test_append_only_detects_no_new_rows():
    """取得が空振りして1行も増えなかった週を検出する。"""
    baseline = make_data()
    rep = run(make_data(), baseline_dir=baseline)
    expect(rep, checks.WARN, "append_only", "追記が0件")


def test_append_only_without_baseline_is_warned_not_silent():
    """ベースラインが無いとき「スキップ」で黙らない（原則1）。"""
    rep = run(make_data())
    expect(rep, checks.WARN, "append_only", "検証できていない")


def test_append_only_accepts_appended_rows():
    """正しい追記（新しい日付の行）は FAIL にしない。"""
    def mutate(d: Path) -> None:
        p = d / "prices" / "daily.csv"
        fields, rows = read_csv(p)
        last = price_rows(rows, "4073")[-1]
        new = dict(last, date="2026-08-11")
        write_csv(p, fields, rows + [new])

    baseline = make_data()
    rep = run(make_data(mutate), baseline_dir=baseline)
    expect_none(rep, checks.FAIL, "append_only")
    expect_none(rep, checks.WARN, "append_only", "追記が0件")


# =============================================================================
# 2. OHLC の整合性
# =============================================================================

def test_ohlc_high_below_low():
    def mutate(d: Path) -> None:
        def fn(rows):
            r = price_rows(rows, "6570")[-1]
            r["high"], r["low"] = r["low"], r["high"]   # 列がずれて高安が入れ替わる
        edit_prices(d, fn)
    rep = run(make_data(mutate))
    expect(rep, checks.FAIL, "ohlc", "high < low")


def test_ohlc_high_below_close():
    def mutate(d: Path) -> None:
        def fn(rows):
            r = price_rows(rows, "3851")[-1]
            r["high"] = str(close_of(r) - 1)
        edit_prices(d, fn)
    rep = run(make_data(mutate))
    expect(rep, checks.FAIL, "ohlc", "high < close")


def test_ohlc_checks_single_source_rows_via_value_primary():
    """close が空の行（SINGLE_SOURCE）も検査される。

    close 列だけを見る検査は、SINGLE_SOURCE の33行と growth250 の269行に対して
    感度がゼロになる。参照値に value_primary を使っていることの確認。
    """
    def mutate(d: Path) -> None:
        def fn(rows):
            r = [x for x in price_rows(rows, "3851") if x["status"] == "SINGLE_SOURCE"][0]
            assert r["close"] == "", "前提: SINGLE_SOURCE の close は空"
            r["low"] = str(float(r["value_primary"]) + 100)
        edit_prices(d, fn)
    rep = run(make_data(mutate))
    expect(rep, checks.FAIL, "ohlc", "low > close")


def test_ohlc_index_is_checked():
    """指数ファイル（close が全行空の growth250）も OHLC 整合性を検査する。"""
    def mutate(d: Path) -> None:
        p = d / "indices" / "growth250.csv"
        fields, rows = read_csv(p)
        rows[-1]["high"], rows[-1]["low"] = rows[-1]["low"], rows[-1]["high"]
        write_csv(p, fields, rows)
    rep = run(make_data(mutate))
    expect(rep, checks.FAIL, "ohlc", "high < low")
    assert hits(rep, checks.FAIL, "ohlc", "growth250")


# =============================================================================
# 3. 取得漏れ（行の不在。status では表現されない欠測）
# =============================================================================

def test_coverage_latest_day_missing_for_one_code():
    day = latest_date()
    def mutate(d: Path) -> None:
        edit_prices(d, lambda rows: [r for r in rows
                                     if not (r["code"] == "4073"
                                             and r["date"] == day)])
    rep = run(make_data(mutate))
    expect(rep, checks.FAIL, "coverage", f"4073: 最新営業日 {day} の行が無い")


def test_coverage_code_completely_absent():
    def mutate(d: Path) -> None:
        edit_prices(d, lambda rows: [r for r in rows if r["code"] != "4937"])
    rep = run(make_data(mutate))
    expect(rep, checks.FAIL, "coverage", "4937: 行が1つも無い")


def test_coverage_history_hole_is_warned():
    def mutate(d: Path) -> None:
        edit_prices(d, lambda rows: [r for r in rows
                                     if not (r["code"] == "6570"
                                             and r["date"] == "2026-03-02")])
    rep = run(make_data(mutate))
    expect(rep, checks.WARN, "coverage", "6570 2026-03-02")


# =============================================================================
# 4. スキーマ（状態と値の対応）
# =============================================================================

def test_schema_ok_without_close():
    def mutate(d: Path) -> None:
        def fn(rows):
            last_ok(rows, "3851")["close"] = ""
        edit_prices(d, fn)
    rep = run(make_data(mutate))
    expect(rep, checks.FAIL, "schema", "status=OK だが close が空")


def test_schema_unverified_value_promoted_to_close():
    """照合を通っていない値が採用値の列に入る（逆向きの壊れ方）。"""
    def mutate(d: Path) -> None:
        def fn(rows):
            r = [x for x in price_rows(rows, "4073") if x["status"] == "SINGLE_SOURCE"][0]
            r["close"] = r["value_primary"]
        edit_prices(d, fn)
    rep = run(make_data(mutate))
    expect(rep, checks.FAIL, "schema", "照合を通っていない値が close（採用値）に入っている")


def test_schema_mismatch_but_values_agree():
    def mutate(d: Path) -> None:
        def fn(rows):
            r = last_ok(rows, "6570")
            r["status"] = "MISMATCH"
            r["close"] = ""
        edit_prices(d, fn)
    rep = run(make_data(mutate))
    expect(rep, checks.FAIL, "schema", "status=MISMATCH だが主副の値が一致している")


def test_schema_unknown_status():
    def mutate(d: Path) -> None:
        def fn(rows):
            price_rows(rows, "4937")[-1]["status"] = "PARTIAL"
        edit_prices(d, fn)
    rep = run(make_data(mutate))
    expect(rep, checks.FAIL, "schema", "status が定義外")


def test_schema_composed_status_is_accepted():
    """status は `|` 区切りで複数フラグを持つ（照合結果はちょうど1つ）。"""
    def mutate(d: Path) -> None:
        def fn(rows):
            r = [x for x in price_rows(rows, "4073")
                 if x["status"] == "SINGLE_SOURCE"][0]
            r["status"] = "SINGLE_SOURCE|NO_TRADE"
            r["volume"] = "0"
        edit_prices(d, fn)
    rep = run(make_data(mutate))
    expect_none(rep, checks.FAIL, "schema", "status が定義外")


def test_schema_status_without_reconcile_result_fails():
    def mutate(d: Path) -> None:
        def fn(rows):
            price_rows(rows, "4937")[-1]["status"] = "VOLUME_MISMATCH"
        edit_prices(d, fn)
    rep = run(make_data(mutate))
    expect(rep, checks.FAIL, "schema", "status が定義外")


def test_schema_no_trade_with_disagreeing_secondary():
    """★NO_TRADE が MISMATCH を握り潰す経路の検査（旧実装は素通りしていた）。"""
    def mutate(d: Path) -> None:
        def fn(rows):
            for r in price_rows(rows, "4937"):
                if r["status"] != "NO_TRADE":
                    continue
                r["source_secondary"] = "minkabu"
                r["value_secondary"] = str(float(r["value_primary"]) * 1.5)
        edit_prices(d, fn)
    rep = run(make_data(mutate))
    expect(rep, checks.FAIL, "schema", "NO_TRADE だが主副の終値が食い違っている")


# =============================================================================
# 4b. master.yaml のスキーマ（人間が手で書く唯一の判定入力）
# =============================================================================

def _edit_master(d: Path, fn) -> None:
    import yaml
    p = d / "master.yaml"
    doc = yaml.safe_load(p.read_text(encoding="utf-8"))
    fn(doc)
    p.write_text(yaml.safe_dump(doc, allow_unicode=True, sort_keys=False),
                 encoding="utf-8")


def test_master_schema_holding_status_typo():
    """`hold` / `保有` などの語彙外は FAIL。judge が黙って「none」に倒さないため。"""
    for bad in ("hold", "保有", True):
        def mutate(d: Path, bad=bad) -> None:
            _edit_master(d, lambda doc: doc["stocks"][0]["holding"]
                         .update({"status": bad, "buy_price": 1000.0,
                                  "buy_date": "2026-06-01"}))
        rep = run(make_data(mutate))
        expect(rep, checks.FAIL, "master_schema", "holding.status が語彙外")


def test_master_schema_none_with_buy_price():
    def mutate(d: Path) -> None:
        _edit_master(d, lambda doc: doc["stocks"][0]["holding"]
                     .update({"status": "none", "buy_price": 1000.0}))
    rep = run(make_data(mutate))
    expect(rep, checks.FAIL, "master_schema", "holding.status=none なのに")


def test_master_schema_holding_without_buy_price():
    def mutate(d: Path) -> None:
        _edit_master(d, lambda doc: doc["stocks"][0]["holding"]
                     .update({"status": "holding"}))
    rep = run(make_data(mutate))
    expect(rep, checks.FAIL, "master_schema", "holding.status=holding なのに")


def test_master_schema_missing_gate():
    def mutate(d: Path) -> None:
        _edit_master(d, lambda doc: doc.pop("liquidity_gate"))
    rep = run(make_data(mutate))
    expect(rep, checks.FAIL, "master_schema", "liquidity_gate が無い")


def test_master_schema_valid_holding_is_accepted():
    def mutate(d: Path) -> None:
        _edit_master(d, lambda doc: doc["stocks"][0]["holding"]
                     .update({"status": "holding", "buy_price": 1000.0,
                              "buy_date": "2026-06-01", "shares": 100}))
    rep = run(make_data(mutate))
    expect_none(rep, checks.FAIL, "master_schema")


# =============================================================================
# 4c. 信用倍率の再計算（買い残0で検査が無効化されないこと）
# =============================================================================

def test_margin_ratio_inconsistent_with_zero_long_balance():
    """買い残0は falsy。真偽値判定だと検査ごとスキップされていた。"""
    def mutate(d: Path) -> None:
        p = d / "margin" / "3851.csv"
        fields, rows = read_csv(p)
        rows[-1].update({"long_balance": "0.0", "short_balance": "100.0",
                         "ratio": "5.0", "status": "OK"})
        write_csv(p, fields, rows)
    rep = run(make_data(mutate))
    expect(rep, checks.FAIL, "margin", "表示倍率と 買残÷売残 が乖離")


def test_schema_unreadable_number():
    def mutate(d: Path) -> None:
        def fn(rows):
            price_rows(rows, "4937")[-1]["volume"] = "1,234"   # カンマ付きが素通りした
        edit_prices(d, fn)
    rep = run(make_data(mutate))
    expect(rep, checks.FAIL, "schema", "数値として読めない値")


def test_schema_missing_fetched_at():
    def mutate(d: Path) -> None:
        def fn(rows):
            price_rows(rows, "3851")[-1]["fetched_at"] = ""
        edit_prices(d, fn)
    rep = run(make_data(mutate))
    expect(rep, checks.FAIL, "schema", "fetched_at が空")


def test_duplicate_key():
    def mutate(d: Path) -> None:
        def fn(rows):
            return rows + [dict(price_rows(rows, "3851")[-1])]
        edit_prices(d, fn)
    rep = run(make_data(mutate))
    expect(rep, checks.FAIL, "duplicate", f"3851/{latest_date('3851')}")


def test_master_unknown_code():
    def mutate(d: Path) -> None:
        def fn(rows):
            price_rows(rows, "3851")[-1]["code"] = "9999"
        edit_prices(d, fn)
    rep = run(make_data(mutate))
    expect(rep, checks.FAIL, "master", "マスタ未登録のコード: 9999")


# =============================================================================
# 5. 分割・外れ値
# =============================================================================

def _halve_last(code: str):
    def mutate(d: Path) -> None:
        def fn(rows):
            rs = price_rows(rows, code)
            prev = float(rs[-2]["close"] or rs[-2]["value_primary"])
            set_price(rs[-1], round(prev / 2, 1))
        edit_prices(d, fn)
    return mutate


def test_split_detected():
    rep = run(make_data(_halve_last("3851")))
    expect(rep, checks.FAIL, "split", "1:2 分割の可能性")
    expect(rep, checks.FAIL, "split", "確認記録が無い")


def test_split_acknowledged_becomes_warn():
    def mutate(d: Path) -> None:
        _halve_last("3851")(d)
        (d / "corporate_actions.yaml").write_text(
            "actions:\n"
            "  - code: \"3851\"\n"
            f"    date: \"{latest_date('3851')}\"\n"
            "    kind: split\n"
            "    ratio: \"1:2\"\n"
            "    source_url: \"https://www.release.tdnet.info/example\"\n",
            encoding="utf-8")
    rep = run(make_data(mutate))
    expect_none(rep, checks.FAIL, "split")
    expect(rep, checks.WARN, "split", "確認済み: split 1:2")


def test_split_acknowledged_without_source_stays_fail():
    """出典なしの「確認済み」は確認していないのと同じ（F8-6 と同じ扱い）。"""
    def mutate(d: Path) -> None:
        _halve_last("3851")(d)
        (d / "corporate_actions.yaml").write_text(
            "actions:\n"
            "  - code: \"3851\"\n"
            f"    date: \"{latest_date('3851')}\"\n"
            "    kind: split\n"
            "    ratio: \"1:2\"\n"
            "    source_url: \"\"\n",
            encoding="utf-8")
    rep = run(make_data(mutate))
    expect(rep, checks.FAIL, "split", "確認記録に source_url または kind が無い")


def test_outlier_detected():
    def mutate(d: Path) -> None:
        def fn(rows):
            rs = price_rows(rows, "4937")
            prev = close_of(rs[-1])
            set_price(rs[-1], round(prev * 1.6, 1))   # 分割比には当たらない急騰
        edit_prices(d, fn)
    rep = run(make_data(mutate))
    expect(rep, checks.WARN, "outlier", f"4937 {latest_date('4937')}")


# =============================================================================
# 6. 「値が動かない」壊れ方（件数の検査では感度がゼロになる領域）
# =============================================================================

def test_cross_code_identity():
    """全銘柄が同じ値で埋まる壊れ方（同じページを取得している）。"""
    def mutate(d: Path) -> None:
        def fn(rows):
            src = {r["date"]: r for r in price_rows(rows, "3851")[-5:]}
            for r in price_rows(rows, "4073")[-5:]:
                s = src.get(r["date"])
                if s:
                    for col in ("open", "high", "low", "close", "volume",
                                "value_primary", "value_secondary"):
                        r[col] = s[col]
        edit_prices(d, fn)
    rep = run(make_data(mutate))
    expect(rep, checks.FAIL, "cross_code", "3851 と 4073 が OHLCV 完全一致: 5日")


def test_frozen_series():
    def mutate(d: Path) -> None:
        def fn(rows):
            rs = price_rows(rows, "6570")
            value = float(rs[-13]["close"] or rs[-13]["value_primary"])
            for r in rs[-12:]:
                set_price(r, value)
        edit_prices(d, fn)
    rep = run(make_data(mutate))
    expect(rep, checks.WARN, "frozen", "6570: 同一終値が 13営業日連続")


def test_no_trade_status_without_zero_volume():
    def mutate(d: Path) -> None:
        def fn(rows):
            for r in rows:
                if r["status"] == "NO_TRADE":
                    r["volume"] = "100"
                    break
        edit_prices(d, fn)
    rep = run(make_data(mutate))
    expect(rep, checks.FAIL, "no_trade", "status=NO_TRADE だが出来高が0でない")


def test_zero_volume_without_no_trade_status():
    def mutate(d: Path) -> None:
        def fn(rows):
            price_rows(rows, "4073")[-1]["volume"] = "0"
        edit_prices(d, fn)
    rep = run(make_data(mutate))
    expect(rep, checks.WARN, "no_trade", "出来高0だが status が NO_TRADE でない")


def test_freshness_stale_batch():
    """取得は動いたのに、返ってきたのが古い日付だった週。"""
    def mutate(d: Path) -> None:
        def fn(rows):
            for r in rows:
                if r["date"] == "2026-07-01":
                    r["fetched_at"] = "2026-10-01T06:00:00+09:00"
        edit_prices(d, fn)
    rep = run(make_data(mutate))
    expect(rep, checks.WARN, "freshness", "直近の取得実行 2026-10-01")


# =============================================================================
# 7. 信用残高
# =============================================================================

def test_margin_ratio_inconsistent():
    """売り残と買い残の列が入れ替わったのに倍率だけ元のまま。"""
    def mutate(d: Path) -> None:
        p = d / "margin" / "3851.csv"
        fields, rows = read_csv(p)
        r = rows[-1]
        r["long_balance"], r["short_balance"] = r["short_balance"], r["long_balance"]
        write_csv(p, fields, rows)
    rep = run(make_data(mutate))
    expect(rep, checks.FAIL, "margin", "表示倍率と 買残÷売残 が乖離")


def test_margin_ratio_na_flag_mismatch():
    """倍率が空なのに RATIO_NA が立っていない（「過熱していない」と読まれる状態）。"""
    def mutate(d: Path) -> None:
        p = d / "margin" / "4073.csv"
        fields, rows = read_csv(p)
        for r in rows:
            r["status"] = "OK"
        write_csv(p, fields, rows)
    rep = run(make_data(mutate))
    expect(rep, checks.FAIL, "margin", "ratio の有無と RATIO_NA が対応していない")


def test_margin_unit_changed():
    def mutate(d: Path) -> None:
        p = d / "margin" / "6570.csv"
        fields, rows = read_csv(p)
        rows[-1]["unit"] = "株"
        write_csv(p, fields, rows)
    rep = run(make_data(mutate))
    expect(rep, checks.WARN, "margin", "単位が途中で変わっている")


def test_margin_file_missing_is_warned():
    def mutate(d: Path) -> None:
        (d / "margin" / "6570.csv").unlink()
    rep = run(make_data(mutate))
    expect(rep, checks.WARN, "margin", "6570: 信用残高が取得できていない")


def test_margin_stale_is_warned():
    def mutate(d: Path) -> None:
        p = d / "margin" / "3851.csv"
        fields, rows = read_csv(p)
        for i, r in enumerate(rows):
            r["date"] = f"2026-05-{i + 1:02d}"
        write_csv(p, fields, rows)
    rep = run(make_data(mutate))
    expect(rep, checks.WARN, "margin", "judge は古い残高を unknown")


# =============================================================================
# 8. 指数
# =============================================================================

def test_index_missing_latest_day():
    day = latest_date()
    def mutate(d: Path) -> None:
        p = d / "indices" / "topix.csv"
        fields, rows = read_csv(p)
        write_csv(p, fields, [r for r in rows if r["date"] != day])
    rep = run(make_data(mutate))
    expect(rep, checks.WARN, "index", f"株価の最新営業日 {day} の行が無い")


def test_index_file_missing_is_warned():
    def mutate(d: Path) -> None:
        (d / "indices" / "topix.csv").unlink()
    rep = run(make_data(mutate))
    expect(rep, checks.WARN, "index", "topix: 指数データが無い")


def test_index_business_day_gap_is_warned():
    def mutate(d: Path) -> None:
        p = d / "indices" / "topix.csv"
        fields, rows = read_csv(p)
        write_csv(p, fields, [r for r in rows if r["date"] != "2026-03-02"])
    rep = run(make_data(mutate))
    expect(rep, checks.WARN, "index", "株価にあって指数に無い営業日 1日")


def test_index_code_column_mismatch():
    def mutate(d: Path) -> None:
        p = d / "indices" / "topix.csv"
        fields, rows = read_csv(p)
        rows[-1]["code"] = "growth250"
        write_csv(p, fields, rows)
    rep = run(make_data(mutate))
    expect(rep, checks.FAIL, "index", "code 列が指数ID と一致しない")


# =============================================================================
# 9. 決算 KPI（LLM が書く唯一のデータファイル）
# =============================================================================

KPI_HEADER = ",".join(checks.KPI_FIELDS)
KPI_OK = ("2026-08-14,4073,revenue,1234,JPY_million,"
          "FY2026Q4cum|連結|日本基準|売上高,false,"
          "https://example.com/tanshin.pdf,2026-08-14T18:30:00+09:00")


def _write_kpi(d: Path, *lines: str) -> None:
    (d / "kpi" / "4073.csv").write_text(
        "\n".join([KPI_HEADER, *lines]) + "\n", encoding="utf-8")


def test_kpi_valid_row_passes():
    rep = run(make_data(lambda d: _write_kpi(d, KPI_OK)))
    expect_none(rep, checks.FAIL, "kpi")


def test_kpi_derived_metric_is_rejected():
    """比率が CSV に書かれている＝LLM が計算している（F8-4 の違反）。"""
    bad = ("2026-08-14,4073,revenue_yoy_pct,32.0,pct,"
           "FY2026Q4cum|連結|日本基準|売上高,false,"
           "https://example.com/t.pdf,2026-08-14T18:30:00+09:00")
    rep = run(make_data(lambda d: _write_kpi(d, KPI_OK, bad)))
    expect(rep, checks.FAIL, "kpi", "比率が CSV に書かれている")


def test_kpi_assumed_without_basis_is_rejected():
    """推測すること自体は許可。**隠すこと**が禁止（D17）。"""
    bad = ("2026-08-14,4073,ordinary_income,50,JPY_million,"
           "FY2026Q4cum|連結|日本基準|経常利益,true,,2026-08-14T18:30:00+09:00")
    rep = run(make_data(lambda d: _write_kpi(d, bad)))
    expect(rep, checks.FAIL, "kpi", "assumed=true だが definition に根拠")


def test_kpi_assumed_with_basis_is_warned_only():
    ok = ("2026-08-14,4073,ordinary_income,50,JPY_million,"
          "FY2026Q4cum|連結|日本基準|経常利益|assumed:前期短信から按分,true,,"
          "2026-08-14T18:30:00+09:00")
    rep = run(make_data(lambda d: _write_kpi(d, ok)))
    expect_none(rep, checks.FAIL, "kpi")
    expect(rep, checks.WARN, "kpi", "推測で埋めた値（assumed=true）")


def test_kpi_missing_source_url_is_rejected():
    bad = ("2026-08-14,4073,revenue,1234,JPY_million,"
           "FY2026Q4cum|連結|日本基準|売上高,false,,2026-08-14T18:30:00+09:00")
    rep = run(make_data(lambda d: _write_kpi(d, bad)))
    expect(rep, checks.FAIL, "kpi", "source_url または fetched_at が空")


def test_kpi_empty_definition_is_rejected():
    bad = ("2026-08-14,4073,revenue,1234,JPY_million,,false,"
           "https://example.com/t.pdf,2026-08-14T18:30:00+09:00")
    rep = run(make_data(lambda d: _write_kpi(d, bad)))
    expect(rep, checks.FAIL, "kpi", "definition が空")


def test_kpi_unit_out_of_enum_is_rejected():
    bad = ("2026-08-14,4073,revenue,1234,百万円,"
           "FY2026Q4cum|連結|日本基準|売上高,false,"
           "https://example.com/t.pdf,2026-08-14T18:30:00+09:00")
    rep = run(make_data(lambda d: _write_kpi(d, bad)))
    expect(rep, checks.FAIL, "kpi", "unit が定義外")


def test_kpi_negative_revenue_is_rejected():
    bad = ("2026-08-14,4073,revenue,-1234,JPY_million,"
           "FY2026Q4cum|連結|日本基準|売上高,false,"
           "https://example.com/t.pdf,2026-08-14T18:30:00+09:00")
    rep = run(make_data(lambda d: _write_kpi(d, bad)))
    expect(rep, checks.FAIL, "kpi", "売上高系が負")


def test_kpi_duplicate_key_is_rejected():
    rep = run(make_data(lambda d: _write_kpi(d, KPI_OK, KPI_OK)))
    expect(rep, checks.FAIL, "duplicate", "4073/2026-08-14/revenue")


# =============================================================================
# 実行
# =============================================================================

def main() -> int:
    tests = [(name, fn) for name, fn in sorted(globals().items())
             if name.startswith("test_") and callable(fn)]
    failed: list[tuple[str, str]] = []
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
        print("\n--- 実データ（4銘柄×269営業日・1076行）の検査結果 ---")
        for line in _REAL_SUMMARY:
            print(line)

    for d in _TMPDIRS:
        shutil.rmtree(d, ignore_errors=True)

    print(f"\n{len(tests) - len(failed)}/{len(tests)} passed")
    if failed:
        for name, msg in failed:
            print(f"  - {name}: {msg}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
