"""チャート形状（楽天 iSPEED / マーケットスピード II「チャート形状検索」の9分類）。

**描画はコード、分類は画像を見た Claude（人間の確認つき）。** 2026-09-05 のマスター決定で
「判定はコード」の不変条件に唯一の例外として認められた（BACKLOG.md 改訂履歴）。

  - 楽天は分類アルゴリズムを公開していない（「独自の分類方法」とだけ記載）。
    数式で近似すると「急上昇 vs 上昇」「調整 vs 上昇」の境界で目視とずれるため、
    楽天と同じ**形の類似**で分類する。台帳が同じ見た目の画像を作り、それを見て
    9分類のどれに最も近いかを記録する
  - 分類は**ゲートに使わない**。台帳では「画像判定※」と明示し、参考情報として出す
  - 揺れ対策: 判定した画像の SHA-256 を一緒に記録し、画像が変わったら「未判定」に戻す。
    判定履歴は scoring/shapes_history.csv に**追記のみ**で残す
  - 決定論（D8）: PNG は標準ライブラリだけで書き、同じ終値列から同じバイト列を出す。
    生成時刻は入れない。基準日は最終足の日付

使い方（週次ルーティン / intake の履歴取得のあと）:

  python src/shape_chart.py               # 監視中の全銘柄の画像を描き、未判定を列挙
  python src/shape_chart.py --pending     # 未判定（画像が変わった銘柄）だけ列挙
  （scoring/shapes/{code}.png を Read で見て、9分類のどれに最も近いかを決める）
  python src/shape_chart.py --set 4073=急上昇 6570=上昇   # 記録（語彙外は拒否）

9分類（楽天のアイコンの形。窓の前半→後半）:

  上昇ストップ  上がってから横ばい        ／  上昇      一直線に右肩上がり
  急上昇        横ばいのあと急な上げ      ／  調整      上がってから下げ
  もみ合い      上下に細かく振れて横ばい  ／  リバウンド 下げてから上げ
  急落          横ばいのあと急な下げ      ／  下落      一直線に右肩下がり
  下げとまった  下げてから横ばい
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import struct
import sys
import zlib
from pathlib import Path
from typing import Sequence

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))

import judge as J        # noqa: E402  load_master / load_bars（I/O はここだけ）
import yamlio as Y       # noqa: E402

SHAPES: tuple[str, ...] = (
    "上昇ストップ", "上昇", "急上昇",
    "調整", "もみ合い", "リバウンド",
    "急落", "下落", "下げとまった",
)
WINDOW_DAYS = 126        # 楽天の「6か月」を日足の営業日数で近似（21日×6）
MIN_POINTS = 100         # これ未満（採用終値が8割を切る）なら形を出さない
WIDTH, HEIGHT, PAD = 320, 180, 14
BG = (58, 58, 58)        # 楽天のアイコンと同じ見た目にする（比較しやすさのため）
FG = (31, 184, 176)
MARK = "画像判定※"        # 台帳での表示語。コードの機械判定ではないことを明示する

SHAPES_JSON = "scoring/shapes.json"
HISTORY_CSV = "scoring/shapes_history.csv"
IMAGE_DIR = "scoring/shapes"
HISTORY_COLUMNS = ("as_of", "code", "shape", "image_sha256")


# =============================================================================
# 描画（標準ライブラリのみ・決定論的）
# =============================================================================

def _png(width: int, height: int, pixels: bytearray) -> bytes:
    """RGB の生ピクセル列を PNG にする。zlib のレベルを固定し同一入力→同一出力。"""
    def chunk(tag: bytes, body: bytes) -> bytes:
        return (struct.pack(">I", len(body)) + tag + body
                + struct.pack(">I", zlib.crc32(tag + body) & 0xFFFFFFFF))
    stride = width * 3
    raw = b"".join(b"\x00" + bytes(pixels[y * stride:(y + 1) * stride])
                   for y in range(height))
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 9))
            + chunk(b"IEND", b""))


def _line(px: bytearray, w: int, h: int, x0: int, y0: int, x1: int, y1: int,
          rgb: tuple[int, int, int]) -> None:
    """Bresenham。太さ2px（右下に1px重ねる）。"""
    dx, dy = abs(x1 - x0), -abs(y1 - y0)
    sx, sy = (1 if x0 < x1 else -1), (1 if y0 < y1 else -1)
    err = dx + dy
    while True:
        for ox, oy in ((0, 0), (1, 0), (0, 1), (1, 1)):
            x, y = x0 + ox, y0 + oy
            if 0 <= x < w and 0 <= y < h:
                i = (y * w + x) * 3
                px[i:i + 3] = bytes(rgb)
        if x0 == x1 and y0 == y1:
            break
        e2 = 2 * err
        if e2 >= dy:
            err += dy
            x0 += sx
        if e2 <= dx:
            err += dx
            y0 += sy


def render_png(closes: Sequence[float], width: int = WIDTH, height: int = HEIGHT) -> bytes:
    """終値列を軸なしの折れ線1本にする。縦は窓内の高安で正規化（楽天のアイコンと同じ）。"""
    n = len(closes)
    if n < 2:
        raise ValueError("2点以上必要")
    px = bytearray(bytes(BG) * (width * height))
    lo, hi = min(closes), max(closes)
    span = hi - lo
    pts = []
    for i, c in enumerate(closes):
        x = PAD + round((width - 1 - 2 * PAD) * i / (n - 1))
        y = (height // 2 if span == 0
             else PAD + round((height - 1 - 2 * PAD) * (hi - c) / span))
        pts.append((x, y))
    for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
        _line(px, width, height, x0, y0, x1, y1, FG)
    return _png(width, height, px)


def window_closes(bars) -> tuple[list[float], str | None]:
    """直近 WINDOW_DAYS 本の日足から採用終値だけを抜く。(終値列, 最終採用日)。"""
    tail = list(bars)[-WINDOW_DAYS:]
    pts = [(b.date, b.close) for b in tail if b.close is not None]
    if not pts:
        return [], None
    return [c for _, c in pts], pts[-1][0]


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# =============================================================================
# 記録（scoring/shapes.json ＋ 追記のみの履歴）
# =============================================================================

def load_shapes(root: Path = ROOT) -> dict:
    path = root / SHAPES_JSON
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    return data if isinstance(data, dict) else {}


def _write_shapes(data: dict, root: Path) -> None:
    path = root / SHAPES_JSON
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                    encoding="utf-8", newline="\n")


def _append_history(row: dict, root: Path) -> bool:
    """同じ内容が末尾（同銘柄の最新行）に無いときだけ1行足す。消さない。"""
    path = root / HISTORY_CSV
    path.parent.mkdir(parents=True, exist_ok=True)
    last: dict | None = None
    if path.exists():
        with path.open(encoding="utf-8", newline="") as f:
            for r in csv.DictReader(f):
                if r.get("code") == row["code"]:
                    last = r
    if last and all(last.get(k) == row[k] for k in HISTORY_COLUMNS):
        return False
    new = not path.exists()
    with path.open("a", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=HISTORY_COLUMNS, lineterminator="\n")
        if new:
            w.writeheader()
        w.writerow({k: row[k] for k in HISTORY_COLUMNS})
    return True


def image_status(code: str, root: Path = ROOT) -> tuple[str | None, str | None, str]:
    """(形状, 基準日, 状態)。状態は ok / stale（画像が判定時と違う）/ none（記録なし）/
    noimage（画像が無い）。build.py が表示に使う。"""
    img = root / IMAGE_DIR / f"{code}.png"
    if not img.exists():
        return None, None, "noimage"
    rec = load_shapes(root).get(code)
    if not rec:
        return None, None, "none"
    if rec.get("image_sha256") != sha256(img.read_bytes()):
        return None, rec.get("as_of"), "stale"
    return rec.get("shape"), rec.get("as_of"), "ok"


def render_all(root: Path = ROOT, master: dict | None = None) -> list[dict]:
    """監視中の全銘柄の画像を描く。返り値は1銘柄1行の状態（表示・--pending 用）。"""
    m = master or J.load_master()
    out_dir = root / IMAGE_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    rows = []
    for s in sorted(Y.watched_stocks(m), key=lambda x: str(x["code"])):
        code = str(s["code"])
        closes, as_of = window_closes(J.load_bars(code))
        path = out_dir / f"{code}.png"
        if len(closes) < MIN_POINTS:
            if path.exists():
                path.unlink()             # 古い画像を残すと古い判定が ok に見える
            rows.append({"code": code, "name": s.get("name", ""), "as_of": as_of,
                         "points": len(closes), "sha": None, "status": "insufficient",
                         "shape": None})
            continue
        png = render_png(closes)
        if not path.exists() or path.read_bytes() != png:
            path.write_bytes(png)
        shape, _, status = image_status(code, root)
        rows.append({"code": code, "name": s.get("name", ""), "as_of": as_of,
                     "points": len(closes), "sha": sha256(png), "status": status,
                     "shape": shape})
    return rows


def set_shapes(pairs: Sequence[tuple[str, str]], root: Path = ROOT) -> list[str]:
    """`code=形状` を記録する。語彙外・画像なしは拒否（1件でも不正なら何も書かない）。"""
    data = load_shapes(root)
    staged = []
    for code, shape in pairs:
        if shape not in SHAPES:
            raise ValueError(f"{code}: 語彙外の形状 {shape!r}。使えるのは {' / '.join(SHAPES)}")
        img = root / IMAGE_DIR / f"{code}.png"
        if not img.exists():
            raise ValueError(f"{code}: 画像 {IMAGE_DIR}/{code}.png が無い"
                             "（先に python src/shape_chart.py で描く）")
        closes, as_of = window_closes(J.load_bars(code))
        digest = sha256(img.read_bytes())
        if len(closes) >= MIN_POINTS and sha256(render_png(closes)) != digest:
            raise ValueError(f"{code}: 画像が現在の終値列と一致しない"
                             "（python src/shape_chart.py で描き直してから判定する）")
        staged.append({"code": code, "shape": shape, "as_of": as_of or "",
                       "image_sha256": digest})
    lines = []
    for row in staged:
        data[row["code"]] = {"as_of": row["as_of"], "shape": row["shape"],
                             "image_sha256": row["image_sha256"]}
        added = _append_history(row, root)
        lines.append(f"{row['code']} {row['shape']}（基準日 {row['as_of']}）"
                     + ("" if added else "・履歴は同内容のため追記なし"))
    _write_shapes(data, root)
    return lines


# =============================================================================
# CLI
# =============================================================================

def _parse_pairs(items: Sequence[str]) -> list[tuple[str, str]]:
    pairs = []
    for it in items:
        if "=" not in it:
            raise ValueError(f"--set の形式は CODE=形状: {it!r}")
        code, shape = it.split("=", 1)
        pairs.append((code.strip(), shape.strip()))
    return pairs


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="チャート形状（6か月・9分類）の描画と記録")
    ap.add_argument("--set", nargs="+", metavar="CODE=形状",
                    help="画像を見て決めた形状を記録する（語彙は SHAPES）")
    ap.add_argument("--pending", action="store_true", help="未判定の銘柄だけ出す")
    args = ap.parse_args(argv)

    if args.set:
        try:
            for line in set_shapes(_parse_pairs(args.set)):
                print(line)
        except ValueError as e:
            print(f"[ERROR] {e}")
            return 1
        return 0

    rows = render_all()
    label = {"ok": "判定済", "stale": "未判定（画像が更新された）", "none": "未判定",
             "insufficient": "描けない（採用終値が不足）"}
    pending = [r for r in rows if r["status"] in ("stale", "none")]
    show = pending if args.pending else rows
    print(f"チャート形状（直近{WINDOW_DAYS}営業日・採用終値のみ）: "
          f"{len(rows)}銘柄・未判定 {len(pending)}")
    for r in show:
        shape = f" {r['shape']}" if r["shape"] else ""
        print(f"  {r['code']} {str(r['name'])[:8]:<8} 基準日 {r['as_of'] or '—'} "
              f"点数 {r['points']:>3}  {label[r['status']]}{shape}")
    if pending:
        print(f"\n{IMAGE_DIR}/<code>.png を見て、次の語彙のどれに最も近いかを記録する:")
        print("  " + " / ".join(SHAPES))
        print("  python src/shape_chart.py --set "
              + " ".join(f"{r['code']}=形状" for r in pending))
    return 0


if __name__ == "__main__":
    sys.exit(main())
