# Tools

Pipeline scripts and the local viewer used by `manim-skill`.

## video_viewer.py

Browser-based viewer for Manim videos with Code / Preview tabs, chapter
navigation, subtitles, and feedback capture — styled to match the Manimate app.

```bash
python3 video_viewer.py <video.mp4> --order <concat.txt> [--script script.py] [--srt subtitles.srt]
```

**Options:**
- `--order` - video order file (same format as ffmpeg concat.txt), required for chapters
- `--script` - Manim script, shown in the viewer's Code tab
- `--srt` - subtitle file; if omitted, per-scene `.srt` files are concatenated automatically
- `--port` - server port (default: auto)

Renders `ui.html` (which lives next to this script). Prints
`VIDEO_READY http://localhost:<port>` when ready and opens the browser.

### Shortcuts

| Key | Action |
|-----|--------|
| `Space` / `K` | Play/Pause |
| `←` / `J` | Back 5s |
| `→` / `L` | Forward 5s |
| `C` | Toggle subtitles |
| `T` | Insert timestamp into feedback |
| `F` | Fullscreen |

## tts-generate.py

Reads the SubtitleSpec from `narration.txt`, generates per-line audio locally,
measures exact durations with ffprobe, concatenates to `voiceover.mp3`, and
writes `timestamps.json`.

Two engines:

- **clone**: your own voice, cloned with Qwen3-TTS from `../voice/reference.wav`
  plus `reference.txt` (see [`voice/README.md`](../voice/README.md)). Run
  `bash setup-voice-clone.sh` once to install it and download the model.
- **kokoro**: Kokoro-82M preset voices.

`--engine auto` (the default) uses the cloned voice when a reference is present
and falls back to Kokoro otherwise.

```bash
python3 tts-generate.py --narration narration.txt                                  # cloned voice if set up, else af_heart
python3 tts-generate.py --narration narration.txt --engine kokoro --voice am_adam  # a Kokoro voice
python3 tts-generate.py --narration narration.txt --clone-model 0.6B               # faster, smaller clone model
python3 tts-generate.py --bust 3 && python3 tts-generate.py --seed-offset 1        # new take of line 3 only
```

The spoken text gets British pronunciation fixes: a lone `z` (also `3z` and
`z-axis`) is said "zed". Subtitles keep the original text. Turn this off with
`--pronunciation none`.

`--narration` defaults to `narration.txt`; `--plan` remains a compatibility alias.
Audio clips are decoded, padded to their measured durations, and encoded once
when joined to preserve subtitle timing.

Per-line results are cached in `.tts-cache/`, so re-runs only regenerate changed
lines (and the model loads only when there's something to generate). Drop one
clip with `--bust <index or text fragment>`.

## lint-subtitles.py

Simulates a Manim script's scene timelines without rendering (sub-second) and
reports overlapping subtitles, animations that overflow their subtitle window,
and subtitles extending beyond the scene. Invalid timings, incomplete simulations,
and scenes without subtitles cannot pass. Run only for narrated videos; verify
rendered audio/video sync separately.

```bash
python3 lint-subtitles.py script.py    # 0 = clean, 1 = issues, 2 = incomplete/invalid check
```

## Requirements

- Python 3.10+, ffmpeg/ffprobe
- Voiceover: `uv add "kokoro>=0.9.4" soundfile` (local Kokoro, no API key)
- Cloned voice: `bash setup-voice-clone.sh` (`qwen-tts`, `torch`; ≈4.5 GB model from Hugging Face)
