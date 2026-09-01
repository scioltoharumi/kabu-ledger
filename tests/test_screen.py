"""候補を名指しする前の検査（src/screen.py）の回帰テスト。

## この検査が生まれた事故（2026-09-01）

高砂熱学工業（1969）を「特に見込みがある」と推した。根拠は通期の営業利益
+47.3% と ROE 計画 19.33%。**だが直近2四半期は営業減益**（-24.7%・-16.2%）で、
その四半期の値は `data/fundamentals/1969.csv` に**採用値として入っていた**。
見なかっただけである。

だからここでの中心の検査は「**1969 に MOMENTUM_NEGATIVE が付くこと**」。
実データが将来変わってこの警告が消えるのは正常（勢いが戻ったということ）なので、
実データべた書きの検査は合成データで行い、実データ側は「計算が通ること」だけを見る
（tests/realdata.py の方針と同じ。翌週かならず落ちる検査を置かない）。

実行:
  python tests/test_screen.py
"""
from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import chartdata as CD  # noqa: E402
import screen as S  # noqa: E402
import yamlio as Y  # noqa: E402

_TMPDIRS: list[Path] = []

HEAD = ("period,code,metric,value,unit,tolerance,status,source_primary,"
        "value_primary,raw_primary,source_secondary,value_secondary,"
        "raw_secondary,sources_all,source_url_primary,source_url_secondary,"
        "fetched_at\n")


def row(period: str, metric: str, value, status: str = "OK") -> str:
    v = "" if value is None else str(value)
    return (f"{period},9999,{metric},{v},JPY_million,1,{status},a,{v},{v},"
            f"b,{v},{v},a={v}|b={v},http://a,http://b,2026-09-01T00:00:00+09:00\n")


def sandbox(rows: str) -> Path:
    """合成 fundamentals を置いた一時 data/ を作り、chartdata を向ける。"""
    base = Path(tempfile.mkdtemp(prefix="kabu-screen-"))
    _TMPDIRS.append(base)
    (base / "fundamentals").mkdir(parents=True)
    (base / "fundamentals" / "9999.csv").write_text(HEAD + rows, encoding="utf-8")
    (base / "prices").mkdir(parents=True)
    return base


class using_data:
    """chartdata の DATA を差し替える（実データを読ませない）。"""

    def __init__(self, base: Path):
        self.base = base

    def __enter__(self):
        self.old = CD.DATA
        CD.DATA = self.base
        CD.clear_cache()

    def __exit__(self, *exc):
        CD.DATA = self.old
        CD.clear_cache()


# =============================================================================
# 1. 事故の再発検査（合成データ）
# =============================================================================

def test_two_negative_quarters_are_flagged():
    """★中心。通期が大幅プラスでも、直近2四半期がマイナスなら警告が出る。

    高砂熱学の形そのもの（通期 +47.3% / 直近2四半期 -24.7%・-16.2%）。
    この検査が無かったために「特に見込みがある」と書いてしまった。
    """
    rows = (row("Q2025-01_2025-03", "operating_income", 11490)
            + row("Q2025-04_2025-06", "operating_income", 10117)
            + row("Q2026-01_2026-03", "operating_income", 8650)
            + row("Q2026-04_2026-06", "operating_income", 8481)
            + row("FY2025-03", "operating_income", 32415)
            + row("FY2026-03", "operating_income", 47745))
    with using_data(sandbox(rows)):
        r = S.screen_one("9999")
    assert "MOMENTUM_NEGATIVE" in r["flags"], \
        f"2四半期連続の減益に警告が付かない: {r['flags']}"
    assert r["fy_yoy_pct"] > 0, "通期はプラスのはず（見出しだけなら好調に見える形）"
    assert r["q0_yoy_pct"] < 0 and r["q1_yoy_pct"] < 0, "直近2四半期はマイナスのはず"


def test_single_negative_quarter_after_growth_is_reversal_not_decline():
    """1四半期だけのマイナスは MOMENTUM_REVERSED（振れと区別する）。"""
    rows = (row("Q2025-01_2025-03", "operating_income", 11918)
            + row("Q2025-04_2025-06", "operating_income", 11098)
            + row("Q2026-01_2026-03", "operating_income", 18156)
            + row("Q2026-04_2026-06", "operating_income", 10731)
            + row("FY2025-03", "operating_income", 41388)
            + row("FY2026-03", "operating_income", 54600))
    with using_data(sandbox(rows)):
        r = S.screen_one("9999")
    assert "MOMENTUM_REVERSED" in r["flags"], f"勢いの反転が出ない: {r['flags']}"
    assert "MOMENTUM_NEGATIVE" not in r["flags"], \
        "1四半期だけのマイナスを『減速確定』にしない（振れと区別する）"


def test_growing_quarters_get_no_momentum_flag():
    rows = (row("Q2025-04_2025-06", "operating_income", 7195)
            + row("Q2026-04_2026-06", "operating_income", 13601)
            + row("FY2025-03", "operating_income", 60979)
            + row("FY2026-03", "operating_income", 90256))
    with using_data(sandbox(rows)):
        r = S.screen_one("9999")
    assert not [f for f in r["flags"] if f.startswith("MOMENTUM")], \
        f"加速している銘柄に勢いの警告が付いた: {r['flags']}"
    assert round(r["q0_yoy_pct"], 1) == 89.0, r["q0_yoy_pct"]


def test_flat_company_plan_is_flagged():
    """好決算の直後の横ばい計画は、会社が『ここまで』と見ている合図。"""
    rows = (row("FY2025-03", "operating_income", 32415)
            + row("FY2026-03", "operating_income", 47745)
            + row("FY2027-03", "operating_income_plan", 50000))
    with using_data(sandbox(rows)):
        r = S.screen_one("9999")
    assert "PLAN_FLAT" in r["flags"], f"横ばい計画に警告が付かない: {r['flags']}"
    assert r["plan_yoy_pct"] < S.PLAN_FLAT_PCT


def test_missing_data_is_flagged_not_silently_passed():
    """データが無い銘柄は「問題なし」ではなく『語れない』と出す。"""
    with using_data(sandbox("")):
        r = S.screen_one("9999")
    assert "NO_QUARTERLY" in r["flags"], f"四半期の欠測が見えない: {r['flags']}"
    assert "NO_PRICE_DATA" in r["flags"], f"株価の欠測が見えない: {r['flags']}"


def test_unadopted_values_are_not_used():
    """照合が成立していない値（SINGLE_SOURCE）を伸びの計算に使わない。"""
    rows = (row("Q2025-04_2025-06", "operating_income", 100, "SINGLE_SOURCE")
            + row("Q2026-04_2026-06", "operating_income", 999, "SINGLE_SOURCE"))
    with using_data(sandbox(rows)):
        r = S.screen_one("9999")
    assert r["q0_yoy_pct"] is None, "未照合の値で伸びを出してはいけない"
    assert "NO_QUARTERLY" in r["flags"]


def test_zero_or_negative_base_does_not_produce_a_ratio():
    """前年が 0 以下のとき比率は意味を持たないので出さない（÷0 も防ぐ）。"""
    rows = (row("Q2025-04_2025-06", "operating_income", 0)
            + row("Q2026-04_2026-06", "operating_income", 500))
    with using_data(sandbox(rows)):
        r = S.screen_one("9999")
    assert r["q0_yoy_pct"] is None, "前年0からの伸び率を出してはいけない"


def test_previous_year_key_is_the_same_quarter():
    assert S._prev_year_key("Q2026-04_2026-06") == "Q2025-04_2025-06"
    assert S._prev_year_key("Q2026-01_2026-03") == "Q2025-01_2025-03"


# =============================================================================
# 2. 実データ（値はべた書きしない。計算が通ることだけ見る）
# =============================================================================

def test_runs_on_every_watched_stock():
    """監視中の全銘柄で例外なく計算でき、結果の形が揃っている。"""
    master = Y.safe_load((ROOT / "data" / "master.yaml").read_text(encoding="utf-8"))
    codes = [str(s["code"]) for s in Y.watched_stocks(master)]
    assert codes, "監視中の銘柄が無い"
    for c in codes:
        r = S.screen_one(c)
        assert r["code"] == c
        assert isinstance(r["flags"], list)
        for fl in r["flags"]:
            assert fl in S.FLAG_JA, f"{c}: 説明の無い警告 {fl}"


def test_render_lists_every_stock_and_explains_each_flag():
    master = Y.safe_load((ROOT / "data" / "master.yaml").read_text(encoding="utf-8"))
    names = {str(s["code"]): s.get("name", "") for s in master["stocks"]}
    codes = [str(s["code"]) for s in Y.watched_stocks(master)]
    rows = [S.screen_one(c) for c in codes]
    text = S.render(rows, names)
    for c in codes:
        assert c in text, f"{c} が表に出ていない"
    for r in rows:
        for fl in r["flags"]:
            assert S.FLAG_JA[fl] in text, f"{fl} の意味が表示されていない"


# =============================================================================
# 実行
# =============================================================================

def main() -> int:
    tests = [(n, f) for n, f in sorted(globals().items())
             if n.startswith("test_") and callable(f)]
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
    for d in _TMPDIRS:
        shutil.rmtree(d, ignore_errors=True)
    print(f"\n{len(tests) - len(failed)}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
