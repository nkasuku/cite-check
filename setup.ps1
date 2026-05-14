$ErrorActionPreference = "Stop"

Set-Location -Path $PSScriptRoot

Write-Host "Installing cite-check Python dependencies..."
pip install -r requirements.txt

Write-Host "Installing Playwright Chromium browser..."
python -m playwright install chromium

Write-Host ""
Write-Host "✓ cite-check dependencies installed." -ForegroundColor Green
Write-Host ""
Write-Host "Next:"
Write-Host "  1) Verify setup:        python skills\cite-check\tools\cite.py setup-check"
Write-Host "  2) Refresh corpus:      python skills\cite-check\tools\cite.py refresh-corpus"
Write-Host "  3) From Copilot CLI:    use cite-check on <issue or risk>"
