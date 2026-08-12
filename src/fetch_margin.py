"""信用残高の取得（週次公表）。判定の過熱チェック（信用倍率5倍超）に使う。

原則（fetch.py と同じ）:
  - 取得元は sources.yaml の margin.chain のみ。探索しない。
  - CSVは append-only。既存 (code, date) は上書きしない。
  - セレクタが外れたら例外にせず欠測として扱う。推定値で埋めない。
  - 単位はページ側の見出しから読む。コード側で「千株だろう」と仮定しない。

対象テーブル（kabutan 株価ページ内）:
  th : ['日付', '売り残', '買い残', '倍率', '07/31', ...]
  row: ['07/31', '0.0', '111.3', '－']
  id も class も無いため、見出しテキスト（match_th）で特定する。

倍率が「－」になる銘柄がある（4073 は制度信用が買建のみで売り残が常に0）。
倍率は定義できないだけなので None として記録し、status に RATIO_NA を立てる。
**倍率が無いことを「過熱していない」と読み替えてはならない。**

使い方:
  python src/fetch_margin.py          # 全銘柄（直近5週分が毎回返る）
  python src/fetch_margin.py --code 4073
"""
from __future__ import annotations

import argparse
import csv
import re
import sys
import time
from datetime import date, datetime, timezone, timedelta
from pathlib import Path

try:  # ローカル(Windows)は SSL 検査プロキシ配下。CI(Linux) では不要
    import truststore

    truststore.inject_into_ssl()
except ImportError:
    pass

import requests
import yaml
from bs4 import BeautifulSoup

from fetch import _num, find_table

ROOT = Path(__file__).resolve().parents[1]
JST = timezone(timedelta(hours=9))
FIELDS = [
    "date", "code", "long_balance", "short_balance", "ratio", "unit",
    "status", "source_url", "fetched_at",
]

UNIT_RE = re.compile(r"単位\s*[:：]\s*([^)）\s]+)")

# 倍率の表示値と 買い残÷売り残 の乖離許容。列の取り違え（売り残と買い残の逆）を検出する。
# 残高は 0.1 単位に丸めて表示されるため、売り残が小さいと相対誤差が大きく出る。
RATIO_TOLERANCE = 0.15
RATIO_CHECK_MIN_SHORT = 1.0


def parse_unit(table, heading_keyword: str) -> str | None:
    """テーブル直前の見出しから単位を読む。読めなければ None（仮定しない）。"""
    node = table
    for _ in range(4):
        node = node.find_previous(["h2", "h3", "caption"])
        if node is None:
            return None
        text = node.get_text(" ", strip=True).replace("\xa0", " ")
        if heading_keyword in text:
            m = UNIT_RE.search(text)
            return m.group(1) if m else None
    return None


def norm_md(text: str, today: date) -> str | None:
    """'07/31' -> '2026-07-31'。信用残は必ず過去日なので、未来になるなら前年と解釈する。"""
    m = re.match(r"(\d{1,2})/(\d{1,2})$", (text or "").strip())
    if not m:
        return None
    mo, d = int(m.group(1)), int(m.group(2))
    for year in (today.year, today.year - 1):
        try:
            cand = date(year, mo, d)
        except ValueError:
            continue          # 2/29 など、その年に存在しない日付
        if cand <= today:
            return cand.isoformat()
    return None


def parse_margin(html: str, entry: dict, code: str, url: str, today: date,
                 na_marks: list[str]) -> list[dict]:
    """信用残テーブルを読む。セレクタが外れたら空リスト（欠測）。"""
    soup = BeautifulSoup(html, "html.parser")
    table = find_table(soup, entry)
    if table is None:
        return []

    unit = parse_unit(table, entry.get("unit_heading", "信用"))
    cols = entry["columns"]
    idx = {name: i for i, name in enumerate(cols)}
    now = datetime.now(JST).isoformat()
    na = set(na_marks or [])

    rows: list[dict] = []
    for tr in table.find_all("tr"):
        cells = [c.get_text(strip=True) for c in tr.find_all(["td", "th"])]
        if len(cells) < len(cols):
            continue
        d = norm_md(cells[idx["date"]], today)
        if not d:
            continue          # 見出し行など

        raw_ratio = cells[idx["ratio"]].strip()
        short_balance = _num(cells[idx["short_balance"]])
        long_balance = _num(cells[idx["long_balance"]])
        ratio = None if raw_ratio in na else _num(raw_ratio)

        flags: list[str] = []
        if short_balance is None or long_balance is None:
            flags.append("BALANCE_MISSING")
        if ratio is None:
            flags.append("RATIO_NA")       # 売り残0 等で定義不能。「過熱なし」ではない
        if unit is None:
            flags.append("UNIT_UNKNOWN")   # ページ構造が変わった可能性。値の解釈を保留する

        # 列の取り違え検出。倍率 = 買い残 ÷ 売り残 のはず。
        # ★真偽値ではなく `is not None` で判定する。買い残 0 は falsy なので、
        #   `if ratio and long_balance and ...` だと検査ごとスキップされる。
        if (ratio is not None and ratio != 0
                and short_balance is not None and long_balance is not None
                and short_balance >= RATIO_CHECK_MIN_SHORT):
            calc = long_balance / short_balance
            if abs(calc - ratio) / ratio > RATIO_TOLERANCE:
                flags.append("RATIO_INCONSISTENT")

        rows.append({
            "date": d,
            "code": code,
            "long_balance": long_balance,
            "short_balance": short_balance,
            "ratio": ratio,
            "unit": unit,
            "status": "OK" if not flags else "|".join(flags),
            "source_url": url,
            "fetched_at": now,
        })
    return rows


def fetch_code(code: str, entry: dict, pol: dict, today: date,
               na_marks: list[str]) -> list[dict]:
    url = entry["url"].format(code=code)
    rows: list[dict] = []
    for attempt in range(pol["retries"] + 1):
        try:
            r = requests.get(
                url,
                headers={"User-Agent": pol["user_agent"]},
                timeout=pol["timeout_sec"],
            )
            r.raise_for_status()
            rows = parse_margin(r.text, entry, code, url, today, na_marks)
            if not rows:
                print(f"  [{code}] {entry['id']} セレクタ不一致: {url}", file=sys.stderr)
            break
        except Exception as e:  # noqa: BLE001
            if attempt == pol["retries"]:
                print(f"  [{code}] {entry['id']} 取得失敗: {e}", file=sys.stderr)
    time.sleep(pol["interval_sec"])
    return rows


def append_only(path: Path, rows: list[dict]) -> int:
    """既存 (code, date) は書き換えない。冪等性の担保。"""
    existing: set[tuple[str, str]] = set()
    if path.exists():
        with path.open(encoding="utf-8") as f:
            for r in csv.DictReader(f):
                existing.add((r["code"], r["date"]))

    new = [r for r in rows if (r["code"], r["date"]) not in existing]
    if not new:
        return 0

    path.parent.mkdir(parents=True, exist_ok=True)
    write_header = not path.exists()
    new.sort(key=lambda r: (r["date"], r["code"]))
    with path.open("a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        if write_header:
            w.writeheader()
        w.writerows(new)
    return len(new)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--code", help="1銘柄だけ取得する（動作確認用）")
    args = ap.parse_args()

    cfg = yaml.safe_load((ROOT / "data" / "sources.yaml").read_text(encoding="utf-8"))
    master = yaml.safe_load((ROOT / "data" / "master.yaml").read_text(encoding="utf-8"))
    pol = cfg["fetch_policy"]
    mcfg = cfg["margin"]
    na_marks = mcfg.get("ratio_unavailable_marks", [])
    today = datetime.now(JST).date()

    stocks = [s for s in master["stocks"] if not args.code or s["code"] == args.code]
    failed: list[str] = []

    for s in stocks:
        code = s["code"]
        print(f"取得中: {code} {s['name']}")
        rows: list[dict] = []
        for entry in mcfg["chain"]:
            rows = fetch_code(code, entry, pol, today, na_marks)
            if rows:
                break          # チェーンは上から順。取れた時点で打ち切る
        if not rows:
            failed.append(code)
            continue

        added = append_only(ROOT / "data" / "margin" / f"{code}.csv", rows)
        ok = sum(1 for r in rows if r["status"] == "OK")
        unit = rows[0]["unit"]
        print(f"  {len(rows)}週分（OK {ok} / 要注意 {len(rows) - ok}）"
              f" 単位={unit or '不明'} → {added}件を追記")
        for r in rows:
            if r["status"] != "OK":
                print(f"    {r['date']} {r['status']}"
                      f" 売残={r['short_balance']} 買残={r['long_balance']} 倍率={r['ratio']}")

    if failed:
        print(f"要確認: 信用残の取得失敗 {', '.join(failed)}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
