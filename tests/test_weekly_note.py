"""weekly_note.py の回帰テスト（週次追記の機械化）。

**ネットワークを使わない。実データの日付・数値をべた書きしない。**
合成した data/・scoring/・reports/ を一時ディレクトリに組み立て、
weekly_note のパス系グローバル（DATA / REPORTS / SCORING）を差し替えて読ませる。

見るもの:
  1. --collect が採用終値を「status に OK がある行だけ」から数えること（D53）と週境界
  2. --write が挿入のみで、既存行を1バイトも変えないこと（updated だけ例外）
  3. 同じ週に2回書くと（続報）（続報2）と自動採番されること
  4. 週次アップデート節が無い md は exit 2 で銘柄名を出して失敗し、何も書かないこと
  5. 出力（facts.json・挿入後の md）が LF であること

実行:
  $env:PYTHONIOENCODING = "utf-8"; python tests/test_weekly_note.py
"""
from __future__ import annotations

import io
import json
import shutil
import sys
import tempfile
from contextlib import redirect_stderr, redirect_stdout
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import weekly_note as W  # noqa: E402


def eq(actual, expected, label=""):
    assert actual == expected, f"{label}: expected {expected!r}, got {actual!r}"


# =============================================================================
# 合成データの土台
# =============================================================================
# 対象週は 2024-W02（月曜 2024-01-08）。実データ（2026年〜）と重ならない合成の週。

WEEK = "2024-W02"
MONDAY = "2024-01-08"

PRICE_COLS = ["date", "code", "open", "high", "low", "close", "volume",
              "status", "source_primary", "value_primary",
              "source_secondary", "value_secondary", "fetched_at"]

MARGIN_COLS = ["date", "code", "long_balance", "short_balance", "ratio",
               "unit", "status", "source_url", "fetched_at"]

LOG_COLS = ["disclosed_on", "code", "pdf_url", "status", "pages",
            "text_chars", "metrics_written", "note", "fetched_at"]

MASTER_YAML = """stocks:
  - {code: "8888", name: "第二テスト"}
  - {code: "9999", name: "テスト株式会社"}
"""

REPORT_9999 = """---
code: "9999"
name: "テスト株式会社"
updated: 2001-01-01
---

# テスト株式会社（9999）

> **一行でいうと**：テスト用の合成レポート。

## 週次アップデート

> 週ごとに追記していく。過去の記述は書き換えない。

### 2023-W50（2023-12-11 週）

**先週のダミー。**

- ダミー行1

## この会社は何者か

本文はここ。数値 100 を含む既存行。
"""

# 週次アップデート節を持たないレポート（exit 2 の検証用）
REPORT_8888 = """---
code: "8888"
name: "第二テスト"
updated: 2001-01-01
---

# 第二テスト（8888）

## この会社は何者か

節が足りないレポート。
"""


def prow(day, close, status, volume, code="9999"):
    return {"date": day, "code": code, "open": "1", "high": "1", "low": "1",
            "close": "" if close is None else str(close),
            "volume": str(volume), "status": status,
            "source_primary": "a", "value_primary": "1",
            "source_secondary": "b", "value_secondary": "1",
            "fetched_at": "2024-01-01T00:00:00+09:00"}


class Sandbox:
    """一時ディレクトリに data/・scoring/・reports/ を組み立てる。"""

    def __init__(self):
        self.root = Path(tempfile.mkdtemp(prefix="kabu-weekly-note-"))
        for d in ["data/prices", "data/margin", "data/tanshin",
                  "scoring", "reports"]:
            (self.root / d).mkdir(parents=True)
        self._saved = (W.DATA, W.REPORTS, W.SCORING)
        W.DATA = self.root / "data"
        W.REPORTS = self.root / "reports"
        W.SCORING = self.root / "scoring"

        self._text("data/master.yaml", MASTER_YAML)
        self._text("reports/9999.md", REPORT_9999)
        self._text("reports/8888.md", REPORT_8888)
        self._text("scoring/stamps.json",
                   json.dumps({"9999": "様子見(過熱)", "8888": "調査"},
                              ensure_ascii=False) + "\n")

        # 株価: 前週 2024-W01（1/1〜1/7）と対象週 2024-W02（1/8〜1/14）
        self._csv("data/prices/daily.csv", PRICE_COLS, [
            # 前週: SINGLE_SOURCE は close_start に使わない
            prow("2024-01-04", None, "SINGLE_SOURCE", 400),
            prow("2024-01-05", 100, "OK", 1000),          # 前週最後の採用終値
            # 対象週
            prow("2024-01-08", 110, "OK", 2000),
            prow("2024-01-09", None, "SINGLE_SOURCE", 500),  # 採用外・出来高のみ
            prow("2024-01-10", 120, "OK", 3000),
            # close が入っているのに OK でない行（旧 NO_TRADE 系）。採用しない
            prow("2024-01-11", 999, "NO_TRADE", 100),
            # 8888: 対象週に採用終値が1日も無い
            prow("2024-01-08", None, "SINGLE_SOURCE", 700, code="8888"),
        ])
        self._csv("data/margin/9999.csv", MARGIN_COLS, [
            {"date": "2024-01-05", "code": "9999", "long_balance": "50.0",
             "short_balance": "10.0", "ratio": "5.0", "unit": "千株",
             "status": "OK", "source_url": "https://example.invalid/m",
             "fetched_at": "2024-01-06T00:00:00+09:00"},
            {"date": "2024-01-12", "code": "9999", "long_balance": "60.0",
             "short_balance": "0.0", "ratio": "", "unit": "千株",
             "status": "RATIO_NA", "source_url": "https://example.invalid/m",
             "fetched_at": "2024-01-13T00:00:00+09:00"},
        ])
        self._csv("data/tanshin/fetch_log.csv", LOG_COLS, [
            {"disclosed_on": "2023-12-15", "code": "9999",     # 週外 → 含めない
             "pdf_url": "https://example.invalid/old.pdf", "status": "OK",
             "pages": "10", "text_chars": "100", "metrics_written": "1",
             "note": "", "fetched_at": "2023-12-16T00:00:00+09:00"},
            {"disclosed_on": "2024-01-09", "code": "9999",     # 週内 → 含める
             "pdf_url": "https://example.invalid/t.pdf", "status": "OK",
             "pages": "10", "text_chars": "100", "metrics_written": "1",
             "note": "", "fetched_at": "2024-01-10T00:00:00+09:00"},
        ])

    def _text(self, rel: str, text: str):
        with (self.root / rel).open("w", encoding="utf-8", newline="\n") as f:
            f.write(text)

    def _csv(self, rel: str, cols: list[str], rows: list[dict]):
        import csv
        with (self.root / rel).open("w", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=cols, lineterminator="\n")
            w.writeheader()
            for r in rows:
                w.writerow({c: r.get(c, "") for c in cols})

    def close(self):
        W.DATA, W.REPORTS, W.SCORING = self._saved
        shutil.rmtree(self.root, ignore_errors=True)

    def notes(self, stocks: dict) -> Path:
        path = self.root / "notes.json"
        with path.open("w", encoding="utf-8", newline="\n") as f:
            json.dump({"week": WEEK, "stocks": stocks}, f, ensure_ascii=False)
            f.write("\n")
        return path


NOTE_9999 = {
    "summary": "合成データの一文。",
    "interpretation": ["一行目の解釈。", "二行目の解釈。"],
    "news": [{"date": "2024-01-09", "title": "テスト開示",
              "url": "https://example.invalid/n1", "fetched": "2024-01-10"}],
    "next_week": ["Aを見る", "Bを見る"],
}


def run_main(argv: list[str]) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        rc = W.main(argv)
    return rc, out.getvalue(), err.getvalue()


def assert_lines_preserved(before: str, after: str):
    """before の全行が、順序を保ったまま after に1バイトも変わらず残っていること。

    唯一の例外は front matter の updated 行（内容は変わってよいが行は残る）。
    """
    b, a = before.split("\n"), after.split("\n")
    ai = 0
    for line in b:
        if line.startswith("updated:"):
            while ai < len(a) and not a[ai].startswith("updated:"):
                ai += 1
            assert ai < len(a), "updated 行が消えている"
            ai += 1
            continue
        while ai < len(a) and a[ai] != line:
            ai += 1
        assert ai < len(a), f"既存行が変わった/消えた: {line!r}"
        ai += 1


# =============================================================================
# テスト本体
# =============================================================================

def test_collect_uses_only_ok_rows() -> None:
    sb = Sandbox()
    try:
        out_path = sb.root / "facts.json"
        rc, _, err = run_main(["--collect", "--week", WEEK,
                               "--out", str(out_path)])
        eq(rc, 0, f"exit code（stderr: {err}）")
        payload = json.loads(out_path.read_text(encoding="utf-8"))
        eq(payload["week"], WEEK, "week")
        eq(payload["monday"], MONDAY, "monday")

        s = payload["stocks"]["9999"]
        eq(s["name"], "テスト株式会社", "name")
        eq(s["close_start"], 100, "close_start は前週最後の OK 行")
        eq(s["close_start_date"], "2024-01-05", "close_start_date")
        eq(s["close_end"], 120, "close_end は今週最後の OK 行")
        eq(s["pct"], 20.0, "pct")
        eq(s["ok_days"], 2, "ok_days は OK 行だけ（SINGLE_SOURCE/NO_TRADE を数えない）")
        eq(s["week_high"], 120, "week_high に close 入り非OK 行(999)を混ぜない")
        eq(s["week_low"], 110, "week_low")
        # 出来高は照合外の参考値。全行を数え、記号必須
        assert "※" in s["volume_note"], s["volume_note"]
        assert "+300.0%" in s["volume_note"], s["volume_note"]  # 5600/1400
        eq(s["margin"]["date"], "2024-01-12", "margin は最新行")
        eq(s["margin"]["ratio"], None, "RATIO_NA は null")
        assert "※" in s["margin"]["note"], s["margin"]["note"]
        eq(s["stamp"], "様子見(過熱)", "stamp")
        eq(s["disclosures"], [{"date": "2024-01-09", "label": "決算短信"}],
           "disclosures は週内のみ")
        eq(s["last_entry_week"], "2023-W50", "last_entry_week")
        eq(s["this_week_entry_exists"], False, "this_week_entry_exists")
        eq(s["flags"], [], "flags")

        # 8888: 今週の採用終値が0日 → pct null + no_adopted_close
        s2 = payload["stocks"]["8888"]
        eq(s2["ok_days"], 0, "8888 ok_days")
        eq(s2["pct"], None, "8888 pct は null")
        eq(s2["flags"], ["no_adopted_close"], "8888 flags")
    finally:
        sb.close()


def test_write_preserves_existing_lines() -> None:
    sb = Sandbox()
    try:
        path = sb.root / "reports" / "9999.md"
        before = path.read_bytes().decode("utf-8")
        rc, out, err = run_main(["--write", str(sb.notes({"9999": NOTE_9999}))])
        eq(rc, 0, f"exit code（stderr: {err}）")
        after = path.read_bytes().decode("utf-8")

        assert_lines_preserved(before, after)
        head_new = f"### {WEEK}（{MONDAY} 週）"
        assert head_new in after, "新しい週の見出しが無い"
        assert after.index(head_new) < after.index("### 2023-W50"), \
            "新エントリが節の先頭（既存エントリより前）に入っていない"
        assert after.index("> 週ごとに追記していく") < after.index(head_new), \
            "引用行より前に挿入されている"
        # 機械の計測と一筆が合成されていること
        assert "**合成データの一文。**" in after
        assert "- 株価: 週間 +20.0%（100→120円・採用終値ベース、照合成立 2日、週内 110〜120円）" in after
        assert "出来高" in after and after.count("※") >= 2, \
            "出来高・信用倍率の※記号が無い"
        assert "開示: 01/09 決算短信" in after
        assert "<https://example.invalid/n1>（取得日 2024-01-10）" in after
        assert "（解釈）一行目の解釈。" in after
        assert "**次週に見ること**: ① Aを見る ② Bを見る" in after
        # updated は今日に更新（唯一の例外）
        assert f"updated: {date.today().isoformat()}" in after, "updated 未更新"
        assert "updated: 2001-01-01" not in after
    finally:
        sb.close()


def test_same_week_becomes_zokuhou() -> None:
    sb = Sandbox()
    try:
        path = sb.root / "reports" / "9999.md"
        notes = sb.notes({"9999": NOTE_9999})
        for _ in range(3):
            rc, _, err = run_main(["--write", str(notes)])
            eq(rc, 0, f"exit code（stderr: {err}）")
        after = path.read_text(encoding="utf-8")
        assert f"### {WEEK}（{MONDAY} 週）" in after, "1回目の見出し"
        assert f"### {WEEK}（続報）" in after, "2回目は（続報）"
        assert f"### {WEEK}（続報2）" in after, "3回目は（続報2）"
        # 最新（続報2）が節の先頭に来る
        assert after.index("（続報2）") < after.index("（続報）\n"), \
            "続報2 が先頭に挿入されていない"
    finally:
        sb.close()


def test_missing_section_exits_2() -> None:
    sb = Sandbox()
    try:
        p8888 = sb.root / "reports" / "8888.md"
        p9999 = sb.root / "reports" / "9999.md"
        before_8888 = p8888.read_bytes()
        before_9999 = p9999.read_bytes()
        rc, _, err = run_main(["--write",
                               str(sb.notes({"8888": NOTE_9999,
                                             "9999": NOTE_9999}))])
        eq(rc, 2, "exit code は 2")
        assert "第二テスト" in err, f"銘柄名が stderr に無い: {err!r}"
        # 1銘柄でも欠けていれば、どのレポートにも書かない
        eq(p8888.read_bytes(), before_8888, "8888 は無変更")
        eq(p9999.read_bytes(), before_9999, "9999 も無変更（部分適用しない）")
    finally:
        sb.close()


def test_outputs_are_lf() -> None:
    sb = Sandbox()
    try:
        out_path = sb.root / "facts.json"
        rc, _, _ = run_main(["--collect", "--week", WEEK, "--out", str(out_path)])
        eq(rc, 0, "collect exit code")
        assert b"\r\n" not in out_path.read_bytes(), "facts.json に CRLF"
        rc, _, _ = run_main(["--write", str(sb.notes({"9999": NOTE_9999}))])
        eq(rc, 0, "write exit code")
        assert b"\r\n" not in (sb.root / "reports" / "9999.md").read_bytes(), \
            "挿入後の md に CRLF"
    finally:
        sb.close()


def main() -> int:
    tests = [
        ("--collect は status OK 行だけを採用終値に使う（週境界も）",
         test_collect_uses_only_ok_rows),
        ("--write は挿入のみで既存行を変えない", test_write_preserves_existing_lines),
        ("同じ週の2回目以降は（続報）（続報2）と採番する", test_same_week_becomes_zokuhou),
        ("週次アップデート節が無い md は exit 2（何も書かない）",
         test_missing_section_exits_2),
        ("出力は LF", test_outputs_are_lf),
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
