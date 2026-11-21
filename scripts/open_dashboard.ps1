# Opens the StealthBot control panel in your browser, already signed in.
# It asks the running bot for a one-time sign-in ticket (valid 60 seconds, usable once) so the real API key
# never appears in a URL or in your browser history.
param([int]$Port = 8000)

$ErrorActionPreference = "Stop"
$base = "http://127.0.0.1:$Port"
$tokenFile = Join-Path $env:LOCALAPPDATA "stealthbot\data\api_token"

if (-not (Test-Path $tokenFile)) {
    Write-Host "The bot has not been started yet (no API key file). Run scripts\start.bat first." -ForegroundColor Yellow
    exit 1
}
$key = (Get-Content -Raw $tokenFile).Trim()

try {
    $ticket = (Invoke-RestMethod -Method Post -Uri "$base/api/auth/ticket" -Headers @{ "X-API-Key" = $key } -TimeoutSec 5).ticket
} catch {
    Write-Host "Could not reach the bot at $base. Is scripts\start.bat running?" -ForegroundColor Yellow
    Write-Host $_.Exception.Message
    exit 1
}

Start-Process "$base/dashboard#ticket=$ticket"
Write-Host "Dashboard opened. If the page asks you to sign in, run this script again (tickets expire after 60 s)."
