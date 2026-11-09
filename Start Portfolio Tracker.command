#!/bin/bash
# Double-click this file in Finder to start the Portfolio tracker in your browser.
# The first time, macOS may block it: right-click the file > Open > Open.
cd "$(dirname "$0")" || exit 1

# Decide by what actually works, not by whether the folder exists (a half-finished setup leaves .venv behind).
if ! .venv/bin/python -c "import pandas, yfinance" >/dev/null 2>&1; then
    echo "First start: setting up (this takes a few minutes, only once)..."
    if ! command -v python3 >/dev/null 2>&1; then
        echo "Python 3 is not installed. Install it from https://www.python.org/downloads/ and try again."
        read -r -p "Press Enter to close."
        exit 1
    fi
    rm -rf .venv
    if ! python3 -m venv .venv || ! .venv/bin/python -m pip install -r requirements.txt; then
        echo "Setup failed (see the messages above). Check your internet connection and try again."
        rm -rf .venv
        read -r -p "Press Enter to close."
        exit 1
    fi
fi

source .venv/bin/activate
echo
echo "Portfolio tracker is starting. Your browser will open."
echo "Close this window to stop it."
echo
python -m portfolio_tracker ui
echo
echo "The Portfolio tracker has stopped. You can close this window."
read -r -p ""
