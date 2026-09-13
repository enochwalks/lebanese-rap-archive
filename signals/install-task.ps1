# Makes sigfilter start by itself whenever you log in to Windows, and
# restart itself if it ever crashes. Run once:   .\install-task.ps1
# To remove it later:                            .\install-task.ps1 -Remove

param([switch]$Remove)

$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

$taskName = "sigfilter-watch"

if ($Remove) {
    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue
    Write-Host "Removed the scheduled task. sigfilter will no longer start on its own."
    exit 0
}

$venvPy = Join-Path $PSScriptRoot ".venv\Scripts\pythonw.exe"
if (-not (Test-Path $venvPy)) {
    Write-Host "Run .\setup.ps1 first." -ForegroundColor Red
    exit 1
}

# pythonw.exe runs without a console window, so it sits quietly in the background.
$action = New-ScheduledTaskAction -Execute $venvPy `
    -Argument "-m sigfilter.cli watch" -WorkingDirectory $PSScriptRoot

$trigger = New-ScheduledTaskTrigger -AtLogOn

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -RestartCount 99 -RestartInterval (New-TimeSpan -Minutes 2) `
    -ExecutionTimeLimit (New-TimeSpan -Days 0)

Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger `
    -Settings $settings -Description "Telegram signal filter" -Force | Out-Null

Start-ScheduledTask -TaskName $taskName

Write-Host ""
Write-Host "Done. sigfilter now runs in the background and starts automatically" -ForegroundColor Green
Write-Host "every time you log in to Windows. It has been started just now."
Write-Host ""
Write-Host "Check what it is doing:      .\run.ps1 recent"
Write-Host "Check channel quality:       .\run.ps1 stats"
Write-Host "Stop it for now:             Stop-ScheduledTask -TaskName sigfilter-watch"
Write-Host "Turn off auto-start:         .\install-task.ps1 -Remove"
Write-Host ""
Write-Host "Note: it runs while YOUR user is logged in. If the PC is off or"
Write-Host "you are signed out, nothing is read - Telegram keeps the history,"
Write-Host "but old signals are rejected as stale when it catches up."
