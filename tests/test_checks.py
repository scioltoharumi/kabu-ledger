"""checks.py のテスト。

方針:
  - **「検査を書いた」ことと「その検査が効いている」ことは別**。全 FAIL/WARN について
    「その検査だけが反応する壊し方」を実データのコピーに作り、反応することを確認する。
  - 壊していない実データ（4銘柄×269営業日・1076行）で FAIL 0 になることを確認する。
    誤検知する検査は、毎週 FAIL を出してビルドを止めるので実質的に使えない。
  - 壊し方は「起こりうる形」で作る。存在しない列を消すのではなく、
    セレクタがずれて高安が入れ替わる・過去行が書き換わる・1銘柄だけ取得漏れる、など。
  - **実データの日付・値・件数をべた書きしない。** データは週次で増えるので、
    写経した期待値は次の取得で必ず壊れる（実際に壊れた）。期待値は `realdata`
    から引き、合成データで済むものは合成データで完結させる。

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
sys.path.insert(0, str(Path(__file__).resolve().parent))

import checks  # noqa: E402
import realdata as rd  # noqa: E402

_TMPDIRS: list[Path] = []
_REAL_SUMMARY: list[str] = []


# =============================================================================
# ヘルパ
# =============================================================================

_BASE: Path | None = None


def _base_data() -> Path:
    """make_data の複製元。初回だけ実データをローカルの一時領域へ写す。

    ROOT は G:（Google Drive）にあり、そこからの copytree は1回 約0.17秒 ×
    128回 ≒ 22秒かかっていた。2段化してローカル→ローカルの複製に変える。
    ベースは毎プロセス実データから作るので実データ性は維持される。
    ハードリンクは使わない（edit_prices の open("w") が共有実体を壊す）。
    """
    global _BASE
    if _BASE is None:
        base = Path(tempfile.mkdtemp(prefix="kabu-base-"))
        _TMPDIRS.append(base)
        shutil.copytree(ROOT / "data", base / "data")
        if (ROOT / "reports").exists():
            shutil.copytree(ROOT / "reports", base / "reports")
        _BASE = base
    return _BASE


def make_data(mutate=None) -> Path:
    """実データの data/ と reports/ を一時ディレクトリに複製し、mutate で壊す。

    reports/ も複製するのは、レポートの数値と検証済み数値の突合
    （`check_report_numbers`）が data/ だけでは成立しないため。
    run_checks は既定で `data_dir.parent / "reports"` を見る。
    """
    src = _base_data()
    base = Path(tempfile.mkdtemp(prefix="kabu-checks-"))
    _TMPDIRS.append(base)
    dst = base / "data"
    shutil.copytree(src / "data", dst)
    if (src / "reports").exists():
        shutil.copytree(src / "reports", base / "reports")
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
    """実データの最新営業日を返す（`realdata` への薄い別名）。

    このテスト群は実データを複製して壊す方式なので、期待値に日付を
    べた書きすると **データが1行増えるたびに落ちる**（週次で必ず壊れる）。
    期待値はここから引いてデータに追随させる。
    """
    return rd.latest_date(code)


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
    """外れ値検定が「点数不足で検定不能」に落ちていない。

    **「σ の WARN が出ていること」は要求しない。** 外れ値が実際にあるかは
    その週のデータ次第で、無い週があってもそれは正常である。検定器が生きて
    いることは `test_outlier_detected`（合成の急騰を入れて反応を見る）が担保する。
    ここで見るのは「材料不足で検定そのものが行われていない」状態だけ。
    """
    rep = run(ROOT / "data")
    expect_none(rep, checks.WARN, "outlier", "検定できない")


def test_real_data_no_trade_is_visible():
    """出来高0（売買不成立）の日が WARN として見えている（異常にはしない）。

    件数はデータから引く。べた書きすると NO_TRADE が1日増えた週に落ちる。
    """
    rep = run(ROOT / "data")
    counted = 0
    for code in rd.codes():
        n = rd.no_trade_days(code)
        if not n:
            continue
        counted += 1
        expect(rep, checks.WARN, "no_trade",
               f"{code}: 出来高0（売買不成立）が {n}日")
    assert counted, "NO_TRADE の行が1つも無く、この検査が空回りしている"


def test_real_data_index_all_single_source_is_not_fail():
    """growth250 は全行 close 空。既知の仕様であり FAIL にしない（が WARN で見える）。"""
    rep = run(ROOT / "data")
    expect_none(rep, checks.FAIL, "index")
    n = rd.index_status_days("growth250", "SINGLE_SOURCE")
    assert n, "growth250 に SINGLE_SOURCE の行が無い（前提が変わっている）"
    expect(rep, checks.WARN, "missing", f"growth250 SINGLE_SOURCE: {n}日")


# =============================================================================
# 1. append-only（過去行の改変・削除）
# =============================================================================

def test_append_only_detects_modified_past_row():
    day = rd.mid_date("4073")      # 履歴の中ほど。日付をべた書きしない
    baseline = make_data()
    target = make_data(lambda d: edit_prices(
        d, lambda rows: [dict(r, close="99999.0") if
                         (r["code"] == "4073" and r["date"] == day) else r
                         for r in rows]))
    rep = run(target, baseline_dir=baseline)
    expect(rep, checks.FAIL, "append_only", "過去行が変更されている")
    expect(rep, checks.FAIL, "append_only", f"4073/{day}")


def test_append_only_detects_deleted_past_row():
    day = rd.mid_date("4073")
    baseline = make_data()
    target = make_data(lambda d: edit_prices(
        d, lambda rows: [r for r in rows
                         if not (r["code"] == "4073" and r["date"] == day)]))
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
    """正しい追記（**最新営業日の次の1日**）は FAIL にしない。

    追記する日付は実データの末尾から作る。固定日を書くと、実データが
    その日を追い越した時点で「追記」ではなく「過去への挿入」になる。
    """
    day = rd.next_business_day()

    def mutate(d: Path) -> None:
        p = d / "prices" / "daily.csv"
        fields, rows = read_csv(p)
        write_csv(p, fields, rows + rd.advance_rows(rows, day, confirmed=True))

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
    # 題材は**監視対象**の銘柄でなければならない。対象外は取得を止めており
    # 最新営業日が無くて当然なので、coverage は何も言わない（下のテスト）。
    code = rd.watched_codes()[0]
    day = latest_date()
    def mutate(d: Path) -> None:
        edit_prices(d, lambda rows: [r for r in rows
                                     if not (r["code"] == code
                                             and r["date"] == day)])
    rep = run(make_data(mutate))
    expect(rep, checks.FAIL, "coverage", f"{code}: 最新営業日 {day} の行が無い")


def test_coverage_code_completely_absent():
    code = rd.watched_codes()[0]
    def mutate(d: Path) -> None:
        edit_prices(d, lambda rows: [r for r in rows if r["code"] != code])
    rep = run(make_data(mutate))
    expect(rep, checks.FAIL, "coverage", f"{code}: 行が1つも無い")


def test_coverage_ignores_excluded_stocks():
    """**監視から外した銘柄の取得が止まっても FAIL にしない。**

    ここを見落とすと、フラグを立てた翌週に coverage が必ず落ちて公開が止まる
    （ci.yml が言う「翌週かならず落ちるテスト＝公開を止める仕掛け」と同じ形）。
    """
    excluded = rd.excluded_codes()
    if not excluded:
        return                      # 対象外が1件も無い週は確かめるものが無い
    code, day = excluded[0], latest_date()
    def mutate(d: Path) -> None:
        edit_prices(d, lambda rows: [r for r in rows
                                     if not (r["code"] == code
                                             and r["date"] == day)])
    rep = run(make_data(mutate))
    hits = [r.line() for r in rep.results
            if r.level == checks.FAIL and code in r.line() and "最新営業日" in r.line()]
    assert not hits, f"対象外の {code} が coverage で FAIL している: {hits}"


def test_coverage_history_hole_is_warned():
    day = rd.mid_date("6570")      # 履歴の中ほど（先頭・末尾は別の検査が反応する）
    def mutate(d: Path) -> None:
        edit_prices(d, lambda rows: [r for r in rows
                                     if not (r["code"] == "6570"
                                             and r["date"] == day)])
    rep = run(make_data(mutate))
    expect(rep, checks.WARN, "coverage", f"6570 {day}")


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


def test_master_schema_watch_out_of_vocab():
    """`watch` の語彙外は FAIL。

    yamlio.watch_state は語彙外を **active に倒す**（打ち間違いで銘柄が黙って
    台帳から消える方が悪いため）。倒した結果「対象外にしたつもりが取得も判定も
    続いている」状態になるので、**ここで言わないと誰も気づかない**。
    """
    for bad in ("exclude", "除外", "off", True):
        def mutate(d: Path, bad=bad) -> None:
            _edit_master(d, lambda doc: doc["stocks"][0].update({"watch": bad}))
        rep = run(make_data(mutate))
        expect(rep, checks.FAIL, "master_schema", "watch が語彙外")


def test_master_schema_watch_excluded_needs_a_reason():
    """理由を書かずに監視から外させない。

    外した理由が残っていないと、半年後に「なぜ止まっているのか」が誰にも
    分からなくなる。判断の記録が台帳の主役なので、ここは FAIL にする。
    """
    def mutate(d: Path) -> None:
        def f(doc):
            doc["stocks"][0]["watch"] = "excluded"
            doc["stocks"][0].pop("watch_reason", None)
        _edit_master(d, f)
    rep = run(make_data(mutate))
    expect(rep, checks.FAIL, "master_schema", "watch_reason が無い")


def test_master_schema_watch_excluded_with_a_reason_is_accepted():
    def mutate(d: Path) -> None:
        _edit_master(d, lambda doc: doc["stocks"][0].update(
            {"watch": "excluded", "watch_reason": "テスト用に外した"}))
    rep = run(make_data(mutate))
    hits = [r.line() for r in rep.results
            if r.level == checks.FAIL and "watch" in r.line()]
    assert not hits, f"正しく書かれた watch で FAIL している: {hits}"


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
    """取得は動いたのに、返ってきたのが古い日付だった週。

    「実行日」は実データのどの `fetched_at` よりも後ろに取る。固定日を書くと、
    実データの取得時刻がその日を追い越した瞬間に「直近の取得実行」でなくなり、
    検査そのものが空振りする。
    """
    day = rd.mid_date()
    ran_at = rd.day_after_all_fetches()

    def mutate(d: Path) -> None:
        def fn(rows):
            for r in rows:
                if r["date"] == day:
                    r["fetched_at"] = f"{ran_at}T06:00:00+09:00"
        edit_prices(d, fn)
    rep = run(make_data(mutate))
    expect(rep, checks.WARN, "freshness", f"直近の取得実行 {ran_at}")


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
    day = rd.shared_history_date("topix")   # 株価にも指数にもある中ほどの1日
    def mutate(d: Path) -> None:
        p = d / "indices" / "topix.csv"
        fields, rows = read_csv(p)
        write_csv(p, fields, [r for r in rows if r["date"] != day])
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

# 実データの最新営業日とは別の合成日付を使う（べた書きすると
# test_tests_do_not_hardcode_todays_latest_business_day に引っかかる）。
_KPI_DATE = rd.latest_date()
KPI_HEADER = ",".join(checks.KPI_FIELDS)
KPI_OK = (f"{_KPI_DATE},4073,revenue,1234,JPY_million,"
          "FY2026Q4cum|連結|日本基準|売上高,false,"
          f"https://example.com/tanshin.pdf,{_KPI_DATE}T18:30:00+09:00")


def _write_kpi(d: Path, *lines: str) -> None:
    (d / "kpi" / "4073.csv").write_text(
        "\n".join([KPI_HEADER, *lines]) + "\n", encoding="utf-8")


def test_kpi_valid_row_passes():
    rep = run(make_data(lambda d: _write_kpi(d, KPI_OK)))
    expect_none(rep, checks.FAIL, "kpi")


def test_kpi_derived_metric_is_rejected():
    """比率が CSV に書かれている＝LLM が計算している（F8-4 の違反）。"""
    bad = (f"{_KPI_DATE},4073,revenue_yoy_pct,32.0,pct,"
           "FY2026Q4cum|連結|日本基準|売上高,false,"
           f"https://example.com/t.pdf,{_KPI_DATE}T18:30:00+09:00")
    rep = run(make_data(lambda d: _write_kpi(d, KPI_OK, bad)))
    expect(rep, checks.FAIL, "kpi", "比率が CSV に書かれている")


def test_kpi_assumed_without_basis_is_rejected():
    """推測すること自体は許可。**隠すこと**が禁止（D17）。"""
    bad = (f"{_KPI_DATE},4073,ordinary_income,50,JPY_million,"
           f"FY2026Q4cum|連結|日本基準|経常利益,true,,{_KPI_DATE}T18:30:00+09:00")
    rep = run(make_data(lambda d: _write_kpi(d, bad)))
    expect(rep, checks.FAIL, "kpi", "assumed=true だが definition に根拠")


def test_kpi_assumed_with_basis_is_warned_only():
    ok = (f"{_KPI_DATE},4073,ordinary_income,50,JPY_million,"
          "FY2026Q4cum|連結|日本基準|経常利益|assumed:前期短信から按分,true,,"
          f"{_KPI_DATE}T18:30:00+09:00")
    rep = run(make_data(lambda d: _write_kpi(d, ok)))
    expect_none(rep, checks.FAIL, "kpi")
    expect(rep, checks.WARN, "kpi", "推測で埋めた値（assumed=true）")


def test_kpi_missing_source_url_is_rejected():
    bad = (f"{_KPI_DATE},4073,revenue,1234,JPY_million,"
           f"FY2026Q4cum|連結|日本基準|売上高,false,,{_KPI_DATE}T18:30:00+09:00")
    rep = run(make_data(lambda d: _write_kpi(d, bad)))
    expect(rep, checks.FAIL, "kpi", "source_url または fetched_at が空")


def test_kpi_empty_definition_is_rejected():
    bad = (f"{_KPI_DATE},4073,revenue,1234,JPY_million,,false,"
           f"https://example.com/t.pdf,{_KPI_DATE}T18:30:00+09:00")
    rep = run(make_data(lambda d: _write_kpi(d, bad)))
    expect(rep, checks.FAIL, "kpi", "definition が空")


def test_kpi_unit_out_of_enum_is_rejected():
    bad = (f"{_KPI_DATE},4073,revenue,1234,百万円,"
           "FY2026Q4cum|連結|日本基準|売上高,false,"
           f"https://example.com/t.pdf,{_KPI_DATE}T18:30:00+09:00")
    rep = run(make_data(lambda d: _write_kpi(d, bad)))
    expect(rep, checks.FAIL, "kpi", "unit が定義外")


def test_kpi_negative_revenue_is_rejected():
    bad = (f"{_KPI_DATE},4073,revenue,-1234,JPY_million,"
           "FY2026Q4cum|連結|日本基準|売上高,false,"
           f"https://example.com/t.pdf,{_KPI_DATE}T18:30:00+09:00")
    rep = run(make_data(lambda d: _write_kpi(d, bad)))
    expect(rep, checks.FAIL, "kpi", "売上高系が負")


def test_kpi_duplicate_key_is_rejected():
    rep = run(make_data(lambda d: _write_kpi(d, KPI_OK, KPI_OK)))
    expect(rep, checks.FAIL, "duplicate", f"4073/{_KPI_DATE}/revenue")


# =============================================================================
# 10. 財務数値（data/fundamentals/{code}.csv）
# =============================================================================
#
# 株価の close と同じ規律を財務数値に通す検査。各検査について
# 「正しく検出すること」と「誤検知しないこと」を対にして確認する。
# **期待値に実データの値を書かない**（合成データで完結させる）。

FUND_HEADER = ",".join(checks.FUNDAMENTALS_FIELDS)
FUND_FETCHED = "2026-08-13T10:00:00+09:00"


def fund_line(period: str, metric: str, value, unit: str, status: str = "OK",
              primary=None, secondary=None, tolerance="0.1",
              observed=None, code: str = "4073", sources=None) -> str:
    """fundamentals の1行。既定は「別サイト2つが一致して採用」。

    `observed` は `sources_all` に並べる観測値（第3のソースを表現するため）。
    省略すると主・副の2つになる。
    `sources` は観測値に対応する取得元の名前（`kabutan_fy` のようにサイト＋ページ）。
    省略すると `src0` / `src1` … になり、**すべて別サイト**として扱われる。
    """
    v = "" if value is None else str(value)
    p = v if primary is None else str(primary)
    s = v if secondary is None else str(secondary)
    seen = observed if observed is not None else [x for x in (p, s) if x != ""]
    names = list(sources) if sources is not None \
        else [f"src{i}" for i in range(len(seen))]
    pairs = "|".join(f"{n}={x}" for n, x in zip(names, seen))
    src_primary = names[0] if names else "kabutan"
    src_secondary = "" if s == "" else (names[1] if len(names) > 1 else "irbank")
    url_secondary = "" if s == "" else "https://example.com/secondary"
    return ",".join([period, code, metric, v, unit, str(tolerance), status,
                     src_primary, p, p, src_secondary, s, s, pairs,
                     "https://example.com/primary", url_secondary, FUND_FETCHED])


def write_fund(d: Path, *lines: str, code: str = "4073") -> None:
    """合成の fundamentals に**置き換える**（実データの中身に依存させない）。

    実データは週次で増えるので、残したままだと「壊していないのに FAIL が出る／
    出ない」がデータの中身で変わってしまう。実データでの FAIL 0 は
    `test_real_data_has_no_fail` の担当。
    """
    fd = d / "fundamentals"
    if fd.exists():
        shutil.rmtree(fd)
    fd.mkdir(parents=True)
    (fd / f"{code}.csv").write_text(
        "\n".join([FUND_HEADER, *lines]) + "\n", encoding="utf-8")


def write_report(d: Path, front: str, body: str = "", code: str = "4073") -> None:
    """reports/ を合成レポート1件だけに置き換える（d は data ディレクトリ）。"""
    rd = d.parent / "reports"
    if rd.exists():
        shutil.rmtree(rd)
    rd.mkdir(parents=True)
    text = "---\n" + front.strip("\n") + "\n---\n\n" + body.strip("\n") + "\n"
    (rd / f"{code}.md").write_text(text, encoding="utf-8")


def revenue_chart(value, unit: str = "億円", label: str = "2025/6") -> str:
    """売上高の棒グラフだけを持つ front matter。"""
    return ('code: "4073"\n'
            'name: "テスト"\n'
            'charts:\n'
            '  revenue_10y:\n'
            '    type: bar\n'
            f'    unit: {unit}\n'
            '    data:\n'
            f'      - {{label: "{label}", value: {value}}}\n')


# --- スキーマと status（D7: 照合を通っていない値を採用値に格上げしない） -------

def test_fundamentals_valid_rows_pass():
    def m(d):
        write_fund(d,
                   fund_line("2025-06", "revenue", 1844, "JPY_million"),
                   fund_line("2025-06", "operating_income", -80, "JPY_million"))
    rep = run(make_data(m))
    expect_none(rep, checks.FAIL, "fundamentals")


def test_fundamentals_mismatch_with_adopted_value_is_rejected():
    """2ソースが食い違ったのに採用値が入っている（D7 の中核）。"""
    def m(d):
        write_fund(d, fund_line("2025-06", "revenue", 1844, "JPY_million",
                                status="MISMATCH", primary=1844, secondary=1850))
    rep = run(make_data(m))
    expect(rep, checks.FAIL, "fundamentals", "status=MISMATCH の行に採用値が入っている")


def test_fundamentals_mismatch_without_value_is_accepted():
    """不一致を「採用しない」で記録するのは正しい形。FAIL にしない。"""
    def m(d):
        write_fund(d, fund_line("2025-06", "revenue", None, "JPY_million",
                                status="MISMATCH", primary=1844, secondary=1850))
    rep = run(make_data(m))
    expect_none(rep, checks.FAIL, "fundamentals")


def test_fundamentals_ok_without_value_is_rejected():
    def m(d):
        write_fund(d, fund_line("2025-06", "revenue", None, "JPY_million",
                                status="OK", primary=1844, secondary=1844))
    rep = run(make_data(m))
    expect(rep, checks.FAIL, "fundamentals", "status=OK だが採用値が空")


def test_fundamentals_ok_disagreeing_with_sources_is_rejected():
    """採用値が照合値と違う（採用の過程で値が化けている）。"""
    def m(d):
        write_fund(d, fund_line("2025-06", "revenue", 1900, "JPY_million",
                                status="OK", primary=1844, secondary=1844))
    rep = run(make_data(m))
    expect(rep, checks.FAIL, "fundamentals", "採用値が照合値と食い違う")


# --- 採用の「根拠の数」（値の一致だけ見ていると素通りする壊れ方） ---------------
#
# 株価側には「照合を通っていない値が close に入っている」を止める検査がある。
# 財務側で同じ位置にあるのがここ。値どうしの一致だけを見ていると、
# **1サイトしか参加していない行**や**同じサイトの別ページを2つ数えた行**が
# `OK` の顔で採用値を持ったまま通り、レポートの突合では「検証済み」として扱われる。

def test_fundamentals_ok_from_a_single_source_is_rejected():
    """参加した取得元が1つだけなのに OK（＝照合していないのに採用）。"""
    def m(d):
        write_fund(d, fund_line("2025-06", "revenue", 1844, "JPY_million",
                                status="OK", primary=1844, secondary="",
                                observed=[1844], sources=["kabutan_fy"]))
    rep = run(make_data(m))
    expect(rep, checks.FAIL, "fundamentals", "独立した取得元が")


def test_fundamentals_ok_from_two_pages_of_one_site_is_rejected():
    """同じサイトの別ページ2つは独立した確認ではない（`fetch_fundamentals` と同じ規律）。

    値は一致しているので「主副の一致」を見る検査は全部通る。ここだけが止められる。
    """
    def m(d):
        write_fund(d, fund_line("2025-06", "revenue", 1844, "JPY_million",
                                status="OK", primary=1844, secondary=1844,
                                sources=["kabutan_fy", "kabutan_q10"]))
    rep = run(make_data(m))
    expect(rep, checks.FAIL, "fundamentals", "独立した取得元が")


def test_fundamentals_ok_from_two_sites_is_accepted():
    """対照: 別サイト2つなら FAIL にしない（誤検知しないことの確認）。"""
    def m(d):
        write_fund(d, fund_line("2025-06", "revenue", 1844, "JPY_million",
                                status="OK", primary=1844, secondary=1844,
                                sources=["kabutan_fy", "irbank_pl"]))
    rep = run(make_data(m))
    expect_none(rep, checks.FAIL, "fundamentals")


def test_fundamentals_required_sites_comes_from_sources_yaml():
    """必要サイト数は `sources.yaml` が正（検査側にべた書きしない）。

    3サイト必要と宣言したら、2サイト一致の採用は不通過になる。
    """
    def m(d):
        import yaml
        p = d / "sources.yaml"
        doc = yaml.safe_load(p.read_text(encoding="utf-8"))
        doc["fundamentals"]["required_sites"] = 3
        p.write_text(yaml.safe_dump(doc, allow_unicode=True, sort_keys=False),
                     encoding="utf-8")
        write_fund(d, fund_line("2025-06", "revenue", 1844, "JPY_million",
                                status="OK", primary=1844, secondary=1844,
                                sources=["kabutan_fy", "irbank_pl"]))
    rep = run(make_data(m))
    expect(rep, checks.FAIL, "fundamentals", "3サイトに満たない")


def test_fundamentals_ok_without_source_names_is_fatal():
    """取得元の名前がどこにも無ければ独立性を検査できない。

    株価側では「status=OK だが第2ソースの記録が無い」は FAIL。財務側だけ
    WARN だったため、**観測の痕跡を消すだけで採用値が無検査で通る**穴が
    開いていた（レポートの表に載らない metric なら WARN 1件で済んだ）。
    """
    def m(d):
        line = fund_line("2025-06", "revenue", 1844, "JPY_million",
                         status="OK", primary=1844, secondary=1844,
                         sources=["kabutan_fy", "irbank_pl"])
        cells = line.split(",")
        fields = list(checks.FUNDAMENTALS_FIELDS)
        for name in ("sources_all", "source_primary", "source_secondary"):
            cells[fields.index(name)] = ""
        write_fund(d, ",".join(cells))
    rep = run(make_data(m))
    expect(rep, checks.FAIL, "fundamentals", "参加した取得元の名前が記録されていない")


def test_fundamentals_single_source_with_two_sites_is_warned():
    """逆向き: 別サイト2つが参加しているのに採用されていない（採用漏れ）。"""
    def m(d):
        write_fund(d, fund_line("2025-06", "revenue", None, "JPY_million",
                                status="SINGLE_SOURCE", primary=1844,
                                secondary=1844,
                                sources=["kabutan_fy", "irbank_pl"]))
    rep = run(make_data(m))
    expect(rep, checks.WARN, "fundamentals", "独立した取得元が複数参加している")


def test_fundamentals_single_source_from_one_site_is_not_warned():
    """対照: 同一サイトの2ページなら SINGLE_SOURCE で正しい。"""
    def m(d):
        write_fund(d, fund_line("2025-06", "revenue", None, "JPY_million",
                                status="SINGLE_SOURCE", primary=1844,
                                secondary=1844,
                                sources=["kabutan_fy", "kabutan_q10"]))
    rep = run(make_data(m))
    expect_none(rep, checks.WARN, "fundamentals", "独立した取得元が複数参加している")


def test_real_fundamentals_ok_rows_have_independent_sites():
    """実データ: 採用値のある行はすべて別サイト2つ以上に支えられている。

    期待値は CSV から引く（銘柄・期が増えても壊れない）。
    """
    seen = 0
    for path in sorted((ROOT / "data" / "fundamentals").glob("*.csv")):
        _, rows = read_csv(path)
        for r in rows:
            if "OK" not in str(r["status"]).split("|"):
                continue
            seen += 1
            names = [p.rsplit("=", 1)[0] for p in r["sources_all"].split("|")
                     if "=" in p]
            sites = {n.split("_", 1)[0] for n in names}
            assert len(sites) >= 2, \
                f"{path.name} {r['period']} {r['metric']}: 参加サイト {sorted(sites)}"
    assert seen, "採用値のある行が1つも無く、この検査が空回りしている"


def test_fundamentals_unresolvable_columns_are_warned_not_silent():
    """列名が想定と違っても**黙って素通りさせない**（設計原則1）。

    こちらが知らないスキーマで書かれた場合、FAIL にすると毎週ビルドが止まる。
    WARN で「この銘柄の財務数値は検査されていない」と表に出す。
    """
    def m(d):
        fd = d / "fundamentals"
        fd.mkdir(parents=True, exist_ok=True)
        (fd / "4073.csv").write_text("a,b,c\n1,2,3\n", encoding="utf-8")
    rep = run(make_data(m))
    expect(rep, checks.WARN, "fundamentals", "列を解決できない")
    expect_none(rep, checks.FAIL, "fundamentals")


def test_fundamentals_rounding_adopts_the_finest_observation():
    """「29.1」と「29.07」は表示解像度が違うだけ。細かいほうの採用は正しい。

    採用値は参加した**全観測**から選ばれるので、主・副の2列に無い値もありうる。
    それを「作られた値」と誤検知しないこと。
    """
    def m(d):
        write_fund(d, fund_line("FY2025-06", "eps", 29.07, "JPY",
                                status="OK|ROUNDING", primary=29.1, secondary=29.1,
                                tolerance=0.1, observed=[29.1, 29.1, 29.07]))
    rep = run(make_data(m))
    expect_none(rep, checks.FAIL, "fundamentals")


def test_fundamentals_invented_adopted_value_is_rejected():
    """どの観測値でもない数字が採用値に入っている＝計算か捏造。"""
    def m(d):
        write_fund(d, fund_line("FY2025-06", "eps", 30.0, "JPY",
                                status="OK|ROUNDING", primary=29.1, secondary=29.1,
                                tolerance=0.1, observed=[29.1, 29.07]))
    rep = run(make_data(m))
    expect(rep, checks.FAIL, "fundamentals", "どの観測値とも一致しない")


def test_fundamentals_observation_outside_tolerance_is_rejected():
    """許容幅の外にある観測値があるのに OK になっている（照合の破綻）。"""
    def m(d):
        write_fund(d, fund_line("FY2025-06", "eps", 29.07, "JPY",
                                status="OK|ROUNDING", primary=29.1, secondary=29.1,
                                tolerance=0.1, observed=[29.1, 29.07, 25.0]))
    rep = run(make_data(m))
    expect(rep, checks.FAIL, "fundamentals", "許容幅を超えて観測値と離れている")


def test_fundamentals_runaway_tolerance_is_warned():
    """許容幅が膨らむと「許容内か」の検査は何でも通す。外から幅を縛る。"""
    def m(d):
        write_fund(d, fund_line("FY2025-06", "eps", 29.07, "JPY",
                                status="OK|ROUNDING", primary=29.1, secondary=29.1,
                                tolerance=10.0, observed=[29.1, 29.07]))
    rep = run(make_data(m))
    expect(rep, checks.WARN, "fundamentals", "許容幅が採用値の10%を超えている")


def test_fundamentals_missing_source_url_is_rejected():
    def m(d):
        line = fund_line("FY2025-06", "revenue", 1844, "JPY_million")
        write_fund(d, line.replace("https://example.com/primary", ""))
    rep = run(make_data(m))
    expect(rep, checks.FAIL, "fundamentals", "source_url または fetched_at が空")


def test_fundamentals_no_adopted_row_is_warned():
    def m(d):
        write_fund(d, fund_line("2025-06", "revenue", None, "JPY_million",
                                status="SINGLE_SOURCE", primary=1844, secondary=""))
    rep = run(make_data(m))
    expect(rep, checks.WARN, "fundamentals", "照合が1件も成立していない")


def test_fundamentals_missing_directory_is_warned():
    """データが無いこと自体が「レポートは無検証」という報告すべき事実。"""
    def m(d):
        if (d / "fundamentals").exists():
            shutil.rmtree(d / "fundamentals")
    rep = run(make_data(m))
    expect(rep, checks.WARN, "fundamentals", "機械照合されていない")


# --- 範囲 ---------------------------------------------------------------------

def test_fundamentals_ratio_out_of_range_is_rejected():
    """原価率 683% は小数点の位置を取り違えている。"""
    def m(d):
        write_fund(d, fund_line("2025-06", "cost_ratio_pct", 683.0, "pct"))
    rep = run(make_data(m))
    expect(rep, checks.FAIL, "fundamentals", "採用値の比率が")


def test_fundamentals_ratio_slightly_over_100_is_warned_not_fatal():
    """赤字期の販管費率は 100% を超える（実データ 4937 FY2019-09 は 105.12%）。

    ここを FAIL にすると、**採用値が増えた瞬間にビルドが止まる**。
    実在しうる値は WARN に留め、明らかな桁違いだけを FAIL にする。
    """
    def m(d):
        write_fund(d, fund_line("2025-06", "sga_ratio_pct", 105.12, "pct"))
    rep = run(make_data(m))
    expect_none(rep, checks.FAIL, "fundamentals")
    expect(rep, checks.WARN, "fundamentals", "比率が 0〜100% の外にある")


def test_fundamentals_out_of_range_observation_is_seen_even_when_not_adopted():
    """採用値でなくても、観測値の桁違いは表に出す。

    範囲検査が採用値にしか効いていなかったため、範囲外の値が
    SINGLE_SOURCE で `value` 空のまま入っているあいだは**完全に無検査**で、
    2ソース一致した週にいきなり FAIL していた。
    """
    def m(d):
        write_fund(d, fund_line("2025-06", "cost_ratio_pct", None, "pct",
                                status="SINGLE_SOURCE", primary=685.0,
                                sources=["irbank_pl"]))
    rep = run(make_data(m))
    expect(rep, checks.WARN, "fundamentals", "観測値の比率が")


def test_fundamentals_ratio_in_range_is_accepted():
    def m(d):
        write_fund(d, fund_line("2025-06", "cost_ratio_pct", 68.3, "pct"))
    rep = run(make_data(m))
    expect_none(rep, checks.FAIL, "fundamentals")


def test_fundamentals_negative_roe_is_not_flagged():
    """ROE は赤字なら負・自己資本が薄ければ100%超。範囲検査の対象にしない。"""
    def m(d):
        write_fund(d, fund_line("2025-06", "roe_pct", -42.5, "pct"),
                   fund_line("2024-06", "roe_pct", 130.0, "pct"))
    rep = run(make_data(m))
    expect_none(rep, checks.FAIL, "fundamentals")


# --- 恒等式 -------------------------------------------------------------------

def _pl_lines(op_value):
    return (fund_line("2025-06", "revenue", 1844, "JPY_million"),
            fund_line("2025-06", "cost_of_sales", 1261, "JPY_million"),
            fund_line("2025-06", "sga", 663, "JPY_million"),
            fund_line("2025-06", "operating_income", op_value, "JPY_million"))


def test_fundamentals_pl_identity_holds():
    """1844 − 1261 − 663 = −80。正しい組は FAIL にしない。"""
    rep = run(make_data(lambda d: write_fund(d, *_pl_lines(-80))))
    expect_none(rep, checks.FAIL, "fundamentals")


def test_fundamentals_pl_identity_broken_is_rejected():
    """営業利益の符号を取り違えたケース（−80 を +80 と読む）。"""
    rep = run(make_data(lambda d: write_fund(d, *_pl_lines(80))))
    expect(rep, checks.FAIL, "fundamentals", "営業利益と合わない")


def test_fundamentals_ratio_identity_holds():
    """原価率 + 販管費率 + 営業利益率 = 100%（実額が無くても同じ恒等式が立つ）。"""
    def m(d):
        write_fund(d,
                   fund_line("FY2025-06", "cost_ratio", 68.4, "pct"),
                   fund_line("FY2025-06", "sga_ratio", 35.9, "pct"),
                   fund_line("FY2025-06", "operating_margin", -4.3, "pct"))
    rep = run(make_data(m))
    expect_none(rep, checks.FAIL, "fundamentals")


def test_fundamentals_ratio_identity_broken_is_rejected():
    def m(d):
        write_fund(d,
                   fund_line("FY2025-06", "cost_ratio", 68.4, "pct"),
                   fund_line("FY2025-06", "sga_ratio", 35.9, "pct"),
                   fund_line("FY2025-06", "operating_margin", 4.3, "pct"))
    rep = run(make_data(m))
    expect(rep, checks.FAIL, "fundamentals", "営業利益と合わない")


def test_fundamentals_uncheckable_identities_are_declared():
    """材料が無くて1期も検算できていないことを「異常なし」と見せない。"""
    def m(d):
        write_fund(d, fund_line("FY2025-06", "revenue", 1844, "JPY_million"))
    rep = run(make_data(m))
    expect(rep, checks.WARN, "fundamentals", "1期も検算できていない整合検査")


def test_fundamentals_equity_ratio_consistent():
    """250 ÷ 2520 = 9.92%。表示が 9.9% でも丸めの範囲内なので FAIL にしない。"""
    def m(d):
        write_fund(d,
                   fund_line("2025-06", "equity", 250, "JPY_million"),
                   fund_line("2025-06", "total_assets", 2520, "JPY_million"),
                   fund_line("2025-06", "equity_ratio_pct", 9.9, "pct"))
    rep = run(make_data(m))
    expect_none(rep, checks.FAIL, "fundamentals")


def test_fundamentals_equity_ratio_inconsistent_is_rejected():
    def m(d):
        write_fund(d,
                   fund_line("2025-06", "equity", 250, "JPY_million"),
                   fund_line("2025-06", "total_assets", 2520, "JPY_million"),
                   fund_line("2025-06", "equity_ratio_pct", 19.9, "pct"))
    rep = run(make_data(m))
    expect(rep, checks.FAIL, "fundamentals", "自己資本÷総資産")


# --- 桁 -----------------------------------------------------------------------

def test_fundamentals_scale_jump_is_rejected():
    """売上が前期比10倍。千円と百万円の取り違えはここに出る。"""
    def m(d):
        write_fund(d,
                   fund_line("2025-06", "revenue", 1844, "JPY_million"),
                   fund_line("2026-06", "revenue", 18440, "JPY_million"))
    rep = run(make_data(m))
    expect(rep, checks.FAIL, "fundamentals", "前期比で桁が飛んでいる")


def test_fundamentals_normal_growth_is_not_flagged():
    def m(d):
        write_fund(d,
                   fund_line("2025-06", "revenue", 1844, "JPY_million"),
                   fund_line("2026-06", "revenue", 2400, "JPY_million"))
    rep = run(make_data(m))
    expect_none(rep, checks.FAIL, "fundamentals")


def _jump_with_confirmation(source_url: str):
    """10倍の前期比 ＋ 確認記録。`source_url` の有無で扱いが変わるのを見る。"""
    def m(d):
        write_fund(d,
                   fund_line("2025-06", "revenue", 1844, "JPY_million"),
                   fund_line("2026-06", "revenue", 18440, "JPY_million"))
        (d / "fundamentals_confirmations.yaml").write_text(
            "confirmations:\n"
            "  - code: \"4073\"\n"
            "    metric: revenue\n"
            "    from_period: \"2025-06\"\n"
            "    to_period: \"2026-06\"\n"
            "    reason: \"創業期の実成長\"\n"
            f"    source_url: \"{source_url}\"\n",
            encoding="utf-8")
    return m


def test_fundamentals_scale_jump_confirmed_becomes_warn():
    """一次情報ではなく取得元そのもので確認した前期比は WARN に落ちる。

    創業期の会社は売上が本当に10倍動く（実測: 5137 スマートドライブ 8→97百万円）。
    **消えるのではなく WARN として残る**ので、なぜ通したかが検査結果に出続ける。
    """
    rep = run(make_data(_jump_with_confirmation("https://example.com/evidence")))
    expect_none(rep, checks.FAIL, "fundamentals")
    expect(rep, checks.WARN, "fundamentals", "確認済み: 創業期の実成長")


def test_fundamentals_scale_jump_confirmed_without_source_stays_fail():
    """出典なしの「確認済み」は確認していないのと同じ（分割の確認記録と同じ扱い）。"""
    rep = run(make_data(_jump_with_confirmation("")))
    expect(rep, checks.FAIL, "fundamentals", "前期比で桁が飛んでいる")
    expect(rep, checks.FAIL, "fundamentals", "確認記録はあるが source_url が空")


def test_fundamentals_scale_jump_confirmation_does_not_leak_to_other_periods():
    """確認記録は (code, metric, 期, 期) の1組だけに効く。別の期には効かない。"""
    def m(d):
        write_fund(d,
                   fund_line("2025-06", "revenue", 1844, "JPY_million"),
                   fund_line("2026-06", "revenue", 18440, "JPY_million"),
                   fund_line("2027-06", "revenue", 400, "JPY_million"))
        (d / "fundamentals_confirmations.yaml").write_text(
            "confirmations:\n"
            "  - code: \"4073\"\n"
            "    metric: revenue\n"
            "    from_period: \"2025-06\"\n"
            "    to_period: \"2026-06\"\n"
            "    reason: \"創業期の実成長\"\n"
            "    source_url: \"https://example.com/evidence\"\n",
            encoding="utf-8")
    rep = run(make_data(m))
    expect(rep, checks.FAIL, "fundamentals", "前期比で桁が飛んでいる")


def test_fundamentals_profit_swing_is_not_flagged_as_digit_error():
    """小型株の利益は 5 → 386 のように実際に動く。桁検査の対象にしない。"""
    def m(d):
        write_fund(d,
                   fund_line("2019-06", "operating_income", 5, "JPY_million"),
                   fund_line("2020-06", "operating_income", 386, "JPY_million"))
    rep = run(make_data(m))
    expect_none(rep, checks.FAIL, "fundamentals")


def test_fundamentals_quarter_and_annual_are_not_compared():
    """1Q累計と通期を「前期比10倍」として比較しない（期の形が違う）。"""
    def m(d):
        write_fund(d,
                   fund_line("2025-06Q1cum", "revenue", 200, "JPY_million"),
                   fund_line("2025-06", "revenue", 1844, "JPY_million"))
    rep = run(make_data(m))
    expect_none(rep, checks.FAIL, "fundamentals")


# --- 符号 ---------------------------------------------------------------------

def test_fundamentals_margin_sign_mismatch_is_rejected():
    def m(d):
        write_fund(d,
                   fund_line("2025-06", "operating_income", -80, "JPY_million"),
                   fund_line("2025-06", "operating_margin_pct", 4.3, "pct"))
    rep = run(make_data(m))
    expect(rep, checks.FAIL, "fundamentals", "符号が一致しない")


def test_fundamentals_margin_sign_match_is_accepted():
    def m(d):
        write_fund(d,
                   fund_line("2025-06", "operating_income", -80, "JPY_million"),
                   fund_line("2025-06", "operating_margin_pct", -4.3, "pct"))
    rep = run(make_data(m))
    expect_none(rep, checks.FAIL, "fundamentals")


def test_fundamentals_standalone_quarter_is_not_mixed_with_full_year():
    """決算期末月に終わる4Q単独を、通期と同じ組に入れない。

    実データで踏んだ（4707 キタック・10月決算）。`FY2025-10` と
    `Q2025-08_2025-10` はどちらも year=2025 / month=10 / quarter=None /
    cumulative=False で、`standalone` を鍵から落とすと同じ組になる。
    通期の営業利益（黒字）と4Q単独の営業利益率（赤字）が突き合わされて、
    符号の検査が誤って FAIL していた。**別の期なので比べてはいけない。**
    """
    def m(d):
        write_fund(d,
                   fund_line("FY2025-10", "operating_income", 146,
                             "JPY_million"),
                   fund_line("FY2025-10", "operating_margin_pct", 4.21, "pct"),
                   fund_line("Q2025-08_2025-10", "operating_income", -20,
                             "JPY_million"),
                   fund_line("Q2025-08_2025-10", "operating_margin_pct", -2.2,
                             "pct"))
    rep = run(make_data(m))
    expect_none(rep, checks.FAIL, "fundamentals")


def test_fundamentals_standalone_quarter_sign_is_still_checked():
    """組を分けても、単独四半期の中の符号違いは今までどおり FAIL にする。

    上の修正が「4Q単独を検査から外す」ことにならないのを押さえる。
    """
    def m(d):
        write_fund(d,
                   fund_line("FY2025-10", "operating_income", 146,
                             "JPY_million"),
                   fund_line("FY2025-10", "operating_margin_pct", 4.21, "pct"),
                   fund_line("Q2025-08_2025-10", "operating_income", -20,
                             "JPY_million"),
                   fund_line("Q2025-08_2025-10", "operating_margin_pct", 2.2,
                             "pct"))
    rep = run(make_data(m))
    expect(rep, checks.FAIL, "fundamentals", "符号が一致しない")


# --- 四半期累計と通期計画 -------------------------------------------------------

def test_fundamentals_cumulative_over_plan_is_warned():
    def m(d):
        write_fund(d,
                   fund_line("2026-06Q3cum", "revenue", 2500, "JPY_million"),
                   fund_line("2026-06予", "revenue", 2400, "JPY_million"))
    rep = run(make_data(m))
    expect(rep, checks.WARN, "fundamentals", "四半期累計が通期計画を超えている")


def test_fundamentals_cumulative_under_plan_is_not_warned():
    def m(d):
        write_fund(d,
                   fund_line("2026-06Q3cum", "revenue", 1200, "JPY_million"),
                   fund_line("2026-06予", "revenue", 2400, "JPY_million"))
    rep = run(make_data(m))
    expect_none(rep, checks.WARN, "fundamentals", "四半期累計が通期計画を超えている")


# --- append-only の一意キーが解決できているか -----------------------------------

def test_fundamentals_append_is_not_flagged_as_rewrite():
    """期を追記しただけで「過去行が変更されている」と言わない。

    一意キーの解決（code+period+metric）が壊れて ("code","date") に落ちると、
    全行が同じキーに潰れて追記が改変として報告される。その退行を捕まえる。
    """
    old = fund_line("2025-06", "revenue", 1844, "JPY_million")
    new = fund_line("2026-06", "revenue", 2400, "JPY_million")
    baseline = make_data(lambda d: write_fund(d, old))
    target = make_data(lambda d: write_fund(d, old, new))
    rep = run(target, baseline)
    expect_none(rep, checks.FAIL, "append_only", "fundamentals")


def test_fundamentals_past_row_rewrite_is_detected():
    old = fund_line("2025-06", "revenue", 1844, "JPY_million")
    tampered = fund_line("2025-06", "revenue", 1900, "JPY_million")
    baseline = make_data(lambda d: write_fund(d, old))
    target = make_data(lambda d: write_fund(d, tampered))
    rep = run(target, baseline)
    expect(rep, checks.FAIL, "append_only", "過去行が変更されている")


# =============================================================================
# 11. レポートの数値 と 検証済み数値 の突合
# =============================================================================

def _fund_revenue_2025(value=1844) -> str:
    return fund_line("2025-06", "revenue", value, "JPY_million")


def test_report_number_matches_verified_value():
    """18.4億円 と 1,844百万円 は同じ値。単位が違っても突合する。"""
    def m(d):
        write_fund(d, _fund_revenue_2025())
        write_report(d, revenue_chart(18.4))
    rep = run(make_data(m))
    expect_none(rep, checks.FAIL, "report")
    expect(rep, checks.WARN, "report", "突合 1 /")


def test_report_digit_error_is_detected():
    """桁を1つ落とした転記（18.4 → 1.84億円）。"""
    def m(d):
        write_fund(d, _fund_revenue_2025())
        write_report(d, revenue_chart(1.84))
    rep = run(make_data(m))
    expect(rep, checks.FAIL, "report", "検証済みの採用値と食い違う")


def test_report_transposed_digits_are_detected():
    """数字の入れ替え（18.4 → 14.8億円）。丸めスラックでは吸収されない。"""
    def m(d):
        write_fund(d, _fund_revenue_2025())
        write_report(d, revenue_chart(14.8))
    rep = run(make_data(m))
    expect(rep, checks.FAIL, "report", "検証済みの採用値と食い違う")


def test_report_rounding_is_not_a_mismatch():
    """1,844百万円 を 18.4億円 と丸めて書くのは正しい。FAIL にしない。"""
    def m(d):
        write_fund(d, _fund_revenue_2025(1844))
        write_report(d, revenue_chart(18.4))
    rep = run(make_data(m))
    expect_none(rep, checks.FAIL, "report")


def test_report_wrong_period_is_not_matched_to_another_period():
    """会社計画（2026/6予）と実績（2025/6）を同じ期として突き合わせない。"""
    def m(d):
        write_fund(d, _fund_revenue_2025())
        write_report(d, revenue_chart(24.0, label="2026/6予"))
    rep = run(make_data(m))
    expect_none(rep, checks.FAIL, "report")
    expect(rep, checks.WARN, "report", "対応する検証済み数値が無い")


def test_report_table_number_is_compared():
    """本文の表からも数値を拾う（front matter だけでは足りない）。"""
    front = 'code: "4073"\nname: "テスト"\n'
    body = ("## 財務の推移と健全性\n\n"
            "| 指標 | 直近（26/6期 3Q） | 意味 |\n"
            "|---|---:|---|\n"
            "| 自己資本比率 | 19.9% | 総資産のうち自前の資本 |\n")

    def m(d):
        write_fund(d, fund_line("2026-06Q3", "equity_ratio_pct", 9.9, "pct"))
        write_report(d, front, body)
    rep = run(make_data(m))
    expect(rep, checks.FAIL, "report", "検証済みの採用値と食い違う")


def test_report_table_number_matching_is_accepted():
    front = 'code: "4073"\nname: "テスト"\n'
    body = ("## 財務の推移と健全性\n\n"
            "| 指標 | 直近（26/6期 3Q） | 意味 |\n"
            "|---|---:|---|\n"
            "| 自己資本比率 | 9.9% | 総資産のうち自前の資本 |\n")

    def m(d):
        write_fund(d, fund_line("2026-06Q3", "equity_ratio_pct", 9.9, "pct"))
        write_report(d, front, body)
    rep = run(make_data(m))
    expect_none(rep, checks.FAIL, "report")
    expect(rep, checks.WARN, "report", "突合 1 /")


def test_report_unverified_value_is_surfaced():
    """照合が成立していない数値をレポートが断定している状態を可視化する。"""
    def m(d):
        write_fund(d, fund_line("2025-06", "revenue", None, "JPY_million",
                                status="MISMATCH", primary=1844, secondary=1850))
        write_report(d, revenue_chart(18.4))
    rep = run(make_data(m))
    expect(rep, checks.WARN, "report", "検証が成立していない")


def test_report_without_fundamentals_counts_everything_as_unmatched():
    """検証済みデータが無い状態を「異常なし」と表示しない。"""
    def m(d):
        if (d / "fundamentals").exists():
            shutil.rmtree(d / "fundamentals")
        write_report(d, revenue_chart(18.4))
    rep = run(make_data(m))
    expect(rep, checks.WARN, "report", "突合 0 / 未突合 1")


def test_report_extraction_failure_is_warned():
    """数値を1件も抽出できない＝抽出器が壊れている可能性。黙らせない。"""
    def m(d):
        write_report(d, 'code: "4073"\nname: "テスト"\n', "本文だけのレポート。")
    rep = run(make_data(m))
    expect(rep, checks.WARN, "report", "数値を1件も抽出できていない")


# --- 図型2種（timeline / diagram）の書き方 -----------------------------------
#
# 値そのものは chartdata が検証済み CSV から引くか、そもそも持たない（diagram）。
# checks が見るのは書き方だけ: 数字の混入した定性図は chartdata / chart の
# 両方が描画を拒否するので、黙って「描けず」になる前に名指しで知らせる。

def _diagram_front(note: str) -> str:
    return ('code: "4073"\nname: "テスト"\n'
            'charts:\n'
            '  biz_model:\n'
            '    type: diagram\n'
            '    steps:\n'
            f'      - {{label: "開発案件", note: "{note}"}}\n')


def test_report_diagram_with_digits_is_flagged():
    def m(d):
        write_report(d, _diagram_front("1件あたり大きい"))
    rep = run(make_data(m))
    expect(rep, checks.WARN, "report", "steps[0] に数字が入っている")


def test_report_diagram_without_digits_is_quiet():
    """対照: 数字の無い定性図に誤検知しない。"""
    def m(d):
        write_report(d, _diagram_front("案件ごとに大きく入る"))
    rep = run(make_data(m))
    expect_none(rep, checks.WARN, "report", "数字が入っている")


def _timeline_front(events: str) -> str:
    return ('code: "4073"\nname: "テスト"\n'
            'charts:\n'
            '  events_52w:\n'
            '    type: timeline\n'
            '    source: {dataset: prices, window_weeks: 52}\n'
            '    events:\n'
            + events)


def test_report_timeline_unsorted_events_are_flagged():
    """events は日付昇順で書く（periods の昇順規則と同じ）。"""
    def m(d):
        write_report(d, _timeline_front(
            '      - {date: "2026-07-21", label: "大型案件"}\n'
            '      - {date: "2026-06-24", label: "安値"}\n'))
    rep = run(make_data(m))
    expect(rep, checks.WARN, "report", "日付の昇順でない")


def test_report_timeline_bad_event_date_is_flagged():
    def m(d):
        write_report(d, _timeline_front(
            '      - {date: "来週", label: "出来事"}\n'))
    rep = run(make_data(m))
    expect(rep, checks.WARN, "report", "date を読めない")


def test_report_timeline_sorted_events_are_quiet():
    """対照: 昇順で日付が読めるなら図型の指摘は出ない。"""
    def m(d):
        write_report(d, _timeline_front(
            '      - {date: "2026-06-24", label: "安値"}\n'
            '      - {date: "2026-07-21", label: "大型案件"}\n'))
    rep = run(make_data(m))
    expect_none(rep, checks.WARN, "report", "図の指定が読者の見え方と食い違う")


def test_period_notations_are_normalized():
    """突合の土台。表記が違っても同じ期として扱えること／違う期は混ぜないこと。"""
    same = [("2026-06Q3", "26/6 3Q"), ("2026-06", "2026/6"), ("2026-06予", "26/6予")]
    for a, b in same:
        pa, pb = checks.parse_period(a), checks.parse_period(b)
        assert pa is not None and pb is not None, f"{a} / {b} を解釈できない"
        assert pa.matches(pb), f"{a} と {b} が同じ期として扱われていない"
    differ = [("2026-06", "2026-06予"), ("2026-06", "2026-06Q3"),
              ("2026-06Q3cum", "2026-06Q3"), ("2025-06", "2026-06")]
    for a, b in differ:
        pa, pb = checks.parse_period(a), checks.parse_period(b)
        assert not pa.matches(pb), f"{a} と {b} を同じ期として扱っている"


def test_extractor_period_keys_are_understood():
    """`fetch_fundamentals` が実際に書く期間キーをそのまま解釈できること。

    ここが解釈できないと、抽出器が正しく動いていても突合は全件が未突合になり、
    レポートの数値は無検証のまま「異常なし」に見える。
    """
    fy = checks.parse_period("FY2026-06")
    assert fy is not None and (fy.year, fy.month) == (2026, 6)
    assert fy.quarter is None and not fy.cumulative and not fy.standalone

    cum = checks.parse_period("C2025-07_2026-03")     # 3Q累計（6月期）
    assert cum is not None and cum.cumulative and cum.quarter == 3
    assert (cum.year, cum.month) == (2026, 6), cum.label()

    half = checks.parse_period("H2025-07_2025-12")    # 2Q累計（6月期）
    assert half is not None and half.cumulative and half.quarter == 2
    assert (half.year, half.month) == (2026, 6), half.label()

    solo = checks.parse_period("Q2026-01_2026-03")    # 単独3か月
    assert solo is not None and solo.standalone
    assert not solo.matches(checks.parse_period("FY2026-03")), "単独四半期を通期と混ぜている"
    assert not cum.matches(checks.parse_period("FY2026-06")), "累計を通期と混ぜている"


def test_extractor_metric_names_are_understood():
    """抽出器の metric 名（cost_ratio 等）が語彙に載っていること。"""
    assert checks.metric_of("cost_ratio") == "cost_ratio_pct"
    assert checks.metric_of("equity_ratio") == "equity_ratio_pct"
    assert checks.metric_of("sga_ratio") == "sga_ratio_pct"
    assert checks.metric_of("operating_margin") == "operating_margin_pct"
    assert checks.metric_of("roe") == "roe_pct"
    assert checks.metric_of("interest_bearing_debt_ratio") \
        == "interest_bearing_debt_ratio"
    assert checks.split_plan_suffix("revenue_plan") == ("revenue", True)


def test_report_plan_value_matches_plan_metric():
    """会社計画は metric 名の `_plan` で表現される（period には出ない）。"""
    def m(d):
        write_fund(d, fund_line("FY2026-06", "revenue_plan", 2403, "JPY_million"))
        write_report(d, revenue_chart(24.0, label="2026/6予"))
    rep = run(make_data(m))
    expect_none(rep, checks.FAIL, "report")
    expect(rep, checks.WARN, "report", "突合 1 /")


def test_report_quarter_end_balance_matches_period_end_key():
    """「26/6期 3Q」の残高は、抽出側では四半期末（FY2026-03）で持たれている。"""
    front = 'code: "4073"\nname: "テスト"\n'
    body = ("## 財務の推移と健全性\n\n"
            "| 指標 | 直近（26/6期 3Q） |\n"
            "|---|---:|\n"
            "| 自己資本比率 | 9.9% |\n")

    def m(d):
        write_fund(d, fund_line("FY2026-03", "equity_ratio", 9.9, "pct"))
        write_report(d, front, body)
    rep = run(make_data(m))
    expect_none(rep, checks.FAIL, "report")
    expect(rep, checks.WARN, "report", "突合 1 /")


def test_report_quarter_matches_cumulative_span_key():
    """「26/6期 3Q」は抽出側では累計の期間キー（C2025-07_2026-03）で持たれている。"""
    front = 'code: "4073"\nname: "テスト"\n'
    body = ("## 財務の推移と健全性\n\n"
            "| 指標 | 直近（26/6期 3Q） |\n"
            "|---|---:|\n"
            "| 自己資本比率 | 9.9% |\n")

    def m(d):
        write_fund(d, fund_line("C2025-07_2026-03", "equity_ratio", 9.9, "pct"))
        write_report(d, front, body)
    rep = run(make_data(m))
    expect_none(rep, checks.FAIL, "report")
    expect(rep, checks.WARN, "report", "突合 1 /")


def test_report_quarter_does_not_match_standalone_quarter():
    """単独3か月（Q…）は累計と別の量。突き合わせない。"""
    front = 'code: "4073"\nname: "テスト"\n'
    body = ("## 財務の推移と健全性\n\n"
            "| 指標 | 直近（26/6期 3Q） |\n"
            "|---|---:|\n"
            "| 営業利益 | 68百万円 |\n")

    def m(d):
        write_fund(d, fund_line("Q2026-01_2026-03", "operating_income", -78,
                                "JPY_million"))
        write_report(d, front, body)
    rep = run(make_data(m))
    expect_none(rep, checks.FAIL, "report")
    expect(rep, checks.WARN, "report", "対応する検証済み数値が無い")


def test_report_ratio_and_multiple_are_the_same_quantity():
    """有利子負債倍率は kabutan が「倍」、IR BANK が「%」。同じ量として突合する。"""
    front = 'code: "4073"\nname: "テスト"\n'
    body = ("## 財務の推移と健全性\n\n"
            "| 指標 | FY2026-06 |\n"
            "|---|---:|\n"
            "| 有利子負債倍率 | 7.99倍 |\n")

    def m(d):
        write_fund(d, fund_line("FY2026-06", "interest_bearing_debt_ratio",
                                799.0, "pct"))
        write_report(d, front, body)
    rep = run(make_data(m))
    expect_none(rep, checks.FAIL, "report")
    expect(rep, checks.WARN, "report", "突合 1 /")


def test_metric_aliases_prefer_the_longest_match():
    """「営業利益率」を「営業利益」に結び付けない（比率と実額の取り違え）。"""
    assert checks.metric_of("営業利益") == "operating_income"
    assert checks.metric_of("営業利益率") == "operating_margin_pct"
    assert checks.metric_of("売上原価") == "cost_of_sales"
    assert checks.metric_of("売上原価率") == "cost_ratio_pct"
    assert checks.metric_of("自己資本") == "equity"
    assert checks.metric_of("自己資本比率") == "equity_ratio_pct"
    assert checks.metric_of("よく分からない項目") is None


# =============================================================================
# 12. 出典URLの死活監視
# =============================================================================

LINK_HEADER = ",".join(checks.LINK_STATUS_FIELDS)


def write_link_status(d: Path, *lines: str) -> None:
    (d / "link_status.csv").write_text(
        "\n".join([LINK_HEADER, *lines]) + "\n", encoding="utf-8")


def _report_with_url(url: str) -> tuple[str, str]:
    front = 'code: "4073"\nname: "テスト"\n'
    body = f"## 出典\n\n| 内容 | 出典 |\n|---|---|\n| 会社概要 | <{url}> |\n"
    return front, body


def test_links_never_checked_are_listed():
    """既定はネットワークを使わない。**未確認であること**を表に出す。"""
    front, body = _report_with_url("https://example.com/ir")
    rep = run(make_data(lambda d: write_report(d, front, body)))
    expect(rep, checks.WARN, "links", "一度も死活確認していない")
    expect_none(rep, checks.FAIL, "links")


def test_links_previously_dead_is_reported_without_network():
    url = "https://example.com/gone"
    front, body = _report_with_url(url)

    def m(d):
        write_report(d, front, body)
        write_link_status(d, f"2026-08-13T10:00:00+09:00,4073,{url},404,false,")
    rep = run(make_data(m))
    expect(rep, checks.WARN, "links", "前回の記録で到達できなかった")


def test_links_previously_alive_is_not_reported():
    url = "https://example.com/alive"
    front, body = _report_with_url(url)

    def m(d):
        write_report(d, front, body)
        write_link_status(d, f"2026-08-13T10:00:00+09:00,4073,{url},200,true,")
    rep = run(make_data(m))
    expect_none(rep, checks.WARN, "links", "前回の記録で到達できなかった")
    expect_none(rep, checks.WARN, "links", "一度も死活確認していない")


def test_links_latest_record_wins():
    """append-only なので同じURLに複数行が積まれる。最後の記録で判断する。"""
    url = "https://example.com/recovered"
    front, body = _report_with_url(url)

    def m(d):
        write_report(d, front, body)
        write_link_status(d,
                          f"2026-08-01T10:00:00+09:00,4073,{url},404,false,",
                          f"2026-08-13T10:00:00+09:00,4073,{url},200,true,")
    rep = run(make_data(m))
    expect_none(rep, checks.WARN, "links", "前回の記録で到達できなかった")


def test_link_status_append_is_not_flagged_as_rewrite():
    """link_status.csv の一意キー（url+checked_at）が解決できているか。"""
    url = "https://example.com/ir"
    old = f"2026-08-01T10:00:00+09:00,4073,{url},200,true,"
    new = f"2026-08-13T10:00:00+09:00,4073,{url},200,true,"
    front, body = _report_with_url(url)

    def base(d):
        write_report(d, front, body)
        write_link_status(d, old)

    def after(d):
        write_report(d, front, body)
        write_link_status(d, old, new)
    rep = run(make_data(after), make_data(base))
    expect_none(rep, checks.FAIL, "append_only", "link_status")


def test_link_collection_finds_front_matter_and_body_urls():
    front = ('code: "4073"\nname: "テスト"\n'
             'links:\n  - {label: "IR", url: "https://example.com/ir"}\n')
    body = "本文の出典 <https://example.com/body> を見る。"
    data_dir = make_data(lambda d: write_report(d, front, body))
    urls = [u for _, u in checks.collect_report_urls(data_dir.parent / "reports")]
    assert "https://example.com/ir" in urls, urls
    assert "https://example.com/body" in urls, urls


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
