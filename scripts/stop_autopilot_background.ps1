param(
    [string]$Username = "@iamnekiy"
)

$ErrorActionPreference = "Stop"
$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$RuntimeLogs = Join-Path $ProjectRoot "runtime_logs"
$normalizedUsername = $Username.Trim()
if (-not $normalizedUsername.StartsWith("@")) {
    $normalizedUsername = "@$normalizedUsername"
}
$safeUsername = $normalizedUsername.TrimStart("@") -replace "[^A-Za-z0-9_.-]", "_"

$processes = Get-CimInstance Win32_Process |
    Where-Object {
        $_.CommandLine -match "scripts\\run_autopilot\.py" -and
        $_.CommandLine -match [regex]::Escape($normalizedUsername)
    }

if (-not $processes) {
    Write-Output "autopilot already stopped for $normalizedUsername"
    exit 0
}

$processes | ForEach-Object {
    Write-Output "stopping pid=$($_.ProcessId)"
    Stop-Process -Id $_.ProcessId -Force
}

$pidFile = Join-Path $RuntimeLogs "autopilot_${safeUsername}.pid"
if (Test-Path $pidFile) {
    Remove-Item -LiteralPath $pidFile -Force
}

Write-Output "autopilot stopped for $normalizedUsername"
