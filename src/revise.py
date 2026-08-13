"""訂正台帳（data/revisions.csv）への追記と、追記専用 CSV の訂正書き込み。

なぜ要るのか:

    `data/` の CSV は append-only で、鍵は株価が (code, date)、財務が
    (code, period, metric)。**どちらも二度と新しくならない鍵**なので、
    ある週に取得が片肺だった行は「照合不成立」のまま永久に固定されていた。
    翌週に2ソースが揃っても採用値は空のまま、`MISMATCH` も空のまま。
    直す経路がどこにも無かった。

    実害は静かで大きい。株価では、途中の1日が採用終値を持たないと
    `indicators` の20日/25日窓が丸ごと None になり、流動性ゲート（判定の最上位）が
    unknown に落ちて **全銘柄が4〜5週間「調査」に固定される**。
    財務では、片方のサイトが1回落ちただけでその銘柄の採用値が全滅する。

原則（破らないこと）:

  1. **一方向だけ**。訂正は「採用値が空 ⇄ 非空」に限る。
     `repair`   照合不成立 → 成立（空 → 埋まる）
     `withdraw` 照合が無効と判明 → 取り下げ（埋まる → 空）
     **採用値を別の値に書き換えることは、記録があっても許さない。**
  2. **黙って直さない**。訂正した事実・理由・前後の値を
     `data/revisions.csv`（追記専用）に必ず残す。`checks.py` は
     この記録と突き合わせ、記録の無い書き換えを従来どおり FAIL にする。
  3. `withdraw` は自動化しない。取得の一時的な不調で採用値が消えると、
     まさに直したかった障害（指標が算出できない）を自分で起こす。
     取り下げは人間が根拠を書いて実行する（`--withdraw`）。
"""
from __future__ import annotations

import csv
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REVISIONS = ROOT / "data" / "revisions.csv"
FIELDS = ["revised_at", "file", "key", "column", "old_value", "new_value",
          "kind", "reason"]

REPAIR = "repair"
WITHDRAW = "withdraw"


def _blank(v) -> bool:
    return v is None or str(v).strip() == ""


def diff_columns(old: dict, new: dict, columns) -> list[tuple[str, str, str]]:
    """(列, 旧値, 新値) の並び。値が同じ列は返さない。"""
    out = []
    for c in columns:
        a, b = str(old.get(c) or ""), str(new.get(c) or "")
        if a != b:
            out.append((c, a, b))
    return out


def is_repair(old: dict, new: dict, adopted_column: str) -> bool:
    """採用値が空 → 非空 で、かつ照合が成立している変更か。"""
    if not _blank(new.get("status")) and "OK" not in str(new["status"]).split("|"):
        return False
    return _blank(old.get(adopted_column)) and not _blank(new.get(adopted_column))


def append_records(rel_file: str, key: str, diffs, kind: str, reason: str,
                   revised_at: str, path: Path | None = None) -> int:
    """訂正台帳に追記する。戻り値は追記した行数。"""
    target = path if path is not None else REVISIONS
    rows = [{"revised_at": revised_at, "file": rel_file, "key": key,
             "column": c, "old_value": a, "new_value": b,
             "kind": kind, "reason": reason} for c, a, b in diffs]
    if not rows:
        return 0
    target.parent.mkdir(parents=True, exist_ok=True)
    write_header = not target.exists()
    with target.open("a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        if write_header:
            w.writeheader()
        w.writerows(rows)
    return len(rows)
