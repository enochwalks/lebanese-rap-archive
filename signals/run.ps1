# Runs any sigfilter command inside the project's virtual environment.
#   .\run.ps1 login
#   .\run.ps1 channels
#   .\run.ps1 watch
#   .\run.ps1 stats
#   .\run.ps1 test --text "XAUUSD BUY 2340 TP 2365 SL 2332"

$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

$venvPy = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $venvPy)) {
    Write-Host "Setup has not been run yet. Run this first:" -ForegroundColor Red
    Write-Host "    .\setup.ps1"
    exit 1
}

if ($args.Count -eq 0) {
    Write-Host "Usage: .\run.ps1 <command>   (login, channels, watch, poll, stats, recent, test)"
    exit 1
}

& $venvPy -m sigfilter.cli @args
