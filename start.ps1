# start.ps1 - run the Solana Sniper (paper mode by default)
Set-Location $PSScriptRoot
if (-not (Test-Path .venv)) {
    Write-Host "venv not found. Run setup.bat (or: python -m venv .venv) first." -ForegroundColor Yellow
    pause
    exit 1
}
if (-not (Test-Path .env)) {
    Write-Host ".env not found. Copying from example - EDIT IT before trading." -ForegroundColor Yellow
    Copy-Item .env.example .env
}
& .venv\Scripts\Activate.ps1
python snipe.py
