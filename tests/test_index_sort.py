"""一覧の「推定由来の数値で並び替え」の回帰テスト（src/build.py）。

なぜあるか（2026-09-05）: 監視中の全銘柄に次期利益推定を置き、一覧を
**利益が何％増えるか**で並べられるようにした。並び替えの材料は
<tr data-est-*> 属性で、値が無い行には属性を付けない（並び替えスクリプトは
属性の無い行を末尾に置く。「無い」を 0＝小さいと読ませない）。
前期の利益が小さい銘柄は率が極端に出るので「低ベース」の印を付ける。

実データの値はべた書きしない（翌週かならず落ちる検査を置かない）。
数値の検査は合成データ、実データ側は「計算が通り形が揃うこと」だけを見る。

実行:
  python tests/test_index_sort.py
"""
from __future__ import annotations

import re
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import build as B  # noqa: E402
import yamlio as Y  # noqa: E402

_TMPDIRS: list[Path] = []

HEAD = ("period,code,metric,value,unit,tolerance,status,source_primary,"
        "value_primary,raw_primary,source_secondary,value_secondary,"
        "raw_secondary,sources_all,source_url_primary,source_url_secondary,"
        "fetched_at\n")


def row(period: str, metric: str, value, status: str = "OK") -> str:
    v = str(value)
    return (f"{period},9999,{metric},{v},JPY_million,1,{status},a,{v},{v},"
            f"b,{v},{v},a={v}|b={v},http://a,http://b,2026-09-01T00:00:00+09:00\n")


ESTIMATE = """
market_forecast:
  name: "—"
  note: "テスト用。カバー無し"
models:
  - as_of: "2026-09-05"
    period: "FY2027-03"
    status: draft
    revenue:
      unit: JPY_million
      segments:
        - name: "全社"
          expr: "base * growth"
          vars:
            base:
              value: 1000
              basis: actual
              source: "data/fundamentals/9999.csv"
            growth:
              value: 1.2
              basis: assumed
              note: "テスト"
    profit:
      op_margin:
        value: 0.1
        basis: assumed
        note: "テスト"
"""


def sandbox(prev_rev: float, prev_op: float, plan_op: float | None) -> Path:
    """estimates/9999.yaml と fundamentals/9999.csv を置いた一時 ROOT。"""
    base = Path(tempfile.mkdtemp(prefix="kabu-sort-"))
    _TMPDIRS.append(base)
    (base / "estimates").mkdir()
    (base / "estimates" / "9999.yaml").write_text(ESTIMATE, encoding="utf-8")
    (base / "data" / "fundamentals").mkdir(parents=True)
    rows = row("FY2026-03", "revenue", prev_rev) + row("FY2026-03", "operating_income", prev_op)
    if plan_op is not None:
        rows += row("FY2027-03", "operating_income_plan", plan_op)
    (base / "data" / "fundamentals" / "9999.csv").write_text(HEAD + rows, encoding="utf-8")
    return base


class using_root:
    def __init__(self, base: Path):
        self.base = base

    def __enter__(self):
        self.old = B.ROOT
        B.ROOT = self.base

    def __exit__(self, *exc):
        B.ROOT = self.old


# =============================================================================
# 1. 合成データ（数値の検査）
# =============================================================================

def test_metrics_growth_and_plan():
    """推定OP 120（1000×1.2×0.1）。前期 100 → 前期比 +20%、計画 150 → 計画比 -20%。"""
    with using_root(sandbox(prev_rev=1000, prev_op=100, plan_op=150)):
        m = B.estimate_metrics("9999")
    assert m is not None
    assert round(m["op"]) == 120, m
    assert round(m["growth_pct"], 1) == 20.0, m
    assert round(m["plan_pct"], 1) == -20.0, m
    assert m["mf_pct"] is None, "市場予想が無いのに比率が出た"
    assert m["draft"] is True and m["low_base"] is False, m


def test_low_base_is_flagged_not_hidden():
    """前期の利益率 1%（＜2%）→ 前期比 +1100% は出すが「低ベース」の印を付ける。"""
    with using_root(sandbox(prev_rev=1000, prev_op=10, plan_op=None)):
        m = B.estimate_metrics("9999")
    assert m is not None and m["low_base"] is True, m
    assert round(m["growth_pct"]) == 1100, m
    line = B._estimate_line_html(m)
    assert "低ベース" in line, line
    assert "前期比" in line and "+1100.0%" in line, line
    assert "会社計画比" not in line, "計画が無いのに計画比が出た"


def test_negative_base_is_low_base():
    with using_root(sandbox(prev_rev=1000, prev_op=-50, plan_op=None)):
        m = B.estimate_metrics("9999")
    assert m is not None and m["low_base"] is True, m
    assert m["growth_pct"] > 0, "赤字からの黒字化は上振れ（分母は絶対値）"


def test_row_attrs_only_for_available_values():
    est = {"op": 120.4, "growth_pct": 12.34, "plan_pct": None, "mf_pct": None,
           "low_base": False, "draft": True, "period": "FY2027-03"}
    a = B._est_row_attrs(est)
    assert ' data-est-op="120"' in a, a
    assert ' data-est-growth="12.3"' in a, a
    assert "data-est-plan" not in a, "無い値に属性を付けてはいけない（末尾に置くため）"
    assert B._est_row_attrs(None) == "", "推定が無い行に属性を付けない"
    assert B._estimate_line_html(None) == ""


def test_missing_estimate_is_none():
    with using_root(sandbox(prev_rev=1000, prev_op=100, plan_op=None)):
        assert B.estimate_metrics("0000") is None


# =============================================================================
# 2. 実データ（形が揃うことだけ）
# =============================================================================

def test_every_watched_stock_has_sortable_estimate():
    """監視中の全銘柄が推定モデルを持ち、一覧の並び替えに乗る（2026-09-05 の方針）。"""
    master = Y.safe_load((ROOT / "data" / "master.yaml").read_text(encoding="utf-8"))
    missing = []
    for s in Y.watched_stocks(master):
        m = B.estimate_metrics(str(s["code"]))
        if m is None or m.get("growth_pct") is None:
            missing.append(str(s["code"]))
    assert not missing, f"推定モデルが無い／前期比を出せない監視銘柄: {missing}"


def test_index_has_sort_control_and_row_attrs():
    """生成した index.html に select・全キーの option・data-est-* 行・並び替えの script がある。"""
    out = Path(tempfile.mkdtemp(prefix="kabu-sort-docs-"))
    _TMPDIRS.append(out)
    master = Y.safe_load((ROOT / "data" / "master.yaml").read_text(encoding="utf-8"))
    old = B.DOCS
    B.DOCS = out
    try:
        B.build_index(master, {}, "2026-09-05")
    finally:
        B.DOCS = old
    page = (out / "index.html").read_text(encoding="utf-8")
    assert 'id="list-sort"' in page
    for key, label in B.SORT_KEYS:
        assert f'<option value="{key}">{label}</option>' in page, label
    assert re.search(r'<tr[^>]* data-est-growth="-?\d+\.\d"', page), "並び替えの属性が行に無い"
    assert 'data-est-"+k' in page and "kabu:list-sort" in page, "並び替えの script が無い"
    # 対象外の行には推定の属性を付けない（凍った記録を並び替えの母数に混ぜない）
    for m in re.finditer(r'<tr class="[^"]*row-excluded[^"]*"([^>]*)>', page):
        assert "data-est-" not in m.group(1), "対象外の行に推定の属性が付いた"


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
