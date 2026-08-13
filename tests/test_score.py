"""score.py のテスト。

方針:
  - **F-03 の回帰**: 株価・信用残から引ける metric が、KPI が1本も無い状態でも解決できること。
  - **F-13 の回帰**: 採点が「CSV の最終行」ではなく **resolve_by 時点の値**で行われること。
  - **未計算を「外れ」に丸めない**こと（判定不能は expired であって miss ではない）。
  - 期限前に先取りで確定しないこと。確定した予測を後から書き換えないこと。
  - 実データ（append-only）で例外なく解決でき、2回実行しても同一結果になること。
  - **実データの日付・値をべた書きしない。** 期待値は `realdata` かデータ自身から
    引く。「今週 4937 は雲の上」のようなその週の事実を期待値にすると、
    値動き次第でテストが落ちる（テストが壊れるのであって、コードは壊れていない）。

実行:
  $env:PYTHONIOENCODING = "utf-8"; python tests/test_score.py
"""
from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import score as S  # noqa: E402
import realdata as rd  # noqa: E402

# 採点の基準日。**確定している（2ソース照合を通った）最後の営業日**を使う。
# 最新営業日は片方の取得元がまだ当日分を出しておらず照合が成立しないことがあり、
# その日を基準にすると指標が1本も出ない。日付を直書きすると、データが1日進んだ
# だけで「基準日の指標が無い」以外の理由でも前提がずれていく。
AS_OF = rd.last_confirmed_date()


# --- 検証ヘルパ ---------------------------------------------------------------

def eq(actual, expected, label=""):
    assert actual == expected, f"{label}: expected {expected!r}, got {actual!r}"


def is_none(actual, label=""):
    assert actual is None, f"{label}: expected None, got {actual!r}"


def close_to(actual, expected, label="", tol=1e-6):
    assert actual is not None, f"{label}: expected {expected!r}, got None"
    assert abs(actual - expected) <= tol, f"{label}: expected {expected!r}, got {actual!r}"


def pred(**over) -> dict:
    p = {"id": "P-TEST-01", "code": rd.codes()[2], "metric": "avg_turnover_20d",
         "operator": ">", "reference": 30000000, "resolve_by": AS_OF,
         "confidence": 0.5, "status": "open"}
    p.update(over)
    return p


def tmp_repo(kpi_rows: str | None = None, code: str = "9999") -> S.Repo:
    """kpi CSV だけを持つ一時リポジトリ。KPI 経路の検証に使う。"""
    d = Path(tempfile.mkdtemp(prefix="kabu-score-"))
    (d / "data" / "kpi").mkdir(parents=True)
    if kpi_rows is not None:
        (d / "data" / "kpi" / f"{code}.csv").write_text(kpi_rows, encoding="utf-8")
    return S.Repo(root=d)


def repo_without_kpi() -> S.Repo:
    """実データの `data/` から **`data/kpi/` を外した**一時リポジトリ。

    F-03 は「KPI が1本も無いと、株価から算出できる metric まで判定不能になる」
    という退行。実リポジトリの `data/kpi/` が空であることを前提に置くと、
    決算を1件取り込んだ瞬間にこの回帰テストは（バグが再発しても）素通りする。
    """
    d = Path(tempfile.mkdtemp(prefix="kabu-score-nokpi-"))
    shutil.copytree(ROOT / "data", d / "data")
    shutil.rmtree(d / "data" / "kpi", ignore_errors=True)
    return S.Repo(root=d)


def margin_latest(code: str) -> dict:
    """信用残の最新行（採点が実際に使う行）。期待値はここから引く。"""
    rows = S.Repo().margin_rows(code)
    assert rows, f"{code} の信用残が無い"
    return rows[-1]


def codes_with_margin_ratio() -> list[str]:
    return [c for c in rd.codes()
            if str(margin_latest(c).get("ratio") or "").strip()]


def codes_with_ratio_na() -> list[str]:
    """信用倍率が構造的に定義できない銘柄（制度信用が買建のみ・D27）。"""
    return [c for c in rd.codes()
            if "RATIO_NA" in str(margin_latest(c).get("status") or "")]


_KPI_HEADER = "date,code,metric,value,unit,definition,assumed,source_url,fetched_at\n"


def _kpi_row(date, metric, value, unit="JPY_million", definition="FY2027Q1cum|連結|日本基準|x"):
    return (f"{date},9999,{metric},{value},{unit},{definition},false,"
            f"https://example.invalid/,2026-05-14T18:00:00+09:00\n")


# =============================================================================
# 経路① 株価（F-03 の回帰。KPI が無くても解決できること）
# =============================================================================

def test_price_metric_resolves_without_any_kpi():
    """★F-03 の回帰テスト。

    旧実装は全 metric を data/kpi/{code}.csv に向けていたため、株価から算出できる
    avg_turnover_20d まで判定不能になっていた。KPI を**外した**複製で確かめるので、
    実リポジトリに決算が入っても回帰テストとして生き続ける。
    """
    repo = repo_without_kpi()
    for code in rd.codes():
        mv = S.resolve_metric(code, "avg_turnover_20d", AS_OF, repo)
        eq(mv.source, S.SRC_PRICE, f"{code}: 経路は price")
        assert mv.value is not None, f"{code}: 株価から解決できるはず: {mv.detail}"
        eq(mv.as_of, rd.last_confirmed_date(code), f"{code}: 値の基準日")


def test_price_metrics_all_resolve_on_real_data():
    repo = S.Repo()
    price_metrics = [m.name for m in S.CATALOG if m.source == S.SRC_PRICE]
    for code in rd.codes():
        for name in price_metrics:
            mv = S.resolve_metric(code, name, AS_OF, repo)
            assert mv.value is not None, f"{code} {name} が未計算: {mv.detail}"
            eq(mv.source, S.SRC_PRICE, f"{code} {name} の経路")


def test_ordinal_metric_is_comparable():
    """カテゴリ値は順序数に写像して比較する（operator を4種に保つため）。

    **「いま雲の上にいる」を期待値にしない。** それはその週の値動きであって、
    採点コードの仕様ではない。実際の水準名を読み取り、同じ水準名を reference に
    置いたときに `>=` が成立することを見る（どの水準にいても成り立つ）。
    """
    repo = S.Repo()
    code = rd.codes()[0]
    mv = S.resolve_metric(code, "ichimoku_position", AS_OF, repo)
    assert mv.value is not None, mv.detail
    levels = [n for n in ("above", "in", "below") if n in mv.display]
    eq(len(levels), 1, f"水準名を1つに読み取れない: {mv.display!r}")
    level = levels[0]

    p = pred(code=code, metric="ichimoku_position", operator=">=",
             reference=level, resolve_by=AS_OF)
    ref = S.resolve_reference(p, AS_OF, repo)
    eq(ref.value, mv.value, f"水準名 {level} が metric と同じ順序数に解決される")
    out = S.resolve(p, rd.next_business_day(AS_OF), repo)
    eq(out["status"], S.STATUS_RESOLVED, "解決される")
    eq(out["result"], S.RESULT_HIT, f"{level} >= {level}")


def test_unknown_ordinal_level_is_not_silently_zero():
    repo = S.Repo()
    p = pred(code=rd.codes()[0], metric="ichimoku_position", operator=">=",
             reference="上抜け", resolve_by=AS_OF)
    out = S.resolve(p, rd.next_business_day(AS_OF), repo)
    eq(out["status"], S.STATUS_EXPIRED, "解決できない reference は判定不能")
    eq(out["result"], S.RESULT_NA, "外れではない")


def test_relative_perf_uses_same_business_days():
    repo = S.Repo()
    code = rd.codes()[-1]
    mv = S.resolve_metric(code, "relative_perf_4w", AS_OF, repo)
    assert mv.value is not None, mv.detail
    # 手計算との突合: 個別の騰落率 − TOPIX の騰落率
    bars = repo.bars_upto(code, AS_OF)
    idx = {b.date: b.close for b in repo.index_bars()}
    n = 20
    s0, s1 = bars[-1 - n], bars[-1]
    expected = ((s1.close / s0.close - 1) * 100
                - (idx[s1.date] / idx[s0.date] - 1) * 100)
    close_to(mv.value, expected, "対TOPIX 4週")


def test_relative_perf_is_none_when_index_missing():
    """指数側の終値が空なら未計算にする（value_primary で埋めない）。

    実データでは growth250 が第2ソース未確定でこの状態にある。ただし
    「growth250 の close が全行空であること」を**前提として書かない**
    （第2ソースが入った週に、正しい変更のほうがテストを落とす）。
    不変条件は合成の足で確かめ、実データは「あるなら同じ結論になる」で見る。
    """
    repo = S.Repo()
    bars = repo.bars_upto(rd.codes()[-1], AS_OF)
    blank = [S.tech.Bar(date=b.date, open=b.open, high=b.high, low=b.low,
                        close=None, volume=b.volume) for b in bars]
    is_none(S.relative_perf_pct(bars, blank, 20),
            "終値が空なら未計算（value_primary で埋めない）")

    g = repo.index_bars("growth250")
    if not [b for b in g if b.close is not None]:
        is_none(S.relative_perf_pct(bars, g, 20),
                "growth250（照合不成立）でも同じ結論になる")


# =============================================================================
# 経路② 信用残高
# =============================================================================

def test_margin_ratio_resolves():
    """期待値は CSV の最新行から引く（信用残は毎週追記されるため直書きしない）。"""
    codes = codes_with_margin_ratio()
    assert codes, "信用倍率の入った銘柄が1つも無い"
    for code in codes:
        row = margin_latest(code)
        mv = S.resolve_metric(code, "margin_ratio", AS_OF, S.Repo())
        eq(mv.source, S.SRC_MARGIN, f"{code}: 経路は margin")
        close_to(mv.value, float(row["ratio"]), f"{code}: 信用倍率")
        eq(mv.as_of, str(row["date"]), f"{code}: 公表日")


def test_margin_ratio_na_is_unresolved_not_zero():
    """売り残0（RATIO_NA）は「倍率0倍」でも「過熱していない」でもなく未計算。

    合成の信用残で不変条件を確かめる（どの銘柄が RATIO_NA かは
    取引所の制度信用区分次第で、テストが持つべき知識ではない）。
    そのうえで、実データに RATIO_NA があれば同じ結論になることを見る。
    """
    d = Path(tempfile.mkdtemp(prefix="kabu-score-margin-"))
    (d / "data" / "margin").mkdir(parents=True)
    (d / "data" / "margin" / "9999.csv").write_text(
        "date,code,long_balance,short_balance,ratio,unit,status,source_url,fetched_at\n"
        f"{AS_OF},9999,327.6,0.0,,単位,OK|RATIO_NA,https://example.invalid/,"
        f"{AS_OF}T18:00:00+09:00\n", encoding="utf-8")
    repo = S.Repo(root=d)
    mv = S.resolve_metric("9999", "margin_ratio", AS_OF, repo)
    is_none(mv.value, "倍率は定義できない")
    assert "RATIO_NA" in mv.detail, mv.detail
    # 一方で買残・売残そのものは実額として引ける（0 を欠測に潰さない）
    eq(S.resolve_metric("9999", "margin_short_balance", AS_OF, repo).value, 0.0,
       "売り残 0 は有効な値")

    for code in codes_with_ratio_na():
        real = S.resolve_metric(code, "margin_ratio", AS_OF, S.Repo())
        is_none(real.value, f"{code}: 実データでも倍率は未計算")


def test_margin_is_unresolved_when_stale():
    """期限から遠く離れた古い残高で採点しない。

    期限は「最新の信用残から十分離れた日」を実データから作る（日付を直書きしない。
    信用残は毎週追記されるため固定日だと将来 stale でなくなる）。
    """
    from datetime import date, timedelta
    repo = S.Repo()
    latest = max(str(r["date"]) for r in repo.margin_rows("3851"))
    far = (date.fromisoformat(latest)
           + timedelta(days=S.MARGIN_MAX_AGE_DAYS + 30)).isoformat()
    mv = S.resolve_metric("3851", "margin_ratio", far, repo)
    is_none(mv.value, "古い残高は未計算")
    assert "古い" in mv.detail, mv.detail


# =============================================================================
# 経路③ 決算（KPI）
# =============================================================================

def test_kpi_raw_metric_resolves():
    repo = tmp_repo(_KPI_HEADER + _kpi_row("2026-08-14", "operating_income", "123"))
    mv = S.resolve_kpi_metric("9999", "operating_income", "2026-08-21", repo)
    close_to(mv.value, 123.0, "実額")
    eq(mv.as_of, "2026-08-14", "開示日")


def test_kpi_derived_ratio_is_computed_by_code():
    rows = (_KPI_HEADER
            + _kpi_row("2026-08-14", "revenue", "1300")
            + _kpi_row("2026-08-14", "revenue_prev_year", "1000")
            + _kpi_row("2026-08-14", "segment_revenue:payment_service", "800"))
    repo = tmp_repo(rows)
    close_to(S.resolve_kpi_metric("9999", "revenue_yoy_pct", "2026-08-21", repo).value,
             30.0, "売上高 前年同四半期比")
    close_to(S.resolve_kpi_metric("9999", "stock_revenue_ratio", "2026-08-21", repo).value,
             800 / 1300, "ストック売上構成比")


def test_kpi_ratio_not_computed_across_units():
    rows = (_KPI_HEADER
            + _kpi_row("2026-08-14", "revenue", "1300")
            + _kpi_row("2026-08-14", "segment_revenue:payment_service", "800000",
                       unit="JPY_thousand"))
    mv = S.resolve_kpi_metric("9999", "stock_revenue_ratio", "2026-08-21", tmp_repo(rows))
    is_none(mv.value, "単位が違う行どうしで比を作らない（換算しない）")


def test_kpi_uses_latest_disclosure_before_resolve_by():
    rows = (_KPI_HEADER
            + _kpi_row("2026-05-14", "operating_income", "100")
            + _kpi_row("2026-08-14", "operating_income", "200"))
    repo = tmp_repo(rows)
    close_to(S.resolve_kpi_metric("9999", "operating_income", "2026-06-30", repo).value,
             100.0, "期限前の開示だけを見る")
    close_to(S.resolve_kpi_metric("9999", "operating_income", "2026-08-21", repo).value,
             200.0, "期限までの最新開示")


def test_unknown_metric_name_is_reported_as_undefined():
    mv = S.resolve_metric(rd.codes()[1], "revenue_growth", AS_OF, S.Repo())
    is_none(mv.value, "未定義")
    assert "未定義" in mv.detail, mv.detail


# =============================================================================
# ★F-13 の回帰: resolve_by 時点の値で採点する
# =============================================================================

def test_metric_is_read_as_of_resolve_by_not_latest():
    """★F-13 の回帰テスト。

    旧 load_metric() は CSV の最終行を返していたため、解決が遅れるほど
    resolve_by より後の値で採点されていた。

    早い期限も**データから取る**（履歴の中ほどの営業日）。日付を直書きすると、
    履歴の初回一括取得の範囲が変わったときに黙って前提が崩れる。
    """
    repo = S.Repo()
    code = rd.codes()[2]
    early_day = rd.mid_date(code)
    early = S.resolve_metric(code, "avg_turnover_20d", early_day, repo)
    late = S.resolve_metric(code, "avg_turnover_20d", AS_OF, repo)
    eq(early.as_of, early_day, "早い期限では当時の値")
    eq(late.as_of, rd.last_confirmed_date(code), "遅い期限では最新の値")
    assert early.value != late.value, "期限が違えば採点に使う値も違う"

    # 同じ予測を「期限直後」と「ずっと後」に採点しても結論が変わらないこと。
    # 基準値は当時の実測値そのものにするので、値動きに関係なく必ず成立する。
    p = pred(code=code, metric="avg_turnover_20d", operator=">",
             reference=early.value / 2, resolve_by=early_day)
    a = S.resolve(p, rd.next_business_day(early_day), repo)
    b = S.resolve(p, rd.next_business_day(AS_OF), repo)
    eq(a["result"], b["result"], "採点時期によって結論が変わらない")
    eq(a["actual"], b["actual"], "使う値も同じ")
    eq(a["result"], S.RESULT_HIT, "当時の実測値はその半分より大きい")


# =============================================================================
# 採点の作法
# =============================================================================

def test_not_scored_before_resolve_by():
    """期限前に先取りで確定させない（株価由来 metric は毎日動く）。"""
    repo = S.Repo()
    future = rd.next_business_day(rd.day_after_all_fetches())
    p = pred(resolve_by=future)
    out = S.resolve(p, AS_OF, repo)
    eq(out["status"], S.STATUS_OPEN, "期限前は open のまま")
    assert "result" not in out, "結果を書かない"
    assert "actual" not in out, "実値も書かない"


def test_unresolvable_becomes_expired_not_miss():
    """未計算を「外れ」に丸めない。

    KPI が1本も無い複製で見る。実リポジトリに決算が入ると「未取得」でなくなり、
    このテストが確かめたい経路を通らなくなるため。
    """
    repo = repo_without_kpi()
    deadline = rd.next_business_day(AS_OF)
    p = pred(code=rd.codes()[1], metric="operating_income", operator=">",
             reference=0, resolve_by=deadline)
    out = S.resolve(p, rd.next_business_day(deadline), repo)
    eq(out["status"], S.STATUS_EXPIRED, "判定不能は expired")
    eq(out["result"], S.RESULT_NA, "外れではない")
    assert "KPI未取得" in out["reason"], out["reason"]


def test_resolved_is_frozen():
    repo = S.Repo()
    for status, result in ((S.STATUS_RESOLVED, S.RESULT_HIT),
                           (S.STATUS_EXPIRED, S.RESULT_NA)):
        p = pred(status=status, result=result, resolve_by=rd.mid_date())
        out = S.resolve(p, rd.next_business_day(AS_OF), repo)
        eq(out["status"], status, "確定済みは動かさない")
        eq(out["result"], result, "結果も動かさない")


def test_invalid_prediction_is_flagged_not_scored():
    repo = S.Repo()
    for bad, why in ((pred(operator="=="), "operator"),
                     (pred(operator="~"), "operator"),
                     (pred(resolve_by="いつか"), "resolve_by"),
                     (pred(confidence=1.5), "confidence")):
        out = S.resolve(bad, "2026-12-31", repo)
        eq(out["status"], S.STATUS_OPEN, f"{why}: status は動かさない")
        assert out.get("invalid"), f"{why}: 不成立として報告する"


def test_reference_can_be_another_metric():
    """reference に metric 名を書ける。

    期待する当否は**両方の metric を引いて比較して**決める。
    「終値 > 雲の上端」がその週たまたま成り立つことを期待値にすると、
    値動きでテストが落ちる。
    """
    repo = S.Repo()
    code = rd.codes()[2]
    left = S.resolve_metric(code, "close", AS_OF, repo)
    right = S.resolve_metric(code, "ichimoku_cloud_top", AS_OF, repo)
    assert left.value is not None and right.value is not None, (left, right)
    expected = S.RESULT_HIT if left.value > right.value else S.RESULT_MISS

    p = pred(code=code, metric="close", operator=">",
             reference="ichimoku_cloud_top", resolve_by=AS_OF)
    out = S.resolve(p, rd.next_business_day(AS_OF), repo)
    eq(out["status"], S.STATUS_RESOLVED, "metric 名の reference も解決する")
    eq(out["result"], expected, f"終値 {left.value} vs 雲の上端 {right.value}")


def test_brier_and_summary():
    preds = [
        {"id": "a", "metric": "rsi14", "result": S.RESULT_HIT, "confidence": 1.0,
         "status": S.STATUS_RESOLVED, "metric_source": S.SRC_PRICE},
        {"id": "b", "metric": "rsi14", "result": S.RESULT_MISS, "confidence": 1.0,
         "status": S.STATUS_RESOLVED, "metric_source": S.SRC_PRICE},
        {"id": "c", "metric": "revenue_yoy_pct", "result": S.RESULT_NA,
         "confidence": 0.5, "status": S.STATUS_EXPIRED, "metric_source": S.SRC_KPI},
    ]
    close_to(S.brier(preds), 0.5, "確信度1.0で1勝1敗 → (0+1)/2")
    s = S.summarize(preds, "2026-08-10")
    eq(s["hit"], 1, "的中")
    eq(s["miss"], 1, "外れ")
    eq(s["unresolvable"], 1, "判定不能")
    eq(s["hit_rate"], 0.5, "的中率は判定不能を分母に入れない")
    eq(s["as_of"], "2026-08-10", "as_of は日足の最終営業日（壁時計ではない）")
    eq(s["by_metric_source"], {"kpi": 1, "price": 2}, "経路別の件数")


def test_summary_has_no_wall_clock():
    s = S.summarize([], S.Repo().data_as_of())
    eq(s["as_of"], rd.latest_date(),
       "集計基準日は日足の最終営業日（実行日ではない）")
    assert s["as_of"] >= AS_OF, "append-only なので基準日は戻らない"


# =============================================================================
# 実データ・決定論
# =============================================================================

def test_registered_predictions_resolve_or_report_why():
    """登録済みの予測が、期限到来後に必ず「解決」か「理由つき判定不能」になること。

    件数はファイルから数える（予測は週ごとに増える）。0件だけは失敗にする
    ——「登録が読めていない」と「1件も無い」が区別できなくなるため。
    """
    import yaml
    repo = S.Repo()
    far_future = rd.day_after_all_fetches(365)
    seen = 0
    for path in sorted((ROOT / "predictions").glob("*.yaml")):
        doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        for p in doc.get("predictions", []):
            seen += 1
            out = S.resolve(p, far_future, repo)
            assert out["status"] in (S.STATUS_RESOLVED, S.STATUS_EXPIRED), \
                f"{p['id']}: {out}"
            assert out.get("reason"), f"{p['id']}: 理由が空"
    assert seen, "predictions/*.yaml から予測を1件も読めていない"


def test_deterministic():
    repo1, repo2 = S.Repo(), S.Repo()
    day = rd.next_business_day(AS_OF)
    p = pred(code=rd.codes()[2], resolve_by=rd.mid_date())
    eq(S.resolve(p, day, repo1), S.resolve(p, day, repo2), "同一入力→同一出力")
    eq(S.resolve(p, day, repo1), S.resolve(p, day, repo1), "同じ repo でも再現する")


def test_main_dry_run_writes_nothing():
    before = (ROOT / "scoring" / "summary.yaml").read_text(encoding="utf-8") \
        if (ROOT / "scoring" / "summary.yaml").exists() else None
    eq(S.main(["--dry-run", "--today", rd.day_after_all_fetches(365)]), 0,
       "終了コード")
    after = (ROOT / "scoring" / "summary.yaml").read_text(encoding="utf-8") \
        if (ROOT / "scoring" / "summary.yaml").exists() else None
    eq(after, before, "--dry-run はファイルを書かない")


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

    print(f"\n{len(tests) - len(failed)}/{len(tests)} passed")
    if failed:
        for name, msg in failed:
            print(f"  - {name}: {msg}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
