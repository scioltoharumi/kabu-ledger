"""src/shape_chart.py のテスト（素の python で動く）。

守らせること:
  - 同じ終値列から同じ PNG バイト列（決定論・D8）
  - 語彙外の形状は拒否し、1件でも不正なら何も書かない
  - 画像が変わったら記録は「未判定（stale）」に戻る（古い判定を今の形に見せない）
  - 履歴は追記のみ。同内容の再登録は増えない
"""
from __future__ import annotations

import csv
import json
import sys
import tempfile
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import indicators as ind   # noqa: E402
import shape_chart as SC   # noqa: E402

_TMPDIRS: list[str] = []


def eq(a, b, label=""):
    assert a == b, f"{label}: expected {b!r}, got {a!r}"


def _closes(n: int = 126) -> list[float]:
    # 横ばい→急上昇（「急上昇」の形）。決定論的な合成列
    return [1000.0 + (i % 3) for i in range(90)] + [1000.0 + 20.0 * k for k in range(n - 90)]


def _bars(closes: list[float]) -> list[ind.Bar]:
    out = []
    for i, c in enumerate(closes):
        d = f"2026-{1 + i // 28:02d}-{1 + i % 28:02d}"
        out.append(ind.Bar(date=d, open=c, high=c + 1, low=c - 1, close=c, volume=1000))
    return out


def _png_dims(data: bytes) -> tuple[int, int]:
    assert data[:8] == b"\x89PNG\r\n\x1a\n", "PNG シグネチャ"
    w = int.from_bytes(data[16:20], "big")
    h = int.from_bytes(data[20:24], "big")
    return w, h


def test_render_is_deterministic_and_valid_png():
    a = SC.render_png(_closes())
    b = SC.render_png(_closes())
    eq(a, b, "同一入力→同一バイト列")
    eq(_png_dims(a), (SC.WIDTH, SC.HEIGHT), "寸法")
    # IDAT が解凍でき、生データ長が (幅×3+1)×高さ であること
    i = a.index(b"IDAT")
    n = int.from_bytes(a[i - 4:i], "big")
    raw = zlib.decompress(a[i + 4:i + 4 + n])
    eq(len(raw), (SC.WIDTH * 3 + 1) * SC.HEIGHT, "生ピクセル長")
    # 線の色が実際に描かれている
    assert bytes(SC.FG) in raw, "線が描かれていない"


def test_render_differs_when_series_differs():
    a = SC.render_png(_closes())
    c = list(reversed(_closes()))
    assert a != SC.render_png(c), "逆向きの列で同じ画像になってはいけない"


def test_window_closes_uses_last_window_and_skips_none():
    bars = _bars([100.0 + i for i in range(200)])
    bars[-1] = bars[-1]._replace(close=None)
    closes, as_of = SC.window_closes(bars)
    eq(len(closes), SC.WINDOW_DAYS - 1, "窓の中の None は除く")
    eq(as_of, bars[-2].date, "基準日は最後の採用終値の日付")


def _tmp_root() -> Path:
    d = tempfile.mkdtemp(prefix="shape_")
    _TMPDIRS.append(d)
    return Path(d)


def _fake_master_and_bars(monkey_closes: dict[str, list[float]]):
    """render_all / set_shapes が読む I/O を差し替える。"""
    master = {"stocks": [{"code": c, "name": f"銘柄{c}", "watch": "active"}
                         for c in monkey_closes]}
    orig_bars = SC.J.load_bars
    orig_master = SC.J.load_master
    SC.J.load_bars = lambda code: _bars(monkey_closes[code])
    SC.J.load_master = lambda: master
    return master, (orig_bars, orig_master)


def _restore(orig):
    SC.J.load_bars, SC.J.load_master = orig


def test_set_rejects_unknown_vocabulary_and_writes_nothing():
    root = _tmp_root()
    master, orig = _fake_master_and_bars({"1111": _closes()})
    try:
        SC.render_all(root, master)
        try:
            SC.set_shapes([("1111", "右肩上がり")], root)
            raise AssertionError("語彙外を受け付けた")
        except ValueError as e:
            assert "語彙外" in str(e), e
        assert not (root / SC.SHAPES_JSON).exists(), "不正な登録で shapes.json を作らない"
        assert not (root / SC.HISTORY_CSV).exists(), "履歴も書かない"
    finally:
        _restore(orig)


def test_set_records_and_stale_after_image_change():
    root = _tmp_root()
    series = {"1111": _closes(), "2222": list(reversed(_closes()))}
    master, orig = _fake_master_and_bars(series)
    try:
        rows = SC.render_all(root, master)
        eq([r["status"] for r in rows], ["none", "none"], "初回は未判定")
        lines = SC.set_shapes([("1111", "急上昇"), ("2222", "急落")], root)
        eq(len(lines), 2)
        data = json.loads((root / SC.SHAPES_JSON).read_text(encoding="utf-8"))
        eq(data["1111"]["shape"], "急上昇")
        eq(data["1111"]["as_of"], _bars(series["1111"])[-1].date, "基準日は最終足")
        eq(SC.image_status("1111", root)[2], "ok")
        # 同内容の再登録は履歴を増やさない
        SC.set_shapes([("1111", "急上昇")], root)
        with (root / SC.HISTORY_CSV).open(encoding="utf-8", newline="") as f:
            hist = list(csv.DictReader(f))
        eq(len(hist), 2, "履歴は2行のまま")
        # 判定を変えると履歴が1行増え、消えない
        SC.set_shapes([("1111", "上昇")], root)
        with (root / SC.HISTORY_CSV).open(encoding="utf-8", newline="") as f:
            hist = list(csv.DictReader(f))
        eq(len(hist), 3, "追記のみ")
        eq([h["shape"] for h in hist if h["code"] == "1111"], ["急上昇", "上昇"])
        # 終値列が変わる → 画像が変わる → stale（古い判定を今の形に見せない）
        series["1111"] = series["1111"][1:] + [series["1111"][-1] + 500.0]
        rows = SC.render_all(root, master)
        st = {r["code"]: r["status"] for r in rows}
        eq(st["1111"], "stale", "画像が変わった銘柄は未判定に戻る")
        eq(st["2222"], "ok", "変わっていない銘柄は判定済みのまま")
        eq(SC.image_status("1111", root)[0], None, "stale では形状を返さない")
    finally:
        _restore(orig)


def test_set_rejects_when_image_is_not_current():
    root = _tmp_root()
    series = {"1111": _closes()}
    master, orig = _fake_master_and_bars(series)
    try:
        SC.render_all(root, master)
        series["1111"] = list(reversed(series["1111"]))     # 描き直さずに終値だけ変わった
        try:
            SC.set_shapes([("1111", "急上昇")], root)
            raise AssertionError("古い画像への判定を受け付けた")
        except ValueError as e:
            assert "一致しない" in str(e), e
    finally:
        _restore(orig)


def test_insufficient_points_removes_image():
    root = _tmp_root()
    series = {"1111": _closes()}
    master, orig = _fake_master_and_bars(series)
    try:
        SC.render_all(root, master)
        assert (root / SC.IMAGE_DIR / "1111.png").exists()
        series["1111"] = _closes()[:50]                      # 採用終値が足りない
        rows = SC.render_all(root, master)
        eq(rows[0]["status"], "insufficient")
        assert not (root / SC.IMAGE_DIR / "1111.png").exists(), "古い画像を残さない"
        eq(SC.image_status("1111", root)[2], "noimage")
    finally:
        _restore(orig)


def test_vocabulary_is_the_nine_rakuten_shapes():
    eq(len(SC.SHAPES), 9)
    eq(len(set(SC.SHAPES)), 9, "重複なし")
    for w in ("上昇", "急上昇", "上昇ストップ", "調整", "もみ合い",
              "リバウンド", "急落", "下落", "下げとまった"):
        assert w in SC.SHAPES, w


def test_index_filter_keys_cover_every_shape():
    """一覧の絞り込みキー（build.SHAPE_KEYS）が9分類を漏れなく持ち、CSS に規則がある。"""
    import re
    import build as B      # noqa: WPS433  markdown が要るので遅延 import
    import style as S
    eq(set(B.SHAPE_KEYS), set(SC.SHAPES), "語彙とキーの対応が一致")
    keys = set(B.SHAPE_KEYS.values()) | {B.SHAPE_NONE_KEY}
    eq(len(keys), 10, "キーは9分類＋未判定")
    css = S.CSS if hasattr(S, "CSS") else S.css()
    for k in keys:
        assert re.search(rf"#f-sh-{k}:not\(:checked\)\)\s*tr\.sh-{k}", css), \
            f"形状キー {k} の絞り込み規則が style.py に無い"
    for k in B.SHAPE_KEYS.values():
        assert k in B.SHAPE_TONE, f"色調が未定義: {k}"


if __name__ == "__main__":
    import shutil
    tests = [(n, f) for n, f in sorted(globals().items())
             if n.startswith("test_") and callable(f)]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"  PASS  {name}")
        except BaseException as e:      # noqa: BLE001
            failed += 1
            print(f"  FAIL  {name}: {type(e).__name__}: {e}")
    for d in _TMPDIRS:
        shutil.rmtree(d, ignore_errors=True)
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
