"""次期売上・利益のフェルミ推定（estimates/{code}.yaml の検証・計算・感応度・実績比較）。

estimates/{code}.yaml は追記型。モデルは書き換えず新しい版を末尾に足す
（変遷そのものが学習履歴。最新モデル = models の末尾）。

役割分担（3行）:
  - 計算はコードが行う（式の評価・感応度・実績との突合。eval は使わず AST で評価）。
  - 値の選定・仮定は人間/LLM が行い、basis: assumed と note で仮定を必ず明示する。
  - コードは仮定の中身の妥当性を判定しない。検査するのは形式と計算可能性だけ。

原則:
  - 式は ast.parse で解析し、変数・数値・+ - * /・符号 **のみ** 許可（関数呼び出し・
    属性・添字は検証エラー）。eval は使わない。
  - basis=assumed は note 必須。basis=actual/disclosed は source 必須。
    expr の変数は vars に全て定義されていること。
  - comparisons は data/fundamentals/{code}.csv の **status に OK を含む行だけ** を
    採用する（株価の採用終値と同じ規律・D53）。value の有無で判定しない。
  - 出力に実行時刻を埋め込まない（D8）。並びは入力順・感応度は決定的なソートで固定。
  - このモジュールはファイルを書かない（読み・計算・表示のみ）。

実行:
  $env:PYTHONIOENCODING = "utf-8"; python src/estimate.py --code 6570
  $env:PYTHONIOENCODING = "utf-8"; python src/estimate.py --all
"""
from __future__ import annotations

import argparse
import ast
import csv
import re
import sys
from pathlib import Path

import yaml

import yamlio as Y

ROOT = Path(__file__).resolve().parents[1]

_BASIS = ("actual", "disclosed", "assumed")
_STATUS = ("draft", "confirmed")
MARKET_UNIT = "JPY_million"                        # market_forecast の数値は百万円固定

# 感応度の op_margin 行に必ず添える注記（sensitivity の trivial=True）。
TRIVIAL_NOTE = ("乗法モデルなので定義上つねに +10%。売上側の変数との大小比較には"
                "意味が無い（率の水準そのものを疑うこと）")
_PERIOD_RE = re.compile(r"^FY(\d{4})-(\d{2})$")   # fundamentals の period 表記と同じ
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# 式で許可する AST ノード。これ以外は全て検証エラー（ホワイトリスト方式）。
_BIN_OPS: dict[type, tuple[str, int]] = {          # 型 -> (表示記号, 優先順位)
    ast.Add: ("+", 1), ast.Sub: ("-", 1), ast.Mult: ("×", 2), ast.Div: ("÷", 2),
}
_ALLOWED_NODES = (ast.Expression, ast.BinOp, ast.UnaryOp, ast.Name, ast.Constant,
                  ast.Load, ast.Add, ast.Sub, ast.Mult, ast.Div, ast.UAdd, ast.USub)
_NODE_LABEL = {ast.Call: "関数呼び出し", ast.Attribute: "属性参照",
               ast.Subscript: "添字", ast.Pow: "べき乗", ast.Mod: "剰余",
               ast.FloorDiv: "切り捨て除算", ast.Compare: "比較", ast.BoolOp: "論理演算",
               ast.Lambda: "lambda", ast.IfExp: "条件式"}


# =============================================================================
# 安全な式評価（eval を使わない）
# =============================================================================

def _is_number(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _expr_errors(expr, where: str) -> tuple[list[str], ast.expr | None]:
    """式を解析して (エラー一覧, 解析済みノード) を返す。エラー時ノードは None。"""
    if not isinstance(expr, str) or not expr.strip():
        return [f"{where}: expr が空か文字列でない: {expr!r}"], None
    try:
        tree = ast.parse(expr, mode="eval")
    except SyntaxError as e:
        return [f"{where}: expr を式として解析できない: {expr!r}（{e.msg}）"], None
    errors: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, _ALLOWED_NODES):
            if isinstance(node, ast.Constant) and not _is_number(node.value):
                errors.append(f"{where}: expr の定数が数値でない: {node.value!r}")
            continue
        label = next((lab for t, lab in _NODE_LABEL.items() if isinstance(node, t)),
                     type(node).__name__)
        errors.append(f"{where}: expr に使えない要素（{label}）が含まれる"
                      f"（使えるのは変数・数値・+ - * /・符号のみ）: {expr!r}")
    errors = list(dict.fromkeys(errors))           # 同型ノードの重複を畳む（順序維持）
    return errors, (None if errors else tree.body)


def _expr_names(node: ast.expr) -> list[str]:
    """式が参照する変数名（初出順）。"""
    return list(dict.fromkeys(
        n.id for n in ast.walk(node) if isinstance(n, ast.Name)))


def _eval(node: ast.expr, vals: dict[str, float]) -> float:
    """検証済みノードを評価する。ゼロ除算は ZeroDivisionError のまま上げる。"""
    if isinstance(node, ast.Constant):
        return float(node.value)
    if isinstance(node, ast.Name):
        return float(vals[node.id])
    if isinstance(node, ast.UnaryOp):
        v = _eval(node.operand, vals)
        return -v if isinstance(node.op, ast.USub) else v
    left, right = _eval(node.left, vals), _eval(node.right, vals)
    op = type(node.op)
    if op is ast.Add:
        return left + right
    if op is ast.Sub:
        return left - right
    if op is ast.Mult:
        return left * right
    return left / right                            # Div（許可済みの残りはこれだけ）


def _fmt(v) -> str:
    """代入表示用の数値表記（21060 -> '21,060'・0.083 -> '0.083'）。

    整数判定は float 誤差に耐えるようにする。二進小数では 80×280 が
    22399.999999999996 になり得て、`is_integer()` だけで見ると '22,400.0' と
    小数点が1つ生えた表示になる（数値は同じなのに版によって見た目が変わる）。
    """
    f = float(v)
    r = round(f)
    if abs(f - r) <= max(1e-9, abs(f) * 1e-12):
        return f"{int(r):,}"
    return f"{round(f, 4):,}"


def _render(node: ast.expr, vals: dict[str, float]) -> tuple[str, int]:
    """式に値を代入した表示文字列（'78 × 270' など）と優先順位を返す。"""
    if isinstance(node, ast.Constant):
        return _fmt(node.value), 9
    if isinstance(node, ast.Name):
        return _fmt(vals[node.id]), 9
    if isinstance(node, ast.UnaryOp):
        s, p = _render(node.operand, vals)
        if p < 3:
            s = f"({s})"
        return ("-" if isinstance(node.op, ast.USub) else "+") + s, 3
    sym, prec = _BIN_OPS[type(node.op)]
    ls, lp = _render(node.left, vals)
    rs, rp = _render(node.right, vals)
    if lp < prec:
        ls = f"({ls})"
    if rp < prec or (rp == prec and isinstance(node.op, (ast.Sub, ast.Div))):
        rs = f"({rs})"
    return f"{ls} {sym} {rs}", prec


# =============================================================================
# 検証
# =============================================================================

def _entry_errors(entry, where: str) -> list[str]:
    """vars の1変数（op_margin も同じ構造）の検証。"""
    if not isinstance(entry, dict):
        return [f"{where}: 辞書でない（value / basis / note / source を持つこと）: {entry!r}"]
    errors: list[str] = []
    if not _is_number(entry.get("value")):
        errors.append(f"{where}: value が数値でない: {entry.get('value')!r}")
    basis = entry.get("basis")
    if basis not in _BASIS:
        errors.append(f"{where}: basis が不正（actual / disclosed / assumed のみ）: {basis!r}")
    elif basis == "assumed":
        if not str(entry.get("note") or "").strip():
            errors.append(f"{where}: basis=assumed には note（仮定の根拠）が必須")
    else:                                          # actual / disclosed
        if not str(entry.get("source") or "").strip():
            errors.append(f"{where}: basis={basis} には source（出典）が必須")
    return errors


def _segment_errors(seg, where: str) -> list[str]:
    if not isinstance(seg, dict):
        return [f"{where}: セグメントが辞書でない: {seg!r}"]
    errors: list[str] = []
    name = seg.get("name")
    if not str(name or "").strip():
        errors.append(f"{where}: name（セグメント名）がない")
    variables = seg.get("vars")
    if not isinstance(variables, dict) or not variables:
        errors.append(f"{where}: vars（変数定義）が空か辞書でない")
        variables = {}
    for vname in variables:
        errors += _entry_errors(variables[vname], f"{where}.vars.{vname}")

    expr_errs, node = _expr_errors(seg.get("expr"), where)
    errors += expr_errs
    if node is None:
        return errors
    undefined = [n for n in _expr_names(node) if n not in variables]
    for n in undefined:
        errors.append(f"{where}: expr の変数 {n} が vars に定義されていない")
    if errors:
        return errors
    # 形式が通ったものだけ試算し、計算可能性（ゼロ除算）を先に検出する
    vals = {k: float(v["value"]) for k, v in variables.items()}
    try:
        _eval(node, vals)
    except ZeroDivisionError:
        errors.append(f"{where}: expr の評価でゼロ除算が起きる"
                      f"（変数の値を見直すこと）: {seg.get('expr')!r}")
    return errors


def validate_model(model) -> list[str]:
    """モデル1件のスキーマ・式・数値・basis 制約を検証する。空リスト = 有効。"""
    if not isinstance(model, dict):
        return [f"モデルが辞書でない: {model!r}"]
    errors: list[str] = []
    if not _DATE_RE.match(str(model.get("as_of") or "")):
        errors.append(f"as_of が YYYY-MM-DD でない: {model.get('as_of')!r}")
    if not _PERIOD_RE.match(str(model.get("period") or "")):
        errors.append(f"period が FYyyyy-mm 形式でない"
                      f"（fundamentals の period 表記と揃えること）: {model.get('period')!r}")
    if model.get("status") not in _STATUS:
        errors.append(f"status が不正（draft / confirmed のみ）: {model.get('status')!r}")

    revenue = model.get("revenue")
    if not isinstance(revenue, dict):
        errors.append("revenue がない（unit と segments を持つこと）")
    else:
        if not str(revenue.get("unit") or "").strip():
            errors.append("revenue.unit がない（表示単位。例: JPY_million）")
        segments = revenue.get("segments")
        if not isinstance(segments, list) or not segments:
            errors.append("revenue.segments が空（少なくとも1セグメント必要）")
        else:
            for i, seg in enumerate(segments):
                label = ""
                if isinstance(seg, dict) and seg.get("name"):
                    label = f"({seg['name']})"
                errors += _segment_errors(seg, f"revenue.segments[{i}]{label}")

    profit = model.get("profit")
    if not isinstance(profit, dict) or "op_margin" not in profit:
        errors.append("profit.op_margin がない（営業利益率。vars と同じ構造）")
    else:
        errors += _entry_errors(profit["op_margin"], "profit.op_margin")
    return errors


def _market_errors(mf, model_units=()) -> list[str]:
    """ファイル先頭の market_forecast:（市場予想・任意）の検証。

    数値を載せるなら出典必須。数値が無いなら「無い理由」の note 必須
    （カバー0社であること自体が情報なので、黙って省略させない）。

    数値があるときは、さらに次も見る。表示側（build.render_estimate）が
    当台帳推定と同じ表に百万円で並べ、乖離率まで出すため、ここを素通りさせると
    「単位も出所も違う数どうしの引き算」が黙って画面に出る:
      - name（誰の予想か）が無い匿名のコンセンサスは検証しようがない
      - 売上がマイナスはあり得ない（営業利益は赤字があるので許す）
      - モデルの revenue.unit が百万円でないなら、並べた時点で桁が違う
    """
    if mf is None:
        return []
    if not isinstance(mf, dict):
        return ["market_forecast は辞書"
                "（name / revenue / operating_income / source / note）で書く"]
    errs: list[str] = []
    has_num = False
    for k in ("revenue", "operating_income"):
        v = mf.get(k)
        if v is None:
            continue
        has_num = True
        if not isinstance(v, (int, float)) or isinstance(v, bool):
            errs.append(f"market_forecast.{k} は数値（百万円）で書く")
        elif k == "revenue" and v < 0:
            errs.append(f"market_forecast.revenue が負: {v!r}（売上に負は無い）")
    if has_num:
        if not str(mf.get("source") or "").strip():
            errs.append("market_forecast: 数値を載せるなら source（URL＋取得日）が必須")
        if not str(mf.get("name") or "").strip():
            errs.append("market_forecast: 数値を載せるなら name（誰の予想か）が必須")
        bad_units = [u for u in dict.fromkeys(model_units) if u != MARKET_UNIT]
        if bad_units:
            errs.append(
                f"market_forecast の数値は{MARKET_UNIT}（百万円）固定だが、"
                f"revenue.unit が {' / '.join(bad_units)} のモデルがある"
                "（同じ表に並べると桁が違う。単位を揃えるか数値を載せない）")
    elif not str(mf.get("note") or "").strip():
        errs.append("market_forecast: 数値が無いなら note（無い理由）が必須")
    return errs


def _doc_errors(path: Path) -> tuple[dict, list, list[str]]:
    """ファイル全体を (doc, models, エラー一覧) に読む。"""
    try:
        data = Y.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as e:
        return {}, [], [f"{path.name}: YAML を解析できない: {e}"]
    if not isinstance(data, dict) or not isinstance(data.get("models"), list):
        return {}, [], [f"{path.name}: 先頭に models:（モデルのリスト）が必要"]
    models = data["models"]
    if not models:
        return data, [], [f"{path.name}: models が空（少なくとも1モデル必要）"]
    errors: list[str] = []
    units: list[str] = []
    for i, model in enumerate(models):
        errors += [f"{path.name} models[{i}]: {e}" for e in validate_model(model)]
        if isinstance(model, dict) and isinstance(model.get("revenue"), dict):
            unit = str(model["revenue"].get("unit") or "").strip()
            if unit:
                units.append(unit)
    errors += [f"{path.name}: {e}"
               for e in _market_errors(data.get("market_forecast"), units)]
    return data, models, errors


def validate_file(path) -> list[str]:
    """checks.py から呼ぶ用。ファイル内の全モデルの検証エラーを返す（空 = 有効）。"""
    return _doc_errors(Path(path))[2]


def load_estimate(code: str, root=ROOT) -> dict | None:
    """estimates/{code}.yaml を読む。無ければ None。errors が空なら有効。"""
    path = Path(root) / "estimates" / f"{code}.yaml"
    if not path.exists():
        return None
    data, models, errors = _doc_errors(path)
    return {"models": models, "errors": errors,
            "market_forecast": data.get("market_forecast")}


# =============================================================================
# 計算（検証を通ったモデルにだけ使う）
# =============================================================================

def outputs(model) -> dict:
    """売上（セグメント別＋合計）と営業利益。validate_model が空のモデル専用。"""
    segments_out: list[dict] = []
    total = 0.0
    for seg in model["revenue"]["segments"]:
        node = ast.parse(seg["expr"], mode="eval").body
        vals = {k: float(v["value"]) for k, v in seg["vars"].items()}
        value = _eval(node, vals)
        filled, _ = _render(node, vals)
        segments_out.append({"name": seg["name"], "value": value,
                             "expr_filled": f"{filled} = {_fmt(value)}"})
        total += value
    margin = float(model["profit"]["op_margin"]["value"])
    return {"revenue_total": total, "segments": segments_out,
            "operating_income": total * margin, "op_margin": margin}


def _delta_pct(new_oi: float, base_oi: float) -> float | None:
    if base_oi == 0:
        return None
    return round((new_oi - base_oi) / abs(base_oi) * 100.0, 6)


def sensitivity(model) -> list[dict]:
    """各変数（op_margin 含む）を +10% したときの営業利益の変化率。影響の大きい順。

    並びは決定的: |変化率| 降順 → セグメント名 → 変数名（D8。同率でも順が揺れない）。

    `trivial` は「その行の数字が発見ではない」印。営業利益 = 売上合計 × op_margin
    という乗法モデルでは op_margin を +10% すれば営業利益は定義上つねに +10% になり、
    必ず（またはタイで）先頭に来る。これを売上側の変数と同じ表で並べると
    「最も効く変数は op_margin」という当たり前の結論を発見のように読ませてしまうため、
    表示側が区別できるよう印を付ける（TRIVIAL_NOTE を添えて出す）。
    """
    base = outputs(model)
    base_oi = base["operating_income"]
    margin = base["op_margin"]
    rows: list[dict] = []
    for seg in model["revenue"]["segments"]:
        node = ast.parse(seg["expr"], mode="eval").body
        vals = {k: float(v["value"]) for k, v in seg["vars"].items()}
        seg_value = _eval(node, vals)
        for name in vals:
            bumped = dict(vals)
            bumped[name] = bumped[name] * 1.1
            try:
                new_total = base["revenue_total"] - seg_value + _eval(node, bumped)
                delta = _delta_pct(new_total * margin, base_oi)
            except ZeroDivisionError:              # +10% で分母が 0 になる端
                delta = None
            rows.append({"var": name, "segment": seg["name"], "delta_op_pct": delta,
                         "trivial": False})
    rows.append({"var": "op_margin", "segment": None, "trivial": True,
                 "delta_op_pct": _delta_pct(base["revenue_total"] * margin * 1.1,
                                            base_oi)})
    rows.sort(key=lambda r: (r["delta_op_pct"] is None,
                             -abs(r["delta_op_pct"] or 0.0),
                             r["segment"] or "", r["var"]))
    return rows


# =============================================================================
# 実績比較（fundamentals との突合）
# =============================================================================

def _flags(status) -> list[str]:
    return [p for p in str(status or "").split("|") if p]


def _num(v) -> float | None:
    s = str(v or "").strip().replace(",", "")
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _read_csv(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def _prev_period(period: str) -> str:
    m = _PERIOD_RE.match(str(period))
    if not m:
        raise ValueError(f"period が FYyyyy-mm 形式でない: {period!r}")
    return f"FY{int(m.group(1)) - 1}-{m.group(2)}"


def comparisons(code: str, period: str, root=ROOT, unit=None) -> dict:
    """会社計画・前期実績・当期実績を data/fundamentals/{code}.csv から引く。

    **status に OK を含む行だけ** を使う（採用終値と同じ規律・D53。value の有無や
    SINGLE_SOURCE の value_primary で判定しない）。append-only なので同じ
    period×metric が複数あれば後の行（最新）を採る。無い値は None（決算が届いたら
    actual が埋まる）。

    `unit` を渡すと fundamentals の unit 列と突き合わせ、**違う単位の行は採らない**。
    推定モデルは百万円（revenue.unit）で組むのに fundamentals 側は metric ごとに
    unit を持つ（JPY / pct / x もある）ため、突き合わせないと「百万円の推定」と
    「円の実績」を黙って同じ表に並べ、乖離率まで計算してしまう。除外した行は
    戻り値の `unit_mismatch` に残す（黙って落とさない）。
    """
    prev = _prev_period(period)
    rows = _read_csv(Path(root) / "data" / "fundamentals" / f"{code}.csv")
    want = str(unit or "").strip()
    mismatch: list[str] = []

    def pick(per: str, metric: str) -> float | None:
        got = None
        for r in rows:
            if r.get("period") != per or r.get("metric") != metric:
                continue
            if "OK" not in _flags(r.get("status")):
                continue
            row_unit = str(r.get("unit") or "").strip()
            if want and row_unit and row_unit != want:
                mismatch.append(f"{per} {metric}: fundamentals の unit={row_unit} が"
                                f"モデルの unit={want} と違うため比較から除外した")
                continue
            v = _num(r.get("value"))
            if v is not None:
                got = v                            # 後の行 = 最新の採用値
        return got

    out = {
        "plan": {"revenue": pick(period, "revenue_plan"),
                 "operating_income": pick(period, "operating_income_plan")},
        "prev_actual": {"revenue": pick(prev, "revenue"),
                        "operating_income": pick(prev, "operating_income")},
        "actual": {"revenue": pick(period, "revenue"),
                   "operating_income": pick(period, "operating_income")},
    }
    out["unit"] = want or None
    out["unit_mismatch"] = list(dict.fromkeys(mismatch))   # 並びは決定的（D8）
    return out


# =============================================================================
# CLI
# =============================================================================

def _fmt_or_dash(v: float | None) -> str:
    return "—" if v is None else _fmt(v)


def _print_code(code: str, root: Path) -> int:
    est = load_estimate(code, root=root)
    if est is None:
        print(f"estimates/{code}.yaml が無い", file=sys.stderr)
        return 1
    if est["errors"]:
        print(f"== {code} 検証 NG（{len(est['errors'])}件） ==", file=sys.stderr)
        for e in est["errors"]:
            print(f"  - {e}", file=sys.stderr)
        return 1

    model = est["models"][-1]                      # 追記型: 末尾が最新
    out = outputs(model)
    unit = model["revenue"]["unit"]
    status = model["status"] + ("（作りかけ）" if model["status"] == "draft" else "")
    print(f"== {code} 検証 OK（モデル {len(est['models'])}件・最新を表示） ==")
    print(f"as_of={model['as_of']} / period={model['period']} / status={status}")
    if str(model.get("note") or "").strip():
        print(f"note: {model['note']}")

    print(f"\n売上の内訳（単位: {unit}）:")
    for seg in out["segments"]:
        print(f"  - {seg['name']}: {seg['expr_filled']}")
    print(f"  売上合計: {_fmt(out['revenue_total'])}")
    print(f"営業利益: {_fmt(out['revenue_total'])} × {_fmt(out['op_margin'])}"
          f" = {_fmt(out['operating_income'])}（op_margin）")

    print("\n感応度（各変数 +10% → 営業利益の変化率・影響の大きい順）:")
    for row in sensitivity(model):
        label = row["var"] if row["segment"] is None else \
            f"{row['var']}（{row['segment']}）"
        pct = "—" if row["delta_op_pct"] is None else f"{row['delta_op_pct']:+.2f}%"
        tail = f"  ※{TRIVIAL_NOTE}" if row.get("trivial") else ""
        print(f"  {label}: {pct}{tail}")

    comp = comparisons(code, model["period"], root=root, unit=unit)
    fund = Path(root) / "data" / "fundamentals" / f"{code}.csv"
    print(f"\n実績比較（{model['period']} ／ status に OK を含む採用値のみ・"
          f"単位 {unit} の行だけ採用）:")
    if not fund.exists():
        print(f"  （data/fundamentals/{code}.csv が無い）")
    for warn in comp["unit_mismatch"]:
        print(f"  ⚠ {warn}")
    for key, label in (("plan", "会社計画"), ("prev_actual", "前期実績"),
                       ("actual", "当期実績")):
        rev = _fmt_or_dash(comp[key]["revenue"])
        oi = _fmt_or_dash(comp[key]["operating_income"])
        note = "（決算待ち）" if key == "actual" and \
            comp[key]["revenue"] is None and comp[key]["operating_income"] is None else ""
        print(f"  {label}: 売上 {rev} ／ 営業利益 {oi}{note}")
    return 0


def _print_all(root: Path) -> int:
    est_dir = Path(root) / "estimates"
    if not est_dir.exists():
        print("estimates/ ディレクトリが無い（検証対象なし）")
        return 0
    files = sorted(est_dir.glob("*.yaml"))
    if not files:
        print("estimates/ に *.yaml が無い（検証対象なし）")
        return 0
    failed = 0
    for path in files:
        _doc, models, errors = _doc_errors(path)
        if errors:
            failed += 1
            print(f"NG  {path.name}（{len(errors)}件）")
            for e in errors:
                print(f"    - {e}")
        else:
            print(f"OK  {path.name}（モデル {len(models)}件）")
    print(f"\n{len(files) - failed}/{len(files)} OK")
    return 1 if failed else 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="フェルミ推定の検証と表示（--code で1銘柄、--all で全ファイル検証のみ）")
    ap.add_argument("--code", help="銘柄コード（estimates/{code}.yaml を読む）")
    ap.add_argument("--all", action="store_true", help="estimates/ の全ファイルを検証のみ")
    ap.add_argument("--root", help="リポジトリルートの差し替え（テスト用）")
    args = ap.parse_args(argv)
    root = Path(args.root) if args.root else ROOT
    if bool(args.code) == bool(args.all):
        print("--code か --all のどちらか一方を指定する", file=sys.stderr)
        return 2
    if args.all:
        return _print_all(root)
    return _print_code(args.code, root)


if __name__ == "__main__":
    raise SystemExit(main())
