"""score.py のテスト。

方針:
  - **F-03 の回帰**: 株価・信用残から引ける metric が、KPI が1本も無い状態でも解決できること。
  - **F-13 の回帰**: 採点が「CSV の最終行」ではなく **resolve_by 時点の値**で行われること。
  - **未計算を「外れ」に丸めない**こと（判定不能は expired であって miss ではない）。
  - 期限前に先取りで確定しないこと。確定した予測を後から書き換えないこと。
  - 実データ（4銘柄・append-only）で例外なく解決でき、2回実行しても同一結果になること。

実行:
  $env:PYTHONIOENCODING = "utf-8"; python tests/test_score.py
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import score as S  # noqa: E402

AS_OF = "2026-08-10"          # 実データの最終営業日（append-only なので過去窓は不変）


# --- 検証ヘルパ ---------------------------------------------------------------

def eq(actual, expected, label=""):
    assert actual == expected, f"{label}: expected {expected!r}, got {actual!r}"


def is_none(actual, label=""):
    assert actual is None, f"{label}: expected None, got {actual!r}"


def close_to(actual, expected, label="", tol=1e-6):
    assert actual is not None, f"{label}: expected {expected!r}, got None"
    assert abs(actual - expected) <= tol, f"{label}: expected {expected!r}, got {actual!r}"


def pred(**over) -> dict:
    p = {"id": "P-TEST-01", "code": "4937", "metric": "avg_turnover_20d",
         "operator": ">", "reference": 30000000, "resolve_by": "2026-08-10",
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
    avg_turnover_20d まで判定不能になっていた。
    """
    assert not (ROOT / "data" / "kpi" / "4937.csv").exists(), \
        "この回帰テストは KPI 未整備の状態を前提にしている"
    mv = S.resolve_metric("4937", "avg_turnover_20d", AS_OF, S.Repo())
    eq(mv.source, S.SRC_PRICE, "経路は price")
    assert mv.value is not None, f"株価から解決できるはず: {mv.detail}"
    eq(mv.as_of, AS_OF, "値の基準日")


def test_price_metrics_all_resolve_on_real_data():
    repo = S.Repo()
    price_metrics = [m.name for m in S.CATALOG if m.source == S.SRC_PRICE]
    for code in ("3851", "4073", "4937", "6570"):
        for name in price_metrics:
            mv = S.resolve_metric(code, name, AS_OF, repo)
            assert mv.value is not None, f"{code} {name} が未計算: {mv.detail}"
            eq(mv.source, S.SRC_PRICE, f"{code} {name} の経路")


def test_ordinal_metric_is_comparable():
    """カテゴリ値は順序数に写像して比較する（operator を4種に保つため）。"""
    repo = S.Repo()
    mv = S.resolve_metric("4937", "ichimoku_position", AS_OF, repo)
    eq(mv.value, 1.0, "雲の上 = +1")
    assert "above" in mv.display, mv.display
    p = pred(code="4937", metric="ichimoku_position", operator=">=",
             reference="above", resolve_by=AS_OF)
    ref = S.resolve_reference(p, AS_OF, repo)
    eq(ref.value, 1.0, "水準名 above は +1 に解決される")
    out = S.resolve(p, "2026-08-11", repo)
    eq(out["status"], S.STATUS_RESOLVED, "解決される")
    eq(out["result"], S.RESULT_HIT, "above >= above")


def test_unknown_ordinal_level_is_not_silently_zero():
    repo = S.Repo()
    p = pred(code="4937", metric="ichimoku_position", operator=">=",
             reference="上抜け", resolve_by=AS_OF)
    out = S.resolve(p, "2026-08-11", repo)
    eq(out["status"], S.STATUS_EXPIRED, "解決できない reference は判定不能")
    eq(out["result"], S.RESULT_NA, "外れではない")


def test_relative_perf_uses_same_business_days():
    repo = S.Repo()
    mv = S.resolve_metric("6570", "relative_perf_4w", AS_OF, repo)
    assert mv.value is not None, mv.detail
    # 手計算との突合: 個別の騰落率 − TOPIX の騰落率
    bars = repo.bars_upto("6570", AS_OF)
    idx = {b.date: b.close for b in repo.index_bars()}
    n = 20
    s0, s1 = bars[-1 - n], bars[-1]
    expected = ((s1.close / s0.close - 1) * 100
                - (idx[s1.date] / idx[s0.date] - 1) * 100)
    close_to(mv.value, expected, "対TOPIX 4週")


def test_relative_perf_is_none_when_index_missing():
    """growth250 は第2ソースが無く close が全行空。埋めずに未計算にする。"""
    repo = S.Repo()
    bars = repo.bars_upto("6570", AS_OF)
    g = repo.index_bars("growth250")
    assert g, "growth250 の行は存在する"
    is_none(S.relative_perf_pct(bars, g, 20), "終値が空なら未計算（value_primary で埋めない）")


# =============================================================================
# 経路② 信用残高
# =============================================================================

def test_margin_ratio_resolves():
    mv = S.resolve_metric("3851", "margin_ratio", AS_OF, S.Repo())
    eq(mv.source, S.SRC_MARGIN, "経路は margin")
    close_to(mv.value, 5.49, "信用倍率")
    eq(mv.as_of, "2026-07-31", "公表日")


def test_margin_ratio_na_is_unresolved_not_zero():
    """売り残0（RATIO_NA）は「倍率0倍」でも「過熱していない」でもなく未計算。"""
    mv = S.resolve_metric("4073", "margin_ratio", AS_OF, S.Repo())
    is_none(mv.value, "倍率は定義できない")
    assert "RATIO_NA" in mv.detail, mv.detail
    # 一方で買残・売残そのものは実額として引ける（0 を欠測に潰さない）
    eq(S.resolve_metric("4073", "margin_short_balance", AS_OF, S.Repo()).value, 0.0,
       "売り残 0 は有効な値")


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
    mv = S.resolve_metric("4073", "revenue_growth", AS_OF, S.Repo())
    is_none(mv.value, "未定義")
    assert "未定義" in mv.detail, mv.detail


# =============================================================================
# ★F-13 の回帰: resolve_by 時点の値で採点する
# =============================================================================

def test_metric_is_read_as_of_resolve_by_not_latest():
    """★F-13 の回帰テスト。

    旧 load_metric() は CSV の最終行を返していたため、解決が遅れるほど
    resolve_by より後の値で採点されていた。
    """
    repo = S.Repo()
    early = S.resolve_metric("4937", "avg_turnover_20d", "2026-01-15", repo)
    late = S.resolve_metric("4937", "avg_turnover_20d", AS_OF, repo)
    eq(early.as_of, "2026-01-15", "早い期限では当時の値")
    eq(late.as_of, AS_OF, "遅い期限では最新の値")
    assert early.value != late.value, "期限が違えば採点に使う値も違う"

    # 同じ予測を「期限直後」と「半年後」に採点しても結論が変わらないこと
    p = pred(code="4937", metric="avg_turnover_20d", operator=">",
             reference=10_000_000, resolve_by="2026-01-15")
    a = S.resolve(p, "2026-01-16", repo)
    b = S.resolve(p, "2026-08-11", repo)
    eq(a["result"], b["result"], "採点時期によって結論が変わらない")
    eq(a["actual"], b["actual"], "使う値も同じ")
    eq(a["result"], S.RESULT_HIT, "2026-01-15 時点では2,200万円 > 1,000万円")


# =============================================================================
# 採点の作法
# =============================================================================

def test_not_scored_before_resolve_by():
    """期限前に先取りで確定させない（株価由来 metric は毎日動く）。"""
    repo = S.Repo()
    p = pred(resolve_by="2026-09-12")
    out = S.resolve(p, AS_OF, repo)
    eq(out["status"], S.STATUS_OPEN, "期限前は open のまま")
    assert "result" not in out, "結果を書かない"
    assert "actual" not in out, "実値も書かない"


def test_unresolvable_becomes_expired_not_miss():
    """未計算を「外れ」に丸めない。"""
    repo = S.Repo()
    p = pred(code="4073", metric="operating_income", operator=">", reference=0,
             resolve_by="2026-08-21")
    out = S.resolve(p, "2026-08-22", repo)
    eq(out["status"], S.STATUS_EXPIRED, "判定不能は expired")
    eq(out["result"], S.RESULT_NA, "外れではない")
    assert "KPI未取得" in out["reason"], out["reason"]


def test_resolved_is_frozen():
    repo = S.Repo()
    for status, result in ((S.STATUS_RESOLVED, S.RESULT_HIT),
                           (S.STATUS_EXPIRED, S.RESULT_NA)):
        p = pred(status=status, result=result, resolve_by="2026-01-15")
        out = S.resolve(p, "2026-08-11", repo)
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
    repo = S.Repo()
    p = pred(code="4937", metric="close", operator=">",
             reference="ichimoku_cloud_top", resolve_by=AS_OF)
    out = S.resolve(p, "2026-08-11", repo)
    eq(out["status"], S.STATUS_RESOLVED, "metric 名の reference も解決する")
    eq(out["result"], S.RESULT_HIT, "終値 1968 > 雲の上端 1524.75")


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
    import csv
    with (ROOT / "data" / "prices" / "daily.csv").open(encoding="utf-8") as f:
        last = max(str(r["date"]) for r in csv.DictReader(f))
    s = S.summarize([], S.Repo().data_as_of())
    eq(s["as_of"], last, "集計基準日は日足の最終営業日（実行日ではない）")
    assert s["as_of"] >= AS_OF, "append-only なので基準日は戻らない"


# =============================================================================
# 実データ・決定論
# =============================================================================

def test_registered_predictions_resolve_or_report_why():
    """登録済みの3件が、期限到来後に必ず「解決」か「理由つき判定不能」になること。"""
    import yaml
    repo = S.Repo()
    seen = 0
    for path in sorted((ROOT / "predictions").glob("*.yaml")):
        doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        for p in doc.get("predictions", []):
            seen += 1
            out = S.resolve(p, "2026-12-31", repo)
            assert out["status"] in (S.STATUS_RESOLVED, S.STATUS_EXPIRED), \
                f"{p['id']}: {out}"
            assert out.get("reason"), f"{p['id']}: 理由が空"
    assert seen >= 3, f"予測が読めていない（{seen}件）"


def test_deterministic():
    repo1, repo2 = S.Repo(), S.Repo()
    p = pred(code="4937", resolve_by="2026-06-30")
    eq(S.resolve(p, "2026-08-11", repo1), S.resolve(p, "2026-08-11", repo2),
       "同一入力→同一出力")
    eq(S.resolve(p, "2026-08-11", repo1), S.resolve(p, "2026-08-11", repo1),
       "同じ repo でも再現する")


def test_main_dry_run_writes_nothing():
    before = (ROOT / "scoring" / "summary.yaml").read_text(encoding="utf-8") \
        if (ROOT / "scoring" / "summary.yaml").exists() else None
    eq(S.main(["--dry-run", "--today", "2026-12-31"]), 0, "終了コード")
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
