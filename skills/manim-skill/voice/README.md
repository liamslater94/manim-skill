# Cloned voice

Put a recording of your own voice here and `tools/tts-generate.py` narrates in
it automatically (engine `auto` picks it up; `--engine clone` forces it).

```
voice/
├── reference.wav   # 10–30 s of you speaking (.wav, .mp3, .flac or .m4a)
└── reference.txt   # the exact words spoken in that clip
```

Tips:

- Read in your narration voice — calm, explanatory. The clone copies the style
  of the clip.
- Quiet room, no music, no long pauses at the start or end.
- `reference.txt` must match the clip word for word; mismatches make the clone
  mumble or skip words.
- Avoid the letter z in the clip if it's said "zee" — the spoken text is
  rewritten to "zed" automatically, but an American "zee" in the reference
  pulls the accent that way.

`reference.*` is git-ignored because this repo is public: anyone with the clip
can clone the voice. If the repo becomes private, remove that line from
`.gitignore` and commit the clip so every new session has it.

## Setup in a fresh session

```bash
bash skills/manim-skill/tools/setup-voice-clone.sh
```

Installs `qwen-tts`, `torch` and `soundfile` and pre-downloads the Qwen3-TTS
model (≈4.5 GB). In a Claude Code cloud environment, the network allowlist must
include `huggingface.co` and `*.hf.co` (the model files are served from
`*.hf.co` storage hosts).

Speed on a 4-core CPU with no GPU: about 5–6 s of compute per second of speech
with the 1.7B model (`--clone-model 0.6B` is roughly twice as fast, slightly
lower quality). Much faster on a CUDA GPU.
