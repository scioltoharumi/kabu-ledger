"""checks._git_cat_batch（git cat-file --batch）と `git show` 単発のパリティ検査。

`_git_baseline` と `check_report_updates_append_only` は HEAD の内容突合を
1プロセスの `git cat-file --batch` でまとめて読む（`git show` のプロセス起動代
約0.15秒 × ファイル数の節約）。バッチの読み違い——objectsize の数え間違い・
復号や改行正規化の差・missing 応答での対応ずれ——は追記性検査を**黙って**
素通りさせるので、git 管理下の実データ全対象ファイルについて、従来の
`git show` 単発と結果が完全一致することをここで確かめる。

実行:
  $env:PYTHONIOENCODING = "utf-8"; python tests/test_git_baseline.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import checks  # noqa: E402


def _context() -> tuple[Path, str]:
    top = checks._git_toplevel(str(ROOT.resolve()))
    assert top is not None, "git 管理下でない（このテストは実リポジトリで走らせる）"
    toplevel = Path(top)
    prefix = ROOT.resolve().relative_to(toplevel.resolve()).as_posix()
    prefix = "" if prefix == "." else f"{prefix}/"
    return toplevel, prefix


def _show(toplevel: Path, name: str) -> str | None:
    """従来経路（git show 1回）。パリティの比較基準。"""
    r = checks._git(["git", "show", name], toplevel)
    return r.stdout if r.returncode == 0 else None


def _baseline_names() -> tuple[Path, list[str]]:
    toplevel, prefix = _context()
    baseline = checks._git_baseline(ROOT)
    assert baseline is not None, "_git_baseline が None（HEAD が読めない）"
    rels = sorted(baseline.files)
    assert rels, "追記性検査の対象ファイルが0件（ls-files が壊れている）"
    return toplevel, [f"HEAD:{prefix}{rel}" for rel in rels]


def test_batch_matches_git_show_for_all_data_files():
    """HEAD の data/ 配下（追記性検査の全対象）でバッチと git show が一致する。"""
    toplevel, names = _baseline_names()
    batch = checks._git_cat_batch(toplevel, names)
    assert batch is not None, "cat-file --batch が異常終了した"
    for name in names:
        show = _show(toplevel, name)
        assert show is not None, f"git show が失敗: {name}"
        assert name in batch, (
            f"batch に {name} が無い（HEAD に在るのに missing 扱い）")
        assert batch[name] == show, (
            f"{name}: batch と git show の結果が一致しない"
            f"（batch {len(batch[name])}文字 / show {len(show)}文字）")


def test_batch_matches_git_show_for_reports():
    """check_report_updates_append_only が読む reports/*.md でも一致する。"""
    toplevel, prefix = _context()
    reports = ROOT / "reports"
    if not reports.exists() or not sorted(reports.glob("*.md")):
        print("    (reports/*.md が無いのでスキップ)")
        return
    names = [f"HEAD:{prefix}reports/{p.name}"
             for p in sorted(reports.glob("*.md"))]
    batch = checks._git_cat_batch(toplevel, names)
    assert batch is not None, "cat-file --batch が異常終了した"
    for name in names:
        show = _show(toplevel, name)
        if show is None:              # HEAD に無い＝新規レポートは対象外
            assert name not in batch, f"HEAD に無いのに batch が返した: {name}"
            continue
        assert name in batch, f"batch に {name} が無い"
        assert batch[name] == show, (
            f"{name}: batch と git show の結果が一致しない")


def test_batch_missing_entry_does_not_derail_the_stream():
    """missing 応答を挟んでも後続の対応（名前↔本文）がずれない。"""
    toplevel, names = _baseline_names()
    real = [names[0], names[-1]]
    ghost = "HEAD:data/__no_such_file__.csv"
    batch = checks._git_cat_batch(toplevel, [real[0], ghost, real[1]])
    assert batch is not None, "cat-file --batch が異常終了した"
    assert ghost not in batch, "存在しない名前が辞書に入っている"
    for name in real:
        assert name in batch and batch[name] == _show(toplevel, name), (
            f"missing を挟むと {name} の対応がずれる")


def test_batch_empty_input_returns_empty_dict():
    toplevel, _ = _context()
    assert checks._git_cat_batch(toplevel, []) == {}


def main() -> int:
    tests = [(name, fn) for name, fn in sorted(globals().items())
             if name.startswith("test_") and callable(fn)]
    failed: list[tuple[str, str]] = []
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
