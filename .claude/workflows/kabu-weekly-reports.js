export const meta = {
  name: 'kabu-weekly-reports',
  description: 'kabu-ledger の銘柄レポートを1銘柄1エージェントで並列に生成し、別コンテキストで裏取りする（初回登録・deep_dive 用）',
  whenToUse: '新規銘柄の初回レポート一括生成（intake が mode:"初回" で呼ぶ）、または deep_dive をまとめて回すとき。週次の定例更新には使わない（kabu-ledger-weekly v2 が weekly_note.py で機械追記する）。args に銘柄コードの配列',
  phases: [
    { title: '執筆' },
    { title: '裏取り' },
    { title: '検査' },
  ],
}

// 銘柄どうしは独立している（レポートも裏取り記録も1銘柄1ファイル、共有するのは
// master.yaml だけで、それは誰も書かない）。だから壁時間は銘柄数ではなく
// 同時実行数で決まる。逐次だと 20 銘柄で 10 時間を超える。
const REPO = 'kabu-ledger'

// args の受け取り:
//   ["3851","4073"]                       → この2銘柄
//   {codes:["3851"], mode:"full"}         → モードを強制
//   未指定                                 → master.yaml の全銘柄
const _args = args || {}
const CODES = Array.isArray(_args) ? _args : (_args.codes || null)
const FORCE_MODE = Array.isArray(_args) ? null : (_args.mode || null)
const SKIP_VERIFY = Array.isArray(_args) ? false : Boolean(_args.skipVerify)

const WROTE_SCHEMA = {
  type: 'object',
  required: ['code', 'mode', 'wrote', 'new_claims', 'week', 'summary'],
  additionalProperties: false,
  properties: {
    code: { type: 'string' },
    mode: { type: 'string', enum: ['初回', '再調査', '追記のみ', 'スキップ'] },
    wrote: { type: 'boolean', description: 'reports/{code}.md を実際に書き換えたか' },
    new_claims: { type: 'boolean', description: '今週の追記に、出典を伴う新しい事実主張が含まれるか。「特筆すべき動きなし」等の定型1行だけなら false' },
    week: { type: 'string', description: '追記した週キー（YYYY-Www）。書いていなければ空' },
    summary: { type: 'string', description: '今週の要点を1〜2文' },
    sources: { type: 'array', items: { type: 'string' }, description: '今回使った出典URL' },
    unresolved: { type: 'array', items: { type: 'string' }, description: '確かめられなかったこと' },
  },
}

const VERIFY_SCHEMA = {
  type: 'object',
  required: ['code', 'claims', 'supported', 'problems'],
  additionalProperties: false,
  properties: {
    code: { type: 'string' },
    claims: { type: 'integer' },
    supported: { type: 'integer' },
    problems: { type: 'array', items: { type: 'string' } },
  },
}

const COMMON = `
リポジトリ: ${REPO}（相対パスはこのディレクトリを基点にする）
シェルは Windows PowerShell 5.1。&& と || は使えない。; と if ($?) を使う。
Python を叩く前に $env:PYTHONIOENCODING = "utf-8" を設定する。

**あなたは1銘柄だけを担当する。** 他の銘柄の reports/ や data/verification/ を
読まない・書かない（並列実行で衝突する）。
**data/ と docs/ と master.yaml は書かない。** git 操作（add/commit/push）もしない。
`

phase('執筆')

const codes = CODES && CODES.length
  ? CODES
  : (await agent(
      `${COMMON}\n${REPO}/data/master.yaml の stocks から証券コードだけを抜き、` +
      `JSON 配列で返せ。他には何も書かない。例: ["3851","4073"]`,
      { label: '銘柄一覧', phase: '執筆', effort: 'low' },
    ).then((t) => {
      try { return JSON.parse(String(t).match(/\[[\s\S]*\]/)[0]) } catch (e) { return [] }
    }))

if (!codes.length) {
  log('銘柄が1件も取れなかった。master.yaml を確認すること')
  return { error: 'no codes' }
}
log(`${codes.length} 銘柄を並列で処理する: ${codes.join(', ')}`)

const results = await pipeline(
  codes,
  // --- 1. 執筆（1銘柄1エージェント） ---
  (code) => agent(
    `${COMMON}
## あなたの担当: 銘柄 ${code} のレポート

**必ず ${REPO}/.claude/skills/kabu-ledger-report/SKILL.md を最初に読み、その手順に従うこと。**
このプロンプトと SKILL.md が食い違ったら SKILL.md が正。

${FORCE_MODE ? `モードは "${FORCE_MODE}" を強制する。` : 'モードは SKILL.md の判定表に従って自分で決める。'}

追記ルール・書き方・モード判定・出典の扱いはすべて SKILL.md が正（ここに要約は置かない）。

終わったら ${REPO} で
\`$env:PYTHONIOENCODING="utf-8"; python src/checks.py > "$env:TEMP\\checks-${code}.txt"\`
を実行し、出力ファイルを Grep で「${code}」を含む行だけ確認して
自銘柄の FAIL が無いことを確かめる。**出力全文をコンテキストに読み込まない**
（他の銘柄の FAIL は並列作業中の別エージェントのもの。直そうとしない・
自分の失敗として報告しない。報告文に1行添えるだけでよい）。
自分の銘柄で「週次アップデート ... が書き換わっている」が出たら、
過去週を元に戻して別エントリとして書き直す（この FAIL を残したまま返してはならない）。`,
    { label: `執筆:${code}`, phase: '執筆', schema: WROTE_SCHEMA },
  ),

  // --- 2. 裏取り（**別コンテキスト**。書いた本人にはやらせない） ---
  // 新しい事実主張が無い週（「動きなし」定型のみ）は検証対象が増えていないので回さない。
  // new_claims が未報告（undefined）の場合は裏取りに回す＝フェイルセーフは検証が増える側。
  (wrote, code) => {
    if (SKIP_VERIFY || !wrote || !wrote.wrote || wrote.new_claims === false) {
      return { code, skipped: true, wrote }
    }
    return agent(
      `${COMMON}
## あなたの担当: 銘柄 ${code} のレポートの裏取り

**あなたはこのレポートを書いていない。** 書いた本人とは別の文脈で検証するのが目的なので、
本文の言い分を信用せず、出典を自分で取り直して判定すること。

**必ず ${REPO}/.claude/skills/kabu-ledger-verify/SKILL.md を読み、その手順に従うこと。**
出典の再取得は src/fetch_source.py を使い、結果を data/verification/${code}.yaml に
**追記**する（過去 run を書き換えない）。

裏が取れない記述は落とすか「未確認」と明示する。黙って残さない。`,
      { label: `裏取り:${code}`, phase: '裏取り', schema: VERIFY_SCHEMA },
    ).then((v) => ({ code, wrote, verify: v }))
  },
)

const done = results.filter(Boolean)
log(`執筆 ${done.length} 銘柄が完了。全体検査に入る`)

phase('検査')

// 全銘柄ぶんが揃ってから1回だけ。銘柄ごとに走らせると git HEAD 比較が
// 互いの中間状態を拾う。
const final = await agent(
  `${COMMON}
## あなたの担当: 全銘柄ぶんの反映後の検査

${REPO} で次を順に実行し、結果をそのまま報告せよ。**直してはならない**
（何が落ちているかを人間に見せるのが仕事。勝手に直すと原因が消える）。

    $env:PYTHONIOENCODING = "utf-8"
    python src/checks.py
    python tools/run_tests.py
    python src/build.py

報告に必ず含めるもの:
- checks.py の FAIL 件数と、FAIL があればその全文
- run_tests.py で exit≠0 になったファイル名
- \`git status --short\` の出力（どのファイルが変わったか）

git 操作はしない。commit も push もしない。`,
  { label: '検査:全体', phase: '検査' },
)

return {
  codes,
  written: done.filter((d) => d.wrote && d.wrote.wrote).map((d) => d.code || d.wrote.code),
  reports: done,
  check: final,
}
