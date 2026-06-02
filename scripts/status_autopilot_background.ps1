param(
    [string]$Username = "@iamnekiy",
    [int]$Tail = 20
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
    Write-Output "autopilot stopped for $normalizedUsername"
} else {
    Write-Output "autopilot running for $normalizedUsername"
    $processes | ForEach-Object {
        Write-Output "pid=$($_.ProcessId) parent=$($_.ParentProcessId)"
    }
}

$latestOut = Get-ChildItem -Path $RuntimeLogs -Filter "autopilot_${safeUsername}_*.out.log" -ErrorAction SilentlyContinue |
    Sort-Object LastWriteTime -Descending |
    Select-Object -First 1
$latestErr = Get-ChildItem -Path $RuntimeLogs -Filter "autopilot_${safeUsername}_*.err.log" -ErrorAction SilentlyContinue |
    Sort-Object LastWriteTime -Descending |
    Select-Object -First 1

if ($latestOut) {
    Write-Output ""
    Write-Output "stdout=$($latestOut.FullName)"
    Get-Content -Path $latestOut.FullName -Tail $Tail
}

if ($latestErr -and $latestErr.Length -gt 0) {
    Write-Output ""
    Write-Output "stderr=$($latestErr.FullName)"
    Get-Content -Path $latestErr.FullName -Tail $Tail
}
