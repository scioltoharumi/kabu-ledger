# kabu-ledger

楽天証券スクリーニング「成長株0606」の通過銘柄を週次で監視し、
GitHub Pages に台帳として公開する。

**売買の実行判断は人間が行う。** このリポジトリの責務は候補提示と記録まで。
判定スタンプは候補提示であって推奨ではない。個人の検討用であり、投資助言ではない。

- 運用ルール・不変条件・マスターへの確認事項: [CLAUDE.md](./CLAUDE.md)
- 決算データの取り込み手順: [.claude/skills/kabu-ledger/SKILL.md](./.claude/skills/kabu-ledger/SKILL.md)
- 公開先: GitHub Pages（`docs/`）／**公開は push が起こす**（`.github/workflows/ci.yml` が緑 → `publish.yml`）
- 取得の起点は2つ。**スケジュールで勝手に取りに行かない**（cron は置いていない）
  - スクショを貼った → `.claude/skills/kabu-ledger-intake/`（新しい銘柄の履歴を取る）
  - 週次ルーティン → `.claude/skills/kabu-ledger-weekly/`（既存銘柄の差分を取る）

---

## セットアップ

```bash
pip install -r requirements.txt
```

GitHub 側の設定:

1. Settings → Pages → Source を「GitHub Actions」に変更
2. Secrets に `CLAUDE_CODE_OAUTH_TOKEN` を登録（ベアケース生成用。
   未登録でも `bear` ジョブが失敗するだけで台帳の生成と通知は動く）
3. Actions → weekly → Run workflow で初回実行

### 初回だけ必要なこと

```bash
python src/fetch.py --historical        # 日足を1年分さかのぼる（D16）
python src/fetch_index.py --historical  # TOPIX / グロース250 を1年分
python src/checks.py --scan-all         # 分割・外れ値を全履歴で走査
```

2回目以降は引数なしで直近ページのみを差分追記する。
信用残高は毎回直近4週分が返るため `--historical` を持たない。

---

## 実行順

```text
tests → fetch.py → fetch_margin.py → fetch_index.py → fetch_fundamentals.py
      → fetch_tanshin.py → checks.py → (checks.py --check-links) → score.py
      → (bear) → (verify) → build.py → notify.py
```

```bash
python -m pytest tests -q           # 全テスト（1本ずつ素の python でも実行できる）

python src/fetch.py                 # 株価 OHLCV（運営の異なる2ソース照合・append-only）
python src/fetch_margin.py          # 信用残高
python src/fetch_index.py           # TOPIX / グロース250
python src/fetch_fundamentals.py    # 財務数値（別サイト2つの一致でだけ採用）
python src/fetch_tanshin.py         # 決算短信PDF（一次情報）
python src/checks.py                # FAIL があればここで停止する
python src/checks.py --check-links  # 出典URLの死活（ネットワークを使う・別ステップ）
python src/score.py                 # 予測の機械採点
python src/build.py                 # docs/ と scoring/stamps.json を生成
python src/notify.py                # 判定変化・未解決の contradicted を Issue 起票
```

判定だけを確認したいとき:

```bash
python src/judge.py                    # 全銘柄の判定と根拠を表示
python src/judge.py --stamps           # {証券コード: 判定} を JSON で
python src/judge.py --json             # 全ゲートの評価を JSON で
python src/judge.py --indicators-only  # master.yaml を読まずに指標だけ（裏取り用）
```

`checks.py` が FAIL したら後続を実行しない。
`bear`（ベアケース生成）と `verify`（記述の裏取り）は Should 要件なので、
失敗しても `build.py` / `notify.py` は実行する
（公開は `ci.yml` が緑になったのを受けて `publish.yml` が行う）。
ただし **出典と食い違うと判定された記述の始末が記録されていない**と
`checks.py` が FAIL するので、直すまで翌週の取得・公開は動かない。

---

## 注意点

### 実行環境（Windows / PowerShell）

- Python から日本語を出力するので、**先に `$env:PYTHONIOENCODING = "utf-8"` を設定する**。
  設定しないと標準出力が化ける（CI では `ci.yml` / `publish.yml` の `env` で設定済み）。
- SSL 検査プロキシ配下では `truststore` が要る（`requirements.txt` に Windows 限定で入っている）。
  HTTPS を叩くコードは先頭で `truststore.inject_into_ssl()` を試みる。
- 複数行のコードを `python -c` に渡すと PowerShell がパースに失敗する。
  検証スクリプトはファイルに書いてから実行すること。

### データの扱い

- `data/` 配下の CSV と `data/verification/*.yaml` は **append-only**。
  過去行・過去 run を書き換えると `checks.py` が FAIL する。
  ローカルで追記性を検証するには `python src/checks.py --baseline <以前の data/ のコピー>`。
  git 管理下なら HEAD が自動でベースラインになる。
- **唯一の例外は「照合不成立 → 成立」の訂正**で、`data/revisions.csv` に
  前後の値と理由が必ず残る。鍵が期や日付で不変なため、1回の取得失敗が
  その行の検証状態を恒久的に固定していた（途中の1日が採用終値を持たないと
  20日/25日窓が丸ごと算出不能になり、全銘柄が「調査」に固定される）。
  採用値を**下げる**方向は自動化していない
  （`python src/fetch_fundamentals.py --withdraw-invalid --reason "…"` で人間が実行）。
- 取得失敗は推定値で埋めず、`null` + `status` で記録する。
- `close`（採用値）が入るのは**運営の異なる2つの取得元**で照合が成立した行だけ。
  独立性は取得元 id ではなく `sources.yaml` の `operator` で数える
  （株探と みんかぶ は同一運営なので、その一致は独立した確認ではない）。
  `SINGLE_SOURCE` / `MISMATCH` の行は空のままで、生値は
  `value_primary` / `value_secondary` に残る。**`close` が空＝データが無い、ではない。**
- 逆に **`close` が入っている＝照合が成立した、でもない**。旧 `fetch.py` は
  売買不成立の日に照合結果を潰して主ソース値を書いており、その行が実データに7行残る
  （`status` が `NO_TRADE` 単独）。**採用終値を数えるコードは `status` に `OK` があるかで
  判定する**（`chartdata.adopted_close`）。`close` の有無で数えると、
  照合していない値が図と「採用終値 N/M日」に混ざる。
- `growth250` は第2ソースが無いため `close` が全行空。値は `value_primary` を読む。
- 財務では**別々の勘定科目を突き合わせない**。IR BANK の BS 表の「株主資本」は
  `shareholders_equity` で、kabutan の「自己資本」（`equity`）とは別物。

### 生成物

- `build.py` の出力に**生成時刻を埋め込まない**。2回実行しても同じ内容になる
  （`git diff` が「先週から何が変わったか」そのものになる）。
- `build.py` は `docs/` と **`scoring/stamps.json`** を書く。
  後者は `notify.py` の唯一の入力なので、**書かれないと判定が変わっても Issue が出ない**
  （実際に v2.0 改稿で出力が丸ごと落ちていた）。`checks.py` が
  「スタンプの銘柄集合が master.yaml と違う／空」を検査する。
- 判定は毎週ゼロから計算し直す。前週の判定を入力に持たない。

### 検証はどこまで届いているか

`checks.py` はレポートの数値について「機械照合できたのは何件か」を分母つきで出す。
実測（2026-08-13・4銘柄）:

| レポート | 機械照合 | 未突合 | 本文の散文 | front matter の説明文 |
|---|---|---|---|---|
| 3851 | 58/217 | 1 | 138 | 20 |
| 4073 | 21/137 | 12 | 88 | 16 |
| 4937 | 57/255 | 2 | 164 | 32 |
| 6570 | 56/243 | 2 | 160 | 25 |

散文と説明文は突合が届かないので `data/verification/{code}.yaml`
（別コンテキストの裏取り）が受け持つ。**その記録があるのは 3851 と 4073 だけで、
どちらも記録後に本文が書き換わっている**（台帳に「記録が古い」と出る）。
4937 / 6570 は記録が無く「未検証」と出る。

**「FAIL 0」は「全部確かめた」ではない。** 何を確かめていないかが台帳と WARN に出る。

### 判定を読むときに

- 「買」は①〜⑤のゲートを通過したという意味しかない。
  鉄則の全項目を確認したわけではない。**何を見ていないかは
  `docs/index.html` の「この台帳が見ていない鉄則」に一覧がある**
  （`judge.UNEVALUATED_RULES` から `build.py` が生成する）。
- 「—」は「計算できなかった」であって「ゼロだった」ではない。
- 「?」を○にも×にも読み替えない。
- 保有銘柄の**売り**シグナル（雲の下・逆指値抵触・基準到達×デッドクロス気味）は、
  流動性・トレンド・過熱より**先に**評価する。「調査で止める」は買い側だけの原則。
