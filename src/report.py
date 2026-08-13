"""銘柄レポート（reports/{code}.md）の読み込み。

v2.0 の主役。レポート本文は人間が読む文章であり、コードは
「front matter を切り出して Markdown を HTML にする」までしか関与しない。

不変条件:
  - 週次アップデート（## ④ 週次アップデート 節）は append-only。
    build 側で並べ替えたり削ったりしない。書かれた順のまま出す。
  - 生成時刻を埋め込まない（D8）。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import yamlio as Y

ROOT = Path(__file__).resolve().parents[1]
REPORTS = ROOT / "reports"

# レポートの節見出し。**この順で並べる**（reports/*.md 側の書き順に依存しない）。
#
# 週次アップデートを先頭に置いている。会社の素性・財務・展望は一度読めば
# 頭に入るが、毎週見に来る理由は「今週何があったか」だから。
# 見出しの丸数字は照合時に無視するので、md 側に番号が残っていても構わない。
SECTIONS = [
    ("updates", "週次アップデート"),
    ("company", "この会社は何者か"),
    ("financials", "財務の推移と健全性"),
    ("outlook", "今後の展望とリスク"),
    ("price", "値動きと市場の評価"),
    ("sources", "出典"),
]


@dataclass
class Report:
    code: str
    meta: dict
    lead: str = ""                       # 「一行でいうと」の引用ブロック
    sections: dict = field(default_factory=dict)   # key -> markdown 本文
    raw: str = ""

    @property
    def name(self) -> str:
        return self.meta.get("name", self.code)

    @property
    def deep_dive(self) -> bool:
        return bool(self.meta.get("deep_dive"))

    @property
    def charts(self) -> dict:
        """front matter の charts。本文の {{chart:id}} と対応する。"""
        return self.meta.get("charts") or {}

    @property
    def links(self) -> list[dict]:
        """会社・一次情報への外部リンク。見出しの横に出す。"""
        return self.meta.get("links") or []

    @property
    def updated(self) -> str:
        v = self.meta.get("updated")
        return str(v) if v else "—"

    def week_entries(self) -> list[tuple[str, str]]:
        """週次アップデートを (見出し, 本文markdown) に分解する。新しい順で返す。"""
        body = self.sections.get("updates", "")
        parts = re.split(r"^###\s+(.+?)\s*$", body, flags=re.MULTILINE)
        # parts = [前置き, 見出し1, 本文1, 見出し2, 本文2, ...]
        out: list[tuple[str, str]] = []
        for i in range(1, len(parts) - 1, 2):
            out.append((parts[i].strip(), parts[i + 1].strip()))
        out.sort(key=lambda x: x[0], reverse=True)   # 週の表記は YYYY-Www なので文字列降順で新しい順
        return out

    def latest_week(self) -> tuple[str, str] | None:
        entries = self.week_entries()
        return entries[0] if entries else None


def _split_front_matter(text: str) -> tuple[dict, str]:
    """--- で囲まれた YAML front matter を切り出す。無ければ空 dict。"""
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n(.*)$", text, flags=re.DOTALL)
    if not m:
        return {}, text
    meta = Y.safe_load(m.group(1)) or {}
    return meta, m.group(2)


def _normalize_title(title: str) -> str:
    """見出しの先頭にある丸数字・番号・記号を落として照合用に揃える。

    表示順は SECTIONS が決めるので、md 側に「① 」等が残っていてもよい
    （番号と表示順がずれると読み手が混乱するため、md 側も番号なしを推奨）。
    """
    return re.sub(r"^[①-⑳\d]+[.\s　]*", "", title).strip()


def _split_sections(body: str) -> tuple[str, dict[str, str]]:
    """`## 見出し` で節に分ける。SECTIONS の見出しに一致するものだけ拾う。

    戻り値: (リード文, {key: markdown})
    """
    title_to_key = {_normalize_title(title): key for key, title in SECTIONS}

    # 最初の `## ` より前をリード（`# 銘柄名` と「一行でいうと」の引用）とする
    chunks = re.split(r"^##\s+(.+?)\s*$", body, flags=re.MULTILINE)
    lead = chunks[0]
    sections: dict[str, str] = {}
    for i in range(1, len(chunks) - 1, 2):
        title = _normalize_title(chunks[i].strip())
        content = chunks[i + 1].strip()
        key = title_to_key.get(title)
        if key:
            sections[key] = content
    return lead.strip(), sections


def load_report(code: str) -> Report | None:
    """reports/{code}.md を読む。無ければ None（レポート未作成として扱う）。"""
    path = REPORTS / f"{code}.md"
    if not path.exists():
        return None
    text = path.read_text(encoding="utf-8")
    meta, body = _split_front_matter(text)
    lead, sections = _split_sections(body)
    return Report(code=code, meta=meta, lead=lead, sections=sections, raw=text)


def load_all(codes: list[str]) -> dict[str, Report]:
    out: dict[str, Report] = {}
    for c in codes:
        r = load_report(c)
        if r is not None:
            out[c] = r
    return out


def one_liner(report: Report) -> str:
    """リード文から「一行でいうと」の中身を取り出す（引用ブロックの本文）。"""
    m = re.search(r">\s*\*\*一行でいうと\*\*[：:]\s*(.+?)(?:\n\n|\Z)",
                  report.lead, flags=re.DOTALL)
    if not m:
        return ""
    return re.sub(r"\s*\n>\s*", " ", m.group(1)).strip()
