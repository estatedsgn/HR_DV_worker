param(
    [string]$Username = "@iamnekiy",
    [int]$PollIntervalSeconds = 2,
    [int]$RecoverOlderThanSeconds = 3,
    [double]$TypingDelaySeconds = 10,
    [double]$TypingMinDelaySeconds = 5,
    [int]$VoiceRecordingDelaySeconds = 40,
    [int]$InboundDebounceSeconds = 3,
    [int]$CrmchatTimeoutSeconds = 60,
    [int]$ErrorBackoffSeconds = 20,
    [int]$FastPacingSeconds = 1,
    [string]$ModelTestProfile = "plus_max_router"
)

$ErrorActionPreference = "Stop"
$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$Python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
$RuntimeLogs = Join-Path $ProjectRoot "runtime_logs"

if (-not (Test-Path $Python)) {
    throw "Python venv not found: $Python"
}

New-Item -ItemType Directory -Force -Path $RuntimeLogs | Out-Null

$normalizedUsername = $Username.Trim()
if (-not $normalizedUsername.StartsWith("@")) {
    $normalizedUsername = "@$normalizedUsername"
}

$existing = Get-CimInstance Win32_Process |
    Where-Object {
        $_.CommandLine -match "scripts\\run_autopilot\.py" -and
        $_.CommandLine -match [regex]::Escape($normalizedUsername)
    }

if ($existing) {
    Write-Output "autopilot already running for $normalizedUsername"
    $existing | ForEach-Object { Write-Output "pid=$($_.ProcessId)" }
    exit 0
}

$stamp = Get-Date -Format "yyyyMMddTHHmmss"
$safeUsername = $normalizedUsername.TrimStart("@") -replace "[^A-Za-z0-9_.-]", "_"
$stdout = Join-Path $RuntimeLogs "autopilot_${safeUsername}_${stamp}.out.log"
$stderr = Join-Path $RuntimeLogs "autopilot_${safeUsername}_${stamp}.err.log"
$pidFile = Join-Path $RuntimeLogs "autopilot_${safeUsername}.pid"

$env:BRAIN_INBOUND_DEBOUNCE_SECONDS = "$InboundDebounceSeconds"
$env:OUTBOUND_TYPING_DELAY_SECONDS = "$TypingDelaySeconds"
$env:OUTBOUND_TYPING_MIN_DELAY_SECONDS = "$TypingMinDelaySeconds"
$env:OUTBOUND_VOICE_RECORDING_DELAY_SECONDS = "$VoiceRecordingDelaySeconds"
$env:CRMCHAT_TIMEOUT_SECONDS = "$CrmchatTimeoutSeconds"
$env:MODEL_TEST_PROFILE = "$ModelTestProfile"

$argsList = @(
    "-u",
    "scripts\run_autopilot.py",
    "--all-accounts",
    "--only-username", $normalizedUsername,
    "--allow-real-send",
    "--test-fast-pacing-seconds", "$FastPacingSeconds",
    "--poll-interval-seconds", "$PollIntervalSeconds",
    "--error-backoff-seconds", "$ErrorBackoffSeconds",
    "--recover-older-than-seconds", "$RecoverOlderThanSeconds",
    "--typing-delay-seconds", "$TypingDelaySeconds",
    "--model-test-profile", "$ModelTestProfile"
)

$process = Start-Process `
    -FilePath $Python `
    -ArgumentList $argsList `
    -WorkingDirectory $ProjectRoot `
    -RedirectStandardOutput $stdout `
    -RedirectStandardError $stderr `
    -WindowStyle Hidden `
    -PassThru

Set-Content -Path $pidFile -Value $process.Id -Encoding ascii
Write-Output "autopilot started for $normalizedUsername"
Write-Output "pid=$($process.Id)"
Write-Output "stdout=$stdout"
Write-Output "stderr=$stderr"
