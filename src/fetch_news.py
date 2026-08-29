"""週次ニュース見出しの収集（銘柄別ニュース一覧 → JSON）。

取得元は sources.yaml の news チェーンのみ（自律的に追加しない）。
見出しは週次レポートの「素材」であって採用値ではない（2ソース照合を通らない）ため、
data/ の append-only 台帳には書かず、stdout / --out の JSON として渡すだけにする。
解釈・取捨選択はレポート側（人間と週次エントリ）が行う。

使い方:
  python src/fetch_news.py [--days 7] [--codes 3851,4073] [--out path.json]
    codes 省略時は data/master.yaml の全銘柄。out 省略時は stdout に JSON。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urljoin

try:  # ローカル(Windows)は SSL 検査プロキシ配下。CI(Linux) では不要
    import truststore

    truststore.inject_into_ssl()
except ImportError:
    pass

import requests
from bs4 import BeautifulSoup

sys.path.insert(0, str(Path(__file__).resolve().parent))

import yamlio as Y  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
JST = timezone(timedelta(hours=9))


# --- パース（純関数。ネットワークに触れない） -----------------------------

def _news_date(attr: str, text: str) -> str | None:
    """time 要素から YYYY-MM-DD を得る。

    第一候補は datetime 属性（ISO8601。例 2026-08-07T15:30:00+09:00）。
    属性が無い・壊れているときだけ表示テキスト（例 26/08/07 15:30）から読む。
    どちらも読めなければ None（その行はスキップ。推測で埋めない）。
    """
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", attr or "")
    if m:
        return m.group(0)
    m = re.match(r"(\d{2})/(\d{1,2})/(\d{1,2})", (text or "").strip())
    if m:
        y, mo, d = m.groups()
        return f"{2000 + int(y):04d}-{int(mo):02d}-{int(d):02d}"
    return None


def parse_news(html: str, entry: dict) -> list[dict]:
    """ニュース一覧テーブルを [{date, title, url, category}, ...] に読む。

    行の構造（2026-08-16 実地確認）:
      <tr><td class="news_time"><time datetime="ISO8601">yy/mm/dd hh:mm</time></td>
          <td><div class="newslist_ctg ...">開示|決算|材料|テク|注目|市況|…</div></td>
          <td><a href="...">見出し</a></td></tr>
    区分は閉じた語彙ではない（2026-08-16 実測で6種確認）。文字列のまま渡す。
    href は記事が相対（/stock/news?code=...）、開示PDFが絶対。base_url で絶対化する。
    セレクタが外れたら例外にせず空リストを返す（fetch.py の parse_ohlcv と同じ規律）。
    date か見出しが読めない行はスキップする（推測で埋めない）。
    """
    try:
        soup = BeautifulSoup(html, "html.parser")
    except Exception:  # noqa: BLE001  壊れた入力は「読めなかった」＝空
        return []
    table = soup.select_one(entry.get("table_selector") or "table.s_news_list")
    if table is None:
        return []

    base = entry.get("base_url") or "https://kabutan.jp/"
    items: list[dict] = []
    for tr in table.find_all("tr"):
        t = tr.find("time")
        d = _news_date(t.get("datetime") if t else "", t.get_text(strip=True) if t else "")
        a = tr.find("a", href=True)
        title = a.get_text(strip=True) if a else ""
        if not d or not title:
            continue
        ctg = tr.find(class_="newslist_ctg")
        category = (ctg.get_text(strip=True) or None) if ctg else None
        items.append({
            "date": d,
            "title": title,
            "url": urljoin(base, a["href"]),
            "category": category,
        })
    return items


def filter_recent(items: list[dict], days: int, today: date) -> list[dict]:
    """today を含む直近 days 日（暦日）の見出しだけ残す。

    days=7 なら today-6 〜 today。未来日付は対象外（時刻ずれの混入を弾く）。
    today は呼び出し側が渡す（テストが今日の日付に依存しないため）。
    """
    out: list[dict] = []
    for it in items:
        try:
            d = datetime.strptime(it["date"], "%Y-%m-%d").date()
        except (KeyError, ValueError):
            continue
        if 0 <= (today - d).days < days:
            out.append(it)
    return out


# --- 取得 -----------------------------------------------------------------

def fetch_stock_news(code: str, chain: list[dict], pol: dict) -> list[dict]:
    """1銘柄ぶんの見出しを news チェーンから取る（上から順に試行）。

    全滅したら最後の例外を投げる（呼び出し側が銘柄単位で errors に記録する）。
    """
    last_err: Exception | None = None
    for entry in chain:
        url = entry["url"].format(code=code)
        for attempt in range(pol["retries"] + 1):
            try:
                r = requests.get(
                    url,
                    headers={"User-Agent": pol["user_agent"]},
                    timeout=pol["timeout_sec"],
                )
                r.raise_for_status()
                items = parse_news(r.text, entry)
                if not items:
                    print(f"  [{code}] {entry['id']} 見出し0件"
                          f"（セレクタ不一致の可能性）: {url}", file=sys.stderr)
                time.sleep(pol["interval_sec"])
                return items
            except Exception as e:  # noqa: BLE001
                last_err = e
                if attempt == pol["retries"]:
                    print(f"  [{code}] {entry['id']} 取得失敗: {e}", file=sys.stderr)
        time.sleep(pol["interval_sec"])
    raise last_err if last_err else RuntimeError("news チェーンが空")


def main() -> int:
    ap = argparse.ArgumentParser(description="週次ニュース見出しの収集")
    ap.add_argument("--days", type=int, default=7,
                    help="今日を含む直近何日（暦日）を残すか")
    ap.add_argument("--codes", default="",
                    help="対象コード（カンマ区切り）。省略時は master.yaml の全銘柄")
    ap.add_argument("--out", default="", help="JSON の出力先。省略時は stdout")
    args = ap.parse_args()

    cfg = Y.safe_load((ROOT / "data" / "sources.yaml").read_text(encoding="utf-8"))
    pol = cfg["fetch_policy"]
    chain = cfg["news"]["chain"]

    if args.codes:
        codes = [c.strip() for c in args.codes.split(",") if c.strip()]
    else:
        master = Y.safe_load((ROOT / "data" / "master.yaml").read_text(encoding="utf-8"))
        codes = [str(s["code"]) for s in Y.watched_stocks(master)]

    today = datetime.now(JST).date()
    stocks: dict[str, list[dict]] = {}
    errors: dict[str, str] = {}
    for code in codes:
        stocks[code] = []
        try:
            items = fetch_stock_news(code, chain, pol)
            stocks[code] = filter_recent(items, args.days, today)
            print(f"  [{code}] 直近{args.days}日 {len(stocks[code])}件"
                  f"（ページ上 {len(items)}件）", file=sys.stderr)
        except Exception as e:  # noqa: BLE001  銘柄単位で止める。パイプラインは止めない
            errors[code] = f"{type(e).__name__}: {e}"

    payload = {
        "fetched_at": datetime.now(JST).isoformat(),
        "days": args.days,
        "stocks": stocks,
        "errors": errors,
    }
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        with out.open("w", encoding="utf-8", newline="") as f:
            f.write(text + "\n")
    else:
        print(text)

    if codes and len(errors) == len(codes):
        return 1        # 全銘柄失敗のときだけ異常終了
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
