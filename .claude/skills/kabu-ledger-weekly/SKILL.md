---
name: kabu-ledger-weekly
description: kabu-ledger の週次更新を最初から最後まで回す。差分取得 → 検査 → レポートの週次アップデート（並列）→ 裏取り → 生成 → push まで。claude.ai の週次ルーティンが呼ぶ入口であり、「週次更新して」「今週のアップデート」「ルーティンを回して」と言われた場合に必ず使用する。新規銘柄の登録には使わない（kabu-ledger-intake が担当）。
---

# kabu-ledger-weekly — 週次更新の入口

**週次の起点はこれ1本。** GitHub Actions に cron は無い。
ルーティンが走らなかった週は何も更新されない（それが正しい。
「動いていないのに更新されたように見える」より良い）。

**ルーティンの文面にはこのファイルを参照させること。** 手順をルーティン側に
書き写すと、リポジトリの実装と食い違っても機械が気づかない
（実際に「Actions が取得を済ませている」という、成立していない前提が
ルーティンに書かれていた）。

## 全体像

```
① 差分取得（コード）      fetch → checks
② レポート更新（判断）     1銘柄1エージェントで並列
③ 裏取り（判断・別文脈）   書いた本人にはやらせない
④ 生成と push             build.py → push
⑤ 公開                    push が deploy.yml を起こす。操作は不要
```

## ① 差分取得

```powershell
$env:PYTHONIOENCODING = "utf-8"
python src/fetch.py                 # 直近ページのみ差分追記（--historical は初回だけ）
python src/fetch_margin.py
python src/fetch_index.py
python src/fetch_fundamentals.py
python src/fetch_tanshin.py         # 落ちても止めない
python src/checks.py                # FAIL があればここで停止
python src/checks.py --check-links --no-git   # 落ちても止めない
python src/score.py
```

- **`checks.py` が FAIL したら、レポート更新に進まない。** 壊れたデータの上に
  今週の解釈を積むと、後から取り消せない
- 取得に失敗した項目は推定値で埋めない。`null` + `status` のまま残す
- **取得が丸ごと失敗した週は、そう報告して止める。** 値動きが先週のままなのに
  「今週の値動き」を書くと、レポートが嘘になる

## ② レポートの週次アップデート（並列）

```
Workflow: .claude/workflows/kabu-weekly-reports.js
  args: 省略（master.yaml の全銘柄）
```

各エージェントは `.claude/skills/kabu-ledger-report/SKILL.md` に従う。
モードは自動判定される。

| 状態 | やること |
|---|---|
| レポート無し | 全節を書き下ろす |
| `deep_dive: true` | 全節を見直し ＋ 週次アップデート追記 |
| `deep_dive: false` | **週次アップデートを追記するだけ** |

**大半の銘柄は「追記のみ」になる。** 初回のコストを毎週払わない設計であり、
銘柄が20に増えても週次の負荷はここで抑える。

**過去の週を書き換えない。** `checks.py` の
`check_report_updates_append_only` が git HEAD と突き合わせて FAIL させる。
同じ週にもう一度書くなら `### YYYY-Www（続報）` を別エントリとして足す。

## ③ 裏取り（別コンテキスト）

`.claude/skills/kabu-ledger-verify/SKILL.md` に従う。上のワークフローが
銘柄ごとに別エージェントとして起動するので、**書いた本人が自分の記述を検証しない**。

裏が取れない記述は落とすか「未確認」と明示する。黙って残さない。

## ④ 生成と push

```powershell
python src/build.py
python tools/run_tests.py           # 約45秒。CI と同じ全数を並列で
git add -A ; if ($?) { git commit -m "週次更新 YYYY-Www" }
git pull --rebase origin main ; if ($?) { git push origin main }
```

`pull --rebase` が `docs/` で衝突したら、中身を読まずに `python src/build.py` →
`Select-String -Path docs -Pattern '^<<<<<<< ' -Recurse` が0件を確認 →
`git add docs/` → `git rebase --continue`。
**`data/` には同じ手を使わない**（append-only。衝突したら `git rebase --abort` して人間に上げる）。

## ⑤ 公開の確認

```powershell
.\tools\published.ps1 -Marker "<今週の週キー。例 2026-W34>"
```

`PUBLISHED`（exit 0）が出てから「更新しました」と報告する。
**`gh workflow run` は叩かない。** push が `deploy.yml` を起こし、
テスト → 検査 → 生成 → 公開まで約70秒で走る。

## 報告に必ず含めるもの

- 取得できなかったデータ（あれば）
- 銘柄ごとの一言（今週の要点）
- **裏が取れなかった記述**と、その扱い（落とした／「未確認」と明示した）
- `checks.py` の FAIL / WARN 件数
- 公開の到達（`published.ps1` の結果）

「異常なし」だけの報告をしない。**何を見ていないかが伝わらない報告は、見たことにならない。**
