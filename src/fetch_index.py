"""指数の日足取得（TOPIX / 東証グロース市場250指数）。

用途は相対パフォーマンス（I-17: 対TOPIX 4週/12週）。
市場要因と個社要因を分離しないと仮説検証にならない。

原則（fetch.py と同じ）:
  - 取得元は sources.yaml の index.targets[].chain のみ。探索しない。
  - 2ソースの生終値一致で採用。不一致は MISMATCH、1件のみは SINGLE_SOURCE。
  - CSVは append-only。既存 (code, date) は上書きしない。
  - セレクタが外れたら例外にせず欠測として扱う。

時系列テーブルの構造が個別銘柄ページと同一のため、パース・照合・追記は
fetch.py の関数をそのまま使う。`code` 列には指数ID（topix / growth250）が入る。

使い方:
  python src/fetch_index.py               # 直近1ページ（約30営業日）
  python src/fetch_index.py --historical  # 初回の遡り（約1年 / D16）
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

try:  # ローカル(Windows)は SSL 検査プロキシ配下。CI(Linux) では不要
    import truststore

    truststore.inject_into_ssl()
except ImportError:
    pass

import yaml

from fetch import Bar, append_only, fetch_source, pages_for, reconcile

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--historical", action="store_true",
                    help="初回の遡り取得（sources.yaml の historical_days 分）")
    ap.add_argument("--target", help="1指数だけ取得する（動作確認用）")
    args = ap.parse_args()

    cfg = yaml.safe_load((ROOT / "data" / "sources.yaml").read_text(encoding="utf-8"))
    pol = cfg["fetch_policy"]
    icfg = cfg["index"]
    required = icfg["required_agreements"]
    days = pol["historical_days"] if args.historical else 0

    failed: list[str] = []
    for target in icfg["targets"]:
        tid = target["id"]
        if args.target and tid != args.target:
            continue

        label = f"（遡り {days}営業日）" if days else "（直近）"
        print(f"取得中: {tid} {target['name']}{label}")

        by_source: list[list[Bar]] = []
        for entry in target["chain"]:
            pages = pages_for(entry, days)
            bars = fetch_source(tid, entry, pol, pages)
            if bars:
                by_source.append(bars)
            if len(by_source) >= required:
                break   # 照合に必要な数が揃えば以降の取得元は試さない

        if not by_source:
            failed.append(tid)
            continue

        rows = reconcile(tid, by_source, required)
        added = append_only(ROOT / "data" / "indices" / f"{tid}.csv", rows)
        ok = sum(1 for r in rows if r["status"] == "OK")
        single = sum(1 for r in rows if r["status"] == "SINGLE_SOURCE")
        mismatch = sum(1 for r in rows if r["status"] == "MISMATCH")
        print(f"  {len(rows)}営業日分（OK {ok} / 単独 {single} / 不一致 {mismatch}）"
              f" → {added}件を追記")
        if mismatch:
            for r in rows:
                if r["status"] == "MISMATCH":
                    print(f"    {r['date']} {r['source_primary']}={r['value_primary']}"
                          f" {r['source_secondary']}={r['value_secondary']}", file=sys.stderr)

    if failed:
        print(f"要確認: 全ソース取得失敗 {', '.join(failed)}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
