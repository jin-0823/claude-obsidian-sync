# タスクスケジューラから定期実行するエントリポイント（パイプラインA）。
# Downloads フォルダを見て、Claude.ai のエクスポートZIPらしきものだけを
# 多段階チェックで確定し、確定したものだけ run_import.ps1 経由でVaultに取り込む。
#
# 誤検知防止の考え方:
#   1次: ファイル名が data-*.zip または *batch*.zip に一致する候補だけを見る
#        (無関係なZIP — 授業資料・ドライバ等 — は毎回ログにも残さず素通り)
#   2次/3次: candidate だけ Python (src\validate_export_zip.py) で
#        zip構造 (conversations.json / users.json の有無) と
#        中身 (chat_messages キーの有無) を検証
#   確定したものだけ run_import.ps1 を呼ぶ。一致しなかった候補は何もせず、
#   判定結果を state\processed_zips.json と本ログに記録するだけに留める。
#
# 二重処理防止: SHA256ハッシュを state\processed_zips.json に記録し、
# status が imported/invalid のファイルは再検証しない。
# status が failed (nexus-cli実行失敗) のものは次回再試行する。

$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$downloadsDir = Join-Path $env:USERPROFILE "Downloads"
$logDir = Join-Path $root "logs"
$statePath = Join-Path $root "state\processed_zips.json"
$validator = Join-Path $root "src\validate_export_zip.py"
$runImport = Join-Path $root "run_import.ps1"

New-Item -ItemType Directory -Force -Path $logDir, (Split-Path -Parent $statePath) | Out-Null

$timestamp = Get-Date -Format "yyyy-MM-dd_HHmmss"
$logFile = Join-Path $logDir "watch_$timestamp.log"

$noBomUtf8 = New-Object System.Text.UTF8Encoding($false)

function Write-Log([string]$Message) {
    $line = "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] $Message"
    [System.IO.File]::AppendAllText($logFile, "$line`r`n", $noBomUtf8)
}

function Get-State {
    if (Test-Path $statePath) {
        return Get-Content $statePath -Raw -Encoding utf8 | ConvertFrom-Json
    }
    return New-Object PSObject
}

function Save-State($state) {
    $state | ConvertTo-Json -Depth 10 | Out-File -FilePath $statePath -Encoding utf8
}

function Get-StateEntry($state, [string]$hash) {
    $prop = $state.PSObject.Properties[$hash]
    if ($prop) { return $prop.Value }
    return $null
}

Write-Log "=== watch_downloads run start ==="

if (-not (Test-Path $downloadsDir)) {
    Write-Log "Downloads folder not found: $downloadsDir"
    exit 1
}

$candidates = Get-ChildItem -Path $downloadsDir -Filter *.zip -File -ErrorAction SilentlyContinue |
    Where-Object { $_.Name -match '^data-.*\.zip$' -or $_.Name -match 'batch.*\.zip$' }

Write-Log "Found $($candidates.Count) filename-candidate zip(s) in $downloadsDir"

$hadFailure = $false

foreach ($file in $candidates) {
    try {
        $hash = (Get-FileHash -Path $file.FullName -Algorithm SHA256).Hash
        $state = Get-State
        $existing = Get-StateEntry $state $hash

        if ($existing -and ($existing.status -eq "imported" -or $existing.status -eq "invalid")) {
            Write-Log "SKIP $($file.Name) (sha256=$hash) — already processed, status=$($existing.status)"
            continue
        }

        Write-Log "CHECK $($file.Name) (sha256=$hash)"
        $prevEAP = $ErrorActionPreference
        $ErrorActionPreference = "Continue"
        $validationJson = & python $validator $file.FullName 2>&1 | Out-String
        $ErrorActionPreference = $prevEAP
        $validation = $null
        try {
            $validation = $validationJson | ConvertFrom-Json
        } catch {
            Write-Log "VALIDATOR ERROR for $($file.Name): could not parse validator output: $validationJson"
            $hadFailure = $true
            continue
        }

        if (-not $validation.valid) {
            Write-Log "REJECT $($file.Name) — $($validation.reason)"
            $state = Get-State
            $entry = [PSCustomObject]@{
                fileName = $file.Name
                status   = "invalid"
                reason   = $validation.reason
                checkedAt = (Get-Date).ToString("o")
            }
            $state | Add-Member -NotePropertyName $hash -NotePropertyValue $entry -Force
            Save-State $state
            continue
        }

        Write-Log "ACCEPT $($file.Name) — looks like a Claude.ai export ($($validation.conversationCount) conversations). Handing off to run_import.ps1"

        & $runImport -ZipPath $file.FullName
        $importExit = $LASTEXITCODE

        if ($importExit -eq 0) {
            Write-Log "IMPORT OK $($file.Name)"
        } else {
            Write-Log "IMPORT FAILED $($file.Name) (exit=$importExit) — will retry next run"
            $hadFailure = $true
        }
    } catch {
        Write-Log "ERROR while processing $($file.Name): $_"
        Write-Log "STACK: $($_.ScriptStackTrace)"
        $hadFailure = $true
    }
}

Write-Log "=== watch_downloads run end ==="

if ($hadFailure) { exit 1 } else { exit 0 }
