# Windows setup for sigfilter.
# Run it from this folder:   .\setup.ps1
# It builds a private virtual environment, so "pip is not recognized" can't happen.

$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

Write-Host ""
Write-Host "=== sigfilter setup ===" -ForegroundColor Cyan
Write-Host ""

# 1. Find Python. The "py" launcher is the reliable one on Windows.
$py = $null
if (Get-Command py -ErrorAction SilentlyContinue) {
    $py = "py"
} elseif (Get-Command python -ErrorAction SilentlyContinue) {
    $py = "python"
} else {
    Write-Host "Python is not installed." -ForegroundColor Red
    Write-Host ""
    Write-Host "Install it, then CLOSE this window and open PowerShell again:"
    Write-Host "    winget install -e --id Python.Python.3.11"
    Write-Host ""
    Write-Host "(Or download from python.org and tick 'Add python.exe to PATH'.)"
    exit 1
}
Write-Host "Python found: $py" -ForegroundColor Green

# 2. Private environment for this project only.
if (-not (Test-Path ".venv")) {
    Write-Host "Creating virtual environment..."
    & $py -m venv .venv
}
$venvPy = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $venvPy)) {
    Write-Host "Virtual environment failed to build." -ForegroundColor Red
    exit 1
}

Write-Host "Installing dependencies (takes a minute)..."
& $venvPy -m pip install --quiet --upgrade pip
& $venvPy -m pip install --quiet -r requirements.txt
Write-Host "Dependencies installed." -ForegroundColor Green

# 3. Config files, only if they don't exist yet - never overwrite your edits.
if (-not (Test-Path ".env")) {
    Copy-Item ".env.example" ".env"
    Write-Host "Created .env" -ForegroundColor Green
} else {
    Write-Host ".env already exists, leaving it alone."
}
if (-not (Test-Path "config.yaml")) {
    Copy-Item "config.example.yaml" "config.yaml"
    Write-Host "Created config.yaml" -ForegroundColor Green
} else {
    Write-Host "config.yaml already exists, leaving it alone."
}

# 4. Tell them exactly what comes next.
# 5. Desktop shortcuts, so none of this needs a terminal day to day.
& powershell -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot "shortcuts.ps1")

Write-Host ""
Write-Host "=== Next steps ===" -ForegroundColor Cyan
Write-Host ""
Write-Host "1. Get your Telegram API keys at https://my.telegram.org"
Write-Host "   (log in with your phone number -> API development tools)"
Write-Host ""
Write-Host "2. Put them in .env - opening it now in Notepad."
Write-Host ""
Write-Host "3. Then run these, one line at a time:"
Write-Host "       .\run.ps1 login" -ForegroundColor Yellow
Write-Host "       .\run.ps1 channels" -ForegroundColor Yellow
Write-Host "   Copy the channel ids into config.yaml, then use the Desktop shortcuts,"
Write-Host "   or run it here with:"
Write-Host "       .\run.ps1 watch" -ForegroundColor Yellow
Write-Host ""

# Absolute path: Start-Process resolves relative paths against the process
# working directory, which is not necessarily this folder.
Start-Process notepad (Join-Path $PSScriptRoot ".env")
