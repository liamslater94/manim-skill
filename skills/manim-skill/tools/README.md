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

Reads the SubtitleSpec from `narration.txt`, generates per-line audio locally with
Kokoro or a clone of your own voice, measures exact durations with ffprobe,
concatenates to `voiceover.mp3`, and writes `timestamps.json`.

```bash
python3 tts-generate.py --narration narration.txt                 # default voice af_heart
python3 tts-generate.py --narration narration.txt --voice am_adam # another Kokoro voice
python3 tts-generate.py --clone me.m4a --qa --pause 1             # your own voice, checked, with breathing room
```

**Pauses.** End a narration line with `[pause 2.5]` to follow it with 2.5 s of
silence (e.g. after a worked step); `--pause N` sets the default for other lines.
The pause counts toward that line's `duration_s` (also reported as `pause_s`)
and is stripped from the subtitle text.

**Voice cloning.** `--clone <sample>` narrates with
[ZipVoice](https://github.com/k2-fsa/ZipVoice) zero-shot TTS, run locally via
sherpa-onnx — no account or API key. Any audio format works; 5–30 s of clean,
expressive speech is ideal (the clone copies the sample's voice, tone and pace).
The sample is trimmed to ≈12 s at a pause and transcribed with a local Whisper
model; pass `--clone-text` to supply the transcript instead. Because the model is
Chinese/English, the text sent to it is respelled for pronunciation (the variable
*a* → "eigh", the article → "uh", single letters → "ex", "why", "zee", …);
subtitles keep the original wording.

| Option | Default | |
|---|---|---|
| `--qa` | off | transcribe each new clip; retake cloned lines heard as < 90% of the script |
| `--retakes` | 2 | extra takes per line with `--qa` (best take wins) |
| `--clone-guidance` | 1.5 | higher = more expressive / closer to the sample, lower = steadier |
| `--clone-steps` | 4 | sampling steps |
| `--natural-pauses` | off | vary each pause with how the line ends (short mid-thought, longer after a sentence or question, `[pause N]` kept), and lay soft room tone under the pauses |
| `--say` | — | respell a letter/word for the cloned voice, e.g. `--say z=zed` (repeatable) |
| `--speed` | 1 | speaking-rate multiplier (both engines) |

Models (≈0.7 GB: ZipVoice, Vocos vocoder, Whisper base.en) download from the
sherpa-onnx GitHub releases on first use into `~/.cache/manim-skill/models`
(`MANIM_SKILL_MODELS` overrides). Expect ≈15–25 s of CPU per line.

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
- Voice cloning / `--qa`: `uv add sherpa-onnx soundfile` (local, no API key)
