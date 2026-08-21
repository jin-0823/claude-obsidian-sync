# nexus-cli 呼び出し本体（パイプラインA）。
# watch_downloads.ps1 から -ZipPath 付きで呼ばれるほか、単体でも実行できる。
#
# 成功時: 対象ZIPを archive\ に移動し、state\processed_zips.json に記録する。
# 失敗時: ZIPはそのまま残す（次回実行時に再試行される）。
#
# 既知の問題1: nexus-cli (moment.js まわりのバグ) は、既存の会話をスキャンする際に
# "parseDate - FAILED: ... ISO_8601" という無害な例外ログを大量に出す。
# これは実際の失敗とは無関係なので、成功判定には使わない
# （終了コードと "--- Import Summary ---" の Failed 件数だけを見る）。
#
# 既知の問題2: ダウンロード直後のファイルはウイルス対策ソフト等に削除/リネームだけ
# ロックされることがある（読み取り・コピーは通る）。そのため移動はCopy+Remove方式にし、
# ファイル書き込み系の操作は Invoke-WithRetry で数回リトライする。

param(
    [Parameter(Mandatory = $true)]
    [string]$ZipPath
)

$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$vaultPath = Join-Path (Split-Path -Parent $root) "obsidian-vault"
$nexusCli = Join-Path $root "cli\cli\dist\nexus-cli.js"
$momentShim = Join-Path $root "nexus_cli_window_moment_shim.js"
$archiveDir = Join-Path $root "archive"
$logDir = Join-Path $root "logs"
$statePath = Join-Path $root "state\processed_zips.json"

New-Item -ItemType Directory -Force -Path $archiveDir, $logDir, (Split-Path -Parent $statePath) | Out-Null

function Invoke-WithRetry {
    param(
        [Parameter(Mandatory = $true)][scriptblock]$Action,
        [int]$MaxAttempts = 5,
        [int]$DelayMs = 500
    )
    for ($i = 1; $i -le $MaxAttempts; $i++) {
        try {
            & $Action
            return
        } catch {
            if ($i -eq $MaxAttempts) { throw }
            Start-Sleep -Milliseconds $DelayMs
        }
    }
}

function Get-State {
    if (Test-Path $statePath) {
        return Get-Content $statePath -Raw -Encoding utf8 | ConvertFrom-Json
    }
    return New-Object PSObject
}

function Save-State($state) {
    $json = $state | ConvertTo-Json -Depth 10
    Invoke-WithRetry { $json | Out-File -FilePath $statePath -Encoding utf8 -ErrorAction Stop }
}

$noBomUtf8 = New-Object System.Text.UTF8Encoding($false)

function Write-Log([string]$Message) {
    $line = "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] $Message"
    Write-Output $line
    Invoke-WithRetry { [System.IO.File]::AppendAllText($script:currentLogFile, "$line`r`n", $noBomUtf8) }
}

if (-not (Test-Path $ZipPath)) {
    throw "ZIP not found: $ZipPath"
}
if (-not (Test-Path $nexusCli)) {
    throw "nexus-cli not found at $nexusCli — see CLAUDE.md for build steps"
}
if (-not (Test-Path $momentShim)) {
    throw "moment shim not found at $momentShim"
}

$timestamp = Get-Date -Format "yyyy-MM-dd_HHmmss"
$script:currentLogFile = Join-Path $logDir "import_$timestamp.log"

$fileName = Split-Path -Leaf $ZipPath
$hash = (Get-FileHash -Path $ZipPath -Algorithm SHA256).Hash

Write-Log "Importing $fileName (sha256=$hash)"

# stderr を 2>&1 で合流させる際、$ErrorActionPreference = "Stop" のままだと
# nexus-cli が標準エラーに出す無害な例外ログ（moment.js のバグ由来）まで
# 終端エラーとして扱われ、スクリプトが異常終了してしまう。この呼び出しの間だけ緩める。
$prevEAP = $ErrorActionPreference
$ErrorActionPreference = "Continue"
$nodeOutput = & node --require $momentShim $nexusCli import --vault $vaultPath --input $ZipPath --provider claude --verbose 2>&1 | Out-String
$exitCode = $LASTEXITCODE
$ErrorActionPreference = $prevEAP

$summaryMatch = [regex]::Match($nodeOutput, "Created:\s*(\d+)\s*[\r\n]+Updated:\s*(\d+)\s*[\r\n]+Skipped:\s*(\d+)\s*[\r\n]+Failed:\s*(\d+)")

if ($summaryMatch.Success) {
    $created = [int]$summaryMatch.Groups[1].Value
    $updated = [int]$summaryMatch.Groups[2].Value
    $skipped = [int]$summaryMatch.Groups[3].Value
    $failed = [int]$summaryMatch.Groups[4].Value
    Write-Log "nexus-cli summary: created=$created updated=$updated skipped=$skipped failed=$failed (exit=$exitCode)"
} else {
    Write-Log "nexus-cli did not print an Import Summary (exit=$exitCode) — treating as failure"
    Write-Log "--- full nexus-cli output ---"
    Invoke-WithRetry { [System.IO.File]::AppendAllText($script:currentLogFile, "$nodeOutput`r`n", $noBomUtf8) }
}

# 生の nexus-cli 出力は常にログへ全文保存する（moment.js の例外ログも含む。デバッグ用）
$rawLogFile = Join-Path $logDir "import_${timestamp}_raw.log"
Invoke-WithRetry { $nodeOutput | Out-File -FilePath $rawLogFile -Encoding utf8 -ErrorAction Stop }

$success = ($exitCode -eq 0) -and $summaryMatch.Success -and ($failed -eq 0)

$state = Get-State
$entry = [PSCustomObject]@{
    fileName      = $fileName
    status        = if ($success) { "imported" } else { "failed" }
    importedAt    = (Get-Date).ToString("o")
    exitCode      = $exitCode
    created       = if ($summaryMatch.Success) { $created } else { $null }
    updated       = if ($summaryMatch.Success) { $updated } else { $null }
    skipped       = if ($summaryMatch.Success) { $skipped } else { $null }
    failed        = if ($summaryMatch.Success) { $failed } else { $null }
    rawLogFile    = (Split-Path -Leaf $rawLogFile)
}

if (-not $success) {
    Write-Log "Import failed — leaving $fileName in place for retry. See $((Split-Path -Leaf $rawLogFile)) for details."
    Write-Log "RESULT FAILED"
    exit 1
}

# Move-Item (=rename) は、ダウンロード直後のファイルに対して Windows Defender等が
# 削除/リネームだけをブロックすることがある（読み取り・コピーは問題なく通る）。
# そのため Copy-Item で archive\ に複製し、元ファイルの削除はベストエフォートにする。
# コピーさえ成功すれば import 自体は成功として扱ってよい
# （重複防止はファイル内容のSHA256ハッシュで行っており、置き場所には依存しないため）。
$destPath = Join-Path $archiveDir $fileName
try {
    Invoke-WithRetry -MaxAttempts 10 -DelayMs 2000 -Action { Copy-Item -Path $ZipPath -Destination $destPath -Force -ErrorAction Stop }
} catch {
    Write-Log "Import succeeded but copying $fileName to archive\ failed: $($_.Exception.Message) — leaving in Downloads for retry"
    Write-Log "RESULT FAILED"
    exit 1
}
if (-not (Test-Path $destPath)) {
    Write-Log "Copy-Item reported success but $destPath does not exist — treating as failure"
    Write-Log "RESULT FAILED"
    exit 1
}

Write-Log "Copied $fileName to archive\"
$state | Add-Member -NotePropertyName $hash -NotePropertyValue $entry -Force
Save-State $state

try {
    Invoke-WithRetry -MaxAttempts 3 -DelayMs 1000 -Action { Remove-Item -Path $ZipPath -Force -ErrorAction Stop }
    Write-Log "Removed $fileName from Downloads"
} catch {
    Write-Log "WARNING: could not delete $fileName from Downloads (already imported to archive\, safe to delete manually): $($_.Exception.Message)"
}

Write-Log "RESULT SUCCESS"
exit 0
