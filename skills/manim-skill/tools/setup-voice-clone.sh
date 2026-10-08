#!/usr/bin/env bash
# Install the cloned-voice TTS engine and pre-download its model.
# Usage: bash setup-voice-clone.sh [1.7B|0.6B]
set -euo pipefail
SIZE="${1:-1.7B}"

if command -v uv >/dev/null 2>&1 && [ -n "${VIRTUAL_ENV:-}" ]; then
  uv pip install qwen-tts torch soundfile
elif command -v uv >/dev/null 2>&1; then
  uv pip install --system qwen-tts torch soundfile
else
  python3 -m pip install qwen-tts torch soundfile
fi

python3 - "$SIZE" <<'PY'
import sys
from huggingface_hub import snapshot_download
repo = f"Qwen/Qwen3-TTS-12Hz-{sys.argv[1]}-Base"
print("Downloading", repo)
snapshot_download(repo)
PY

VOICE_DIR="${MANIM_VOICE_DIR:-$(cd "$(dirname "$0")/.." && pwd)/voice}"
if ls "$VOICE_DIR"/reference.* >/dev/null 2>&1 && [ -f "$VOICE_DIR/reference.txt" ]; then
  echo "Voice reference found in $VOICE_DIR — tts-generate.py will use the cloned voice."
else
  echo "No voice reference yet: add reference.wav + reference.txt to $VOICE_DIR (see voice/README.md)."
fi
