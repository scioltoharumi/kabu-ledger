"""株価取得（日足 OHLCV）。HTMLのパースはコードで行い、LLMには構造化済みの値のみ渡す。

原則:
  - 取得元は sources.yaml のチェーンのみ。探索しない。
  - 2ソースの生終値一致で採用。不一致は MISMATCH、1件のみは SINGLE_SOURCE。
  - CSVは append-only。既存 (code, date) は上書きしない。
  - 本日のザラ場値は取らない。各ソースの時系列テーブルは前営業日までの確定値のみを含む。
  - 調整後終値は使わない（生終値のみ。混在は MISMATCH の主因になる）。

使い方:
  python src/fetch.py                # 直近1ページ（約30営業日）
  python src/fetch.py --historical   # 初回の遡り（sources.yaml の historical_pages 分 ≒ 1年）
"""
from __future__ import annotations

import argparse
import csv
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from pathlib import Path

try:  # ローカル(Windows)は SSL 検査プロキシ配下。CI(Linux) では不要
    import truststore

    truststore.inject_into_ssl()
except ImportError:
    pass

import requests
import yaml
import yamlio as Y
from bs4 import BeautifulSoup

sys.path.insert(0, str(Path(__file__).resolve().parent))

import revise as RV  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
JST = timezone(timedelta(hours=9))
FIELDS = [
    "date", "code", "open", "high", "low", "close", "volume", "status",
    "source_primary", "value_primary",
    "source_secondary", "value_secondary",
    "fetched_at",
]


@dataclass(frozen=True)
class Bar:
    """1営業日分の四本値。source は取得元 id。"""
    date: str          # YYYY-MM-DD
    open: float | None
    high: float | None
    low: float | None
    close: float | None
    volume: int | None
    source: str


# --- パースの部品 ---------------------------------------------------------

def _num(text: str) -> float | None:
    """'35,700' -> 35700.0 / '－' や '---' -> None"""
    if not text:
        return None
    m = re.search(r"-?[\d,]+(?:\.\d+)?", text)
    if not m:
        return None
    try:
        return float(m.group().replace(",", ""))
    except ValueError:
        return None


def _int(text: str) -> int | None:
    v = _num(text)
    return int(v) if v is not None else None


def _norm_date(text: str, fmt: str) -> str | None:
    """各サイトの日付表記を YYYY-MM-DD に正規化する。"""
    t = (text or "").strip()
    m = re.match(r"(\d{2,4})/(\d{1,2})/(\d{1,2})", t)
    if not m:
        return None
    y, mo, d = m.groups()
    year = int(y)
    if fmt == "yy/mm/dd" or len(y) == 2:
        year += 2000
    return f"{year:04d}-{int(mo):02d}-{int(d):02d}"


def _rows_from(table, skip_header: bool = True):
    """テーブルの各行をセル文字列のリストで返す。"""
    for tr in table.find_all("tr"):
        cells = [c.get_text(strip=True) for c in tr.find_all(["td", "th"])]
        if not cells:
            continue
        if skip_header and not re.match(r"\d{2,4}/\d{1,2}/\d{1,2}", cells[0]):
            continue  # ヘッダ行や見出し行
        yield cells


def find_table(soup: BeautifulSoup, entry: dict):
    """`table_selector`（CSS）か `match_th`（見出しテキスト）でテーブルを特定する。

    id も安定した class も持たないテーブルがある（kabutan の信用残テーブル、
    Yahoo の指数時系列＝ビルドごとに変わるハッシュ class）。そういう相手には
    見出しテキストで当てる。見つからなければ None（F1-7: 例外にせず欠測扱い）。
    """
    sel = entry.get("table_selector")
    if sel:
        table = soup.select_one(sel)
        if table is not None:
            return table

    want = entry.get("match_th")
    if want:
        for table in soup.find_all("table"):
            ths = [x.get_text(strip=True) for x in table.find_all("th")]
            if all(any(w in th for th in ths) for w in want):
                return table
    return None


def parse_ohlcv(html: str, entry: dict, source_id: str) -> list[Bar]:
    """sources.yaml の columns 定義に従って時系列テーブルを読む。

    セレクタが外れたら例外にせず空リストを返し、欠測として扱う。
    columns に無い項目（出来高を載せない指数ページなど）は None になる。
    """
    soup = BeautifulSoup(html, "html.parser")
    table = find_table(soup, entry)
    if table is None:
        return []

    cols = entry["columns"]
    idx = {name: i for i, name in enumerate(cols)}

    def cell(cells: list[str], name: str) -> str:
        i = idx.get(name)
        return cells[i] if i is not None and i < len(cells) else ""

    bars: list[Bar] = []
    for cells in _rows_from(table):
        if len(cells) < len(cols):
            continue
        date = _norm_date(cell(cells, "date"), entry.get("date_format", ""))
        if not date:
            continue
        bars.append(Bar(
            date=date,
            open=_num(cell(cells, "open")),
            high=_num(cell(cells, "high")),
            low=_num(cell(cells, "low")),
            close=_num(cell(cells, "close")),     # 生終値。close_adjusted は使わない
            volume=_int(cell(cells, "volume")),
            source=source_id,
        ))
    return bars


# --- 取得 -----------------------------------------------------------------

def pages_for(entry: dict, days: int) -> int:
    """必要な営業日数を、その取得元の1ページあたり行数から必要ページ数に換算する。"""
    if days <= 0 or not entry.get("pageable"):
        return 1
    per = entry.get("rows_per_page") or 20
    return max(1, -(-days // per))   # 切り上げ


def fetch_source(code: str, entry: dict, pol: dict, pages: int) -> list[Bar]:
    """1つの取得元から日足を取る。ページング対応元のみ pages 分を遡る。"""
    urls: list[str] = []
    if pages > 1 and entry.get("pageable") and entry.get("paged_url"):
        urls.append(entry["url"].format(code=code))
        for p in range(2, pages + 1):
            urls.append(entry["paged_url"].format(code=code, page=p))
    else:
        urls.append(entry["url"].format(code=code))

    out: list[Bar] = []
    for url in urls:
        for attempt in range(pol["retries"] + 1):
            try:
                r = requests.get(
                    url,
                    headers={"User-Agent": pol["user_agent"]},
                    timeout=pol["timeout_sec"],
                )
                r.raise_for_status()
                got = parse_ohlcv(r.text, entry, entry["id"])
                if not got:
                    print(f"  [{code}] {entry['id']} セレクタ不一致: {url}", file=sys.stderr)
                out.extend(got)
                break
            except Exception as e:  # noqa: BLE001
                if attempt == pol["retries"]:
                    print(f"  [{code}] {entry['id']} 取得失敗: {e}", file=sys.stderr)
        time.sleep(pol["interval_sec"])
    return out


# 出来高が2ソースで食い違ったとみなす比（列の取り違え検出用）。
# サイト間の丸め差は数%以内なので、2倍離れていれば「別の列を読んでいる」ほうを疑う。
# 出来高は流動性ゲート（判定の最上位）の唯一の入力なのに、close と違って
# 2ソース照合を通っていない。せめて桁の取り違えは status に残す。
VOLUME_MISMATCH_RATIO = 2.0


def _no_trade(b: Bar) -> bool:
    """そのソースが「売買不成立」を返しているか。

    定義するのは出来高0という事実そのもの。旧実装は `open is None` も条件にして
    いたため、始値を「0」と表示するサイトが主ソースになった週は同じ現実の出来事が
    SINGLE_SOURCE（close 空）として記録された。表記でなく事実で判定する。
    """
    return b.volume == 0


def operators_of(chain) -> dict[str, str]:
    """取得元 id -> 運営（`sources.yaml` の `operator`。無ければ id 自身）。"""
    out: dict[str, str] = {}
    for entry in chain or []:
        out[str(entry["id"])] = str(entry.get("operator") or entry["id"])
    return out


def _independent_pair(found: list[Bar], operators: dict[str, str],
                      required: int) -> Bar | None:
    """主ソースと**運営が異なる**最初のソースを副ソースとして返す。

    株探と みんかぶ は同一運営（ミンカブ・ジ・インフォノイド）なので、
    その2つの一致は「独立した2つの確認」ではない。財務側が
    `_fund_site` で「同じサイトの別ページは独立した確認ではない」を守っているのと
    同じ規律を株価にも通す。
    """
    if required < 2 or not found:
        return None
    head = operators.get(found[0].source, found[0].source)
    for b in found[1:]:
        if operators.get(b.source, b.source) != head:
            return b
    return None


def reconcile(code: str, by_source: list[list[Bar]], required: int,
              operators: dict[str, str] | None = None) -> list[dict]:
    """日付ごとに複数ソースを突き合わせ、行を組み立てる。

    運営の異なる2ソースの生終値が一致 -> OK / 不一致 -> MISMATCH /
    独立した相手がいない -> SINGLE_SOURCE

    **照合結果を上書きしない**（2026-08-12 修正）。旧実装は照合を走らせた直後に
    `if no_trade: row["close"] = primary.close; row["status"] = "NO_TRADE"` を
    無条件で実行していたため、
      (a) 1ソースしか取れていなくても close（＝採用値）が埋まり、
      (b) 2ソースが不一致でも MISMATCH が消えて主ソース値が採用値になっていた。
    不変条件「照合を通っていない値を採用値に格上げしない」（D7）に反する。
    NO_TRADE は照合結果に **付加**する（`OK|NO_TRADE` / `SINGLE_SOURCE|NO_TRADE`）。
    さらに no_trade の判定は主ソース単独ではなく**照合に参加した全ソースの一致**で行う。
    """
    now = datetime.now(JST).isoformat()
    ops = operators or {}

    per_date: dict[str, list[Bar]] = {}
    for bars in by_source:
        for b in bars:
            if b.close is None:
                continue
            per_date.setdefault(b.date, []).append(b)

    rows: list[dict] = []
    for date in sorted(per_date):
        found = per_date[date]
        primary = found[0]
        secondary = _independent_pair(found, ops, required)
        participants = [primary] + ([secondary] if secondary is not None else [])

        # 売買不成立。始値・高値・安値は存在せず、終値欄には気配値が入る。
        # 薄商い銘柄では実際に起きる（4937 で確認）。推定ではなく定義上の扱いとして
        # OHLC を終値で揃え、status で「取引がなかった日」であることを明示する。
        # **参加した全ソースが売買不成立を返したときだけ** NO_TRADE とする。
        # 主ソースだけが 0 で副ソースが通常の足を返しているなら、それは
        # 「売買不成立だった」ではなく「主ソースの描画事故」の可能性がある。
        no_trade = all(_no_trade(b) for b in participants)

        flags: list[str] = []
        row = {
            "date": date,
            "code": code,
            "open": primary.close if no_trade else primary.open,
            "high": primary.close if no_trade else primary.high,
            "low": primary.close if no_trade else primary.low,
            "close": None,
            "volume": primary.volume,
            "status": "SINGLE_SOURCE",
            "source_primary": primary.source,
            "value_primary": primary.close,
            "source_secondary": None,
            "value_secondary": None,
            "fetched_at": now,
        }
        if secondary is not None:
            row["source_secondary"] = secondary.source
            row["value_secondary"] = secondary.close
            if primary.close == secondary.close:
                row["close"] = primary.close      # 照合成立。ここでだけ採用値が埋まる
                flags.append("OK")
            else:
                flags.append("MISMATCH")          # 判定は前週値を据え置き
            # 出来高の食い違い（列の取り違え・単位違いの検出）。
            pv, sv = primary.volume, secondary.volume
            if pv is not None and sv is not None and not (pv == 0 and sv == 0):
                hi, lo = max(pv, sv), min(pv, sv)
                if lo == 0 or hi / lo > VOLUME_MISMATCH_RATIO:
                    flags.append("VOLUME_MISMATCH")
        else:
            flags.append("SINGLE_SOURCE")

        if no_trade:
            flags.append("NO_TRADE")
        row["status"] = "|".join(flags)
        rows.append(row)
    return rows


def append_only(path: Path, rows: list[dict], now: str = "",
                ledger: Path | None = None) -> tuple[int, int]:
    """既存 (code, date) は書き換えない。冪等性の担保。

    **例外は「照合不成立 → 成立」の訂正だけ**（`revise.py` の原則）。
    鍵が (code, date) なので、ある日の取得が片肺だった行は永久に
    `close` が空のまま固定され、その1日が20日/25日窓の内側に入った瞬間に
    **全銘柄の流動性ゲートが unknown に落ちて「調査」で止まる**。
    採用値が空だった行が今回 `OK` になったときだけ書き戻し、
    その事実を `data/revisions.csv` に必ず残す。

    採用値を下げる方向（OK → 空）は**自動では行わない**。取得元の一時的な
    不調で採用終値が消えると、直したかった障害を自分で起こすことになる。

    戻り値は (追記した行数, 訂正した行数)。
    """
    existing: dict[tuple[str, str], dict] = {}
    order: list[tuple[str, str]] = []
    if path.exists():
        with path.open(encoding="utf-8-sig", newline="") as f:
            for r in csv.DictReader(f):
                key = (r["code"], r["date"])
                existing[key] = r
                order.append(key)

    new = [r for r in rows if (r["code"], r["date"]) not in existing]
    repairs = []
    for r in rows:
        key = (r["code"], r["date"])
        old = existing.get(key)
        if old is None or not RV.is_repair(old, r, "close"):
            continue
        diffs = RV.diff_columns(old, r, FIELDS)
        if diffs:
            repairs.append((key, old, r, diffs))

    if repairs:
        try:
            rel = "data/" + path.resolve().relative_to(
                (ROOT / "data").resolve()).as_posix()
        except ValueError:
            rel = "data/" + path.name
        for key, old, fixed, diffs in repairs:
            existing[key] = {c: fixed.get(c) for c in FIELDS}
            RV.append_records(
                rel, "/".join(key), diffs, RV.REPAIR,
                "照合不成立で採用値が空だった行が、今回の取得で"
                "運営の異なる2つの取得元で一致した",
                now, ledger)
        with path.open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=FIELDS)
            w.writeheader()
            w.writerows(existing[k] for k in order)

    if new:
        path.parent.mkdir(parents=True, exist_ok=True)
        write_header = not path.exists()
        new.sort(key=lambda r: (r["date"], r["code"]))
        with path.open("a", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=FIELDS)
            if write_header:
                w.writeheader()
            w.writerows(new)
    return len(new), len(repairs)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--historical", action="store_true",
                    help="初回の遡り取得（sources.yaml の historical_pages 分）")
    args = ap.parse_args()

    cfg = yaml.safe_load((ROOT / "data" / "sources.yaml").read_text(encoding="utf-8"))
    master = yaml.safe_load((ROOT / "data" / "master.yaml").read_text(encoding="utf-8"))
    pol = cfg["fetch_policy"]
    days = pol["historical_days"] if args.historical else 0
    required = cfg["price"]["required_agreements"]
    operators = operators_of(cfg["price"]["chain"])

    all_rows: list[dict] = []
    failed: list[str] = []

    # 監視対象だけ取りに行く（watch: excluded は取得しない）。
    for s in Y.watched_stocks(master):
        code = s["code"]
        label = f"（遡り {days}営業日）" if days else "（直近）"
        print(f"取得中: {code} {s['name']}{label}")
        by_source: list[list[Bar]] = []
        seen_ops: set[str] = set()
        for entry in cfg["price"]["chain"]:
            pages = pages_for(entry, days)
            bars = fetch_source(code, entry, pol, pages)
            if bars:
                by_source.append(bars)
                seen_ops.add(operators.get(str(entry["id"]), str(entry["id"])))
            # **運営が異なる**取得元が必要数そろったら以降は試さない。
            # id の数で打ち切ると、同一運営の2媒体（株探・みんかぶ）で
            # 満足してしまい、独立した確認が1件も入らない。
            if len(seen_ops) >= required:
                break

        if not by_source:
            failed.append(code)
            continue

        rows = reconcile(code, by_source, required, operators)
        all_rows.extend(rows)
        ok = sum(1 for r in rows if r["status"] == "OK")
        print(f"  {len(rows)}営業日分（OK {ok} / 照合不一致・単独 {len(rows) - ok}）")

    now = datetime.now(JST).isoformat()
    added, fixed = append_only(ROOT / "data" / "prices" / "daily.csv",
                               all_rows, now)
    print(f"\n{added}件を追記（取得 {len(all_rows)}件・既存分は据え置き）")
    if fixed:
        print(f"{fixed}件を訂正（照合不成立→成立。data/revisions.csv に記録）")

    if failed:
        print(f"要確認: 全ソース取得失敗 {', '.join(failed)}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
