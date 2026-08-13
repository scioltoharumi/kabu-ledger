<#
.SYNOPSIS
  docs/ の静的ページを headless Edge で採寸し、「横に伸びていないか」を数値で判定する。

.DESCRIPTION
  目視スクショの代わりに scrollWidth / clientWidth を全要素で採り、閾値で FAIL/WARN を出す。
  複数ページ x 複数幅を **Edge 1プロセス**で回す（iframe に埋めて幅を作る）。
  -Shot を付けたときだけ PNG も撮る（PNG は 1プロセス1枚しか撮れないため別ループ）。

  内包している既知の落とし穴:
   (a) --user-data-dir を渡さないと、起動中の Edge に URL を渡して即 exit=0 で終わる
       （出力は「既存のブラウザー セッションで開いています。」の45バイトだけ）。
       → 実行ごとに GUID 付きの絶対パスを渡し、出力が短すぎたら明示的に落とす。
   (b) msedge.exe は GUI サブシステムなので `& msedge --dump-dom` では stdout を拾えない。
       → Start-Process -RedirectStandardOutput でファイルに落とす。
   (c) --window-size の幅は約500px が下限（390 を渡すと 496 でレイアウトされ画像だけ切れる）。
       → 幅は iframe の width で作る。390 でも innerWidth=390 になる。
   (d) 1プロセス1枚しか撮れない（--screenshot を2つ渡すと後勝ち、URL 2つは
       "Multiple targets are not supported in headless mode." で落ちる）。
       → 採寸は1プロセス、撮影だけ枚数分プロセスを起こす。
   (e) PNG は書き終わる前に見に行くと無い。→ サイズが2回続けて同じになるまで待つ。
   (f) 日本語パス・Gドライブを踏まないよう、docs/ を %TEMP% 配下の ASCII パスへ複製してから読む。
       （相対 --user-data-dir を渡すと Gドライブ上に profile を作ろうとして無限ハングする）

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File tools\shot.ps1
  powershell -ExecutionPolicy Bypass -File tools\shot.ps1 -Shot -Widths 1200,390
#>
[CmdletBinding()]
param(
  [string]   $Docs,
  # powershell -File 経由だと "a,b" が1つの文字列で来る（-Widths 1200,390 は
  # 1200390 という1つの数に化ける）。両方受けられるよう object で取って自前で割る。
  [object]   $Pages,
  [object]   $Widths = @(1200, 900, 500, 390),
  [switch]   $Shot,
  [string]   $OutDir,
  [string]   $Edge,
  [int]      $MaxContentPx = 1400,   # どの要素も中身の実寸がこれを超えたら FAIL
  [double]   $MaxRatio     = 2.5,    # 折り返す想定の要素(overflow:visible)の sw/cw 上限
  [int]      $MaxOverPx    = 400,    # 同、はみ出しの絶対量の上限
  [double]   $MaxDesktopScrollRatio = 1.5,  # 幅900px以上での .scroll の sw/cw 上限
  [int]      $TimeoutSec   = 180
)

$ErrorActionPreference = 'Stop'

function Split-List {
  param([object] $Value)
  if ($null -eq $Value) { return @() }
  @($Value) | ForEach-Object { [string]$_ } |
    ForEach-Object { $_ -split '[,;\s]+' } | Where-Object { $_ -ne '' }
}

# ---- 1. 入力の解決 ---------------------------------------------------------
$Widths = @(Split-List $Widths | ForEach-Object { [int]$_ })
foreach ($w in $Widths) {
  if ($w -lt 200 -or $w -gt 4000) {
    throw "幅 $w は範囲外（200-4000）。-Widths は 1200,900 のようにカンマ区切りで渡す"
  }
}
if (-not $Docs) {
  $Docs = Join-Path (Split-Path -Parent $PSScriptRoot) 'docs'
}
$Docs = (Resolve-Path $Docs).Path
if (-not $Edge) {
  foreach ($c in @(
      "${env:ProgramFiles(x86)}\Microsoft\Edge\Application\msedge.exe",
      "C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
      "C:\Program Files\Microsoft\Edge\Application\msedge.exe",
      "C:\Program Files\Google\Chrome\Application\chrome.exe")) {
    if (Test-Path $c) { $Edge = $c; break }
  }
}
if (-not $Edge -or -not (Test-Path $Edge)) {
  Write-Host "[SKIP] Edge/Chrome が見つからない。採寸せず終了する（CI では正常）"
  exit 0
}

$runId = [Guid]::NewGuid().ToString('N').Substring(0, 8)
if (-not $OutDir) { $OutDir = Join-Path $env:TEMP "kabu-shot\$runId" }
$site = Join-Path $OutDir 'site'
$prof = Join-Path $OutDir 'profile'   # (a)(f) 絶対パス・ASCII・実行ごとに新規
New-Item -ItemType Directory -Path $site -Force | Out-Null
New-Item -ItemType Directory -Path $prof -Force | Out-Null

# (f) 日本語パス / Gドライブを避けて %TEMP% に複製する
Copy-Item (Join-Path $Docs '*') $site -Recurse -Force

$Pages = @(Split-List $Pages)
if ($Pages.Count -eq 0) {
  $Pages = @(Get-ChildItem $site -Recurse -Filter *.html |
           ForEach-Object { $_.FullName.Substring($site.Length + 1).Replace('\', '/') } |
           Where-Object { $_ -notlike '__*' } | Sort-Object)
}
Write-Host ("[i] Edge  : {0}" -f $Edge)
Write-Host ("[i] docs  : {0} -> {1}" -f $Docs, $site)
Write-Host ("[i] pages : {0} / widths: {1}" -f ($Pages -join ', '), ($Widths -join ', '))

# ---- 2. 採寸用ハーネスを書く（1プロセスで全ページ x 全幅） -----------------
$js = @'
var PAGES=__PAGES__, WIDTHS=__WIDTHS__, jobs=[], results=[], idx=0;
PAGES.forEach(function(p){WIDTHS.forEach(function(w){jobs.push({p:p,w:w});});});
function sel(e){
  var c=(typeof e.className==="string"&&e.className.trim())?("."+e.className.trim().replace(/\s+/g,".")):"";
  return e.tagName.toLowerCase()+(e.id?("#"+e.id):"")+c;
}
function probe(win,doc){
  var r={iw:win.innerWidth,docsw:doc.documentElement.scrollWidth,
         sh:doc.documentElement.scrollHeight,over:[]},els=doc.querySelectorAll("*"),i,e,d,cs;
  for(i=0;i<els.length;i++){
    e=els[i];
    if(e.namespaceURI!=="http://www.w3.org/1999/xhtml") continue; /* SVG の <text> は別勘定 */
    if(!e.clientWidth) continue;
    d=e.scrollWidth-e.clientWidth;
    if(d>1){
      cs=win.getComputedStyle(e);
      r.over.push({sel:sel(e),cw:e.clientWidth,sw:e.scrollWidth,d:d,ox:cs.overflowX});
    }
  }
  r.over.sort(function(a,b){return b.d-a.d;});
  r.over=r.over.slice(0,8);
  return r;
}
function next(){
  if(idx>=jobs.length){finish();return;}
  var j=jobs[idx++],f=document.createElement("iframe");
  f.style.width=j.w+"px";f.style.height="3000px";f.src=j.p;   /* (c) 幅は iframe で作る */
  f.onload=function(){
    try{
      var d=f.contentDocument;
      f.style.height=Math.max(1000,d.documentElement.scrollHeight+50)+"px"; /* 縦スクロールバーを消す */
      void f.offsetHeight;
      setTimeout(function(){
        var r=probe(f.contentWindow,f.contentDocument);
        r.page=j.p;r.want=j.w;results.push(r);
        f.parentNode.removeChild(f);next();
      },50);
    }catch(err){
      results.push({page:j.p,want:j.w,error:String(err)});
      f.parentNode.removeChild(f);next();
    }
  };
  document.getElementById("host").appendChild(f);
}
function finish(){
  document.getElementById("out").textContent=
    btoa(unescape(encodeURIComponent(JSON.stringify(results))));
  document.title="DONE "+results.length;
}
next();
'@
$js = $js.Replace('__PAGES__',  (ConvertTo-Json @($Pages)  -Compress))
$js = $js.Replace('__WIDTHS__', (ConvertTo-Json @($Widths) -Compress))
$harness = @"
<!DOCTYPE html><html><head><meta charset="utf-8"><title>RUNNING</title>
<style>html,body{margin:0;padding:0}iframe{border:0;display:block}</style></head>
<body><div id="host"></div><pre id="out"></pre><script>
$js
</script></body></html>
"@
$harnessPath = Join-Path $site '__measure.html'
[IO.File]::WriteAllText($harnessPath, $harness, (New-Object Text.UTF8Encoding($false)))

function Invoke-Edge {
  param([string[]] $ExtraArgs, [string] $Url, [string] $StdOut, [int] $Sec)
  $a = @('--headless=new', '--disable-gpu', '--no-first-run', '--no-default-browser-check',
         '--disable-extensions', '--allow-file-access-from-files',
         "--user-data-dir=$prof") + $ExtraArgs + @($Url)
  if ($StdOut) {
    $p = Start-Process -FilePath $Edge -ArgumentList $a -NoNewWindow -PassThru -RedirectStandardOutput $StdOut
  } else {
    $p = Start-Process -FilePath $Edge -ArgumentList $a -NoNewWindow -PassThru
  }
  if (-not $p.WaitForExit($Sec * 1000)) {
    Stop-Process -Id $p.Id -Force -ErrorAction SilentlyContinue
    throw "Edge が $Sec 秒で終わらなかった: $Url"
  }
}

function To-FileUrl { param([string] $Path) 'file:///' + ($Path -replace '\\', '/') }

# ---- 3. 採寸（1プロセス） --------------------------------------------------
$domFile = Join-Path $OutDir 'dom.html'
$budget  = [Math]::Max(20000, 1500 * $Pages.Count * $Widths.Count)
$sw = [Diagnostics.Stopwatch]::StartNew()
Invoke-Edge -ExtraArgs @('--window-size=1400,1000', "--virtual-time-budget=$budget", '--dump-dom') `
            -Url (To-FileUrl $harnessPath) -StdOut $domFile -Sec $TimeoutSec
$sw.Stop()
$dom = Get-Content $domFile -Raw
if (-not $dom -or $dom.Length -lt 200) {
  # (a) 起動中の Edge に横取りされた典型パターン
  Write-Host "[FAIL] Edge が DOM を返さなかった。出力: $dom"
  Write-Host "       起動中の Edge にURLを渡して終了した可能性が高い（--user-data-dir を確認）"
  exit 2
}
if ($dom -notmatch '<title>DONE (\d+)</title>') {
  Write-Host "[FAIL] 採寸が完走しなかった（title が DONE にならない）。$domFile を見ること"
  exit 2
}
if ($dom -notmatch '(?s)<pre id="out">(.*?)</pre>') {
  Write-Host "[FAIL] 結果が取れなかった。$domFile を見ること"; exit 2
}
$json = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($Matches[1].Trim()))
[IO.File]::WriteAllText((Join-Path $OutDir 'metrics.json'), $json)
$rows = $json | ConvertFrom-Json
Write-Host ("[i] 採寸 {0} 件 / {1} ms（Edge 1プロセス）" -f $rows.Count, $sw.ElapsedMilliseconds)
Write-Host ''

# ---- 4. 判定 ---------------------------------------------------------------
$fails = 0; $warns = 0
Write-Host ("{0,-26} {1,-6} {2,-7} {3}" -f 'page', 'width', 'verdict', 'detail')
Write-Host ('-' * 100)
foreach ($r in $rows) {
  $bad = @(); $soft = @()
  if ($r.error) { $bad += "probe error: $($r.error)" }
  foreach ($e in $r.over) {
    $ratio = [Math]::Round($e.sw / [double]$e.cw, 2)
    if ($e.sw -gt $MaxContentPx) {
      $bad += ("{0} の中身が {1}px（上限 {2}px, 枠 {3}px）" -f $e.sel, $e.sw, $MaxContentPx, $e.cw)
    } elseif ($e.ox -eq 'visible' -or $e.ox -eq 'clip') {
      if ($ratio -gt $MaxRatio -or $e.d -gt $MaxOverPx) {
        $bad += ("{0} が枠を {1}px 超過（x{2}, 折り返していない）" -f $e.sel, $e.d, $ratio)
      }
    } elseif ($r.iw -ge 900 -and $ratio -gt $MaxDesktopScrollRatio) {
      # 横スクロールは「スマホで表を内側に流す」ための設計。デスクトップ幅で
      # 大きく流れるのは設計ではなく崩れ（実測: 健全 x1.12 / 崩れ x1.79〜2.39）
      $bad += ("{0} がデスクトップ幅で x{1} 横スクロールする（{2}px / 枠 {3}px）" -f $e.sel, $ratio, $e.sw, $e.cw)
    }
  }
  if ($r.docsw -gt $r.iw + 2) { $soft += ("ページ自体が横スクロールする（{0} > {1}）" -f $r.docsw, $r.iw) }
  if ($bad.Count -gt 0) {
    $fails++
    Write-Host ("{0,-26} {1,-6} {2,-7} {3}" -f $r.page, $r.want, 'FAIL', (($bad | Select-Object -First 2) -join ' / ')) -ForegroundColor Red
  } elseif ($soft.Count -gt 0) {
    $warns++
    Write-Host ("{0,-26} {1,-6} {2,-7} {3}" -f $r.page, $r.want, 'WARN', ($soft -join ' / ')) -ForegroundColor Yellow
  } else {
    Write-Host ("{0,-26} {1,-6} {2,-7} {3}" -f $r.page, $r.want, 'ok', '')
  }
}
Write-Host ''
Write-Host ("FAIL {0} / WARN {1} / 計 {2}" -f $fails, $warns, $rows.Count)

# ---- 5. 目で見たいときだけ PNG（(d) 1プロセス1枚） -------------------------
if ($Shot) {
  $shotDir = Join-Path $OutDir 'png'
  New-Item -ItemType Directory -Path $shotDir -Force | Out-Null
  $n = 0
  foreach ($r in $rows) {
    $n++
    $h = [Math]::Max(400, [Math]::Min([int]$r.sh + 40, 8000))   # 巨大な window-size は Edge が固まる
    $wrap = Join-Path $site ("__shot{0}.html" -f $n)
    $body = @"
<!DOCTYPE html><html><head><meta charset="utf-8"><title>shot</title>
<style>html,body{margin:0;padding:0;overflow:hidden;background:#fff}
iframe{border:0;display:block;width:$($r.want)px;height:$($h)px}</style></head>
<body><iframe src="$($r.page)"></iframe></body></html>
"@
    [IO.File]::WriteAllText($wrap, $body, (New-Object Text.UTF8Encoding($false)))
    $png = Join-Path $shotDir ("{0}_{1}.png" -f ($r.page -replace '[\\/]', '_' -replace '\.html$', ''), $r.want)
    Invoke-Edge -ExtraArgs @("--window-size=$($r.want),$h", "--screenshot=$png") `
                -Url (To-FileUrl $wrap) -Sec 60
    # (e) PNG は書き終わるまで待つ（サイズが2回続けて同じになったら完了とみなす）
    $t = [Diagnostics.Stopwatch]::StartNew(); $last = -1; $stable = 0
    while ($t.ElapsedMilliseconds -lt 15000) {
      if (Test-Path $png) {
        $s = (Get-Item $png).Length
        if ($s -gt 0 -and $s -eq $last) { $stable++; if ($stable -ge 2) { break } } else { $stable = 0 }
        $last = $s
      }
      Start-Sleep -Milliseconds 100
    }
    Write-Host ("[png] {0}  ({1} bytes)" -f $png, $last)
  }
}

Write-Host ''
Write-Host ("[i] 作業物: {0}" -f $OutDir)
if ($fails -gt 0) { exit 1 }
exit 0
