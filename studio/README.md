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
4. **Export.** A 1080p MP4 plus a YouTube title, description, tags and chapter timestamps, and a
   1280×720 **thumbnail**: Claude picks a punchy two-part title and the most striking frame, and
   the app adds your logo and a level badge. Edit the title or frame and regenerate it instantly
   on the video's page (local, no Claude usage). Upload your logo under ⚙ Settings → Thumbnail branding.
   Export also writes **`subtitles.srt`**, a caption track timed to the video: long narration lines
   are split into short two-line captions that clear during thinking pauses. Upload it in YouTube
   Studio → Subtitles → Add language → Upload file → With timing.
   Finally a clean 20-second **end screen** is appended: your logo and "Thanks for watching" at
   the top, your cloned voice saying a short sign-off, and the rest of the frame left empty for
   YouTube's end screen elements (YouTube Studio → Editor → End screen). Claude is told never to
   draw its own end cards or "more videos" boxes. Change the length or sign-off, or turn it off
   (length 0), under ⚙ Settings → End screen.

Paste several questions in a row: one script is written while another video renders, and each
video waits for you at the two review points.

**Your channel in every video.** Each script opens with your greeting ("Hello, and welcome to
Mr. Liam IB Maths!") spoken over your logo, which then shrinks to a corner badge as the topic
begins. Set the greeting and logo under ⚙ Settings → Channel branding. Narration uses natural
pauses by default: short where a thought carries on, longer after questions and worked steps, with
soft room tone instead of dead silence.

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
`narration.txt`, `outline.md`, `voiceover.mp3`, `script.py`, `video.mp4`, `video_1080p.mp4` (`video_1080p_main.mp4` is the same without the end screen),
`youtube.md`, `thumbnail.png`, `subtitles.srt`, plus `studio.log` (everything Claude and the tools did). Settings and your voice
sample are in `~/.manim-skill/studio/`. Set `MANIM_SKILL_HOME` to keep them elsewhere.

## Tips

- **Timing.** Expect a few minutes for the script, 5–10 minutes for the voice and 10–25 for
  the animation, depending on length and your Mac. Rendering 1080p for export takes a while.
- **Check the worked solution** before approving: everything downstream is built on it.
- **Feedback** works best tied to a moment: pause the video, click *Add timestamp*, and say
  what to change ("[1:42] the label overlaps the curve, move it up").
- If a step fails, the activity log says why. Fix the cause (e.g. a missing tool) and press
  *Retry*; finished steps are kept.

## Thumbnails and subtitles without the app

`studio/thumbnail.py` works on any video:

```bash
.venv/bin/python studio/thumbnail.py video.mp4 thumbnail.png --headline "Shortest distance" \
    --subline "between vector lines" --frame 42 --logo logo.png --badge "IB Maths AA HL"
```

`studio/subtitles.py ~/.manim-skill/<video>` writes `subtitles.srt` for any rendered skill session.

`studio/endscreen.py video.mp4 video_end.mp4 --seconds 20 --logo logo.png --voice signoff.mp3`
appends the end screen to any video.

The title font is [Anton](https://fonts.google.com/specimen/Anton) (SIL Open Font License, see
`studio/fonts/OFL.txt`).
