"""裏取り記録（data/verification/{code}.yaml）の読み込みと集計。

`checks.py`（検査）と `build.py`（台帳表示）の両方が使う。同じ語彙を2か所に
書くと片方だけ増えて検査が素通りするので、**語彙と読み方はここが正**。

この記録は何か:

    レポート（reports/{code}.md）を書いたのとは**別のコンテキスト**が、
    本文に書かれている出典URLを実際にもう一度取りに行き、
    「その記述はその出典で裏付けられるか」を1件ずつ判定した結果。
    手順の正は `.claude/skills/kabu-ledger-verify/SKILL.md`。

原則:
  - **append-only**。過去の run を書き換えない。新しい run を末尾に足す
  - 判定は5語彙のみ（下の VERDICTS）。「たぶん合っている」を作らない
  - `supported` を名乗るには**実際に再取得した出典**が要る。
    再取得の記録が無い `supported` は checks.py が FAIL にする
  - 記録は本文の写し（`quote`）を持つ。**本文が後から書き換わったら分かる**
    ように、run は検証時点の本文の sha256 を持つ
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]

# --- 判定語彙（この5つだけ。増やすときは SKILL.md と checks.py の表示も直す） ---
#
# 「裏が取れなかった」を1語にまとめない。**なぜ取れなかったかで打ち手が違う**:
#   unsupported   出典は生きているが、その記述が書かれていない → 出典を替えるか記述を落とす
#   contradicted  出典が別のことを言っている                   → 記述が誤り。直す
#   unverifiable  出典に到達できない／出典が書かれていない       → 出典を足す
#   superseded    項目はあるが値が動いた（株価・時価総額など）   → 「取得日時点」と読む
# 表示文言に注意: `unsupported` を「出典に該当記述が無い」と書くと、読み手には
# **「その事実は無い」**と読める。実際に言えるのは「この出典では確かめられない」
# までで、一次情報の1クリック先に裏付けがあることもある（4073 の V01/V13 が実例。
# 会社の製品ページに「全国180社以上の導入実績」、沿革ページに上場年が実在した）。
# 判定は与えられた材料の中では正しく、**材料の与え方の問題**である。
# 断定と読める表示にしない（D28「表示の嘘」と同じ論点）。
VERDICTS = {
    "supported": "裏付けあり",
    "superseded": "取得日から値が動いた",
    "unsupported": "この出典では確かめられない（別の出典が要る）",
    "contradicted": "出典と食い違う",
    "unverifiable": "確かめられない（到達不可・出典なし）",
}

# 台帳で「裏が取れた」として数えるのはこれだけ。
PASSED = ("supported",)

# 本文に残っていたら FAIL にする判定。**出典が否定している記述を断定形のまま
# 置いておくこと**は、欠測を埋めるのと同じ種類の誤りなので止める。
FATAL_IF_KEPT = ("contradicted",)

# 本文に残っていてよいが、台帳に「未確認」と出す判定。
MARKED_IF_KEPT = ("unsupported", "unverifiable", "superseded")

# 情報の階層。一次情報と二次情報を区別して表示する（F2-4）。
TIERS = {
    "primary": "一次情報",
    "secondary": "二次情報",
    "dataset": "検証済みデータ",
    "none": "出典なし",
}

# sources に URL 以外で書いてよいもの（リポジトリ内の**検証済みデータ**・コード）。
# ★`reports/` を入れない。入れると `sources: ["reports/4073.md"]` が正当な根拠として
#   通り、**レポートがレポートを認証する**（絶対原則1「書き手の主張を根拠にしない」に
#   真っ向から反する）。`tier: dataset` と組み合わせると一度も外に出ずに成立してしまう。
LOCAL_SOURCE_PREFIXES = ("data/", "src/")


@dataclass(frozen=True)
class Claim:
    id: str
    quote: str                  # 本文に実在する部分文字列
    verdict: str
    tier: str = "none"
    sources: tuple = ()
    evidence: str = ""
    action: str = ""
    section: str = ""

    @property
    def passed(self) -> bool:
        return self.verdict in PASSED

    @property
    def verdict_ja(self) -> str:
        return VERDICTS.get(self.verdict, self.verdict)

    @property
    def tier_ja(self) -> str:
        return TIERS.get(self.tier, self.tier)


@dataclass(frozen=True)
class Run:
    run: str                    # 実行時刻（ISO8601+09:00）。実行IDを兼ねる
    report_updated: str = ""
    report_sha256: str = ""
    verifier: str = ""
    urls: tuple = ()            # ((url, http_status), ...) 実際に叩いたもの
    delegated: tuple = ()       # ((url, 委譲先), ...) 機械抽出が担当している出典
    claims: tuple = ()

    def counts(self) -> dict:
        out = {k: 0 for k in VERDICTS}
        for c in self.claims:
            if c.verdict in out:
                out[c.verdict] += 1
        return out

    @property
    def passed(self) -> int:
        return sum(1 for c in self.claims if c.passed)

    @property
    def total(self) -> int:
        return len(self.claims)

    def fetched_ok(self) -> set:
        """実際に 200 番台で取れた URL の集合。`supported` の裏付けに使う。"""
        out = set()
        for url, status in self.urls:
            if isinstance(status, int) and 200 <= status < 400:
                out.add(url)
        return out


@dataclass(frozen=True)
class Resolution:
    """`contradicted` と判定された記述をどう始末したかの記録（人間が書く）。

    判定そのものではないので `runs` には入れない。**判定を上書きしない**：
    `how: removed` なら「その quote が本文に無いこと」を checks.py が
    機械で確かめてから解除する。書いただけでは解除されない。
    """
    id: str
    resolved_at: str = ""
    how: str = ""               # removed / rewritten
    note: str = ""
    quote: str = ""             # rewritten のときの新しい本文


RESOLUTION_HOWS = ("removed", "rewritten")


@dataclass(frozen=True)
class Record:
    code: str
    runs: tuple = ()
    problems: tuple = field(default=())
    resolutions: tuple = ()

    @property
    def latest(self) -> Run | None:
        return self.runs[-1] if self.runs else None

    def folded(self) -> tuple:
        """claim を id で畳んで **最新の判定**だけを残す（実行順）。

        旧実装は `runs[-1].claims` しか見ていなかった。SKILL.md は
        「各 run はその時点の本文の全量スナップショット」と定めているが、
        それを担保する検査が無く、**claim 1件だけの run を足せば
        前回の指摘が全部消える**（悪意は要らない。LLM が別の文を選ぶだけで起きる）。
        id で畳めば、拾い直されなかった指摘も残る。
        """
        latest: dict = {}
        order: list = []
        for run in self.runs:
            for c in run.claims:
                if c.id not in latest:
                    order.append(c.id)
                latest[c.id] = (c, run)
        return tuple((latest[i][0], latest[i][1]) for i in order)

    @property
    def resolved_ids(self) -> set:
        return {r.id for r in self.resolutions if r.id}


def _str(v) -> str:
    return "" if v is None else str(v).strip()


def _tuple(v) -> tuple:
    if v is None:
        return ()
    if isinstance(v, (list, tuple)):
        return tuple(_str(x) for x in v if _str(x))
    return (_str(v),)


def parse(raw, code: str) -> Record:
    """YAML の生データを Record にする。**壊れている箇所は problems に残す**。

    黙って読み飛ばすと「検証済み0件」が「異常なし」に見える（設計原則1）。
    """
    problems: list[str] = []
    if not isinstance(raw, dict):
        return Record(code=code, problems=("ファイルの中身が辞書ではない",))

    got_code = _str(raw.get("code"))
    if got_code and got_code != code:
        problems.append("code が %s になっている（ファイル名は %s）" % (got_code, code))

    resolutions: list[Resolution] = []
    for i, r in enumerate(raw.get("resolutions") or []):
        if not isinstance(r, dict):
            problems.append("resolutions[%d] が辞書ではない" % i)
            continue
        resolutions.append(Resolution(
            id=_str(r.get("id")), resolved_at=_str(r.get("resolved_at")),
            how=_str(r.get("how")), note=_str(r.get("note")),
            quote=_str(r.get("quote"))))

    runs_raw = raw.get("runs")
    if not isinstance(runs_raw, list) or not runs_raw:
        return Record(code=code, problems=tuple(problems + ["runs が空"]),
                      resolutions=tuple(resolutions))

    runs: list[Run] = []
    for i, r in enumerate(runs_raw):
        label = "runs[%d]" % i
        if not isinstance(r, dict):
            problems.append(label + " が辞書ではない")
            continue
        urls: list[tuple] = []
        for u in (r.get("urls_refetched") or []):
            if isinstance(u, dict):
                status = u.get("http_status")
                try:
                    status_int = int(status)
                except (TypeError, ValueError):
                    status_int = None
                urls.append((_str(u.get("url")), status_int))
            else:
                problems.append(label + ".urls_refetched に辞書でない要素がある")
        delegated: list[tuple] = []
        for u in (r.get("urls_delegated") or []):
            if isinstance(u, dict):
                delegated.append((_str(u.get("url")), _str(u.get("to"))))
            else:
                problems.append(label + ".urls_delegated に辞書でない要素がある")
        claims: list[Claim] = []
        for j, c in enumerate(r.get("claims") or []):
            if not isinstance(c, dict):
                problems.append("%s.claims[%d] が辞書ではない" % (label, j))
                continue
            claims.append(Claim(
                id=_str(c.get("id")) or ("%s-%d" % (label, j)),
                quote=_str(c.get("quote")),
                verdict=_str(c.get("verdict")),
                tier=_str(c.get("tier")) or "none",
                sources=_tuple(c.get("sources")),
                evidence=_str(c.get("evidence")),
                action=_str(c.get("action")),
                section=_str(c.get("section")),
            ))
        runs.append(Run(
            run=_str(r.get("run")),
            report_updated=_str(r.get("report_updated")),
            report_sha256=_str(r.get("report_sha256")),
            verifier=_str(r.get("verifier")),
            urls=tuple(urls),
            delegated=tuple(delegated),
            claims=tuple(claims),
        ))
    return Record(code=code, runs=tuple(runs), problems=tuple(problems),
                  resolutions=tuple(resolutions))


def record_path(code: str, data_dir: Path | None = None) -> Path:
    base = data_dir if data_dir is not None else ROOT / "data"
    return base / "verification" / (code + ".yaml")


def load(code: str, data_dir: Path | None = None) -> Record | None:
    """記録を読む。ファイルが無ければ None（＝一度も裏取りしていない）。"""
    path = record_path(code, data_dir)
    if not path.exists():
        return None
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as e:
        return Record(code=code, problems=("YAML を読めない: %s" % e,))
    return parse(raw, code)


def sha256(text: str) -> str:
    """レポート本文のハッシュ。改行コードを揃えてから取る（OS 差で変わらないように）。"""
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def report_sha256(code: str, reports_dir: Path | None = None) -> str | None:
    base = reports_dir if reports_dir is not None else ROOT / "reports"
    path = base / (code + ".md")
    if not path.exists():
        return None
    return sha256(path.read_text(encoding="utf-8"))


def is_local_source(src: str) -> bool:
    return src.startswith(LOCAL_SOURCE_PREFIXES)
