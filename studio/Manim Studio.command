#!/bin/bash
# Double-click to start Manim Studio (macOS). Opens the app in your browser; close this window to stop it.
cd "$(dirname "$0")/.." || exit 1
export PATH="/opt/homebrew/bin:/usr/local/bin:/Library/TeX/texbin:$HOME/.local/bin:$PATH"
if [ ! -x .venv/bin/python ]; then
  echo "First run: setting up the Python environment (needs uv: brew install uv) ..."
  uv sync -p 3.12 || { echo "Setup failed — see the message above."; read -r -p "Press Enter to close."; exit 1; }
fi
exec .venv/bin/python studio/studio.py "$@"
