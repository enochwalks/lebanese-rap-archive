# Opens the dashboard. Starts the little web server first if it is not
# already running, so double-clicking twice never breaks anything.

$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

$port = 8765
$url = "http://127.0.0.1:$port/"

function Test-Port($p) {
    $client = New-Object System.Net.Sockets.TcpClient
    try { $client.Connect("127.0.0.1", $p); $client.Close(); return $true }
    catch { return $false }
}

if (-not (Test-Port $port)) {
    $pyw = Join-Path $PSScriptRoot ".venv\Scripts\pythonw.exe"
    if (-not (Test-Path $pyw)) {
        Start-Process powershell -ArgumentList @(
            "-NoExit", "-Command",
            "Write-Host 'Setup has not been run yet. Run .\setup.ps1 in the signals folder.' -ForegroundColor Red")
        exit 1
    }
    # pythonw = no console window; the server just sits in the background.
    Start-Process -FilePath $pyw `
        -ArgumentList "-m", "sigfilter.cli", "dashboard", "--no-browser", "--port", $port `
        -WorkingDirectory $PSScriptRoot -WindowStyle Hidden

    for ($i = 0; $i -lt 40; $i++) {
        Start-Sleep -Milliseconds 250
        if (Test-Port $port) { break }
    }
}

Start-Process $url
