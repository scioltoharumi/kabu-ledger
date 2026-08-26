"""週次追記の機械化（週次v2「計測は機械・言葉は一筆」）。

役割は2つ:
  --collect  data/ の検証済み CSV から銘柄ごとの「週次事実」を JSON で出す。
             ここまでが機械の仕事（計測）。
  --write    人（または上位工程）が書いた一筆 notes.json と機械の計測を合成し、
             reports/{code}.md の週次アップデート節の**先頭**に固定テンプレートの
             エントリを挿入する。

不変条件（CLAUDE.md「生成」「データ層」に従う）:
  - 既存行は1行も変更しない。**挿入のみ**。唯一の例外は front matter の
    `updated`（checks.check_report_updates_append_only は節内の週エントリだけを
    突き合わせるので対象外。確認済み）
  - 採用終値は「status に OK があるか」で数える（D53）。close の有無で判定しない。
    判定は chartdata.adopted_close を共有する（独自実装で食い違いを作らない）
  - 出来高・信用残は照合を通っていないので、必ず「※」記号を付ける
    （レポート表記規約2026-08-23改訂。凡例は各レポート「## 出典」節の先頭行が持つ）
  - 出力は決定論的。生成時刻を埋め込まない（D8）。銘柄は辞書順。
    「取得日」はニュース素材の属性なので notes.json 由来の値のみ書く
  - 書き込みは newline を固定する（tests/test_eol.py が AST で検査）

設計メモ:
  - close_start（週間騰落の起点）は「対象週の月曜より前の最後の採用終値」。
    前週に採用終値が1日も無い場合も、直近の採用終値まで遡る（close_start_date で
    どの日の値かを JSON に明示する）。起点が無ければ pct は null
  - 同じ週に2回書くときは `### {week}（続報）` `（続報2）`… を自動採番して
    **別エントリ**として挿入する（append-only 検査が本文書き換えと誤認しないため）
  - 週次アップデート節が見つからないレポートは exit 2 で銘柄名を出して失敗させる。
    1銘柄でも欠けていれば**どのレポートにも書かない**（部分適用を作らない）
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from datetime import date, timedelta
from pathlib import Path

import chartdata as CD   # adopted_close（D53「status に OK」判定の共有）
import report as R       # _normalize_title（丸数字つき節見出しの正規化を共有）
import yamlio as Y

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
REPORTS = ROOT / "reports"
SCORING = ROOT / "scoring"

WEEK_ID_RE = re.compile(r"^(\d{4})-W(\d{2})$")
WEEK_HEAD_RE = re.compile(r"^###\s+(\d{4}-W\d{2})")


# =============================================================================
# 週の扱い（ISO 週。月曜はじまり）
# =============================================================================

def parse_week(week: str) -> tuple[int, int]:
    m = WEEK_ID_RE.match(str(week).strip())
    if not m:
        raise ValueError(f"週の形式が不正: {week!r}（例 2026-W34）")
    y, w = int(m.group(1)), int(m.group(2))
    date.fromisocalendar(y, w, 1)   # 存在しない週番号なら ValueError
    return y, w


def week_monday(week: str) -> date:
    y, w = parse_week(week)
    return date.fromisocalendar(y, w, 1)


def current_week(today: date | None = None) -> str:
    d = today or date.today()
    iso = d.isocalendar()
    return f"{iso[0]}-W{iso[1]:02d}"


# =============================================================================
# 入力の読み込み
# =============================================================================

def _read_csv(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def load_master() -> list[tuple[str, str]]:
    """master.yaml の (code, name) を辞書順で返す。"""
    path = DATA / "master.yaml"
    with path.open(encoding="utf-8") as f:
        master = Y.safe_load(f) or {}
    out: list[tuple[str, str]] = []
    for s in master.get("stocks") or []:
        code = str(s.get("code", "") or "").strip()
        if code:
            out.append((code, str(s.get("name") or code)))
    return sorted(out)


def _num(v) -> float | None:
    s = str(v if v is not None else "").strip().replace(",", "")
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _jnum(v):
    """JSON 用。整数なら int にする（535.0 と書かない）。"""
    if v is None:
        return None
    f = float(v)
    return int(f) if f.is_integer() else f


def _fmt_num(v) -> str:
    if v is None:
        return "—"
    f = float(v)
    return str(int(f)) if f.is_integer() else f"{f:g}"


def _mmdd(s) -> str:
    try:
        d = date.fromisoformat(str(s)[:10])
    except ValueError:
        return str(s)
    return f"{d.month:02d}/{d.day:02d}"


# =============================================================================
# --collect: 週次事実の収集
# =============================================================================

def collect_one(code: str, name: str, week: str) -> dict:
    """1銘柄の週次事実。キーが `_` で始まるものは内部用（JSON には出さない）。"""
    mon = week_monday(week)
    sun = mon + timedelta(days=6)
    prev_mon, prev_sun = mon - timedelta(days=7), mon - timedelta(days=1)

    adopted: list[tuple[date, float]] = []      # (日付, 採用終値) 昇順
    vol_this: int | None = None
    vol_prev: int | None = None
    rows = [r for r in _read_csv(DATA / "prices" / "daily.csv")
            if str(r.get("code", "") or "").strip() == code]
    rows.sort(key=lambda r: str(r.get("date", "")))
    for r in rows:
        try:
            d = date.fromisoformat(str(r.get("date", ""))[:10])
        except ValueError:
            continue
        v = CD.adopted_close(r)                  # status に OK が無い行は None（D53）
        if v is not None:
            adopted.append((d, v))
        vol = _num(r.get("volume"))              # 出来高は1ソースのみ＝参考値
        if vol is not None:
            if mon <= d <= sun:
                vol_this = (vol_this or 0) + int(vol)
            elif prev_mon <= d <= prev_sun:
                vol_prev = (vol_prev or 0) + int(vol)

    this_week = [(d, v) for d, v in adopted if mon <= d <= sun]
    before = [(d, v) for d, v in adopted if d < mon]
    ok_days = len(this_week)
    close_end = this_week[-1][1] if this_week else None
    close_end_date = this_week[-1][0].isoformat() if this_week else None
    close_start = before[-1][1] if before else None
    close_start_date = before[-1][0].isoformat() if before else None
    pct = None
    if close_start is not None and close_start != 0 and close_end is not None:
        pct = round((close_end / close_start - 1.0) * 100.0, 2)
    week_high = max(v for _, v in this_week) if this_week else None
    week_low = min(v for _, v in this_week) if this_week else None

    if vol_this is None:
        vol_core = "今週の出来高データなし"
    elif not vol_prev:
        vol_core = f"今週 {vol_this:,}株（前週比は算出不可）"
    else:
        vp = (vol_this / vol_prev - 1.0) * 100.0
        vol_core = f"前週比 {vp:+.1f}%（今週 {vol_this:,}株／前週 {vol_prev:,}株）"

    margin = None
    margin_core = "データなし"
    mrows = _read_csv(DATA / "margin" / f"{code}.csv")
    if mrows:
        m = max(mrows, key=lambda r: str(r.get("date", "")))
        ratio = _num(m.get("ratio"))
        unit = str(m.get("unit", "") or "")
        long_b = _num(m.get("long_balance"))
        short_b = _num(m.get("short_balance"))
        when = _mmdd(m.get("date", ""))
        if ratio is not None:
            margin_core = (f"{_fmt_num(ratio)}倍（買残 {_fmt_num(long_b)}{unit}・"
                           f"売残 {_fmt_num(short_b)}{unit}、{when}時点）")
        else:
            margin_core = (f"算出不可（{str(m.get('status') or 'RATIO_NA')}）・"
                           f"買残 {_fmt_num(long_b)}{unit}（{when}時点）")
        margin = {"date": str(m.get("date", "") or ""), "ratio": _jnum(ratio),
                  "long_balance": _jnum(long_b), "short_balance": _jnum(short_b),
                  "unit": unit, "status": str(m.get("status", "") or ""),
                  "note": margin_core + "※"}

    stamp = None
    stamps_path = SCORING / "stamps.json"
    if stamps_path.exists():
        stamp = (json.loads(stamps_path.read_text(encoding="utf-8")) or {}).get(code)

    disclosures = []
    for r in _read_csv(DATA / "tanshin" / "fetch_log.csv"):
        if str(r.get("code", "") or "").strip() != code:
            continue
        try:
            d = date.fromisoformat(str(r.get("disclosed_on", ""))[:10])
        except ValueError:
            continue
        if mon <= d <= sun:
            disclosures.append({"date": d.isoformat(), "label": "決算短信"})
    disclosures.sort(key=lambda x: x["date"])

    last_entry_week = None
    this_week_entry_exists = False
    path = REPORTS / f"{code}.md"
    if path.exists():
        lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
        span = _find_updates_section(lines)
        if span is not None:
            weeks = []
            for head in _section_week_heads(lines, span):
                m = WEEK_HEAD_RE.match(head)
                if m:
                    weeks.append(m.group(1))
            if weeks:
                last_entry_week = max(weeks)
                this_week_entry_exists = week in weeks

    return {
        "name": name,
        "week": week,
        "close_start": _jnum(close_start),
        "close_start_date": close_start_date,
        "close_end": _jnum(close_end),
        "close_end_date": close_end_date,
        "pct": pct,
        "ok_days": ok_days,
        "week_high": _jnum(week_high),
        "week_low": _jnum(week_low),
        "volume_note": vol_core + "※",
        "margin": margin,
        "stamp": stamp,
        "disclosures": disclosures,
        "last_entry_week": last_entry_week,
        "this_week_entry_exists": this_week_entry_exists,
        "flags": ["no_adopted_close"] if ok_days == 0 else [],
        "_volume_core": vol_core,
        "_margin_core": margin_core,
    }


# =============================================================================
# --write: 週次アップデート節の先頭への挿入
# =============================================================================

def _find_updates_section(lines: list[str]) -> tuple[int, int] | None:
    """週次アップデート節の (見出し行 index, 節末尾 index（排他）)。無ければ None。

    見出しの照合は report._normalize_title を共有し、丸数字（「## ④ 週次アップデート」）
    が残っていても拾う。
    """
    start = None
    for i, line in enumerate(lines):
        m = re.match(r"^##\s+(.+?)\s*$", line.rstrip("\r\n"))
        if not m or line.startswith("###"):
            continue
        if start is not None:
            return start, i
        if R._normalize_title(m.group(1)) == "週次アップデート":
            start = i
    return (start, len(lines)) if start is not None else None


def _section_week_heads(lines: list[str], span: tuple[int, int]) -> list[str]:
    """節内の `### ` 見出し行（末尾の改行・空白を落とした全文）。"""
    h, e = span
    return [line.rstrip() for line in lines[h + 1:e] if line.startswith("### ")]


def _choose_heading(heads: list[str], week: str, monday: date) -> str:
    """同じ週の見出しが既にあれば（続報）（続報2）…を自動採番する。"""
    same = []
    for head in heads:
        m = WEEK_HEAD_RE.match(head)
        if m and m.group(1) == week:
            same.append(head)
    if not same:
        return f"### {week}（{monday.isoformat()} 週）"
    n = 1
    while True:
        label = "（続報）" if n == 1 else f"（続報{n}）"
        cand = f"### {week}{label}"
        if cand not in same:
            return cand
        n += 1


def _insertion_index(lines: list[str], span: tuple[int, int]) -> int:
    """節見出しと直後の引用行（> 週ごとに追記していく…）の後 = 挿入位置。"""
    h, e = span
    i = h + 1
    while i < e and lines[i].strip() == "":
        i += 1
    while i < e and lines[i].lstrip().startswith(">"):
        i += 1
    while i < e and lines[i].strip() == "":
        i += 1
    return i


def _circled(n: int) -> str:
    return chr(0x245F + n) if 1 <= n <= 20 else f"({n})"


def build_entry(heading: str, facts: dict, note: dict) -> str:
    """固定テンプレートのエントリ本文（LF・末尾に区切りの空行1つ）。"""
    lines = [heading, ""]
    summary = str(note.get("summary", "") or "").strip()
    if summary:
        lines += [f"**{summary}**", ""]

    if facts["ok_days"] == 0:
        lines.append("- 株価: 今週の採用終値なし（照合成立 0日）")
    elif facts["pct"] is None:
        lines.append(
            f"- 株価: 週間騰落は算出不可（起点になる過去の採用終値なし）。"
            f"今週終値 {_fmt_num(facts['close_end'])}円・採用終値ベース、"
            f"照合成立 {facts['ok_days']}日、"
            f"週内 {_fmt_num(facts['week_low'])}〜{_fmt_num(facts['week_high'])}円")
    else:
        lines.append(
            f"- 株価: 週間 {facts['pct']:+.1f}%"
            f"（{_fmt_num(facts['close_start'])}→{_fmt_num(facts['close_end'])}円・"
            f"採用終値ベース、照合成立 {facts['ok_days']}日、"
            f"週内 {_fmt_num(facts['week_low'])}〜{_fmt_num(facts['week_high'])}円）")
    lines.append(f"- 出来高: {facts['_volume_core']}※"
                 f"／信用倍率: {facts['_margin_core']}※")
    disc = "、".join(f"{_mmdd(d['date'])} {d['label']}"
                     for d in facts["disclosures"]) or "なし"
    lines.append(f"- 判定: 「{facts['stamp'] or '—'}」／開示: {disc}")
    for n in note.get("news") or []:
        ln = (f"- ニュース: {_mmdd(n.get('date', ''))} "
              f"{str(n.get('title', '') or '').strip()} "
              f"<{str(n.get('url', '') or '').strip()}>")
        fetched = str(n.get("fetched", "") or n.get("fetched_on", "") or "").strip()
        if fetched:
            ln += f"（取得日 {fetched}）"
        lines.append(ln)

    interp = [str(x).strip() for x in (note.get("interpretation") or [])
              if str(x).strip()]
    if interp:
        lines.append("")
        lines.append("（解釈）" + interp[0])
        lines += interp[1:]

    nxt = [str(x).strip() for x in (note.get("next_week") or []) if str(x).strip()]
    if nxt:
        lines.append("")
        lines.append("**次週に見ること**: " +
                     " ".join(f"{_circled(i + 1)} {t}" for i, t in enumerate(nxt)))
    return "\n".join(lines) + "\n\n"


def _bump_updated(lines: list[str], today: date) -> bool:
    """front matter の updated だけを今日にする（唯一許された既存行の書き換え）。"""
    if not lines or lines[0].strip() != "---":
        return False
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            return False
        if re.match(r"^updated:\s*", lines[i]):
            lines[i] = f"updated: {today.isoformat()}\n"
            return True
    return False


def write_one(code: str, week: str, facts: dict, note: dict, today: date) -> str:
    """1銘柄のレポートにエントリを挿入する。呼び出し前に節の存在を検証しておくこと。"""
    path = REPORTS / f"{code}.md"
    with path.open(encoding="utf-8", newline="") as f:   # 改行を変換せず読む
        text = f.read()
    lines = text.splitlines(keepends=True)
    span = _find_updates_section(lines)
    if span is None:                                     # 二重の安全弁
        raise RuntimeError(f"reports/{code}.md に週次アップデート節が無い")
    heading = _choose_heading(_section_week_heads(lines, span), week,
                              week_monday(week))
    entry = build_entry(heading, facts, note)
    i = _insertion_index(lines, span)
    if i > 0 and not lines[i - 1].endswith("\n"):
        entry = "\n" + entry
    lines.insert(i, entry)
    _bump_updated(lines, today)
    with path.open("w", encoding="utf-8", newline="") as f:   # 挿入分は LF のまま
        f.write("".join(lines))
    return heading


# =============================================================================
# CLI
# =============================================================================

def _collect_all(week: str) -> dict:
    stocks = {}
    for code, name in load_master():
        facts = collect_one(code, name, week)
        stocks[code] = {k: v for k, v in facts.items() if not k.startswith("_")}
    return {"week": week, "monday": week_monday(week).isoformat(),
            "stocks": stocks}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="週次追記の機械化（--collect で計測、--write で一筆と合成して挿入）")
    ap.add_argument("--collect", action="store_true",
                    help="銘柄ごとの週次事実を JSON で出す")
    ap.add_argument("--write", metavar="NOTES_JSON",
                    help="notes.json と合成してレポートに挿入する")
    ap.add_argument("--week", help="対象の ISO 週（例 2026-W34）。省略時は今日の週")
    ap.add_argument("--out", help="--collect の出力先ファイル。省略時は stdout")
    args = ap.parse_args(argv)

    if bool(args.collect) == bool(args.write):
        print("--collect か --write のどちらか一方を指定する", file=sys.stderr)
        return 2

    if args.collect:
        try:
            week = args.week or current_week()
            parse_week(week)
        except ValueError as e:
            print(str(e), file=sys.stderr)
            return 2
        if not (DATA / "master.yaml").exists():
            print(f"master.yaml が見つからない: {DATA / 'master.yaml'}",
                  file=sys.stderr)
            return 2
        out = json.dumps(_collect_all(week), ensure_ascii=False, indent=2,
                         sort_keys=True) + "\n"
        if args.out:
            with open(args.out, "w", encoding="utf-8", newline="\n") as f:
                f.write(out)
        else:
            sys.stdout.write(out)
        return 0

    # --write
    notes_path = Path(args.write)
    if not notes_path.exists():
        print(f"notes が見つからない: {notes_path}", file=sys.stderr)
        return 2
    notes = json.loads(notes_path.read_text(encoding="utf-8"))
    try:
        week = args.week or str(notes.get("week") or "") or current_week()
        parse_week(week)
    except ValueError as e:
        print(str(e), file=sys.stderr)
        return 2
    names = dict(load_master()) if (DATA / "master.yaml").exists() else {}
    stock_notes = notes.get("stocks") or {}
    if not stock_notes:
        print("notes.json に stocks が無い（何も書かない）", file=sys.stderr)
        return 0

    # 第1パス: 全レポートを検証。1つでも欠けていれば何も書かずに失敗させる
    missing = []
    for code in sorted(stock_notes):
        path = REPORTS / f"{code}.md"
        name = names.get(code, code)
        if not path.exists():
            missing.append(f"{name}（{code}）: reports/{code}.md が無い")
            continue
        lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
        if _find_updates_section(lines) is None:
            missing.append(f"{name}（{code}）: 週次アップデート節が見つからない")
    if missing:
        for m in missing:
            print(m, file=sys.stderr)
        return 2

    today = date.today()
    for code in sorted(stock_notes):
        facts = collect_one(code, names.get(code, code), week)
        heading = write_one(code, week, facts, stock_notes[code] or {}, today)
        print(f"reports/{code}.md: {heading} を挿入")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
