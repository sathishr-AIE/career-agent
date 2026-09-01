param(
    [string]$RepoPath = (Resolve-Path "$PSScriptRoot\..").Path,
    [string]$Time = "08:00"
)

$python = Join-Path $RepoPath ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) { throw "venv not found at $python" }

$action = New-ScheduledTaskAction -Execute $python `
    -Argument "-m career_agent.run run" -WorkingDirectory $RepoPath
$trigger = New-ScheduledTaskTrigger -Daily -At $Time
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable `
    -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Hours 1)

Register-ScheduledTask -TaskName "CareerAgentDaily" -Action $action `
    -Trigger $trigger -Settings $settings -Force

Write-Host "Installed. Remove with: Unregister-ScheduledTask -TaskName CareerAgentDaily"
