# タスクスケジューラから毎日0:00に叩くエントリポイント。
# 前日 (JST) 分の Claude Code 会話ログを Obsidian Vault に書き出す。
# 成功/失敗を logs\run_YYYY-MM-DD_HHmmss.log に記録する。

$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$logDir = Join-Path $root "logs"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null

$timestamp = Get-Date -Format "yyyy-MM-dd_HHmmss"
$logFile = Join-Path $logDir "run_$timestamp.log"

$script = Join-Path $root "src\export_claude_code.py"

try {
    $output = & python $script 2>&1 | Out-String
    $exitCode = $LASTEXITCODE
} catch {
    $output = "[ERROR] exception while running export_claude_code.py: $_"
    $exitCode = 1
}

if ($exitCode -ne 0) {
    $output += "`n[RESULT] FAILED (exit code $exitCode)`n"
} else {
    $output += "`n[RESULT] SUCCESS`n"
}

$output | Out-File -FilePath $logFile -Encoding utf8

exit $exitCode
