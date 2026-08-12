# kabu-ledger

楽天証券スクリーニング「成長株0606」の通過銘柄を週次で監視し、
GitHub Pages に台帳として公開する。

**売買の実行判断は人間が行う。** このリポジトリの責務は候補提示と記録まで。
判定スタンプは候補提示であって推奨ではない。個人の検討用であり、投資助言ではない。

- 運用ルール・不変条件・マスターへの確認事項: [CLAUDE.md](./CLAUDE.md)
- 決算データの取り込み手順: [.claude/skills/kabu-ledger/SKILL.md](./.claude/skills/kabu-ledger/SKILL.md)
- 公開先: GitHub Pages（`docs/`）／実行: GitHub Actions 週次（JST 土 06:00）

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
tests → fetch.py → fetch_margin.py → fetch_index.py → checks.py → score.py
      → (bear) → build.py → notify.py
```

```bash
python tests/test_indicators.py     # 指標の計算
python tests/test_judge.py          # 判定ロジック
python tests/test_fetch.py          # 2ソース照合
python tests/test_checks.py         # データ品質検査
python tests/test_score.py          # 予測の機械採点
python tests/test_notify.py         # 通知と状態管理

python src/fetch.py                 # 株価 OHLCV（2ソース照合・append-only）
python src/fetch_margin.py          # 信用残高
python src/fetch_index.py           # TOPIX / グロース250
python src/checks.py                # FAIL があればここで停止する
python src/score.py                 # 予測の機械採点
python src/build.py                 # docs/ と scoring/stamps.json を生成
python src/notify.py                # 判定が変化した銘柄だけ Issue 起票
```

判定だけを確認したいとき:

```bash
python src/judge.py                 # 全銘柄の判定と根拠を表示
python src/judge.py --stamps        # {証券コード: 判定} を JSON で
python src/judge.py --json          # 全ゲートの評価を JSON で
```

`checks.py` が FAIL したら後続を実行しない。
`bear`（ベアケース生成）は Should 要件なので、失敗しても `build.py` /
`notify.py` は実行する（`weekly.yml` の `publish` は `data` の成否だけを見る）。

---

## 注意点

### 実行環境（Windows / PowerShell）

- Python から日本語を出力するので、**先に `$env:PYTHONIOENCODING = "utf-8"` を設定する**。
  設定しないと標準出力が化ける（CI では `weekly.yml` の `env` で設定済み）。
- SSL 検査プロキシ配下では `truststore` が要る（`requirements.txt` に Windows 限定で入っている）。
  HTTPS を叩くコードは先頭で `truststore.inject_into_ssl()` を試みる。
- 複数行のコードを `python -c` に渡すと PowerShell がパースに失敗する。
  検証スクリプトはファイルに書いてから実行すること。

### データの扱い

- `data/` 配下の CSV は **append-only**。過去行を書き換えると `checks.py` が FAIL する。
  ローカルで追記性を検証するには `python src/checks.py --baseline <以前の data/ のコピー>`。
  git 管理下なら HEAD が自動でベースラインになる。
- 取得失敗は推定値で埋めず、`null` + `status` で記録する。
- `close`（採用値）が入るのは2ソース照合が成立した行だけ。
  `SINGLE_SOURCE` / `MISMATCH` の行は空のままで、生値は
  `value_primary` / `value_secondary` に残る。**`close` が空＝データが無い、ではない。**
- `growth250` は第2ソースが無いため `close` が全行空。値は `value_primary` を読む。

### 生成物

- `build.py` の出力に**生成時刻を埋め込まない**。2回実行しても同じ内容になる
  （`git diff` が「先週から何が変わったか」そのものになる）。
- 判定は毎週ゼロから計算し直す。前週の判定を入力に持たない。

### 判定を読むときに

- 「買」は①〜⑤のゲートを通過したという意味しかない。
  鉄則の全項目を確認したわけではない（何を見ていないかは `docs/formula.html` に一覧がある）。
- 「—」は「計算できなかった」であって「ゼロだった」ではない。
- 「?」を○にも×にも読み替えない。
- 保有銘柄の**売り**シグナル（雲の下・逆指値抵触・基準到達×デッドクロス気味）は、
  流動性・トレンド・過熱より**先に**評価する。「調査で止める」は買い側だけの原則。
