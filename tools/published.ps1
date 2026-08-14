# 公開の到達確認。live の実バイトを origin/main の blob SHA と突き合わせる。
#
# 前提として2つの誤解を潰してある:
#  - `?cb=` は GitHub Pages の Fastly ではキャッシュキーに入らない（実測 x-cache=HIT /
#    Age は単調増加 / max-age=600）。無害なので付けてはいるが、迂回は期待しない。
#    STALE のときは -WaitSec 待って測り直す。
#  - live と main の**コミット SHA** の差は、docs ツリーが同一なら公開上は無差。
#    見るのは blob SHA-1 だけでよい。
#
# 使い方（marker は「今回入れた印で、いま live に無いもの」に限る）:
#   .\tools\published.ps1 -Marker prose-table   # push 前 → MISSING (exit 1)
#   git push origin main                        # push が公開を起こす（deploy.yml の push 契機）
#   .\tools\published.ps1 -Marker prose-table   # 公開後 → PUBLISHED (exit 0)
#
# **ワークフローを手で叩く手順は無い。** push すれば勝手に公開される。
# 公開まで1分ほどかかるので、STALE のあいだはこのスクリプトが待って再試行する。
param(
  [Parameter(Mandatory=$true)][string]$Marker,
  [int]$MinHits = 1,
  [int]$Retry = 4,
  [int]$WaitSec = 15
)
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$repo = "scioltoharumi/kabu-ledger"
$base = "https://scioltoharumi.github.io/kabu-ledger/"

function Probe {
  param($tree, $Marker)
  $sha1 = [Security.Cryptography.SHA1]::Create()   # FIPS 有効ホストで SHA1Managed は例外
  $bad = 0; $hits = 0; $lines = @()
  foreach ($e in $tree) {
    try {
      $r = Invoke-WebRequest ($base + $e.path + "?cb=" + [guid]::NewGuid()) -UseBasicParsing `
             -Headers @{'Cache-Control'='no-cache'; 'Pragma'='no-cache'} -TimeoutSec 20
      $b = $r.RawContentStream.ToArray()
    } catch {
      $bad++; $lines += ("{0,-16} {1,-6} {2}" -f $e.path, "ERROR", $_.Exception.Message); continue
    }
    $pre  = [Text.Encoding]::ASCII.GetBytes("blob " + $b.Length + "`0")
    $live = [BitConverter]::ToString($sha1.ComputeHash($pre + $b)).Replace('-','').ToLower()
    $ok   = ($live -eq $e.sha); if (-not $ok) { $bad++ }
    $n    = ([regex]::Matches([Text.Encoding]::UTF8.GetString($b), [regex]::Escape($Marker))).Count
    $hits += $n
    $lines += ("{0,-16} {1,-6} live={2} main={3} {4}={5}" -f `
               $e.path, $(if($ok){"OK"}else{"STALE"}), $live.Substring(0,8), $e.sha.Substring(0,8), $Marker, $n)
  }
  [pscustomobject]@{ Bad=$bad; Hits=$hits; Lines=$lines }
}

$tree = (gh api "repos/$repo/git/trees/main:docs?recursive=1" | ConvertFrom-Json).tree |
        Where-Object { $_.type -eq 'blob' -and $_.path -like '*.html' }
if (-not $tree) { "main:docs に .html が1つも無い"; exit 2 }

for ($i = 0; $i -le $Retry; $i++) {
  $res = Probe -tree $tree -Marker $Marker
  if ($res.Bad -eq 0) { break }
  if ($i -lt $Retry) {
    "STALE {0}/{1} ページ。CDN の max-age=600 を待って再試行 ({2}/{3})" -f $res.Bad, $tree.Count, ($i+1), $Retry
    Start-Sleep -Seconds $WaitSec
  }
}
$res.Lines
$fail = $res.Bad
if ($res.Hits -lt $MinHits) { $fail++ }

if ($res.Bad -ne 0) { "NOT PUBLISHED: $($res.Bad) / $($tree.Count) ページが origin/main と違う。publish がまだ回っていない" }
elseif ($res.Hits -lt $MinHits) { "MISSING: live は main と一致しているが marker '$Marker' が $($res.Hits) 件（要 $MinHits 件以上）。docs/ を再生成せずに src/ だけ push した疑い" }
else { "PUBLISHED: 全 $($tree.Count) ページが origin/main と一致 / marker '$Marker' $($res.Hits) 箇所" }
exit $fail
