"""裏取り記録（data/verification/{code}.yaml）の読み込みと検査の回帰テスト。

ここは **ネットワークを使わない**。`verification.parse()` は純関数で、
`checks.check_verification()` は入出力がディレクトリだけなので、
一時ディレクトリに合成のレポートと記録を置いて検証できる。

なぜこのファイルが要るか:
  裏取りの仕組みは「記録があること」ではなく「**記録が裏取りとして成立していること**」
  を保証しないと意味がない。再取得していないURLで supported を出す、evidence を
  空にする、出典と食い違うと判定した記述を本文に残す——このどれもが、
  形式だけ整った「判子」を作る。検査がそこを止めるかを、ここで固定する。

**実データの日付・値・件数をべた書きしない。** 実データは週次で増えるので、
実データを見るテストは「形式が読めること」だけを見る。
"""
from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import checks as CK  # noqa: E402
import verification as VF  # noqa: E402


def eq(actual, expected, label=""):
    assert actual == expected, f"{label}: expected {expected!r}, got {actual!r}"


def truthy(actual, label=""):
    assert actual, f"{label}: expected truthy, got {actual!r}"


# =============================================================================
# 合成データの組み立て
# =============================================================================

BODY = """---
code: "9999"
name: "テスト"
---

# テスト（9999）

裏付けのある記述。時価総額は99億円である。
出典に無い記述。導入は999社以上。
出典と食い違う記述。過去最高は2.5倍である。

<https://example.com/a>
<https://example.com/b>
"""

URL_A = "https://example.com/a"
URL_B = "https://example.com/b"

# 実データが決して届かない日付。`test_data_advance` の「最新営業日をべた書きしない」
# 検査に引っかからないよう、合成データの日付は過去に固定する。
FIXED_DAY = "1999-01-04"


def claim(cid, quote, verdict, sources=None, evidence="根拠", tier="secondary"):
    out = {"id": cid, "quote": quote, "verdict": verdict, "tier": tier,
           "evidence": evidence}
    if sources is not None:
        out["sources"] = list(sources)
    return out


def run_block(claims, urls=None, sha=None, run="2026-01-01T00:00:00+09:00",
              delegated=None):
    if urls is None:
        urls = [{"url": URL_A, "http_status": 200}]
    block = {"run": run, "report_updated": "2026-01-01",
             "report_sha256": VF.sha256(BODY) if sha is None else sha,
             "urls_refetched": urls, "claims": claims}
    if delegated is not None:
        block["urls_delegated"] = delegated
    return block


class Sandbox:
    """tmp/{data,reports} を作り、記録とレポートを置く。"""

    def __init__(self, runs, body=BODY, write_record=True, extra_files=(),
                 extra=None):
        self.root = Path(tempfile.mkdtemp(prefix="kabu-verify-"))
        self.data = self.root / "data"
        self.reports = self.root / "reports"
        (self.data / "verification").mkdir(parents=True)
        self.reports.mkdir(parents=True)
        (self.reports / "9999.md").write_text(body, encoding="utf-8")
        for rel in extra_files:
            path = self.root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("x", encoding="utf-8")
        if write_record:
            import yaml
            doc = {"code": "9999", "runs": runs}
            doc.update(extra or {})
            (self.data / "verification" / "9999.yaml").write_text(
                yaml.safe_dump(doc, allow_unicode=True, sort_keys=False),
                encoding="utf-8")

    def check(self):
        rep = CK.Report()
        CK.check_verification(rep, self.reports, self.data, self.root)
        return rep

    def close(self):
        shutil.rmtree(self.root, ignore_errors=True)


def levels(rep, level):
    return [r.message for r in rep.results
            if r.level == level and r.check == "verify"]


def run_case(runs, **kw):
    box = Sandbox(runs, **kw)
    try:
        rep = box.check()
        return levels(rep, CK.FAIL), levels(rep, CK.WARN)
    finally:
        box.close()


# =============================================================================
# parse（語彙と構造）
# =============================================================================

def test_parse_reads_claims_and_urls():
    raw = {"code": "9999", "runs": [run_block(
        [claim("V1", "時価総額は99億円である", "supported", [URL_A])],
        delegated=[{"url": URL_B, "to": "data/fundamentals/9999.csv"}])]}
    rec = VF.parse(raw, "9999")
    eq(rec.problems, (), "壊れていない記録では problems は空")
    run = rec.latest
    eq(run.total, 1, "claims 件数")
    eq(run.passed, 1, "supported の件数")
    eq(run.fetched_ok(), {URL_A}, "200 で取れたURLだけを裏付けに使える")
    eq(run.delegated, ((URL_B, "data/fundamentals/9999.csv"),), "委譲の記録")


def test_parse_flags_wrong_code_and_empty_runs():
    rec = VF.parse({"code": "1111", "runs": []}, "9999")
    truthy(rec.problems, "code 不一致と runs 空は problems に出る")
    eq(rec.latest, None, "run が無ければ latest は None")


def test_failed_fetch_is_not_counted_as_evidence():
    raw = {"code": "9999", "runs": [run_block(
        [claim("V1", "時価総額は99億円である", "supported", [URL_A])],
        urls=[{"url": URL_A, "http_status": 403}])]}
    run = VF.parse(raw, "9999").latest
    eq(run.fetched_ok(), set(), "403 は「取れた」に数えない")


def test_sha256_is_newline_agnostic():
    eq(VF.sha256("a\r\nb"), VF.sha256("a\nb"), "CRLF と LF で同じハッシュ")


def test_verdict_and_tier_vocabulary_is_closed():
    eq(set(VF.PASSED) <= set(VF.VERDICTS), True, "PASSED は語彙の部分集合")
    eq(set(VF.FATAL_IF_KEPT) <= set(VF.VERDICTS), True, "FATAL も部分集合")
    eq(set(VF.MARKED_IF_KEPT) <= set(VF.VERDICTS), True, "MARKED も部分集合")
    overlap = set(VF.FATAL_IF_KEPT) & set(VF.MARKED_IF_KEPT)
    eq(overlap, set(), "同じ verdict を FAIL と WARN の両方に入れない")


# =============================================================================
# check_verification（記録が裏取りとして成立しているか）
# =============================================================================

def test_missing_record_is_warned_not_silent():
    box = Sandbox([], write_record=False)
    try:
        rep = box.check()
        truthy(levels(rep, CK.WARN), "記録が無い状態を黙って通さない")
        eq(levels(rep, CK.FAIL), [], "記録が無いだけでは FAIL にしない")
    finally:
        box.close()


def test_clean_record_has_no_fail():
    fails, _ = run_case([run_block(
        [claim("V1", "時価総額は99億円である", "supported", [URL_A])])])
    eq(fails, [], "正しく組まれた記録では FAIL 0")


def test_contradicted_claim_left_in_body_is_fatal():
    fails, _ = run_case([run_block(
        [claim("V1", "過去最高は2.5倍である", "contradicted", [URL_A])])])
    truthy(any("食い違う" in m for m in fails),
           "出典と食い違う記述が本文に残っていたら FAIL")


def test_contradicted_claim_dropped_without_a_resolution_is_still_fatal():
    """**本文から消えただけでは解除しない。**

    旧実装は quote の完全一致に乗っていたため、誤った主張を残したまま
    一語だけ言い換えれば FAIL が WARN に落ちた（「直した」と「言い回しを
    変えた」を機械が区別できていなかった）。始末の記録を要求する。
    """
    body = BODY.replace("出典と食い違う記述。過去最高は2.5倍である。\n", "")
    fails, _warns = run_case(
        [run_block([claim("V1", "過去最高は2.5倍である", "contradicted", [URL_A])],
                   sha=VF.sha256(body))],
        body=body)
    truthy(any("始末が記録されていない" in m for m in fails),
           "消えたことの記録が無ければ FAIL のまま")


def test_contradicted_claim_is_cleared_by_a_recorded_resolution():
    """始末を記録し、かつ本文と整合していれば解除する。"""
    body = BODY.replace("出典と食い違う記述。過去最高は2.5倍である。\n", "")
    fails, _warns = run_case(
        [run_block([claim("V1", "過去最高は2.5倍である", "contradicted", [URL_A])],
                   sha=VF.sha256(body))],
        body=body,
        extra={"resolutions": [{"id": "V1", "resolved_at": FIXED_DAY,
                                "how": "removed", "note": "本文から落とした"}]})
    eq([m for m in fails if "食い違う" in m or "始末" in m], [],
       "落としたことを記録すれば解除される")


def test_resolution_that_lies_about_the_body_is_fatal():
    """how: rewritten と書きながら、新しい本文が実在しなければ FAIL。"""
    body = BODY.replace("出典と食い違う記述。過去最高は2.5倍である。\n", "")
    fails, _warns = run_case(
        [run_block([claim("V1", "過去最高は2.5倍である", "contradicted", [URL_A])],
                   sha=VF.sha256(body))],
        body=body,
        extra={"resolutions": [{"id": "V1", "resolved_at": FIXED_DAY,
                                "how": "rewritten",
                                "quote": "本文には存在しない書き直し"}]})
    truthy(any("resolutions" in m for m in fails),
           "書き直したと言うなら、その本文が実在すること")


def test_claims_are_folded_across_runs_by_id():
    """claim 1件だけの run を足しても、過去の指摘は消えない。

    旧実装は最新 run の claims しか見ていなかったため、**別の文を選んだ
    run を1つ足すだけで前回の指摘が全部消えた**（悪意は要らない）。
    """
    fails, warns = run_case([
        run_block([claim("V1", "過去最高は2.5倍である", "contradicted", [URL_A])]),
        run_block([claim("V9", "時価総額は99億円である", "supported", [URL_A])],
                  run="2026-08-14T10:00:00+09:00"),
    ])
    truthy(any("食い違う" in m for m in fails),
           "拾い直されなかった contradicted が残ること")
    truthy(any("拾い直していない" in m for m in warns),
           "拾い直していないこと自体も表に出す")


def test_unsupported_claim_left_in_body_is_warned():
    fails, warns = run_case([run_block(
        [claim("V1", "導入は999社以上", "unsupported", [URL_A])])])
    eq(fails, [], "裏が取れないだけでは公開を止めない")
    truthy(any("裏が取れていない" in m for m in warns), "台帳に印を出すための WARN")


def test_supported_with_unfetched_source_is_fatal():
    fails, _ = run_case([run_block(
        [claim("V1", "時価総額は99億円である", "supported", [URL_B])])])
    truthy(any("再取得できていない" in m for m in fails),
           "叩いていないURLで裏付けたことにしない")


def test_url_outside_report_is_warned_not_fatal():
    """記録のURLが本文に無いのは **WARN**（記録が古い／出典が差し替えられた）。

    実際に起きるのは「人がレポートを編集して出典を差し替えた」であって
    「取得先を勝手に増やした」ではない。裏取りは翌週まで走らない
    （weekly.yml は data → verify）ので、FAIL にすると編集した週から
    次の verify まで data ジョブが落ち続ける。取得先の制限は
    `fetch_source.py` が取得時点で担保している。
    """
    fails, warns = run_case([run_block(
        [claim("V1", "時価総額は99億円である", "supported",
               ["https://example.com/other"])],
        urls=[{"url": "https://example.com/other", "http_status": 200}])])
    eq([m for m in fails if "本文に無い" in m], [], "FAIL にしない")
    truthy(any("現在の本文に無い" in m for m in warns), "記録としては残す")


def test_empty_evidence_is_fatal():
    fails, _ = run_case([run_block(
        [claim("V1", "時価総額は99億円である", "supported", [URL_A],
               evidence="")])])
    truthy(any("evidence" in m for m in fails), "根拠が残っていない判定は FAIL")


def test_missing_sources_is_fatal_except_unverifiable():
    fails, _ = run_case([run_block(
        [claim("V1", "時価総額は99億円である", "supported", [])])])
    truthy(any("sources" in m or "根拠" in m for m in fails),
           "根拠なしの supported は FAIL")

    fails2, _ = run_case([run_block(
        [claim("V1", "導入は999社以上", "unverifiable", [])])])
    eq([m for m in fails2 if "根拠" in m], [],
       "unverifiable は「出典が無い」こと自体が判定内容なので FAIL にしない")


def test_unknown_verdict_is_fatal():
    fails, _ = run_case([run_block(
        [claim("V1", "時価総額は99億円である", "たぶん合っている", [URL_A])])])
    truthy(any("verdict" in m for m in fails), "語彙外の判定は FAIL")


def test_empty_urls_is_fatal():
    fails, _ = run_case([run_block(
        [claim("V1", "導入は999社以上", "unverifiable", [])], urls=[])])
    truthy(any("urls_refetched" in m for m in fails),
           "1件も取りに行っていない記録は裏取りではない")


def test_dataset_only_rerun_without_fetch_is_warn_not_fatal():
    """本文の修正を検証済みデータに当て直しただけの部分再検証は正当。"""
    fails, warns = run_case([run_block(
        [claim("V1", "時価総額は99億円である", "supported",
               ["data/fundamentals/9999.csv"], tier="dataset")], urls=[])],
        extra_files=("data/fundamentals/9999.csv",))
    eq(fails, [], "全 claim が検証済みデータ由来なら FAIL にしない")
    truthy(any("取り直していない" in m for m in warns), "取り直していない事実は残す")


def test_local_source_must_exist():
    fails, _ = run_case([run_block(
        [claim("V1", "時価総額は99億円である", "supported",
               ["data/fundamentals/9999.csv"], tier="dataset")])])
    truthy(any("ファイルが無い" in m for m in fails), "存在しないファイルを根拠にできない")

    fails2, _ = run_case(
        [run_block([claim("V1", "時価総額は99億円である", "supported",
                          ["data/fundamentals/9999.csv"], tier="dataset")])],
        extra_files=("data/fundamentals/9999.csv",))
    eq(fails2, [], "実在するデータファイルなら根拠にしてよい")


def test_stale_report_is_warned():
    _, warns = run_case([run_block(
        [claim("V1", "時価総額は99億円である", "supported", [URL_A])],
        sha="0" * 64)])
    truthy(any("書き換えられている" in m for m in warns),
           "検証後に本文が変わったら記録は現在の本文を保証しない")


def test_runs_must_be_append_only_ascending():
    newer = run_block([claim("V1", "時価総額は99億円である", "supported", [URL_A])],
                      run="2026-02-01T00:00:00+09:00")
    older = run_block([claim("V1", "時価総額は99億円である", "supported", [URL_A])],
                      run="2026-01-01T00:00:00+09:00")
    fails, _ = run_case([newer, older])
    truthy(any("昇順" in m for m in fails), "runs の並びが崩れたら FAIL")


def test_untouched_report_urls_are_reported():
    _, warns = run_case([run_block(
        [claim("V1", "時価総額は99億円である", "supported", [URL_A])])])
    truthy(any("再取得も委譲もされていない" in m for m in warns),
           "触っていない出典URLの件数を必ず出す")


def test_delegated_url_is_not_reported_as_untouched():
    _, warns = run_case([run_block(
        [claim("V1", "時価総額は99億円である", "supported", [URL_A])],
        delegated=[{"url": URL_B, "to": "data/fundamentals/9999.csv"}])],
        extra_files=("data/fundamentals/9999.csv",))
    eq([m for m in warns if "再取得も委譲もされていない" in m], [],
       "機械抽出に委ねた出典は「触っていない」に数えない")


# =============================================================================
# 実データ（形式だけを見る。値・件数はべた書きしない）
# =============================================================================

def test_real_records_parse_cleanly():
    directory = ROOT / "data" / "verification"
    if not directory.exists():
        return
    for path in sorted(directory.glob("*.yaml")):
        rec = VF.load(path.stem)
        assert rec is not None, f"{path.name} を読めない"
        eq(rec.problems, (), f"{path.name} の構造")
        run = rec.latest
        assert run is not None, f"{path.name} に run が無い"
        for c in run.claims:
            assert c.verdict in VF.VERDICTS, f"{path.name} {c.id} verdict"
            assert c.tier in VF.TIERS, f"{path.name} {c.id} tier"
            assert c.quote, f"{path.name} {c.id} quote が空"
            assert c.evidence, f"{path.name} {c.id} evidence が空"


def test_real_quotes_exist_in_reports():
    """記録の quote が本文の実在部分文字列であること（値には触れない）。"""
    directory = ROOT / "data" / "verification"
    if not directory.exists():
        return
    for path in sorted(directory.glob("*.yaml")):
        report = ROOT / "reports" / f"{path.stem}.md"
        if not report.exists():
            continue
        rec = VF.load(path.stem)
        if rec is None or rec.latest is None:
            continue
        text = report.read_text(encoding="utf-8")
        if rec.latest.report_sha256 != VF.sha256(text):
            continue      # 本文が更新された直後。ここでは追わない（checks が WARN を出す）
        for c in rec.latest.claims:
            assert c.quote in text, f"{path.name} {c.id} の quote が本文に無い"


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
