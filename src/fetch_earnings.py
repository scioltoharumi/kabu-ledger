"""次回決算の発表予定日を取り、data/master.yaml の next_earnings に書く。

一覧の「14日以内の決算」タイルと銘柄ページの「次回決算」タイルの入力。
2026-09-23 時点で監視中16銘柄のどれにも予定日が無く、表示の仕組みはあるのに
一度も出ていなかった（予定日は本文の散文に埋もれていた）。

原則:
  - 取得元は sources.yaml の `earnings_date` のみ。取るのは**日付だけ**
  - パースはコード（正規表現）。見つからない銘柄は何も書かない
    （既存の値を消さない。過去日付は build.py が「予定」として出さない）
  - master.yaml は行単位で書き換える（YAML を dump し直すとコメントと並びが壊れる）。
    書くのは `next_earnings` と `next_earnings_source` の2行だけ

使い方:
  python src/fetch_earnings.py            # 監視中の全銘柄
  python src/fetch_earnings.py --dry-run  # 書かずに結果だけ出す
"""
from __future__ import annotations

import argparse
import re
import sys
import time
from pathlib import Path

try:  # ローカル(Windows)は SSL 検査プロキシ配下。CI(Linux) では不要
    import truststore

    truststore.inject_into_ssl()
except ImportError:
    pass

import requests
import yaml
from bs4 import BeautifulSoup

sys.path.insert(0, str(Path(__file__).resolve().parent))

import yamlio as Y  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
MASTER = ROOT / "data" / "master.yaml"

_CODE_LINE = re.compile(r'^  - code:\s*"?([0-9A-Z]{4})"?\s*$')
_KEY_LINE = re.compile(r"^    ([a-z_]+):")


def parse_date(html_text: str, pattern: str) -> str | None:
    """ページ本文から予定日を1つ取り出す（YYYY-MM-DD）。無ければ None。"""
    text = BeautifulSoup(html_text, "html.parser").get_text(" ", strip=True)
    m = re.search(pattern, text)
    if not m:
        return None
    return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"


def set_earnings(lines: list[str], code: str, day: str, url: str) -> bool:
    """master.yaml の行リストで、code の銘柄ブロックに2行を書く。変えたら True。

    既に行があれば値だけ置き換え、無ければ `fiscal_year_end`（無ければ `name`）の
    直後に足す。ブロックの外・他のキーには触らない。
    """
    start = end = None
    for i, ln in enumerate(lines):
        m = _CODE_LINE.match(ln.rstrip("\n"))
        if m:
            if start is not None:
                end = i
                break
            if m.group(1) == code:
                start = i
    if start is None:
        return False
    end = end if end is not None else len(lines)
    want = {"next_earnings": f'    next_earnings: "{day}"\n',
            "next_earnings_source": f'    next_earnings_source: "{url}"\n'}
    changed = False
    anchor = None
    for i in range(start, end):
        m = _KEY_LINE.match(lines[i])
        if not m:
            continue
        key = m.group(1)
        if key in want:
            if lines[i] != want[key]:
                lines[i] = want[key]
                changed = True
            want.pop(key)
        elif key == "fiscal_year_end" or (key == "name" and anchor is None):
            anchor = i
    if want:
        at = (anchor if anchor is not None else start) + 1
        for key in ("next_earnings", "next_earnings_source"):
            if key in want:
                lines.insert(at, want[key])
                at += 1
        changed = True
    return changed


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="master.yaml に書かない")
    args = ap.parse_args()

    cfg = yaml.safe_load((ROOT / "data" / "sources.yaml").read_text(encoding="utf-8"))
    src = cfg["earnings_date"][0]
    pol = cfg["fetch_policy"]
    master = yaml.safe_load(MASTER.read_text(encoding="utf-8"))

    with MASTER.open(encoding="utf-8", newline="") as f:
        lines = f.read().splitlines(keepends=True)
    changed = []
    for s in Y.watched_stocks(master):
        code = str(s["code"])
        url = src["url"].format(code=code)
        day = None
        for attempt in range(pol["retries"] + 1):
            try:
                r = requests.get(url, headers={"User-Agent": pol["user_agent"]},
                                 timeout=pol["timeout_sec"])
                r.raise_for_status()
                day = parse_date(r.text, src["pattern"])
                break
            except Exception as e:  # noqa: BLE001
                if attempt == pol["retries"]:
                    print(f"  [{code}] 取得失敗: {e}", file=sys.stderr)
        time.sleep(pol["interval_sec"])
        print(f"{code} {s.get('name', '')}: {day or '予定日の掲載なし'}")
        if day and set_earnings(lines, code, day, url):
            changed.append(f"{code}={day}")

    if changed and not args.dry_run:
        with MASTER.open("w", encoding="utf-8", newline="") as f:
            f.write("".join(lines))
    print(f"\n更新 {len(changed)}件" + (f": {', '.join(changed)}" if changed else "")
          + ("（--dry-run のため書いていない）" if args.dry_run and changed else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
