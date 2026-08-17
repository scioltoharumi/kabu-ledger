"""「実データが1日進んだ」状況の回帰テスト。

## なぜこのファイルがあるか

週次取得で `daily.csv` に4行増えただけで、テストが全面的に落ちた。
原因はコードではなく**テストの書き方**だった。

  1. テストが実データを複製して使い、期待値に日付・値・件数をべた書きしていた
  2. **最新営業日は照合が成立せず `close` が空になる**（取得元によって当日分が
     載る時刻が違う。minkabu は翌日）のに、「最終行には終値がある」を
     暗黙に仮定していた

tests は weekly.yml の最初のジョブで、ここが落ちると取得もデプロイも動かない。
つまり「翌週かならず落ちるテスト」は、毎週パイプラインを止める仕掛けと同じである。

ここでは実データの末尾に**照合不成立の1日**を足した状態を作り、
検査・指標・判定・採点・生成のすべてがそのまま通ることを確認する。
実データ本体には触れない（一時ディレクトリに複製して壊す）。

## 確かめること

  - `checks.py` が FAIL 0（追記が「過去行の改変」と誤認されない）
  - 全指標が算出できる（確定した最後の日で計算される）
  - `judge` の結論が**1日進む前と一致する**（未確定の1日は判定を動かさない）
  - `score` が解決でき、集計基準日が壁時計ではなくデータから決まる
  - `build` が例外なく生成し、台帳の基準日が**未確定の日に飛ばない**
  - 2日続けて照合不成立でも同じ（片方の取得元が丸2日落ちた週）

実行:
  $env:PYTHONIOENCODING = "utf-8"; python tests/test_data_advance.py
"""
from __future__ import annotations

import re
import shutil
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import checks  # noqa: E402
import indicators as ind  # noqa: E402
import judge as J  # noqa: E402
import realdata as rd  # noqa: E402
import score as S  # noqa: E402

_TMPDIRS: list[Path] = []

# ROOT を差し替える対象。build は import 時に DOCS を、report は REPORTS を、
# chartdata は DATA を組み立てているので、それぞれ別に差し替える。
_PATCH_TARGETS = (
    ("judge", {"ROOT": ""}),
    ("build", {"ROOT": "", "DOCS": "docs", "STAMPS": "scoring/stamps.json"}),
    # STAMPS は import 時に実リポジトリを指したまま固定される。ROOT を差し替えても
    # 追随せず、判定が変わった週に build.main() が実リポジトリの
    # scoring/stamps.json を書き換える。data/ ではないので append-only 検査には
    # 掛からず、git status に理由の分からない差分として出るだけになる。
    ("report", {"ROOT": "", "REPORTS": "reports"}),
    ("chartdata", {"ROOT": "", "DATA": "data"}),
    ("verification", {"ROOT": ""}),
    ("fetch_source", {"ROOT": "", "REPORTS": "reports",
                      "FETCH_LOG": "data/verification/fetch_log.csv"}),
    # log_fetch() は fetch_source.py の CLI からしか呼ばれず build.main() は
    # 書かない。いまは予防。書く経路が生えたときに気づけるよう先に差し替えておく。
)

# 実リポジトリを指したままで構わないモジュール（読むだけ・Path 定数を持たない）。
_UNPATCHED_OK = {"checks", "fetch", "fetch_fundamentals", "fetch_index",
                 "fetch_margin", "fetch_tanshin", "notify", "revise",
                 "score", "chart", "indicators", "style", "yamlio",
                 # estimate は読み取り専用で、パスを使う全 API が root= 注入可
                 # （build.py / checks.py は呼び出し時に root を渡す）。
                 # ROOT 定数は CLI の既定値にすぎない
                 "estimate"}


# =============================================================================
# ヘルパ
# =============================================================================

def eq(actual, expected, label=""):
    assert actual == expected, f"{label}: expected {expected!r}, got {actual!r}"


def repo_copy() -> Path:
    """実リポジトリを一時ディレクトリに複製する。

    `src/` も複製する。検査のなかには「根拠に挙げたリポジトリ内のファイルが
    実在するか」を見るものがあり、data/ と reports/ だけを複製すると
    **進めたかどうかと無関係に**それが落ちる。
    """
    base = Path(tempfile.mkdtemp(prefix="kabu-advance-"))
    _TMPDIRS.append(base)
    shutil.copytree(ROOT / "data", base / "data")
    for name in ("reports", "predictions", "scoring", "theses", "bear", "src"):
        if (ROOT / name).exists():
            shutil.copytree(ROOT / name, base / name,
                            ignore=shutil.ignore_patterns("__pycache__"))
    (base / "docs").mkdir(exist_ok=True)
    return base


def advance(base: Path, days: int = 1, confirmed: bool | None = None) -> list[str]:
    """複製したリポジトリの株価・指数を `days` 営業日ぶん進める。

    `confirmed=False` が今回の事故と同じ形（照合不成立の行が末尾に積まれる）。
    指数は既定（`None`）で系列ごとの実態に合わせる。growth250 は第2ソースが
    無いので、無理に「照合成立」にはしない。
    """
    added: list[str] = []
    files = [(base / "data" / "prices" / "daily.csv", confirmed)]
    idx = base / "data" / "indices"
    files += [(p, None if confirmed is None else confirmed)
              for p in sorted(idx.glob("*.csv"))] if idx.exists() else []
    day = rd.latest_date()
    for _ in range(days):
        day = rd.next_business_day(day)
        added.append(day)
        for path, shape in files:
            fields, rows = rd.read_csv(path)
            if not rows:
                continue
            want = shape
            if path.name != "daily.csv" and confirmed is False:
                want = False
            new = rd.advance_rows(rows, day, confirmed=want)
            rd.write_csv(path, fields, rows + new)
    return added


@contextmanager
def patched_root(base: Path):
    """`ROOT` を持つモジュールを一時的に複製リポジトリに向ける。

    build / judge / chartdata は import 時にパスを組み立てるので、
    ここで差し替えないと**実リポジトリの docs/ を上書きしてしまう**。
    """
    saved: list[tuple[object, str, object]] = []
    for name, attrs in _PATCH_TARGETS:
        mod = sys.modules.get(name)
        if mod is None:
            try:
                mod = __import__(name)
            except ImportError:
                continue
        for attr, rel in attrs.items():
            if not hasattr(mod, attr):
                continue
            saved.append((mod, attr, getattr(mod, attr)))
            setattr(mod, attr, base / rel if rel else base)
    try:
        yield
    finally:
        for mod, attr, old in reversed(saved):
            setattr(mod, attr, old)


def run_checks(base: Path, with_baseline: bool = True) -> checks.Report:
    """複製に対して全検査を走らせる。ベースラインは**進める前の実データ**。"""
    baseline = checks._dir_baseline(base / "data", ROOT / "data") \
        if with_baseline else None
    return checks.run_checks(base / "data", baseline, False)


def fails(rep: checks.Report) -> set[str]:
    return {r.line() for r in rep.results if r.level == checks.FAIL}


_BASELINE_FAILS: list[set[str]] = []


def baseline_fails() -> set[str]:
    """**進める前**の複製で出る FAIL。

    このファイルが答えるのは「1日進めたせいで落ちるか」であって、
    「いま実データが全検査に通っているか」ではない（後者は
    `test_checks.test_real_data_has_no_fail` の担当）。両方をここで見ると、
    別の作業で一時的に出ている FAIL がこの回帰テストを巻き込んで落とす。
    """
    if not _BASELINE_FAILS:
        _BASELINE_FAILS.append(fails(run_checks(repo_copy())))
    return _BASELINE_FAILS[0]


def new_fails(base: Path) -> set[str]:
    """進めたことで**新しく**出た FAIL。"""
    return fails(run_checks(base)) - baseline_fails()


def price_rows(base: Path, code: str) -> list[dict]:
    _, rows = rd.read_csv(base / "data" / "prices" / "daily.csv")
    return sorted((r for r in rows if r["code"] == code),
                  key=lambda r: r["date"])


def last_confirmed(base: Path, code: str) -> str:
    ds = [r["date"] for r in price_rows(base, code) if str(r["close"] or "").strip()]
    assert ds, f"{code}: 採用値のある行が無い"
    return max(ds)


# =============================================================================
# 1. 検査（checks.py）
# =============================================================================

def test_unconfirmed_new_day_produces_no_new_fail():
    """★今回の事故そのもの。照合不成立の1日が積まれても FAIL は増えない。"""
    base = repo_copy()
    day = advance(base, 1, confirmed=False)[0]
    eq(new_fails(base), set(), f"{day} を追記して FAIL が増えた")


def test_confirmed_new_day_produces_no_new_fail():
    base = repo_copy()
    advance(base, 1, confirmed=True)
    eq(new_fails(base), set(), "照合成立の1日を追記して FAIL が増えた")


def test_two_unconfirmed_days_produce_no_new_fail():
    """片方の取得元が丸2日落ちた週。末尾に未確定行が2本積まれる。"""
    base = repo_copy()
    advance(base, 2, confirmed=False)
    eq(new_fails(base), set(), "未確定2日で FAIL が増えた")


def test_new_day_is_not_reported_as_rewrite():
    """追記が「過去行が変更されている」と誤認されないこと。"""
    base = repo_copy()
    advance(base, 1, confirmed=False)
    rep = run_checks(base)
    bad = [r.line() for r in rep.results
           if r.check == "append_only" and r.level == checks.FAIL]
    eq(bad, [], "追記が改変として報告されている")
    # 「追記が0件」の WARN も出てはいけない（実際に増えている）
    zero = [r.line() for r in rep.results
            if r.check == "append_only" and "追記が0件" in r.message]
    eq(zero, [], "追記したのに0件と報告されている")


def test_new_day_is_covered_for_every_code():
    """1銘柄だけ取り込み損ねた週は、逆に FAIL として見えること（対照実験）。

    「FAIL 0 になった」が**検査が動いていないから**ではないことの担保。
    """
    base = repo_copy()
    day = advance(base, 1, confirmed=False)[0]
    path = base / "data" / "prices" / "daily.csv"
    fields, rows = rd.read_csv(path)
    dropped = rd.codes()[0]
    rd.write_csv(path, fields,
                 [r for r in rows
                  if not (r["date"] == day and r["code"] == dropped)])
    rep = run_checks(base)
    hit = [r.line() for r in rep.results
           if r.level == checks.FAIL and r.check == "coverage"
           and dropped in r.message]
    assert hit, f"{dropped} の取得漏れが検出されていない: {sorted(fails(rep))}"


# =============================================================================
# 2. 指標（indicators.py）
# =============================================================================

def _all_indicators(bars) -> dict:
    closes = [b.close for b in bars]
    highs = [b.high for b in bars]
    lows = [b.low for b in bars]
    volumes = [b.volume for b in bars]
    weekly = ind.to_weekly(bars)
    wma = ind.sma_series([w.close for w in weekly], ind.WEEKLY_MA_MID_PERIODS)
    ich = ind.ichimoku(highs, lows, closes)
    return {
        "sma25": ind.sma(closes, ind.DAILY_MA_MID_PERIODS),
        "ma_deviation_pct": ind.ma_deviation_pct(closes),
        "rsi14": ind.rsi(closes),
        "cloud_top": ich.cloud_top,
        "position": ich.position,
        "volume_ratio": ind.volume_ratio(volumes),
        "avg_turnover_20d": ind.avg_turnover(closes, volumes),
        "weekly_slope": ind.slope_pct(wma, ind.SLOPE_LOOKBACK_WEEKS),
    }


def test_indicators_survive_an_unconfirmed_day():
    """未確定の1日が末尾に積まれても、全指標が算出できる。

    `sma` は窓に欠測が1つでもあると None を返すので、末尾を落とさなければ
    **全指標がまとめて消える**。これが「1日進むと台帳が空になる」の正体だった。
    """
    base = repo_copy()
    advance(base, 2, confirmed=False)
    for code in rd.codes():
        bars = ind.bars_from_rows(price_rows(base, code), code=code)
        eq(bars[-1].date, last_confirmed(base, code),
           f"{code}: 指標の基準日は確定した最後の日")
        for name, value in _all_indicators(bars).items():
            assert value is not None, f"{code}: {name} が算出できていない"


def test_indicators_are_unchanged_by_an_unconfirmed_day():
    """未確定の1日は指標を1ミリも動かさない（採用値に格上げしていない担保）。"""
    before = repo_copy()
    after = repo_copy()
    advance(after, 1, confirmed=False)
    for code in rd.codes():
        a = _all_indicators(ind.bars_from_rows(price_rows(before, code), code=code))
        b = _all_indicators(ind.bars_from_rows(price_rows(after, code), code=code))
        eq(b, a, f"{code}: 未確定の1日で指標が動いた")


# =============================================================================
# 3. 判定（judge.py）
# =============================================================================

def test_judge_is_unchanged_by_an_unconfirmed_day():
    """判定も動かない。基準日も確定した最後の日のまま。"""
    before = repo_copy()
    after = repo_copy()
    advance(after, 1, confirmed=False)

    with patched_root(before):
        a = J.judge_all()
    with patched_root(after):
        b = J.judge_all()

    eq([v.code for v in b], rd.codes(), "証券コード順")
    eq(b, a, "未確定の1日で判定が変わった")
    for v in b:
        eq(v.as_of, last_confirmed(after, v.code), f"{v.code}: 基準日")


def test_judge_moves_when_the_new_day_is_confirmed():
    """対照実験: 照合が成立した日を足せば基準日は進む。

    「変わらない」を確かめるテストは、**変わるはずのときに変わること**を
    同時に見ておかないと、判定が固まっているだけでも通ってしまう。
    """
    after = repo_copy()
    day = advance(after, 1, confirmed=True)[0]
    with patched_root(after):
        verdicts = J.judge_all()
    for v in verdicts:
        eq(v.as_of, day, f"{v.code}: 基準日が新しい確定日に進む")


# =============================================================================
# 4. 採点（score.py）
# =============================================================================

def test_score_survives_an_unconfirmed_day():
    base = repo_copy()
    day = advance(base, 1, confirmed=False)[0]
    repo = S.Repo(root=base)
    eq(repo.data_as_of(), day, "集計基準日はデータの最終営業日（壁時計ではない）")

    as_of = last_confirmed(base, rd.codes()[0])
    price_metrics = [m.name for m in S.CATALOG if m.source == S.SRC_PRICE]
    for code in rd.codes():
        for name in price_metrics:
            mv = S.resolve_metric(code, name, as_of, repo)
            assert mv.value is not None, f"{code} {name} が未計算: {mv.detail}"


def test_score_metric_values_are_unchanged_by_an_unconfirmed_day():
    before = repo_copy()
    after = repo_copy()
    advance(after, 1, confirmed=False)
    as_of = last_confirmed(before, rd.codes()[0])
    r1, r2 = S.Repo(root=before), S.Repo(root=after)
    for code in rd.codes():
        for m in (x.name for x in S.CATALOG if x.source == S.SRC_PRICE):
            eq(S.resolve_metric(code, m, as_of, r2),
               S.resolve_metric(code, m, as_of, r1),
               f"{code} {m}: 未確定の1日で採点値が動いた")


# =============================================================================
# 5. 生成（build.py）
# =============================================================================

def _build(base: Path) -> None:
    import build as B
    with patched_root(base):
        eq(B.main(), 0, "build.main() の終了コード")


def test_build_survives_an_unconfirmed_day():
    base = repo_copy()
    advance(base, 1, confirmed=False)
    _build(base)
    index = base / "docs" / "index.html"
    assert index.exists() and index.stat().st_size > 0, "index.html が生成されていない"


def test_ledger_as_of_stays_on_the_confirmed_day():
    """台帳の基準日が**未確定の日に飛ばない**。

    日付だけ新しく見えて、中の指標は前日で計算されている——という
    読み手を誤らせる状態を作らないこと（build.as_of_date のねらい）。
    """
    import build as B
    base = repo_copy()
    day = advance(base, 1, confirmed=False)[0]
    with patched_root(base):
        as_of = B.as_of_date()
    assert as_of != day, f"未確定の {day} が基準日になっている"
    eq(as_of, max(last_confirmed(base, c) for c in rd.codes()),
       "基準日は確定した最後の営業日")


def test_build_generates_the_same_pages():
    """生成されるページの顔ぶれが未確定の1日で変わらない。

    **ページのバイト一致までは要求しない。** 台帳は「記録した営業日のうち
    何日ぶんに採用終値があるか」を出しており、未確定の1日が増えれば
    その**分母は増えるのが正しい**。動いてはいけないのは中身の数値のほうで、
    それは図（`test_chart_values_are_unchanged_by_an_unconfirmed_day`）・
    指標・判定・採点の各テストが個別に押さえている。
    """
    before = repo_copy()
    after = repo_copy()
    advance(after, 1, confirmed=False)
    _build(before)
    _build(after)
    pages = sorted(p.relative_to(before / "docs").as_posix()
                   for p in (before / "docs").rglob("*.html"))
    eq(sorted(p.relative_to(after / "docs").as_posix()
              for p in (after / "docs").rglob("*.html")), pages,
       "生成されたページの顔ぶれが違う")
    assert pages, "ページが1枚も生成されていない"


def test_chart_values_are_unchanged_by_an_unconfirmed_day():
    """図の数値が未確定の1日で動かない（照合を通っていない値を使っていない・D30）。"""
    import chartdata as CD
    import report as R

    before = repo_copy()
    after = repo_copy()
    advance(after, 1, confirmed=False)

    checked = 0
    for code in rd.codes():
        with patched_root(before):
            rep = R.load_report(code)
            a = CD.resolve_charts(code, rep.charts) if rep else None
        with patched_root(after):
            rep2 = R.load_report(code)
            b = CD.resolve_charts(code, rep2.charts) if rep2 else None
        if a is None:
            continue
        checked += 1
        eq(sorted(b), sorted(a), f"{code}: 図の顔ぶれが変わった")
        for cid in sorted(a):
            eq(b[cid].spec, a[cid].spec, f"{code}/{cid}: 図の数値が動いた")
            eq(b[cid].origin, a[cid].origin, f"{code}/{cid}: 図の出所が変わった")
    assert checked, "レポートが1件も読めておらず、図を検査できていない"


# =============================================================================
# 6. 再発防止（規約を検査に変える）
# =============================================================================

def _code_lines(text: str):
    """コメントを落とした行を返す（説明文の日付まで咎めないため）。"""
    for i, line in enumerate(text.splitlines(), 1):
        body = line.split("#", 1)[0]
        if body.strip():
            yield i, body


def test_tests_do_not_hardcode_todays_latest_business_day():
    """テストに**いまの最新営業日**を書かない。

    「データが1日進むと全部落ちる」の直接の原因がこれだった。最新営業日は
    週次で必ず変わるので、書いた時点で**来週落ちることが確定する**。
    必要なら `realdata.latest_date()` から引くこと。

    コメント行は対象外（合成データの由来を説明するために実在の日付を
    書くことはある）。どうしても書く必要がある行には `# 実データ非依存`
    のように理由をコメントで残せば、この検査の対象から外れる。

    ★**日付リテラルとしての一致だけを咎める。** 単純な部分一致にすると
    fixture の `fetched_at`（"2026-08-14T18:30:00+09:00"）まで拾ってしまい、
    実データが追いついた週に**この検査自身が「翌週かならず落ちるテスト」**に
    なる（weekly.yml:41 が禁じているもの）。前後に数字・T・時刻区切りが
    続く場合は日付リテラルではないので対象外にする。
    """
    latest = rd.latest_date()
    pattern = re.compile(r"(?<![0-9T:\-])" + re.escape(latest) + r"(?![0-9T])")
    offenders = []
    for path in sorted((ROOT / "tests").glob("test_*.py")):
        text = path.read_text(encoding="utf-8")
        for lineno, body in _code_lines(text):
            if pattern.search(body):
                offenders.append(f"{path.name}:{lineno}: {body.strip()[:80]}")
    assert not offenders, (
        f"最新営業日 {latest} をべた書きしている箇所がある"
        f"（realdata から引くこと）: {offenders}")


def test_realdata_helper_is_not_collected_as_a_test():
    """`realdata.py` はヘルパであってテストではない。

    weekly.yml は `tests/test_*.py` を1本ずつ `python` で実行する。
    ヘルパを `test_` で始まる名前にすると、テストとして実行されて
    「0件パス」で通ってしまう（気づかないまま検査が消える）。
    """
    helper = ROOT / "tests" / "realdata.py"
    assert helper.exists(), "tests/realdata.py が無い"
    assert not helper.name.startswith("test_"), \
        "ヘルパが test_ で始まっている（weekly.yml に拾われる）"


def test_every_root_derived_path_is_patched():
    """ROOT 派生の module-level Path が差し替え対象から漏れていないか。

    漏れると、テストが**実リポジトリのファイルを書き換える**。data/ 以外なので
    append-only 検査には掛からず、`git status` に理由不明の差分が出るだけになり、
    原因調査に往復を使う。実際に build.STAMPS がこの穴だった。

    score.py は S.Repo(root=base) で注入しているので module-level Path を持たない。
    新しいモジュールはこの形にすれば、差し替え自体が要らなくなる。
    """
    covered = {name: set(attrs) for name, attrs in _PATCH_TARGETS}
    for name in covered:                       # 遅延 import のものを確実に読む
        if name not in sys.modules:
            __import__(name)
    missing = []
    for path in sorted((ROOT / "src").glob("*.py")):
        name = path.stem
        if name in _UNPATCHED_OK:
            continue
        mod = sys.modules.get(name)
        if mod is None:
            continue                           # 読み込んでいない = 影響しない
        for attr in dir(mod):
            if not attr.isupper():
                continue
            v = getattr(mod, attr)
            if not isinstance(v, Path) or attr in covered.get(name, set()):
                continue
            try:
                v.resolve().relative_to(ROOT)
            except ValueError:
                continue
            missing.append(f"{name}.{attr} = {v}")
    assert not missing, (
        f"差し替え漏れの Path 定数がある: {missing}。"
        " _PATCH_TARGETS に足すか、_UNPATCHED_OK に理由つきで載せること")


# =============================================================================
# 実行
# =============================================================================

def main() -> int:
    tests = [(n, f) for n, f in sorted(globals().items())
             if n.startswith("test_") and callable(f)]
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

    for d in _TMPDIRS:
        shutil.rmtree(d, ignore_errors=True)

    print(f"\n{len(tests) - len(failed)}/{len(tests)} passed")
    if failed:
        for name, msg in failed:
            print(f"  - {name}: {msg}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
