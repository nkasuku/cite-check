#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

echo "Installing cite-check Python dependencies..."
pip3 install -r requirements.txt

echo "Installing Playwright Chromium browser..."
python3 -m playwright install chromium

echo
echo "✓ cite-check dependencies installed."
echo
echo "Next:"
echo "  1) Verify setup:        python3 skills/cite-check/tools/cite.py setup-check"
echo "  2) Refresh corpus:      python3 skills/cite-check/tools/cite.py refresh-corpus"
echo "  3) From Copilot CLI:    use cite-check on <issue or risk>"
