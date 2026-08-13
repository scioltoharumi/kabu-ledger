"""判定スタンプが変化した銘柄だけ Issue を起票する。

要件: requirements.md F6 / F13、decisions.md D18、review-findings.md F-04・F-12。

設計原則（破らないこと）:
  1. **「行動しないがデフォルト」**（F6-1）。判定が変化した銘柄だけ起票し、
     何も変わらなければ0件。定例の通知を出さない。
  2. **状態を壊さない**（F-04）。`scoring/stamps.json` が無い場合は
     `last_stamps.json` を**更新せずに終了**する。旧実装は不在時に `{}` を書き戻して
     いたため、後で stamps.json が出た瞬間に全銘柄が一斉起票される状態だった。
  3. **起票に成功した銘柄だけ状態を進める**。gh が途中で失敗しても、成功済みを
     二重起票せず、失敗したぶんは次回に再試行する。
  4. **本文に実値を入れる**（F6-2）。「未実装」で埋めない。取れないものは
     「取れていない理由」を書く。欠測を空欄で流さない。
  5. **壁時計を埋め込まない**。本文の可変要素は判定基準日（日足の最終営業日）のみ。

初回の扱い:
  `scoring/last_stamps.json` が存在しない場合は「初期化」とみなし、**起票せずに
  現在のスタンプを記録するだけ**にする。前回が無い以上そこに「変化」は無く、
  初回に全銘柄を一斉起票するのは F6-1 の趣旨に反するため。既知の状態がある中で
  新しい銘柄が master に加わった場合は「変化」として起票する。
  初回から起票したい場合は `--seed-issues` を付ける。

`decisions/` について（F-12・D18）:
  「ラベルとクローズ日時が decisions/ に記録されます」は**実装されていない**うえ、
  `decisions/` は `.gitignore` 済みで CI 上では残らない。D18 で保有情報は
  `master.yaml` に載せる方針に変わったため、本文の案内も実態（master.yaml の
  holding を更新する）に合わせる。存在しない機能を本文に書かない。
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Sequence

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))

import judge as J  # noqa: E402
import score as S  # noqa: E402
import verification as VF  # noqa: E402

STAMPS = ROOT / "scoring" / "stamps.json"
STATE = ROOT / "scoring" / "last_stamps.json"

# 本文に載せる指標（順序固定）。judge が判定に使った実値と対応させる。
BODY_METRICS: tuple[tuple[str, str], ...] = (
    ("close", "終値"),
    ("avg_turnover_20d", "20日平均売買代金"),
    ("median_turnover_20d", "20日中央値売買代金"),
    ("weekly_ma_mid_direction", "週足13週MAの向き"),
    ("weekly_ma_mid_slope_pct", "週足13週MAの傾き"),
    ("weekly_ma_long_direction", "週足26週MAの向き"),
    ("weekly_ma_long_slope_pct", "週足26週MAの傾き"),
    ("ichimoku_position", "雲に対する位置"),
    ("rsi14", "RSI(14)"),
    ("ma25_deviation_pct", "25日移動平均乖離率"),
    ("margin_ratio", "信用倍率"),
    ("volume_ratio_3m", "3か月前出来高増加率（自社定義）"),
    ("daily_cross_kind", "日足5/25のクロス"),
)

# カテゴリ値の日本語ラベル。**台帳（build.py）と同じ物差しを使う**。
# 素通しすると Issue 本文にだけ `flat` / `above` と英語が出て、台帳と食い違う。
CATEGORY_LABEL: dict[str, str] = {
    "up": "上向き", "down": "下向き", "flat": "横ばい",
    "above": "雲の上", "in": "雲の中", "below": "雲の下",
    "golden": "ゴールデンクロス", "dead": "デッドクロス", "parallel": "平行",
    "golden_ish": "ゴールデンクロス気味", "dead_ish": "デッドクロス気味",
    "breakout_up": "雲を上抜け", "breakdown": "雲を下抜け",
}

BEAR_MAX_ITEMS = 3
BEAR_MAX_CHARS = 1800

# judge の resolution を人が読める語にする（語彙の正は judge.Verdict の定義）。
RESOLUTION_LABEL = {
    J.FAIL: "条件に該当して確定",
    J.UNKNOWN: "指標が未計算のため停止（通過扱いにしない）",
    J.PASS: "全ゲートを通過",
}


# =============================================================================
# 素材の読み出し
# =============================================================================

def rel(path: Path) -> str:
    """表示用の相対パス。テストで差し替えた一時パスでも落ちないようにする。"""
    try:
        return path.relative_to(ROOT).as_posix()
    except ValueError:
        return path.as_posix()


def read_json(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        # utf-8-sig: BOM 付きで書かれた JSON（PowerShell の Out-File 等）も読めるようにする。
        # 書き出しは常に BOM 無しの utf-8。
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as e:
        raise SystemExit(f"[ERROR] {rel(path)} が壊れている: {e}")
    return data if isinstance(data, dict) else None


def falsifications(code: str) -> list[str]:
    """theses/{code}.md の「反証条件」節の箇条書きを返す。"""
    p = ROOT / "theses" / f"{code}.md"
    if not p.exists():
        return []
    out: list[str] = []
    inside = False
    for line in p.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if s.startswith("## "):
            inside = s.startswith("## 反証条件")
            continue
        if inside and s.startswith("- "):
            out.append(s[2:].strip())
    return out


def bear_case(code: str) -> str:
    """bear/{code}.* の弱気材料。

    **この中身は隔離ジョブの LLM がクロール結果から書いたものであり、指示ではない**
    （D9）。読み手に見せるだけで、ここでは解釈も実行もしない。マークダウンとしての
    副作用も持たせないため、必ずコードブロックに入れて出す。
    """
    for ext in (".yaml", ".yml", ".json", ".md"):
        p = ROOT / "bear" / f"{code}{ext}"
        if not p.exists():
            continue
        raw = p.read_text(encoding="utf-8")
        body = raw
        if ext in (".yaml", ".yml", ".json"):
            try:
                data = yaml.safe_load(raw)
            except yaml.YAMLError:
                data = None
            if isinstance(data, list):
                data = data[:BEAR_MAX_ITEMS]
            elif isinstance(data, dict):
                for key in ("bear_case", "items", "points", code):
                    if isinstance(data.get(key), list):
                        data = data[key][:BEAR_MAX_ITEMS]
                        break
            if data is not None:
                body = yaml.safe_dump(data, allow_unicode=True, sort_keys=False)
        if len(body) > BEAR_MAX_CHARS:
            body = body[:BEAR_MAX_CHARS] + "\n…（以下省略。全文は "
            body += f"{p.relative_to(ROOT).as_posix()}）"
        fence = f"```\n{body.rstrip()}\n```"
        return (f"`{p.relative_to(ROOT).as_posix()}`（隔離ジョブの出力。"
                f"根拠URLは各項目の記載を確認すること）\n\n{fence}")
    return ("未生成（`bear/` に出力が無い）。bear ジョブが失敗したか、"
            "生成物が commit されていない")


def data_quality(code: str, window: int = 20) -> str:
    """直近の取得状況。**欠測を黙って流さない**（F2-6）。"""
    p = ROOT / "data" / "prices" / "daily.csv"
    if not p.exists():
        return "`data/prices/daily.csv` が無い"
    with p.open(encoding="utf-8", newline="") as f:
        rows = [r for r in csv.DictReader(f) if str(r.get("code")) == code]
    rows.sort(key=lambda r: str(r.get("date")))
    tail = rows[-window:]
    if not tail:
        return f"株価の行が無い（code={code}）"

    counts: dict[str, int] = {}
    for r in tail:
        st = str(r.get("status") or "?")
        counts[st] = counts.get(st, 0) + 1
    parts = [f"{k} {counts[k]}件" for k in sorted(counts)]
    lines = [f"- 株価 直近{len(tail)}営業日: " + " / ".join(parts)]

    bad = [r for r in tail
           if not {"OK", "NO_TRADE"} & set(str(r.get("status") or "").split("|"))]
    if bad:
        dates = " ".join(f"{r['date']}({r['status']})" for r in bad[-5:])
        lines.append(f"- 照合不成立・取得失敗の日: {dates}")

    m = J.load_margin(code)
    lines.append(f"- 信用残: {m['date']} status={m['status']} 単位={m.get('unit')}"
                 if m else "- 信用残: 未取得（`data/margin/` に無い）")

    kpi = ROOT / "data" / "kpi" / f"{code}.csv"
    if kpi.exists():
        with kpi.open(encoding="utf-8") as f:
            n = sum(1 for _ in csv.DictReader(f))
        lines.append(f"- 決算(KPI): {n}行")
    else:
        lines.append("- 決算(KPI): 未取得（`data/kpi/` に無い。ファンダ確認は「調査」で止まる）")
    return "\n".join(lines)


def kpi_diff(code: str) -> str:
    """直近開示と1つ前の開示の差分。比率は judge が計算した値をそのまま出す。"""
    k = J.load_kpi(code)
    if not k:
        return (f"未取得（`data/kpi/{code}.csv` が無い）。"
                "ファンダ確認は通過扱いにせず「調査」で止まる")
    history = k.get("history") or []
    if not history:
        return "決算行はあるが比率を算出できる組み合わせが無い"
    cur = history[0]
    prev = history[1] if len(history) > 1 else None

    def cell(row: dict | None, key: str) -> str:
        if not row:
            return "—"
        v = row.get(key)
        return "未計算" if v is None else f"{v:+,.1f}%"

    rows = [
        ("売上高 前年同四半期比", "revenue_yoy_pct"),
        ("経常利益 前年同四半期比", "ordinary_income_yoy_pct"),
        ("1Q進捗率", "q1_progress_pct"),
    ]
    out = [f"| 指標 | 直近開示 {cur.get('date')} | 前回開示 "
           f"{prev.get('date') if prev else '—'} |", "|---|---|---|"]
    out += [f"| {label} | {cell(cur, key)} | {cell(prev, key)} |"
            for label, key in rows]
    return "\n".join(out)


def relative_perf(code: str, as_of: str | None, repo: S.Repo) -> str:
    if not as_of:
        return "—（基準日が定まらない）"
    out = []
    for metric, label in (("relative_perf_4w", "4週"), ("relative_perf_12w", "12週")):
        mv = S.resolve_metric(code, metric, as_of, repo)
        out.append(f"{label} {mv.display}" if mv.value is not None
                   else f"{label} 未計算（{mv.detail}）")
    return "対TOPIX  " + " ／ ".join(out)


def metric_table(verdict: J.Verdict | None) -> str:
    """判定に使った指標の実値（F6-2 の「判定に使った指標の実値」）。"""
    if verdict is None:
        return "判定の再計算に失敗したため実値を出せない"
    out = ["| 指標 | 実値 |", "|---|---|"]
    for key, label in BODY_METRICS:
        v = verdict.metrics.get(key)
        if v is None:
            # 信用倍率は値が None でも判定として決着していることがある
            # （残高ゼロ／買い一辺倒／制度信用が買建のみ）。「未計算」と書かない。
            if key == "margin_ratio" and verdict.metrics.get("margin_state") != J.UNKNOWN:
                out.append(f"| {label} | 定義不能: "
                           f"{verdict.metrics.get('margin_detail')} |")
            else:
                out.append(f"| {label} | **未計算** |")
            continue
        text = CATEGORY_LABEL.get(v) if isinstance(v, str) else None
        out.append(f"| {label} | {text or S.format_value(key, v)} |")
    if verdict.unknowns:
        out.append("")
        out.append("未計算の指標: " + " / ".join(verdict.unknowns)
                   + "（未計算は「条件クリア」ではない）")
    if verdict.cautions:
        out.append("")
        out.append("注意（ゲートではない）:")
        out += [f"- {c}" for c in verdict.cautions]
    return "\n".join(out)


def screen_table(verdict: J.Verdict | None) -> str:
    if verdict is None:
        return ""
    out = ["| スクリーニング条件 | 判定 | 内容 |", "|---|---|---|"]
    out += [f"| {s.label} | {s.mark} | {s.detail} |" for s in verdict.screen]
    return "\n".join(out)


def holding_block(verdict: J.Verdict | None) -> str:
    if verdict is None or verdict.holding.status != "holding":
        return ""
    h = verdict.holding
    def f(v, spec=",.2f"):
        return "—" if v is None else format(v, spec)
    hit = ("判定不能" if h.stop_loss_hit is None
           else ("抵触（ザラ場のみ・終値は戻した）" if h.stop_loss_intraday_only
                 else "抵触" if h.stop_loss_hit else "未抵触"))
    return (
        "\n## 保有管理（毎週フラットに再評価する・F13-5）\n\n"
        f"- 買値 {f(h.buy_price)} ／ 買付 {h.buy_date} ／ 数量 {f(h.shares, ',.0f')}\n"
        f"- 逆指値ライン {f(h.stop_loss_price)}（安値で判定: {hit}）\n"
        f"- 経過 {h.elapsed_months}か月 ／ 基準 {f(h.target_pct, '+.0f')}% ／ "
        f"現在 {f(h.return_pct, '+.2f')}% ／ 到達率 {f(h.achievement_ratio, '.2f')}\n"
        f"- クロス {h.cross_kind} → **{h.action}**\n"
    )


def predictions_block(code: str) -> str:
    """この銘柄で走っている予測（未解決のみ）。"""
    rows: list[str] = []
    for path in sorted((ROOT / "predictions").glob("*.yaml")):
        doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        for p in doc.get("predictions") or []:
            if str(p.get("code")) != code or p.get("status") != "open":
                continue
            rows.append(f"| {p.get('id')} | {p.get('metric')} "
                        f"{p.get('operator')} {p.get('reference')} | "
                        f"{p.get('resolve_by')} | {p.get('confidence')} |")
    if not rows:
        return "未解決の予測なし"
    return "\n".join(["| id | 条件 | 期限 | 確信度 |", "|---|---|---|---|"] + rows)


def ledger_urls() -> tuple[str, str]:
    """台帳の URL。CI では GITHUB_REPOSITORY から組み立てる。"""
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    if "/" in repo:
        owner, name = repo.split("/", 1)
        base = f"https://{owner}.github.io/{name}/"
        return base, base + "stock/"
    return "docs/index.html", "docs/stock/"


# =============================================================================
# 本文
# =============================================================================

def build_body(code: str, name: str, before: str, after: str,
               verdict: J.Verdict | None, repo: S.Repo) -> str:
    base, stock_base = ledger_urls()
    as_of = verdict.as_of if verdict else None
    reason = verdict.reason if verdict else "（判定の再計算に失敗）"
    stage = verdict.stage_label if verdict else "—"
    resolution = (RESOLUTION_LABEL.get(verdict.resolution, verdict.resolution)
                  if verdict else "—")

    fals = falsifications(code)
    fals_block = ("\n".join(f"- {x}" for x in fals) if fals else
                  f"`theses/{code}.md` に反証条件が書かれていない"
                  "（テーゼが無い銘柄に買を出さない）")

    parts = [
        f"# {code} {name}  判定 {before} → **{after}**",
        "",
        f"- 判定基準日: {as_of or '—'}（日足の最終営業日）",
        f"- 確定段階: {stage}（{resolution}）",
        f"- 根拠: {reason}",
        "",
        "## 判定に使った指標の実値",
        "",
        metric_table(verdict),
        "",
        "## スクリーニング5条件",
        "",
        screen_table(verdict),
        holding_block(verdict),
        "",
        "## 反証条件（成立したら見送・売り）",
        "",
        fals_block,
        "",
        "## 相対パフォーマンス",
        "",
        relative_perf(code, as_of, repo),
        "",
        "## KPI差分",
        "",
        kpi_diff(code),
        "",
        "## ベアケース（保有理由を与えない隔離ジョブの出力）",
        "",
        bear_case(code),
        "",
        "## データ品質",
        "",
        data_quality(code),
        "",
        "## 走っている予測",
        "",
        predictions_block(code),
        "",
        "---",
        "",
        f"台帳: {base} ／ 銘柄ページ: {stock_base}{code}.html",
        "",
        "このIssueに `買` / `買い増し` / `売り` / `見送り` のラベルを付けて閉じてください。",
        "実際に売買した場合は `data/master.yaml` の `holding`"
        "（status / buy_price / buy_date / shares）を更新してください（D18）。",
        "逆指値ラインと6か月2倍ラインはそこを基準に算出されます。",
        "",
        "売買の実行判断は人間が行います。判定スタンプは候補提示であって推奨ではありません。",
    ]
    return "\n".join(parts).rstrip() + "\n"


# =============================================================================
# 起票
# =============================================================================

def gh_issue(title: str, body: str) -> None:
    subprocess.run(
        ["gh", "issue", "create", "--title", title, "--body", body],
        cwd=ROOT, check=True, env={**os.environ},
    )


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="判定が変化した銘柄だけ Issue を起票する")
    ap.add_argument("--dry-run", action="store_true",
                    help="gh を呼ばず、起票する本文を表示するだけ（状態も書かない）")
    ap.add_argument("--seed-issues", action="store_true",
                    help="初回（last_stamps.json が無い）でも起票する")
    ap.add_argument("--code", default=None,
                    help="この銘柄の本文だけを組み立てて表示する（確認用）")
    args = ap.parse_args(argv)

    master = J.load_master()
    names = {str(s["code"]): str(s.get("name", "")) for s in master.get("stocks", [])}
    repo = S.Repo()

    try:
        verdicts = {v.code: v for v in J.judge_all(master)}
    except Exception as e:  # noqa: BLE001
        print(f"[WARN] 判定の再計算に失敗した（本文の実値は省略される）: "
              f"{type(e).__name__}: {e}")
        verdicts = {}

    if args.code:
        v = verdicts.get(args.code)
        print(build_body(args.code, names.get(args.code, ""),
                         "—", v.stamp if v else "—", v, repo))
        return 0

    current = read_json(STAMPS)
    if not current:
        # ★review-findings F-04 の回帰点。**状態を書き換えずに終了する**。
        # ここで last_stamps.json を {} で上書きすると、次に stamps.json が
        # 出た瞬間に全銘柄が一斉起票される。
        # `is None`（ファイル不在）だけでなく **空 dict も弾く**。read_json は
        # `{}` を「dict なので正常」として返すため、build.write_stamps([]) や
        # 手編集で空になった場合に同じ事故が中身の空で再現していた。
        why = "が無い" if current is None else "が空（判定スタンプが0件）"
        print(f"[WARN] {rel(STAMPS)} {why}。"
              "build.py が判定スタンプを出力しているか確認すること。"
              f"{rel(STATE)} は更新しない（状態を壊さない）。")
        return 0

    first_run = not STATE.exists()
    previous = read_json(STATE) or {}

    # 前回いたのに今回いない銘柄。master から外したのか取得が壊れたのか、
    # ここで黙って落とすと分からなくなる（次に戻ってきたとき一斉起票になる）。
    pruned = sorted(c for c in previous if c not in current)
    if pruned:
        print(f"[WARN] 前回のスタンプにあって今回は無い銘柄: {', '.join(pruned)}。"
              "master.yaml から外したのでなければ判定が落ちている")

    changed = {c: s for c, s in sorted(current.items()) if previous.get(c) != s}

    if first_run and not args.seed_issues:
        # 前回が無い＝「変化」ではない。初期化として記録だけする（F6-1）。
        if not args.dry_run:
            STATE.write_text(json.dumps(current, ensure_ascii=False, indent=2,
                                        sort_keys=True) + "\n", encoding="utf-8",
                             newline="\n")
        print(f"初期化: {rel(STATE)} に現在のスタンプ "
              f"{len(current)}件を記録した。起票 0 件"
              "（初回から起票するには --seed-issues）")
        return 0

    # 起票に成功したぶんだけ状態を進める。current から消えた銘柄は落とす。
    new_state = {c: s for c, s in previous.items() if c in current}
    failed: list[str] = []
    issued = 0

    for code, after in changed.items():
        before = previous.get(code, "—")
        title = f"[{code}] {names.get(code, '')} 判定 {before} → {after}"
        body = build_body(code, names.get(code, ""), before, after,
                          verdicts.get(code), repo)
        if args.dry_run:
            print(f"\n{'=' * 78}\n# title: {title}\n{'=' * 78}\n{body}")
            issued += 1
            continue
        try:
            gh_issue(title, body)
        except (subprocess.CalledProcessError, FileNotFoundError) as e:
            print(f"[ERROR] {code} の起票に失敗（次回再試行する）: "
                  f"{type(e).__name__}: {e}")
            failed.append(code)
            continue
        new_state[code] = after
        issued += 1

    if not args.dry_run:
        STATE.write_text(json.dumps(new_state, ensure_ascii=False, indent=2,
                                    sort_keys=True) + "\n", encoding="utf-8",
                         newline="\n")

    v_issued, v_failed = notify_verification(names, repo, args.dry_run)
    issued += v_issued
    failed += v_failed

    print(f"起票 {issued} 件 / 変化 {len(changed)} 件 / 失敗 {len(failed)} 件"
          + ("（dry-run のため状態は更新していない）" if args.dry_run else ""))
    return 1 if failed else 0


def unresolved_contradictions(code: str) -> list:
    """出典と食い違うと判定され、まだ始末が記録されていない記述。

    `contradicted` は設計上「翌週の data ジョブを止める」いちばん強い状態なのに、
    **人に届く経路がどこにも無かった**。台帳を開くか CI ログを読むまで
    誰も気づかず、翌週ジョブが止まって初めて分かる（気づくのが1週間遅い）。
    """
    rec = VF.load(code)
    if rec is None or rec.latest is None:
        return []
    resolved = rec.resolved_ids
    return [c for c, _owner in rec.folded()
            if c.verdict in VF.FATAL_IF_KEPT and c.id not in resolved]


def verification_body(code: str, name: str, claims: list, repo: str) -> str:
    lines = [
        f"# {code} {name}: 出典と食い違う記述が残っている",
        "",
        "別の文脈が出典を再取得して検証した結果、**出典が別のことを言っている**"
        "と判定された記述。直すまで翌週の取得・公開が止まる"
        "（`checks.py` が FAIL にする）。",
        "",
    ]
    for c in claims:
        lines.append(f"## {c.id}")
        lines.append(f"- 本文: 「{c.quote}」")
        lines.append(f"- 分かったこと: {c.evidence}")
        if c.action:
            lines.append(f"- どうするか: {c.action}")
        if c.sources:
            lines.append("- 出典: " + " / ".join(c.sources))
        lines.append("")
    lines += [
        "## 直したあと",
        "",
        f"`data/verification/{code}.yaml` の `resolutions:` に",
        "`{id, resolved_at, how: removed|rewritten, note}` を追記する。",
        "`how: rewritten` なら書き直した本文を `quote:` に書く"
        "（checks.py がそれが本文に実在することを機械で確かめる）。",
        "**言い回しを変えるだけでは解除されない。**",
        "",
        f"レポート: `reports/{code}.md`",
    ]
    if repo:
        lines.append(f"リポジトリ: {repo}")
    return "\n".join(lines)


def notify_verification(names: dict, repo: str, dry_run: bool) -> tuple:
    """未解決の `contradicted` を Issue にする。戻り値 (起票数, 失敗した銘柄)。"""
    issued = 0
    failed: list[str] = []
    vdir = ROOT / "data" / "verification"
    if not vdir.exists():
        return issued, failed
    for path in sorted(vdir.glob("*.yaml")):
        code = path.stem
        claims = unresolved_contradictions(code)
        if not claims:
            continue
        title = (f"[{code}] {names.get(code, '')} "
                 f"出典と食い違う記述 {len(claims)}件（直すまで翌週が止まる）")
        body = verification_body(code, names.get(code, ""), claims, repo)
        if dry_run:
            print(f"\n{'=' * 78}\n# title: {title}\n{'=' * 78}\n{body}")
            issued += 1
            continue
        try:
            gh_issue(title, body)
        except (subprocess.CalledProcessError, FileNotFoundError) as e:
            print(f"[ERROR] {code} の裏取り Issue 起票に失敗: "
                  f"{type(e).__name__}: {e}")
            failed.append(code)
            continue
        issued += 1
    return issued, failed


if __name__ == "__main__":
    raise SystemExit(main())
