# Puts two shortcuts on your Desktop. Run once:   .\shortcuts.ps1
# To remove them:                                  .\shortcuts.ps1 -Remove

param([switch]$Remove)

$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

# GetFolderPath finds the real Desktop even when OneDrive has moved it.
$desktop = [Environment]::GetFolderPath("Desktop")
$shell = New-Object -ComObject WScript.Shell
$powershell = Join-Path $env:SystemRoot "System32\WindowsPowerShell\v1.0\powershell.exe"

$links = @(
    @{ Name = "Signal Dashboard"
       Script = "open-dashboard.ps1"
       Icon = "icons\dashboard.ico"
       Hidden = $true
       Desc = "See what the signal filter is doing right now" },
    @{ Name = "Start Signal Agent"
       Script = "run.ps1"
       Args = "watch"
       Icon = "icons\agent.ico"
       Hidden = $false
       Desc = "Start reading the Telegram channels (keep the window open)" }
)

if ($Remove) {
    foreach ($link in $links) {
        $path = Join-Path $desktop ($link.Name + ".lnk")
        if (Test-Path $path) { Remove-Item $path; Write-Host "Removed $($link.Name)" }
    }
    exit 0
}

foreach ($link in $links) {
    $path = Join-Path $desktop ($link.Name + ".lnk")
    $target = Join-Path $PSScriptRoot $link.Script
    $arguments = "-ExecutionPolicy Bypass "
    if ($link.Hidden) { $arguments += "-WindowStyle Hidden " } else { $arguments += "-NoExit " }
    $arguments += "-File `"$target`""
    if ($link.Args) { $arguments += " " + $link.Args }

    $lnk = $shell.CreateShortcut($path)
    $lnk.TargetPath = $powershell
    $lnk.Arguments = $arguments
    $lnk.WorkingDirectory = $PSScriptRoot
    $lnk.IconLocation = (Join-Path $PSScriptRoot $link.Icon)
    $lnk.Description = $link.Desc
    $lnk.Save()
    Write-Host "Created: $($link.Name)" -ForegroundColor Green
}

Write-Host ""
Write-Host "Two shortcuts are on your Desktop." -ForegroundColor Cyan
Write-Host "  Signal Dashboard   - double-click any time to see what it is doing."
Write-Host "  Start Signal Agent - only needed if you did NOT run .\install-task.ps1."
Write-Host ""
Write-Host "Remove them later with:  .\shortcuts.ps1 -Remove"
