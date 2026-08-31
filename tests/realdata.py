"""実データ（`data/`）から**期待値を引く**ための共用ヘルパ。

なぜ要るか
----------
テストに日付・値・件数をべた書きすると、**実データが1日進んだだけで CI が
全面的に落ちる**。実際に落ちた（週次取得で daily.csv に4行増えただけで、
「最終行には終値がある」「出来高0は6日」といった前提が一斉に崩れた）。

このプロジェクトのデータは append-only で毎週増える。テストが確かめるべきは
**データの形と不変条件**であって、今週のデータの中身ではない。
実データが要るテストは、期待値をここから引くこと。

  ×  expect(rep, WARN, "no_trade", "4937: 出来高0（売買不成立）が 6日")
  ○  expect(rep, WARN, "no_trade", f"4937: 出来高0（売買不成立）が {rd.zero_volume_days('4937')}日")

もうひとつの罠
--------------
最新営業日は**照合が成立せず `close` が空になるのが普通**である
（取得元によって当日分が載る時刻が違う。minkabu は翌日）。
「最終行には採用値がある」を暗黙に仮定したテストは、翌週かならず落ちる。
「確定した最後の日」は `last_confirmed_date()` で取る。

このファイルは `test_*.py` に一致しないので、pytest にも weekly.yml の
`for f in tests/test_*.py` にも**テストとして拾われない**（ヘルパ専用）。
"""
from __future__ import annotations

import csv
from datetime import date, timedelta
from functools import lru_cache
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
PRICES = DATA / "prices" / "daily.csv"


# --- 読み取り -----------------------------------------------------------------

def read_csv(path: Path) -> tuple[list[str], list[dict]]:
    with Path(path).open(encoding="utf-8-sig", newline="") as f:
        r = csv.DictReader(f)
        return list(r.fieldnames or []), list(r)


def write_csv(path: Path, fields, rows) -> None:
    with Path(path).open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)


@lru_cache(maxsize=1)
def _prices() -> tuple[tuple[str, ...], tuple[dict, ...]]:
    fields, rows = read_csv(PRICES)
    return tuple(fields), tuple(rows)


def price_fields() -> list[str]:
    return list(_prices()[0])


def price_rows(code: str | None = None) -> list[dict]:
    """daily.csv の行（日付昇順）。code を渡すとその銘柄だけ。"""
    rows = [dict(r) for r in _prices()[1]]
    if code is not None:
        rows = [r for r in rows if r["code"] == str(code)]
    return sorted(rows, key=lambda r: r["date"])


@lru_cache(maxsize=1)
def master() -> dict:
    import yaml
    return yaml.safe_load((DATA / "master.yaml").read_text(encoding="utf-8"))


def codes() -> list[str]:
    """master.yaml に載っている証券コード（昇順）。銘柄が増えても追随する。"""
    return sorted(str(s["code"]) for s in master().get("stocks", []))


def watched_codes() -> list[str]:
    """**取得を続けている**銘柄（`watch: excluded` を除く・昇順）。

    対象外にした銘柄はデータが凍るので、「最新営業日まで揃っている」を
    要求する検査はこちらを使う。全銘柄が要るのは追記性の検査だけ。
    """
    import sys
    sys.path.insert(0, str(DATA.parent / "src"))
    import yamlio as Y
    return sorted(str(s["code"]) for s in Y.watched_stocks(master()))


def excluded_codes() -> list[str]:
    """監視から外した銘柄（昇順）。データは凍っているが消してはいない。"""
    return sorted(set(codes()) - set(watched_codes()))


# --- 日付 ---------------------------------------------------------------------

def dates(code: str | None = None) -> list[str]:
    return sorted({r["date"] for r in price_rows(code)})


def latest_date(code: str | None = None) -> str:
    """実データの最新営業日。**この行の `close` は空のことがある**。"""
    return dates(code)[-1]


def first_date(code: str | None = None) -> str:
    return dates(code)[0]


def last_confirmed_date(code: str | None = None) -> str:
    """`close`（2ソース照合を通った採用値）が入っている最後の日。

    指標・判定・台帳の基準日はここに揃う（`indicators.drop_unconfirmed_tail`）。
    """
    ds = [r["date"] for r in price_rows(code) if str(r.get("close") or "").strip()]
    assert ds, "採用値のある行が1つも無い（照合が全滅している）"
    return max(ds)


def mid_date(code: str | None = None) -> str:
    """履歴の中ほどにある営業日（最初でも最後でもない1日）。

    「過去行を書き換えた」「履歴に穴が空いた」を作るための足場。
    先頭・末尾は別の検査が反応してしまうので中央から取る。
    """
    ds = dates(code)
    assert len(ds) >= 3, f"履歴が短すぎる: {len(ds)}日"
    return ds[len(ds) // 2]


def next_business_day(day: str | None = None) -> str:
    """その日の次の平日（祝日表は持たない＝決定論的）。"""
    d = date.fromisoformat(day or latest_date())
    while True:
        d += timedelta(days=1)
        if d.weekday() < 5:
            return d.isoformat()


def day_after_all_fetches(margin_days: int = 60) -> str:
    """CSV に記録された**どの取得時刻より後**の日付。

    「取得は動いたのに古い日付しか返ってこない」壊れ方を作るときに使う。
    固定日（2026-10-01 など）を書くと、実データの fetched_at がその日を
    追い越した瞬間に検査の前提が崩れる。
    """
    fetched = [str(r.get("fetched_at") or "")[:10] for r in price_rows()]
    fetched = [d for d in fetched if d]
    base = max(fetched + [latest_date()])
    return (date.fromisoformat(base) + timedelta(days=margin_days)).isoformat()


# --- 件数（メッセージに件数が出る検査の期待値） ---------------------------------

def zero_volume_days(code: str) -> int:
    return sum(1 for r in price_rows(code) if str(r.get("volume") or "").strip() == "0")


def no_trade_days(code: str) -> int:
    """status に NO_TRADE が立っている日数（`check_no_trade` が数えるのと同じ集合）。"""
    return sum(1 for r in price_rows(code)
               if "NO_TRADE" in str(r.get("status") or "").split("|"))


def status_days(code: str, status: str) -> int:
    return sum(1 for r in price_rows(code)
               if status in str(r.get("status") or "").split("|"))


# --- 指標が未計算になる銘柄（薄い銘柄の欠測） -----------------------------------

def min_bars_needed() -> int:
    """全指標を算出するのに必要な最小の営業日数（**定数から導く**）。

    ここに実データの行数（1076 等）を書くと、次の週次取得で意味を失ううえ、
    「何本あれば足りるのか」がテストから読み取れなくなる。要求は指標側の
    定数で決まっているので、そこから引く。
    """
    import indicators as ind
    return max(
        ind.ICHIMOKU_SPAN_B_PERIODS + ind.ICHIMOKU_DISPLACEMENT + 1,
        ind.VOLUME_RATIO_LOOKBACK_DAYS + ind.VOLUME_RATIO_WINDOW_DAYS,
        ind.WEEKLY_MA_LONG_PERIODS * 5,      # 26週ぶんの営業日
    )


def close_gap_days(code: str, window: int | None = None) -> list[str]:
    """確定足の直近 window 本のうち、採用終値を持たない日。

    `indicators.sma` は**窓に欠測が1つでもあれば None** を返す（設計。
    穴のある窓で平均を出すと嘘になる）。だから穴のある銘柄は指標がまとめて
    未計算になる。**これは欠陥ではない。** 売買が成立しない日がある薄い銘柄では
    普通に起きる（実測 2026-08-30: 6647 森尾電機は 270営業日中 18日が NO_TRADE。
    他の17銘柄は 0〜6日）。

    実データを使う検査は、戻り値が空でない銘柄について「指標が None でもよい」
    と扱う。**穴が無いのに未計算なら本物の欠陥**なので、そちらは落とす。
    全銘柄に算出を要求すると、**薄い銘柄を台帳に載せられない**＝
    フラグを立てた瞬間に公開が止まる仕掛けになる（`excluded` の末尾要求で
    同じ罠を踏んでいる）。判定側は未計算のゲートを通過扱いにせず「調査」で
    止めるので、ここを緩めても危ない方向には倒れない。

    末尾の未確定行（最新営業日は照合が成立していないのが普通）は、指標側の
    `drop_unconfirmed_tail` と同じく先に落としてから数える。
    """
    rows = sorted(price_rows(code), key=lambda r: r["date"])
    while rows and not str(rows[-1].get("close") or "").strip():
        rows.pop()
    if window:
        rows = rows[-window:]
    return [r["date"] for r in rows
            if not str(r.get("close") or "").strip()]


def index_rows(index_id: str) -> list[dict]:
    _, rows = read_csv(DATA / "indices" / f"{index_id}.csv")
    return sorted(rows, key=lambda r: r["date"])


def index_status_days(index_id: str, status: str) -> int:
    return sum(1 for r in index_rows(index_id)
               if status in str(r.get("status") or "").split("|"))


def shared_history_date(index_id: str) -> str:
    """株価にも指数にもあり、かつ最新営業日ではない1日。

    「指数に穴が空いた」を作る足場。最新日を消すと別の検査（取得漏れ）が
    反応してしまうので、履歴の中ほどから取る。
    """
    idx = {r["date"] for r in index_rows(index_id)}
    common = sorted(idx & set(dates()))
    common = [d for d in common if d != latest_date()]
    assert common, f"{index_id} と株価に共通の営業日が無い"
    return common[len(common) // 2]


# --- 行の合成（「1日進んだ」状況を作る） ----------------------------------------

def _has_ok(row: dict) -> bool:
    return "OK" in str(row.get("status") or "").split("|")


def unconfirmed_row(day: str, template: dict, ok_template: dict | None = None) -> dict:
    """**照合が成立しなかった1日**の行を作る（fetch.py が実際に書く形）。

    `close` は空・`status=SINGLE_SOURCE`・副ソースなし。取得元によって当日分が
    載る時刻が違うため、**最新営業日はこの形になるのが普通**である。
    今回の事故（データが1日進んで CI が全面的に落ちた）の再現に使う。
    """
    price = str(template.get("close") or template.get("value_primary") or "")
    src_p = str(template.get("source_primary") or "").strip() or "yahoo_jp"
    row = dict(template)
    row.update({
        "date": day,
        "close": "",
        "status": "SINGLE_SOURCE",
        "source_primary": src_p,
        "value_primary": price,
        "source_secondary": "",
        "value_secondary": "",
        "fetched_at": f"{day}T09:59:00+09:00",
    })
    return row


def confirmed_row(day: str, template: dict, ok_template: dict | None = None) -> dict:
    """照合が成立した1日の行（`close` が埋まる正常形）。

    取得元の名前は、その系列で実際に照合が成立した行から取る。
    `status=OK` は第2ソースの記録があって初めて成立する（checks の schema 検査）。
    """
    base = ok_template or template
    price = str(template.get("close") or template.get("value_primary") or "")
    src_p = str(base.get("source_primary") or "").strip() or "kabutan"
    src_s = str(base.get("source_secondary") or "").strip() or "minkabu"
    row = dict(template)
    row.update({
        "date": day,
        "close": price,
        "status": "OK",
        "source_primary": src_p,
        "value_primary": price,
        "source_secondary": src_s,
        "value_secondary": price,
        "fetched_at": f"{day}T09:59:00+09:00",
    })
    return row


def advance_rows(rows: list[dict], day: str, confirmed: bool | None = None,
                 id_field: str = "code") -> list[dict]:
    """OHLCV 形式の CSV の中身から「1日ぶんの新しい行」を作る。

    銘柄（または指数）ごとに1行。`confirmed` を省略すると、その系列の
    直近の行が照合成立だったかどうかに合わせる（growth250 のように第2ソースが
    無い系列を、実態と違う「照合成立」にしないため）。
    """
    ordered = sorted(rows, key=lambda r: str(r.get("date") or ""))
    last: dict[str, dict] = {}
    last_ok: dict[str, dict] = {}
    for r in ordered:
        key = str(r.get(id_field) or "")
        last[key] = r
        if _has_ok(r):
            last_ok[key] = r
    out = []
    for key in sorted(last):
        template = last[key]
        want = _has_ok(template) if confirmed is None else confirmed
        make = confirmed_row if want else unconfirmed_row
        out.append(make(day, template, last_ok.get(key)))
    return out
