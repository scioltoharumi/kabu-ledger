"""fetch.py の照合（reconcile）とパースの回帰テスト。

ここは **ネットワークを使わない**。`reconcile()` と `parse_ohlcv()` は純関数なので、
組み立てた Bar / HTML を直接渡して検証できる。

なぜこのファイルが要るか:
  `close`（採用値）が埋まってよいのは2ソース照合が成立した行だけ、という不変条件
  （D7）は fetch.py の中でしか守られていないのに、fetch 系にはテストが1本も無かった。
  実際、旧実装は照合を走らせた直後に NO_TRADE で結果を握り潰しており、
  1ソースしか無くても・2ソースが不一致でも主ソース値が close に入っていた。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import fetch as F  # noqa: E402


def eq(actual, expected, label=""):
    assert actual == expected, f"{label}: expected {expected!r}, got {actual!r}"


def is_none(actual, label=""):
    assert actual is None, f"{label}: expected None, got {actual!r}"


def bar(source, close, volume, o=None, h=None, lo=None, date="2026-01-05"):
    return F.Bar(date=date, open=o, high=h, low=lo, close=close,
                 volume=volume, source=source)


def flags(row) -> set[str]:
    return set(str(row["status"]).split("|"))


# =============================================================================
# 通常の照合
# =============================================================================

def test_two_sources_agree_is_ok():
    row = F.reconcile("9999", [[bar("a", 100.0, 500, 99.0, 101.0, 98.0)],
                               [bar("b", 100.0, 500)]], 2)[0]
    eq(flags(row), {"OK"}, "照合成立")
    eq(row["close"], 100.0, "採用値が入る")
    eq(row["value_secondary"], 100.0, "副ソースの値も残す")


def test_two_sources_disagree_is_mismatch_and_close_is_empty():
    row = F.reconcile("9999", [[bar("a", 100.0, 500, 99.0, 101.0, 98.0)],
                               [bar("b", 105.0, 500)]], 2)[0]
    eq(flags(row), {"MISMATCH"}, "不一致")
    is_none(row["close"], "照合を通っていない値を採用値に格上げしない")
    eq(row["value_primary"], 100.0, "生値は両方残す")
    eq(row["value_secondary"], 105.0, "生値は両方残す")


def test_single_source_leaves_close_empty():
    row = F.reconcile("9999", [[bar("a", 100.0, 500, 99.0, 101.0, 98.0)]], 2)[0]
    eq(flags(row), {"SINGLE_SOURCE"}, "1ソースのみ")
    is_none(row["close"], "照合不成立なので採用値は空")


# =============================================================================
# NO_TRADE（★旧実装が照合結果を握り潰していた箇所）
# =============================================================================

def test_no_trade_does_not_overwrite_mismatch():
    """NO_TRADE を照合結果に**付加**する。MISMATCH を消して close を埋めない。"""
    row = F.reconcile("9999", [[bar("a", 100.0, 0)], [bar("b", 105.0, 0)]], 2)[0]
    eq(flags(row), {"MISMATCH", "NO_TRADE"}, "両方のフラグが残る")
    is_none(row["close"], "不一致なので採用値は空のまま")


def test_no_trade_with_single_source_leaves_close_empty():
    row = F.reconcile("9999", [[bar("a", 100.0, 0)]], 2)[0]
    eq(flags(row), {"SINGLE_SOURCE", "NO_TRADE"}, "1ソースの売買不成立")
    is_none(row["close"], "照合を通っていないので採用値は空")


def test_no_trade_requires_all_sources_to_agree():
    """主ソースだけが出来高0で、副ソースが通常の足を返しているなら NO_TRADE にしない。

    「売買不成立だった」という事実の記録が、主ソースの描画事故で作られてしまう。
    """
    row = F.reconcile("9999", [[bar("a", 100.0, 0)],
                               [bar("b", 100.0, 5000, 99.0, 103.0, 98.0)]], 2)[0]
    assert "NO_TRADE" not in flags(row), flags(row)
    eq(flags(row), {"OK", "VOLUME_MISMATCH"}, "終値は一致・出来高は食い違う")
    eq(row["close"], 100.0, "終値の照合は成立している")


def test_no_trade_when_both_sources_agree():
    row = F.reconcile("9999", [[bar("a", 100.0, 0)], [bar("b", 100.0, 0)]], 2)[0]
    eq(flags(row), {"OK", "NO_TRADE"}, "両ソースが売買不成立で一致")
    eq(row["close"], 100.0, "照合成立なので採用値が入る")
    eq(row["open"], 100.0, "始値・高値・安値は終値で代替する")
    eq(row["high"], 100.0, "始値・高値・安値は終値で代替する")
    eq(row["low"], 100.0, "始値・高値・安値は終値で代替する")


def test_no_trade_is_detected_even_when_open_is_zero():
    """始値を「0」と表示するサイトでも売買不成立として扱う（表記でなく事実で判定）。"""
    row = F.reconcile("9999", [[bar("a", 100.0, 0, o=0.0)],
                               [bar("b", 100.0, 0, o=0.0)]], 2)[0]
    eq(flags(row), {"OK", "NO_TRADE"}, "出来高0が判定の根拠")


# =============================================================================
# 出来高の食い違い（列の取り違え・単位違い）
# =============================================================================

def test_volume_mismatch_is_flagged():
    row = F.reconcile("9999", [[bar("a", 100.0, 500_000)],
                               [bar("b", 100.0, 500)]], 2)[0]
    assert "VOLUME_MISMATCH" in flags(row), flags(row)
    eq(row["close"], 100.0, "終値の採用可否には影響しない")


def test_small_volume_difference_is_not_flagged():
    row = F.reconcile("9999", [[bar("a", 100.0, 1000)],
                               [bar("b", 100.0, 1010)]], 2)[0]
    assert "VOLUME_MISMATCH" not in flags(row), flags(row)


# =============================================================================
# パース
# =============================================================================

_HTML = """
<table class="stock_kabuka_dwm">
<tr><th>日付</th><th>始値</th><th>高値</th><th>安値</th><th>終値</th>
    <th>前日比</th><th>前日比(%)</th><th>売買高</th></tr>
<tr><td>26/08/10</td><td>1,000</td><td>1,100</td><td>990</td><td>1,050</td>
    <td>+50</td><td>+5.0</td><td>12,345</td></tr>
<tr><td>26/08/07</td><td>980</td><td>1,010</td><td>970</td><td>1,000</td>
    <td>-10</td><td>-1.0</td><td>--</td></tr>
</table>
"""

_ENTRY = {
    "id": "test",
    "table_selector": "table.stock_kabuka_dwm",
    "date_format": "yy/mm/dd",
    "columns": ["date", "open", "high", "low", "close", "change",
                "change_pct", "volume"],
}


def test_parse_ohlcv():
    bars = F.parse_ohlcv(_HTML, _ENTRY, "test")
    eq(len(bars), 2, "2営業日")
    b = bars[0]
    eq(b.date, "2026-08-10", "yy/mm/dd を YYYY-MM-DD に正規化")
    eq((b.open, b.high, b.low, b.close, b.volume),
       (1000.0, 1100.0, 990.0, 1050.0, 12345), "四本値と出来高")
    is_none(bars[1].volume, "読めない出来高は None（0 にしない）")


def test_parse_ohlcv_missing_table_is_not_an_exception():
    eq(F.parse_ohlcv("<html><body>変更されたページ</body></html>", _ENTRY, "test"),
       [], "セレクタが外れたら空リスト（F1-7: 例外にしない）")


# =============================================================================
# ランナー
# =============================================================================

def main() -> int:
    tests = [(n, f) for n, f in sorted(globals().items())
             if n.startswith("test_") and callable(f)]
    failed = []
    for name, fn in tests:
        try:
            fn()
            print(f"  PASS  {name}")
        except AssertionError as e:
            failed.append((name, str(e)))
            print(f"  FAIL  {name}: {e}")
        except Exception as e:  # noqa: BLE001
            failed.append((name, f"{type(e).__name__}: {e}"))
            print(f"  ERROR {name}: {type(e).__name__}: {e}")
    print(f"\n{len(tests) - len(failed)}/{len(tests)} passed")
    if failed:
        for name, msg in failed:
            print(f"  - {name}: {msg}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
