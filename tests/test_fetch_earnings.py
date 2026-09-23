"""決算予定日の取得（src/fetch_earnings.py）の回帰テスト。ネットワークは使わない。

  python tests/test_fetch_earnings.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import fetch_earnings as FE  # noqa: E402

PATTERN = "決算発表予定日\\s*(\\d{4})/(\\d{2})/(\\d{2})"

MASTER = """stocks:
  - code: "1111"
    name: "予定日なし"
    fiscal_year_end: "03"   # 決算月
    holding:
      status: none

  - code: "2222"
    name: "予定日あり"
    next_earnings: "2026-08-14"
    next_earnings_source: "https://example.invalid/2222"
    watch: active
"""


def test_parse_date():
    html = "<div>決算発表予定日 <span>2026/10/09</span></div><p>発表日 2025/10/10</p>"
    assert FE.parse_date(html, PATTERN) == "2026-10-09"
    assert FE.parse_date("<p>決算期 発表日 2025/10/10</p>", PATTERN) is None, \
        "予定日の見出しが無いページから過去の発表日を拾わない"


def test_insert_after_fiscal_year_end():
    lines = MASTER.splitlines(keepends=True)
    assert FE.set_earnings(lines, "1111", "2026-10-30", "https://k/1111")
    text = "".join(lines)
    assert ('    fiscal_year_end: "03"   # 決算月\n'
            '    next_earnings: "2026-10-30"\n'
            '    next_earnings_source: "https://k/1111"\n'
            "    holding:\n") in text, text
    assert text.count("next_earnings:") == 2, "他の銘柄のブロックに書かない"


def test_replace_existing_value_only():
    lines = MASTER.splitlines(keepends=True)
    assert FE.set_earnings(lines, "2222", "2026-11-13", "https://k/2222")
    text = "".join(lines)
    assert '    next_earnings: "2026-11-13"\n' in text
    assert '"2026-08-14"' not in text
    assert "    watch: active\n" in text, "他のキーは残す"
    assert not FE.set_earnings(lines, "2222", "2026-11-13", "https://k/2222"), \
        "同じ値なら変更なし"


def test_unknown_code_is_untouched():
    lines = MASTER.splitlines(keepends=True)
    assert not FE.set_earnings(lines, "9999", "2026-10-01", "https://k/9999")
    assert "".join(lines) == MASTER


def main() -> int:
    tests = [(n, f) for n, f in sorted(globals().items())
             if n.startswith("test_") and callable(f)]
    failed = []
    for name, fn in tests:
        try:
            fn()
            print(f"  PASS  {name}")
        except AssertionError as e:
            failed.append(name)
            print(f"  FAIL  {name}: {e}")
    print(f"\n{len(tests) - len(failed)}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
