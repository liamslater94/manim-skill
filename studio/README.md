# Manim Studio

Paste a maths question or topic, and get back a narrated explainer video in your own voice,
ready for YouTube. Manim Studio is a small app that runs on your computer and drives the
manim skill end to end:

1. **Script.** Claude Code writes the narration and an outline with a fully worked solution. It
   opens with your greeting, then a proper introduction (the topic, a hook, the question read
   through, and the plan) before any working; includes one or two "pause and try" moments; and
   points out common mistakes. A second Claude then solves the question **independently** and
   flags anything that disagrees (with a button to have the first fix it). You check the maths,
   edit the narration directly or ask for changes, then approve.
2. **Storyboard.** Claude sketches what will be on screen at each line (`storyboard.py`, one still
   per line, in the house style). You can ask for anything to be drawn differently before you
   record, and the animation later follows it. Turn it off under ⚙ Settings → Narration.
3. **Voice.** You read the approved script aloud from a built-in **teleprompter**, with the
   storyboard still for the current line shown beside it, in one take,
   with your Mac's microphone: tap <kbd>Space</kbd> as you start each new line (it lights up the
   next one), or <kbd>R</kbd> to read a line again. The app cleans the take (rumble, background
   noise, key clicks, even level), splits it into lines at your taps, makes sure every
   `[pause N]` is at least that long, and checks each line against the script. Then you listen
   back: re-record any single line, correct a line's subtitle, or record the whole thing again
   (`studio/recording.py`). You can also upload a recording made elsewhere. Record your end-screen
   sign-off once under ⚙ Settings → End screen. Prefer a cloned voice? Choose it under ⚙ Settings →
   Narration (`tools/tts-generate.py --clone`).
4. **Video.** Claude Code writes the Manim animation timed to your narration, renders it,
   checks frames for layout problems and fixes them. Every video uses the channel's house style
   (`studio/house_style.py`): one colour per variable, the question pinned in the top-left corner,
   the current step labelled, what's being said highlighted on screen, answers boxed, and a
   countdown ring for "pause and try" moments. You watch it in the app and leave
   timestamped notes; Claude applies them and re-renders only what changed.
5. **Export.** A 1080p MP4 plus a search-friendly YouTube title (with the exam reference if you
   gave one), description, tags and chapter timestamps, **three title options** and **three
   thumbnails** for YouTube's Test & compare. Each 1280×720 thumbnail has a punchy two-part
   title on a striking frame, and
   the app adds your logo and a level badge. Edit the title or frame and regenerate it instantly
   on the video's page (local, no Claude usage). Upload your logo under ⚙ Settings → Channel branding.
   Export also writes **`subtitles.srt`**, a caption track timed to the video: long narration lines
   are split into short two-line captions that clear during thinking pauses. Upload it in YouTube
   Studio → Subtitles → Add language → Upload file → With timing.
   Finally a clean 20-second **end screen** is appended: your logo and "Thanks for watching" at
   the top, your cloned voice saying a short sign-off, and the rest of the frame left empty for
   YouTube's end screen elements (YouTube Studio → Editor → End screen). Claude is told never to
   draw its own end cards or "more videos" boxes. Change the length or sign-off, or turn it off
   (length 0), under ⚙ Settings → End screen.
   The audio is then mastered: a gentle voice polish, an optional music bed that dips under your
   voice (your own track, or a soft built-in pad), and YouTube's -14 LUFS loudness.
   Also made: a vertical **YouTube Short** (under 60 s) of the best moment, with a hook title and
   big captions (adjust the start and end on the video's page), and a branded **practice
   worksheet** PDF with similar questions and worked answers to link in the description.

Paste several questions in a row: one script is written while another video renders, and each
video waits for you at the two review points. Or use **Plan a series**: pick a syllabus topic
and Claude plans a playlist in teaching order, from the basics to exam-level questions; make each
video with one click.

**Sounding like you.** Read `studio/voice-recording-script.md` (14 short takes, about 5 minutes)
and upload it under ⚙ Settings → Your voice → *Find my best clip*. The app clones a test line from
every take, scores how much each sounds like you (speaker similarity) and how clearly it speaks,
and lets you listen to the best three before choosing (`tools/voice-pick.py`).

**Your channel in every video.** Your greeting ("Hello, and welcome to
Mr. Liam IB Maths!") plays over your logo, which then fades as the topic begins. Set the greeting and logo under ⚙ Settings → Channel branding. Narration uses natural
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

`studio/audio.py video.mp4 out.mp4 --music builtin --region 0:9` masters the audio,
`studio/shorts.py video.mp4 short.mp4 --start 40 --end 85 --title "…" --srt subtitles.srt` cuts a Short,
and `studio/endscreen.py video.mp4 video_end.mp4 --seconds 20 --logo logo.png --voice signoff.mp3`
appends the end screen to any video.

The title font is [Anton](https://fonts.google.com/specimen/Anton) (SIL Open Font License, see
`studio/fonts/OFL.txt`).
