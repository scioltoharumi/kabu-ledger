"""estimate.py の回帰テスト（フェルミ推定の検証・計算・感応度・実績比較）。

**ネットワークを使わない。実データの日付・数値をべた書きしない。**
モデルは Python の辞書で直接組み、ファイル系（load_estimate / validate_file /
comparisons / CLI）は一時ディレクトリに合成 YAML・CSV を組み立てて読ませる。

見るもの:
  1. 2セグメントのモデルで revenue_total / operating_income / expr_filled が正しい
  2. 式の安全性: 関数呼び出し・属性参照・添字などが検証エラーになる（eval を使わない）
  3. basis 制約: assumed は note 必須・actual/disclosed は source 必須
  4. sensitivity: 寄与の大きい変数が先頭（op_margin → 大セグメントの変数 → 小）
  5. comparisons: status に OK を含む行だけ採用（SINGLE_SOURCE を拾わない・D53）
  6. 未定義変数・ゼロ除算が検証エラーになる
  7. load_estimate / validate_file / CLI の入出力（exit code 含む）

実行:
  $env:PYTHONIOENCODING = "utf-8"; python tests/test_estimate.py
"""
from __future__ import annotations

import csv
import io
import shutil
import sys
import tempfile
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import estimate as E  # noqa: E402


def eq(actual, expected, label=""):
    assert actual == expected, f"{label}: expected {expected!r}, got {actual!r}"


def near(actual, expected, label="", tol=1e-6):
    assert actual is not None and abs(actual - expected) <= tol, \
        f"{label}: expected ~{expected!r}, got {actual!r}"


# =============================================================================
# 合成データの土台
# =============================================================================

def var(value, basis="assumed", note="合成の仮定", source=None, unit=""):
    entry = {"value": value, "unit": unit, "basis": basis}
    if note is not None:
        entry["note"] = note
    if source is not None:
        entry["source"] = source
    return entry


def make_model(**over):
    """2セグメントの有効なモデル。施設 10店×200 = 2,000 / 通販 500人×3 = 1,500。"""
    model = {
        "as_of": "2024-01-10",
        "period": "FY2025-03",
        "status": "draft",
        "note": "合成モデル",
        "revenue": {
            "unit": "JPY_million",
            "segments": [
                {"name": "施設運営",
                 "expr": "stores * sales_per_store",
                 "vars": {"stores": var(10),
                          "sales_per_store": var(200)}},
                {"name": "通販",
                 "expr": "users * arpu",
                 "vars": {"users": var(500),
                          "arpu": var(3, basis="disclosed",
                                      note=None, source="https://example.invalid/ir")}},
            ],
        },
        "profit": {"op_margin": var(0.1)},
    }
    model.update(over)
    return model


ESTIMATE_YAML = """models:
  - as_of: "2024-01-10"
    period: "FY2025-03"
    status: draft
    note: "合成の初版"
    revenue:
      unit: JPY_million
      segments:
        - name: "施設運営"
          expr: "stores * sales_per_store"
          vars:
            stores:
              value: 10
              unit: "店"
              basis: assumed
              note: "合成の仮定"
            sales_per_store:
              value: 200
              unit: "百万円/年"
              basis: assumed
              note: "合成の仮定"
    profit:
      op_margin:
        value: 0.1
        basis: assumed
        note: "合成の仮定"
"""

FUND_COLS = ["period", "code", "metric", "value", "unit", "tolerance", "status",
             "source_primary", "value_primary", "fetched_at"]


class Sandbox:
    """一時ディレクトリに estimates/ と data/fundamentals/ を組み立てる。"""

    def __init__(self):
        self.root = Path(tempfile.mkdtemp(prefix="kabu-estimate-"))
        (self.root / "estimates").mkdir()
        (self.root / "data" / "fundamentals").mkdir(parents=True)

    def yaml(self, code: str, text: str) -> Path:
        path = self.root / "estimates" / f"{code}.yaml"
        with path.open("w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        return path

    def fundamentals(self, code: str, rows: list[dict]) -> Path:
        path = self.root / "data" / "fundamentals" / f"{code}.csv"
        with path.open("w", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=FUND_COLS, lineterminator="\n")
            w.writeheader()
            for r in rows:
                w.writerow({c: r.get(c, "") for c in FUND_COLS})
        return path

    def close(self):
        shutil.rmtree(self.root, ignore_errors=True)


def frow(period, metric, value, status, value_primary=""):
    return {"period": period, "code": "9999", "metric": metric,
            "value": "" if value is None else str(value),
            "unit": "JPY_million", "tolerance": "1", "status": status,
            "source_primary": "a", "value_primary": str(value_primary),
            "fetched_at": "2024-01-01T00:00:00+09:00"}


def run_main(argv: list[str]) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        rc = E.main(argv)
    return rc, out.getvalue(), err.getvalue()


# =============================================================================
# テスト本体
# =============================================================================

def test_outputs_two_segments() -> None:
    model = make_model()
    eq(E.validate_model(model), [], "有効なモデルの検証エラー")
    out = E.outputs(model)
    near(out["revenue_total"], 3500.0, "revenue_total")
    near(out["operating_income"], 350.0, "operating_income")
    near(out["op_margin"], 0.1, "op_margin")
    eq([s["name"] for s in out["segments"]], ["施設運営", "通販"], "セグメント順は入力順")
    eq(out["segments"][0]["expr_filled"], "10 × 200 = 2,000", "expr_filled（代入表示）")
    eq(out["segments"][1]["expr_filled"], "500 × 3 = 1,500", "expr_filled（第2セグメント）")
    near(out["segments"][0]["value"], 2000.0, "セグメント値")


def test_expr_rejects_unsafe_nodes() -> None:
    bad_exprs = ["__import__('os')",          # 関数呼び出し（インポート悪用）
                 "stores.attr",               # 属性参照
                 "f(1)",                      # 関数呼び出し
                 "stores[0]",                 # 添字
                 "stores ** 2"]               # べき乗（許可演算子外）
    for expr in bad_exprs:
        model = make_model()
        model["revenue"]["segments"][0]["expr"] = expr
        errors = E.validate_model(model)
        assert errors, f"{expr!r} がエラーにならない"
        assert any("expr" in e and ("使えない要素" in e or "解析できない" in e)
                   for e in errors), f"{expr!r} のエラー文が不明瞭: {errors}"
    # 対照: 四則演算・符号・括弧だけの式は通る
    model = make_model()
    model["revenue"]["segments"][0]["expr"] = "-(stores + 2) * sales_per_store / 1"
    eq(E.validate_model(model), [], "安全な式まで弾いている")


def test_basis_constraints() -> None:
    # assumed で note 欠落 → エラー
    model = make_model()
    model["revenue"]["segments"][0]["vars"]["stores"] = {
        "value": 10, "basis": "assumed"}
    errors = E.validate_model(model)
    assert any("stores" in e and "note" in e for e in errors), \
        f"assumed の note 欠落を検出しない: {errors}"
    # actual で source 欠落 → エラー
    model = make_model()
    model["profit"]["op_margin"] = {"value": 0.1, "basis": "actual", "note": "x"}
    errors = E.validate_model(model)
    assert any("op_margin" in e and "source" in e for e in errors), \
        f"actual の source 欠落を検出しない: {errors}"
    # disclosed で source 欠落 → エラー / basis 語彙外 → エラー
    model = make_model()
    model["revenue"]["segments"][1]["vars"]["arpu"] = {
        "value": 3, "basis": "disclosed"}
    assert any("arpu" in e and "source" in e for e in E.validate_model(model)), \
        "disclosed の source 欠落を検出しない"
    model = make_model()
    model["revenue"]["segments"][0]["vars"]["stores"]["basis"] = "guess"
    assert any("basis" in e for e in E.validate_model(model)), "語彙外 basis を通した"


def test_sensitivity_orders_by_impact() -> None:
    # 施設 90%（9,000）・通販 10%（1,000）。+10% の寄与は op_margin 10% > 施設 9% > 通販 1%
    model = make_model()
    model["revenue"]["segments"][0]["vars"]["stores"] = var(30)
    model["revenue"]["segments"][0]["vars"]["sales_per_store"] = var(300)
    model["revenue"]["segments"][1]["vars"]["users"] = var(1000)
    model["revenue"]["segments"][1]["vars"]["arpu"] = var(1)
    eq(E.validate_model(model), [], "前提モデルが有効でない")
    rows = E.sensitivity(model)
    eq(len(rows), 5, "変数4つ + op_margin")
    eq(rows[0]["var"], "op_margin", "先頭は op_margin（全社に効く）")
    eq(rows[0]["segment"], None, "op_margin の segment は None")
    seg_of = {r["var"]: r["segment"] for r in rows}
    eq(seg_of["stores"], "施設運営", "segment の対応")
    order = [r["var"] for r in rows]
    assert order.index("stores") < order.index("users"), \
        f"大セグメントの変数が小セグメントより後: {order}"
    # 降順であること（構造で検証）
    deltas = [r["delta_op_pct"] for r in rows]
    assert all(a >= b for a, b in zip(deltas, deltas[1:])), f"降順でない: {deltas}"
    near(rows[0]["delta_op_pct"], 10.0, "op_margin +10% は営業利益 +10%")


def test_comparisons_uses_only_ok_rows() -> None:
    sb = Sandbox()
    try:
        sb.fundamentals("9999", [
            # 前期（FY2024-03）実績。OK|ROUNDING もフラグに OK を含むので採用
            frow("FY2024-03", "revenue", 900, "OK"),
            frow("FY2024-03", "revenue", 1000, "OK"),           # 追記の後勝ち
            frow("FY2024-03", "operating_income", 100, "OK|ROUNDING"),
            # 対象期（FY2025-03）の会社計画
            frow("FY2025-03", "revenue_plan", 1200, "OK"),
            # SINGLE_SOURCE は value_primary に値があっても拾わない
            frow("FY2025-03", "operating_income_plan", None, "SINGLE_SOURCE",
                 value_primary=999),
            # 当期実績: revenue は value 入りでも SINGLE_SOURCE なら拾わない
            frow("FY2025-03", "revenue", 5555, "SINGLE_SOURCE"),
            frow("FY2025-03", "operating_income", 130, "OK"),
            # MISMATCH も拾わない
            frow("FY2024-03", "net_income", 50, "MISMATCH"),
        ])
        comp = E.comparisons("9999", "FY2025-03", root=sb.root)
        eq(comp["plan"]["revenue"], 1200.0, "会社計画の売上")
        eq(comp["plan"]["operating_income"], None,
           "SINGLE_SOURCE の計画を拾った（value_primary で埋めてはいけない）")
        eq(comp["prev_actual"]["revenue"], 1000.0, "前期売上は最後の OK 行（追記型）")
        eq(comp["prev_actual"]["operating_income"], 100.0, "OK|ROUNDING は採用")
        eq(comp["actual"]["revenue"], None, "SINGLE_SOURCE の当期売上を拾った")
        eq(comp["actual"]["operating_income"], 130.0, "当期営業利益")
        # CSV そのものが無い銘柄 → 全て None（エラーにしない）
        comp2 = E.comparisons("0000", "FY2025-03", root=sb.root)
        eq(comp2["plan"], {"revenue": None, "operating_income": None}, "CSV 無し")
    finally:
        sb.close()


def test_undefined_var_and_zero_division() -> None:
    # 未定義変数
    model = make_model()
    model["revenue"]["segments"][0]["expr"] = "stores * unknown_var"
    errors = E.validate_model(model)
    assert any("unknown_var" in e and "定義されていない" in e for e in errors), \
        f"未定義変数を検出しない: {errors}"
    # ゼロ除算
    model = make_model()
    model["revenue"]["segments"][0]["expr"] = "stores / divisor"
    model["revenue"]["segments"][0]["vars"]["divisor"] = var(0)
    errors = E.validate_model(model)
    assert any("ゼロ除算" in e for e in errors), f"ゼロ除算を検出しない: {errors}"
    # 数値でない value
    model = make_model()
    model["revenue"]["segments"][0]["vars"]["stores"]["value"] = "10店"
    assert any("数値でない" in e for e in E.validate_model(model)), \
        "文字列の value を通した"


def test_load_validate_and_cli() -> None:
    sb = Sandbox()
    try:
        # 無いコード → None
        eq(E.load_estimate("0000", root=sb.root), None, "無いファイルは None")
        # 有効な YAML
        path = sb.yaml("9999", ESTIMATE_YAML)
        est = E.load_estimate("9999", root=sb.root)
        eq(est["errors"], [], "有効な YAML の errors")
        eq(len(est["models"]), 1, "models 件数")
        eq(E.validate_file(path), [], "validate_file（有効）")
        # 壊れた YAML（note 無し assumed）→ validate_file がファイル名つきで返す
        bad = ESTIMATE_YAML.replace('              note: "合成の仮定"\n', "", 1)
        bad_path = sb.yaml("8888", bad)
        errors = E.validate_file(bad_path)
        assert errors and all("8888.yaml" in e for e in errors), f"NG: {errors}"
        # CLI: 有効 → exit 0。outputs・sensitivity・comparisons が表示される
        sb.fundamentals("9999", [frow("FY2024-03", "revenue", 1800, "OK")])
        rc, out, err = run_main(["--code", "9999", "--root", str(sb.root)])
        eq(rc, 0, f"CLI exit（stderr: {err}）")
        assert "10 × 200 = 2,000" in out, f"expr_filled が表示に無い: {out}"
        assert "op_margin" in out, "感応度に op_margin が無い"
        assert "前期実績: 売上 1,800" in out, f"comparisons が表示に無い: {out}"
        # CLI: 検証 NG → exit 1・stderr にエラー
        rc, _, err = run_main(["--code", "8888", "--root", str(sb.root)])
        eq(rc, 1, "検証 NG の exit code")
        assert "note" in err, f"エラー文が stderr に無い: {err}"
        # CLI: --all は検証のみ（NG が1つでもあれば exit 1）
        rc, out, _ = run_main(["--all", "--root", str(sb.root)])
        eq(rc, 1, "--all の exit code（NG を含む）")
        assert "OK  9999.yaml" in out and "NG  8888.yaml" in out, out
    finally:
        sb.close()


def main() -> int:
    tests = [
        ("2セグメントの outputs（合計・営業利益・代入表示）", test_outputs_two_segments),
        ("式の安全性（関数呼び出し・属性・添字などを弾く）", test_expr_rejects_unsafe_nodes),
        ("basis 制約（assumed は note・actual/disclosed は source）",
         test_basis_constraints),
        ("sensitivity は影響の大きい順", test_sensitivity_orders_by_impact),
        ("comparisons は status に OK を含む行だけ使う（D53）",
         test_comparisons_uses_only_ok_rows),
        ("未定義変数・ゼロ除算・非数値が検証エラー", test_undefined_var_and_zero_division),
        ("load_estimate / validate_file / CLI の入出力", test_load_validate_and_cli),
    ]
    failed = []
    for name, fn in tests:
        try:
            fn()
            print(f"  PASS  {name}")
        except AssertionError as e:
            failed.append(name)
            print(f"  FAIL  {name}: {e}")
        except Exception as e:  # noqa: BLE001
            failed.append(name)
            print(f"  ERROR {name}: {type(e).__name__}: {e}")
    print(f"\n{len(tests) - len(failed)}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
