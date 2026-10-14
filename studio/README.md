# Manim Studio

Paste a maths question or topic, and get back a narrated explainer video in your own voice,
ready for YouTube. Manim Studio is a small app that runs on your computer and drives the
manim skill end to end:

1. **Script.** Claude Code writes the narration and an outline with a fully worked solution.
   You check the maths, edit the narration directly or ask for changes, then approve.
2. **Voice.** Your cloned voice reads the approved script (`tools/tts-generate.py --clone`),
   with thinking pauses after worked steps and every take checked against the script.
3. **Video.** Claude Code writes the Manim animation timed to your narration, renders it,
   checks frames for layout problems and fixes them. You watch it in the app and leave
   timestamped notes; Claude applies them and re-renders only what changed.
4. **Export.** A 1080p MP4 plus a YouTube title, description, tags and chapter timestamps.

Paste several questions in a row: one script is written while another video renders, and each
video waits for you at the two review points.

## Start it

Double-click **`Manim Studio.command`** in this folder (macOS), or from a terminal:

```bash
cd ~/manim-skill
.venv/bin/python studio/studio.py
```

It opens <http://localhost:8765> in your browser. Keep the terminal window open while you use it.

First time: open **⚙ Settings**, upload a 10–30 s recording of yourself speaking clearly and
with energy (the clone copies your tone and pace), and set pronunciations such as `z=zed`.
Without a sample, videos use the stock Kokoro voice.

## Requirements

Everything from the main [installation](../README.md#installation) (`uv sync`, LaTeX, ffmpeg),
plus **Claude Code**, installed and signed in (`brew install --cask claude-code`, then run
`claude` once). The app runs Claude Code in the background with your subscription; nothing is
sent anywhere else, and the server only listens on this computer.

## Where things go

Each video is a folder in `~/.manim-skill/<name>/`, the same layout the skill uses:
`narration.txt`, `outline.md`, `voiceover.mp3`, `script.py`, `video.mp4`, `video_1080p.mp4`,
`youtube.md`, plus `studio.log` (everything Claude and the tools did). Settings and your voice
sample are in `~/.manim-skill/studio/`. Set `MANIM_SKILL_HOME` to keep them elsewhere.

## Tips

- **Timing.** Expect a few minutes for the script, 5–10 minutes for the voice and 10–25 for
  the animation, depending on length and your Mac. Rendering 1080p for export takes a while.
- **Check the worked solution** before approving: everything downstream is built on it.
- **Feedback** works best tied to a moment: pause the video, click *Add timestamp*, and say
  what to change ("[1:42] the label overlaps the curve, move it up").
- If a step fails, the activity log says why. Fix the cause (e.g. a missing tool) and press
  *Retry*; finished steps are kept.
