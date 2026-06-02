param(
    [string]$Username = "@iamnekiy",
    [int]$PollIntervalSeconds = 2,
    [int]$RecoverOlderThanSeconds = 3,
    [double]$TypingDelaySeconds = 10,
    [int]$InboundDebounceSeconds = 3,
    [int]$CrmchatTimeoutSeconds = 60,
    [int]$ErrorBackoffSeconds = 20,
    [int]$FastPacingSeconds = 1,
    [string]$ModelTestProfile = "plus_max_router"
)

$ErrorActionPreference = "Stop"

& (Join-Path $PSScriptRoot "stop_autopilot_background.ps1") -Username $Username
Start-Sleep -Seconds 2
& (Join-Path $PSScriptRoot "start_autopilot_background.ps1") `
    -Username $Username `
    -PollIntervalSeconds $PollIntervalSeconds `
    -RecoverOlderThanSeconds $RecoverOlderThanSeconds `
    -TypingDelaySeconds $TypingDelaySeconds `
    -InboundDebounceSeconds $InboundDebounceSeconds `
    -CrmchatTimeoutSeconds $CrmchatTimeoutSeconds `
    -ErrorBackoffSeconds $ErrorBackoffSeconds `
    -FastPacingSeconds $FastPacingSeconds `
    -ModelTestProfile $ModelTestProfile
