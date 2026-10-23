#!/usr/bin/env python3
"""
Manim Studio — paste a maths question or topic, get a narrated explainer video in your own voice.

    python studio/studio.py            # opens http://localhost:8765 in your browser

Each video moves through four stages, with a review step after the script and after the video:

  1. Script  — Claude Code writes narration.txt (+ outline.md with a worked solution) following
               the manim skill. You check the maths, edit, or ask for changes, then approve.
  2. Voice   — tts-generate.py narrates it in your cloned voice (or Kokoro if no sample is set).
  3. Video   — Claude Code writes the Manim scenes timed to the narration, renders and muxes.
               You watch it, leave timestamped notes, and Claude applies them.
  4. Export  — a 1080p render plus YouTube title, description and chapter timestamps.

Projects live in ~/.manim-skill/<slug>/ (MANIM_SKILL_HOME overrides), like the skill's own sessions.
Requires the `claude` CLI (Claude Code, signed in) and the skill's dependencies (manim, LaTeX,
ffmpeg, sherpa-onnx for voice cloning). Runs only on this computer (binds to 127.0.0.1).
"""

import argparse
import base64
import datetime as dt
import http.server
import json
import os
import re
import shutil
import signal
import socketserver
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path
from urllib.parse import unquote, urlparse

REPO = Path(__file__).resolve().parent.parent
SKILL_DIR = REPO / "skills" / "manim-skill"
TOOLS_DIR = SKILL_DIR / "tools"
HOME = Path(os.environ.get("MANIM_SKILL_HOME", Path.home() / ".manim-skill")).expanduser()
STUDIO_DIR = HOME / "studio"
SETTINGS_FILE = STUDIO_DIR / "settings.json"
UI_FILE = Path(__file__).resolve().parent / "studio.html"
CLAUDE_TOOLS = "Bash Read Write Edit MultiEdit Glob Grep"
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,80}$")
MAX_UPLOAD = 300 * 1024 * 1024

DEFAULT_SETTINGS = {
    "audience": "IB Mathematics: Analysis and Approaches HL students",
    "minutes": 4,
    "voice_mode": "record",
    "voice_sample": "",
    "voice_text": "",
    "voice_pick_used": "",
    "logo": "",
    "channel": "Mr. Liam IB Maths",
    "badge": "IB Maths AA HL",
    "accent": "#FFD23F",
    "say": "z=zed",
    "natural_pauses": True,
    "greeting": "Hello, and welcome to Mr. Liam IB Maths!",
    "endscreen_seconds": 20.0,
    "signoff": "Thanks for watching! Pick one of these videos to keep learning.",
    "house_style": True,
    "pause_try": True,
    "maths_check": True,
    "house_notes": "",
    "music": "intro_outro",
    "music_file": "",
    "music_level": 30.0,
    "shorts": True,
    "worksheet": True,
    "speed": 1.1,
    "pause": 1.0,
    "guidance": 1.5,
    "claude": "claude",
    "model": "",
}

# Status values. "queued_*" statuses are picked up by a worker; the rest are waiting on the user or done.
LIGHT = {"queued_script": "scripting", "queued_check": "checking", "queued_takes": "processing"}
HEAVY = {"queued_voice": "voicing", "queued_video": "rendering",
         "queued_revise": "revising", "queued_export": "exporting"}
RUNNING = set(LIGHT.values()) | set(HEAVY.values())
SERVED_FILES = {"video.mp4", "video_1080p.mp4", "voiceover.mp3", "thumbnail.png", "thumbnail-2.png", "thumbnail-3.png",
                "subtitles.srt", "short.mp4", "worksheet.pdf", "question.png", "question.jpg", "question.jpeg",
                "question.webp", "question.gif"}
STUDIO_FILES = Path(__file__).resolve().parent
PICK_DIR = STUDIO_DIR / "voice-pick"
PLANS_DIR = STUDIO_DIR / "plans"
MUSIC_EXTS = {".mp3", ".m4a", ".wav", ".aac", ".flac", ".ogg"}

LOCK = threading.RLock()
PROCS: dict[str, subprocess.Popen] = {}


# ------------------------------------------------------------------ storage --
def load_settings() -> dict:
    try:
        return {**DEFAULT_SETTINGS, **json.loads(SETTINGS_FILE.read_text())}
    except (OSError, ValueError):
        return dict(DEFAULT_SETTINGS)


def save_settings(values: dict) -> dict:
    current = load_settings()
    for key, default in DEFAULT_SETTINGS.items():
        if key in values:
            value = values[key]
            if isinstance(default, bool):
                current[key] = value is True or str(value).lower() in ("true", "1", "on", "yes")
            elif isinstance(default, str):
                current[key] = str(value)
            else:
                current[key] = max(0, type(default)(value or 0))
    STUDIO_DIR.mkdir(parents=True, exist_ok=True)
    SETTINGS_FILE.write_text(json.dumps(current, indent=2))
    return current


def job_dir(job_id: str) -> Path:
    if not SLUG_RE.match(job_id or ""):
        raise KeyError(job_id)
    return HOME / job_id


def read_job(job_id: str) -> dict:
    path = job_dir(job_id) / "studio.json"
    if not path.exists():
        raise KeyError(job_id)
    return json.loads(path.read_text())


def write_job(job: dict) -> dict:
    with LOCK:
        job["updated"] = time.time()
        path = job_dir(job["id"]) / "studio.json"
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(job, indent=2))
        tmp.replace(path)
    return job


def update_job(job_id: str, **changes) -> dict:
    with LOCK:
        job = read_job(job_id)
        job.update(changes)
        return write_job(job)


def all_jobs() -> list[dict]:
    jobs = []
    if HOME.exists():
        for p in HOME.glob("*/studio.json"):
            try:
                jobs.append(json.loads(p.read_text()))
            except ValueError:
                pass
    return sorted(jobs, key=lambda j: j.get("created", 0), reverse=True)


def log(job_id: str, line: str) -> None:
    stamp = dt.datetime.now().strftime("%H:%M:%S")
    with open(job_dir(job_id) / "studio.log", "a") as f:
        for part in str(line).rstrip().splitlines() or [""]:
            f.write(f"[{stamp}] {part}\n")


def tail(path: Path, n: int = 250) -> str:
    try:
        return "".join(path.read_text(errors="replace").splitlines(True)[-n:])
    except OSError:
        return ""


def slugify(text: str) -> str:
    words = re.findall(r"[a-z0-9]+", text.lower())[:6] or ["video"]
    base = "-".join(words)[:48].strip("-") or "video"
    slug, n = base, 2
    while (HOME / slug).exists():
        slug, n = f"{base}-{n}", n + 1
    return slug


def create_job(question: str, audience: str, minutes: float, image: dict | None, source: str = "",
               series: str = "") -> dict:
    question = question.strip()
    if not question and not image:
        raise ValueError("Paste a question or topic (or an image of one).")
    s = load_settings()
    with LOCK:
        job_id = slugify(question or "question")
        d = HOME / job_id
        d.mkdir(parents=True)
    image_name = ""
    if image:
        ext = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp", "image/gif": "gif"}.get(image.get("type"), "png")
        image_name = f"question.{ext}"
        (d / image_name).write_bytes(base64.b64decode(image["data"].split(",")[-1]))
    (d / "question.md").write_text(question + "\n")
    title = question.splitlines()[0][:90] if question else "Question from image"
    job = {"id": job_id, "title": title, "question": question, "image": image_name,
           "audience": audience or s["audience"], "minutes": float(minutes or s["minutes"]),
           "source": source.strip(), "series": series.strip(),
           "status": "queued_script", "stage_note": "", "error": "", "session": "",
           "created": time.time(), "history": []}
    log(job_id, f"Created: {title}")
    return write_job(job)


# ----------------------------------------------------------------- pipeline --
def claude_cmd(prompt: str, session: str = "") -> list[str]:
    s = load_settings()
    cmd = [s["claude"] or "claude", "-p", prompt, "--output-format", "stream-json", "--verbose",
           "--permission-mode", "acceptEdits", "--allowedTools", CLAUDE_TOOLS,
           "--add-dir", str(SKILL_DIR)]
    if s.get("model"):
        cmd += ["--model", s["model"]]
    if session:
        cmd += ["--resume", session]
    return cmd


def tool_env() -> dict:
    """Run tools with this interpreter's environment first on PATH (python, manim from the project venv)."""
    env = dict(os.environ)
    env["PATH"] = os.pathsep.join([str(Path(sys.executable).parent), env.get("PATH", "")])
    env.setdefault("PYTHONUNBUFFERED", "1")
    return env


def describe_event(event: dict) -> list[str]:
    """Turn one stream-json event from Claude Code into short human-readable log lines."""
    out = []
    kind = event.get("type")
    if kind == "assistant":
        for block in event.get("message", {}).get("content", []):
            if block.get("type") == "text" and block.get("text", "").strip():
                out.append("Claude: " + block["text"].strip().replace("\n", " ")[:400])
            elif block.get("type") == "tool_use":
                inp = block.get("input", {})
                what = (inp.get("description") or inp.get("command") or inp.get("file_path")
                        or inp.get("pattern") or "")
                out.append(f"  → {block.get('name')}: {str(what).splitlines()[0][:160] if what else ''}")
    elif kind == "result":
        secs = (event.get("duration_ms") or 0) / 1000
        out.append(f"Claude finished ({'error' if event.get('is_error') else 'ok'}, {secs:.0f}s).")
    return out


def run_process(job_id: str, cmd: list[str], *, claude: bool, record_session: bool = True) -> tuple[int, dict]:
    """Run a step, streaming its output into the job log. Returns (exit code, final Claude result event)."""
    d = job_dir(job_id)
    shown = [Path(cmd[0]).name] + [c if len(c) < 90 and "\n" not in c else "…" for c in cmd[1:]]
    log(job_id, "$ " + " ".join(shown))
    try:
        proc = subprocess.Popen(cmd, cwd=d, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, bufsize=1, env=tool_env(), start_new_session=True)
    except FileNotFoundError:
        what = "Claude Code" if claude else Path(cmd[0]).name
        raise RuntimeError(f"Couldn't start {what} ({cmd[0]}). Is it installed? You can set its path in Settings.")
    PROCS[job_id] = proc
    result = {}
    try:
        for line in proc.stdout:
            if not claude:
                if "ConvertTokensToIds" in line or re.fullmatch(r"\s*-?\d+\.\d+\s*", line):
                    continue  # harmless lexicon warnings from the voice engine
                log(job_id, line)
                continue
            try:
                event = json.loads(line)
            except ValueError:
                log(job_id, line)
                continue
            if event.get("session_id") and record_session:
                update_job(job_id, session=event["session_id"])
            if event.get("type") == "result":
                result = event
            for text in describe_event(event):
                log(job_id, text)
        code = proc.wait()
    finally:
        PROCS.pop(job_id, None)
    if claude and result.get("is_error"):
        log(job_id, "Claude reported an error: " + str(result.get("result", ""))[:500])
        code = code or 1
    return code, result


def skill_preamble(job: dict) -> str:
    return (
        f"You are making a narrated maths explainer video for YouTube, aimed at {job['audience']}.\n"
        f"Follow the manim skill at {SKILL_DIR / 'SKILL.md'} (read it first). TOOLS_DIR is {TOOLS_DIR}.\n"
        f"SESSION_DIR is the current working directory, already created — work only here; do not create "
        f"another session folder. Use the `python` and `manim` on PATH. Never launch the video viewer.\n"
    )


def voice_command(narration: str = "narration.txt", out_audio: str = "voiceover.mp3",
                  out_timestamps: str = "timestamps.json") -> list[str]:
    s = load_settings()
    cmd = [sys.executable, str(TOOLS_DIR / "tts-generate.py"), "--narration", narration,
           "--out-audio", out_audio, "--out-timestamps", out_timestamps,
           "--pause", f"{s['pause']:g}"] + (["--natural-pauses"] if s.get("natural_pauses") else [])
    sample = s.get("voice_sample")
    if sample and Path(sample).exists():
        cmd += ["--clone", sample, "--qa", "--speed", f"{s['speed']:g}", "--clone-guidance", f"{s['guidance']:g}"]
        if s.get("voice_text"):
            cmd += ["--clone-text", s["voice_text"]]
        for item in re.split(r"[,\s]+", s.get("say", "").strip()):
            if "=" in item:
                cmd += ["--say", item]
    return cmd


def script_rules(s: dict) -> str:
    """Opening, watch-along and ending rules for narration.txt, from the settings."""
    greeting = s.get("greeting", "").strip()
    rules = []
    if greeting:
        rules.append(f"- The first line must be exactly: {greeting}\n  (It plays over the channel logo.)")
    rules.append("""- Then a proper introduction, in 3–6 lines, before any working:
  * name the topic and where it sits in the course (e.g. "This is a classic Paper 1 question on systems of
    equations with a parameter");
  * a hook: why it's worth knowing, the trap students often fall into, or what they'll be able to do by the end;
  * read through the question, part by part, saying what each part is really asking;
  * outline the plan of attack ("First we'll…, then…, and finally…").
  Only then start the working. Keep the tone warm and welcoming: you are the teacher, talking to one student.""")
    if s.get("pause_try"):
        rules.append("- Include one or two 'pause and try' moments at natural points: a line inviting the viewer to "
                     "pause the video and try the next step themselves (say exactly what to try), ending with "
                     "[pause 4]. The next line reveals the answer.")
    rules.append("- Point out the common mistakes and where marks are lost ('Be careful here…'), as an experienced "
                 "IB examiner would.")
    if s.get("endscreen_seconds", 0) > 0:
        rules.append("- End with the summary. Don't invite viewers to watch other videos or subscribe: the app adds "
                     "an end screen with a spoken sign-off.")
    return "\n".join(rules) + "\n"


def voice_rules(s: dict) -> str:
    """How to write the narration for the voice that will speak it."""
    if s.get("voice_mode", "record") == "record":
        return """- The teacher will read narration.txt aloud themselves, in one take, from a teleprompter. Write exactly
  what a warm, experienced teacher would say to one student sitting beside them: natural spoken English,
  contractions, short sentences, signposts ("Now look at this.", "So,", "Here's the key idea:"), and the
  odd rhetorical question. It must flow as one piece when read aloud: each line follows on from the last,
  with no repeated sentence openings and nothing that sounds written rather than spoken. Read every line
  aloud in your head before keeping it.
- Write maths as it is said ("x squared", "a minus nine", "the integral from zero to one of …"), never
  symbols or LaTeX, and avoid tongue-twisters and long strings of numbers in one breath.
- End a line with [pause 2] after every worked step or key result: the teleprompter shows the teacher where
  to pause, and the app makes sure the pause is at least that long.
- Keep lines to one or two sentences each (one line is one teleprompter card and one subtitle).
"""
    return """- End a line with [pause 2] after every worked step or key result so viewers can take it in; other pauses
  are timed automatically from how each line ends.
- The narration is read by a cloned human voice: write maths as it should be spoken
  ("x squared", "a minus nine", "the integral from zero to one of …"), never symbols or LaTeX.
- Write it the way you would say it, and punctuate for breathing, because the voice pauses where the
  punctuation does: commas between items and clauses, a colon before a result ("That gives: zero, minus
  four…"), full stops to end ideas. Prefer short sentences, contractions, and conversational signposts
  ("Now look.", "So,", "Here's the key idea:"). A line may end with a comma when the thought continues on
  the next line.
- Keep lines to one or two sentences each.
"""


def step_script(job: dict) -> None:
    feedback = job.get("script_feedback", "")
    if feedback and job.get("session"):
        prompt = (f"The teacher reviewed narration.txt and outline.md and asks for these changes:\n\n{feedback}\n\n"
                  "Update narration.txt and outline.md accordingly (same rules as before). "
                  "Do not generate audio, write animation code or render.")
        session = job["session"]
    else:
        image = (f"The question is also attached as the image {job['image']} in the current directory — "
                 "read it carefully; it is the authoritative statement of the question.\n") if job.get("image") else ""
        source = f"Source: {job['source']}\n" if job.get("source") else ""
        prompt = skill_preamble(job) + f"""
This step is Phase 1 (Narration) only. The question or topic is in question.md:

<question>
{job['question']}
</question>
{source}{image}
Write narration.txt for a video of about {job['minutes']:g} minutes, in the skill's SubtitleSpec format:
{script_rules(load_settings())}- Teach it the way an excellent teacher would at this level: motivate, explain each step, connect to the
  geometry or intuition, and finish with a short summary. Be mathematically exact.
{voice_rules(load_settings())}
Also write outline.md: the scene-by-scene plan (Scene class names and what appears on screen, matching the
narration lines) followed by a complete worked solution, so the teacher can check the mathematics.

Do not generate audio, write animation code or render. Stop once narration.txt and outline.md are written.
"""
        session = ""
    code, _ = run_process(job["id"], claude_cmd(prompt, session), claude=True)
    if code or not (job_dir(job["id"]) / "narration.txt").exists():
        raise RuntimeError("Script step did not produce narration.txt")
    if load_settings().get("maths_check"):
        run_check(read_job(job["id"]))
    update_job(job["id"], status="script_ready", script_feedback="")


CHECK_PROMPT = """You are an independent senior IB examiner checking a teacher's worked solution before it is
turned into a video for {audience}.
1. Read question.md{image}. Solve the question yourself from scratch, carefully and completely, and write
   your solution to check-solution.md. Do this BEFORE you open outline.md or narration.txt.
2. Then read outline.md (the teacher's worked solution) and narration.txt (what will be said). Compare the
   final answers, every case and condition, the validity of each step, the arithmetic, and anything in the
   narration that is wrong, misleading or too loose for this level.
3. Write check.json:
   {{"verdict": "agree" | "disagree" | "unsure", "summary": "one or two sentences",
     "issues": ["each specific problem, and where it is (outline step or narration line)"]}}
   "agree" means the final answers match and nothing is mathematically wrong (minor wording points can
   still be listed as issues). "disagree" means something is wrong. "unsure" means you couldn't settle it.
Do not edit question.md, outline.md or narration.txt.
"""


def run_check(job: dict) -> dict:
    """A fresh Claude session solves the question independently and compares it with the script."""
    d = job_dir(job["id"])
    (d / "check.json").unlink(missing_ok=True)
    log(job["id"], "Independent maths check: solving the question from scratch…")
    image = f" (and the image {job['image']}, the authoritative statement)" if job.get("image") else ""
    prompt = CHECK_PROMPT.format(audience=job["audience"], image=image)
    try:
        run_process(job["id"], claude_cmd(prompt), claude=True, record_session=False)
        check = json.loads((d / "check.json").read_text())
        check = {"verdict": str(check.get("verdict", "unsure")).lower(), "summary": str(check.get("summary", "")),
                 "issues": [str(i) for i in check.get("issues", []) if str(i).strip()]}
    except (OSError, ValueError, RuntimeError, AttributeError) as e:
        check = {"verdict": "unsure", "summary": f"The check didn't finish ({e}). Read the solution carefully.",
                 "issues": []}
    if check["verdict"] not in ("agree", "disagree", "unsure"):
        check["verdict"] = "unsure"
    check["checked"] = time.time()
    (d / "check.json").write_text(json.dumps(check, indent=2))
    log(job["id"], f"Maths check: {check['verdict'].upper()}. {check['summary']}")
    return check


def step_check(job: dict) -> None:
    run_check(job)
    update_job(job["id"], status="script_ready")


def step_takes(job: dict) -> None:
    """Split the teacher's one-take recording into lines (recording.py)."""
    from recording import process
    takes = job_dir(job["id"]) / "takes"
    raw = next(iter(sorted(takes.glob("take-raw.*"))), None)
    if not raw:
        raise RuntimeError("No recording found. Record the narration again.")
    meta = json.loads((takes / "marks.json").read_text())
    log(job["id"], f"Processing your recording ({len(meta.get('marks', []))} lines marked)…")
    lines = process(job_dir(job["id"]), raw, meta.get("marks", []), meta.get("cuts", []))
    flagged = sum(1 for e in lines if e.get("flag"))
    log(job["id"], f"Recording split into {len(lines)} lines" + (f"; {flagged} to check." if flagged else "."))
    update_job(job["id"], status="takes_review")


def step_voice(job: dict) -> None:
    for name in ("voiceover.mp3", "timestamps.json"):
        (job_dir(job["id"]) / name).unlink(missing_ok=True)
    code, _ = run_process(job["id"], voice_command(), claude=False)
    if code or not (job_dir(job["id"]) / "timestamps.json").exists():
        raise RuntimeError("Voice step failed (see log)")
    update_job(job["id"], status="queued_video")


ENDCARD_RULE = ("\nEnding: finish on the summary. Do not draw end cards, 'more videos' boxes, subscribe buttons or "
                "placeholder thumbnails; the app appends a clean end screen for YouTube's end screen elements.\n")


def opening_instructions() -> str:
    """How the video opens: the channel logo during the greeting, then an introduction before any working."""
    s = load_settings()
    greeting = s.get("greeting", "").strip()
    parts = []
    if greeting:
        if s.get("logo") and Path(s["logo"]).exists():
            parts.append("Opening: the first narration line is the channel greeting. For that line show the channel logo, "
                         "channel-logo.png, centred and undistorted (house_style's logo_open(self, <that line's "
                         "duration>) does this), then fade it out.")
        else:
            parts.append(f"Opening: the first narration line is the channel greeting. Show the channel name "
                         f"\"{s.get('channel', '')}\" as a large, clean title card while it plays.")
    parts.append("Then the introduction lines get their own unhurried introduction scene: a title card with the "
                 "topic while it is introduced; then the full question, large and clear, given time to be read "
                 "while the narration reads through it (highlight each part as it is discussed); then the plan of "
                 "attack as a short numbered list if the narration gives one. Start the working only after the "
                 "introduction, in the next scene.")
    return "\n" + " ".join(parts) + "\n"


def house_rules() -> str:
    s = load_settings()
    if not s.get("house_style", True):
        rules = ("\nStyle: design the visuals yourself, following the skill. Keep the screen clean and uncluttered, "
                 "build each step up as it is spoken, and colour-code variables consistently.\n")
        if s.get("pause_try"):
            rules += ("For a line that invites the viewer to pause and try, show the task on screen and a simple "
                      "countdown during that line's pause.\n")
        return rules + (f"The teacher's own style notes: {s['house_notes'].strip()}\n" if s.get("house_notes", "").strip() else "")
    rules = """
House style: house_style.py is in the current directory; read it first. Start script.py with
`from manim import *` then `from house_style import *`, and use its palette, sizes and helpers so every video
on the channel looks the same and is easy to watch along to:
- Colour-code variables with mt() / paint(): x, y, z and the parameters keep their VAR_COLORS all video.
- Keep everything above SAFE_BOTTOM: captions and the YouTube player bar cover the bottom of the frame.
- Once the working starts (after the introduction), keep a QuestionPin of the question in the top-left
  corner (hide it when a full-screen visual needs the room), so students never have to scroll back.
- step_chip() names the current step in the top-right corner (e.g. "STEP 2 · Row reduce").
- Show what is being said: when a line refers to a term, row, equation or part of a diagram, spotlight() or
  emphasise() it during that line, and build each step up as it is spoken rather than all at once.
- result_box() around each final answer; callout("…", WARN) when the narration warns about a common mistake.
- Use these helpers where they help the viewer, not on every line. The screen should feel calm and
  uncluttered, with generous spacing, large readable maths and unhurried animations; the helpers support
  the explanation and never replace good, well-composed visuals of the mathematics itself.
"""
    if s.get("pause_try"):
        rules += ("- For a line that invites the viewer to pause and try (it ends with a pause of about 4 s), show "
                  "the task on screen and call pause_and_try(self, <that pause>) for the pause part of the segment.\n")
    if s.get("house_notes", "").strip():
        rules += f"- The teacher's own style notes: {s['house_notes'].strip()}\n"
    return rules


def latex_escape(text: str) -> str:
    return re.sub(r"([&%$#_{}])", r"\\\1", text).replace("~", r"\textasciitilde{}").replace("^", r"\^{}")


def prepare_assets(job_id: str) -> None:
    """Copy the house style, logo and worksheet template into the video folder."""
    d, s = job_dir(job_id), load_settings()
    shutil.copy(STUDIO_FILES / "house_style.py", d / "house_style.py")
    (d / "house_style.json").write_text(json.dumps({"accent": s.get("accent") or "#FFD23F",
                                                    "channel": s.get("channel", "")}))
    logo = s.get("logo")
    has_logo = False
    if logo and Path(logo).exists():
        try:   # Manim and LaTeX read PNG best; normalise whatever was uploaded
            from PIL import Image
            Image.open(logo).convert("RGBA").save(d / "channel-logo.png")
            has_logo = True
        except Exception as e:  # noqa: BLE001
            log(job_id, f"Couldn't prepare the logo: {e}")
    tex = (STUDIO_FILES / "templates" / "worksheet.tex").read_text()
    tex = (tex.replace("ACCENTHEX", (s.get("accent") or "#FFD23F").lstrip("#").upper())
              .replace("CHANNELNAME", latex_escape(s.get("channel", "")))
              .replace("BADGETEXT", latex_escape(s.get("badge", "")))
              .replace("LOGOGRAPHIC", r"\colorbox{ink}{\includegraphics[height=1.1cm]{channel-logo.png}}"
                       if has_logo else ""))
    (d / "worksheet-template.tex").write_text(tex)


ENDCARD_RULE = ("\nEnding: finish on the summary. Do not draw end cards, 'more videos' boxes, subscribe buttons or "
                "placeholder thumbnails; the app appends a clean end screen for YouTube's end screen elements.\n")


def step_video(job: dict) -> None:
    prompt = skill_preamble(job) + """
narration.txt has been approved and voiced: voiceover.mp3 and timestamps.json are in the current directory,
and outline.md has the scene plan. Do NOT run tts-generate.py and do not edit narration.txt.
Now do Phases 3–5 of the skill: write script.py timed to timestamps.json (each duration_s already includes
that line's pause), lint it, render every scene, write concat.txt, and mux video.mp4 with voiceover.mp3.
Then check it: extract frames at several points in every scene and look at them; fix overlapping or
clipped text, unreadable labels and anything mathematically wrong, re-render those scenes and re-mux.
Finish when video.mp4 is complete and verified with ffprobe.
""" + house_rules() + opening_instructions() + ENDCARD_RULE
    if job.get("retime") and (job_dir(job["id"]) / "script.py").exists():
        prompt = """The narration was re-recorded: voiceover.mp3 and timestamps.json are new (same lines, though a
line's wording may have been corrected). Do NOT run tts-generate.py or edit narration.txt. Update script.py so
every add_subcaption() text and segment duration matches the new timestamps.json (keep the visuals), lint it, re-render every scene, rewrite concat.txt if needed and re-mux
video.mp4 with the new voiceover.mp3. Verify it with ffprobe and check a few frames. Do not launch the viewer.
"""
    prepare_assets(job["id"])
    (job_dir(job["id"]) / "video.mp4").unlink(missing_ok=True)
    code, _ = run_process(job["id"], claude_cmd(prompt, job.get("session", "")), claude=True)
    if code or not (job_dir(job["id"]) / "video.mp4").exists():
        raise RuntimeError("Video step did not produce video.mp4")
    update_job(job["id"], retime=False)
    update_job(job["id"], status="review")


def step_revise(job: dict) -> None:
    voice = " ".join(f'"{c}"' if " " in c else c for c in voice_command())
    prepare_assets(job["id"])
    if job.get("voice_mode") == "record":
        prompt = f"""The teacher watched video.mp4 and gives this feedback (timestamps refer to video.mp4):

{job.get('video_feedback', '')}

Apply it, following the skill's Phase 6 iteration rules: for a referenced moment, extract that frame and
look at it before changing code. The narration is the teacher's own recording: never edit narration.txt,
voiceover.mp3 or timestamps.json, and never run tts-generate.py. If the feedback asks to change what is
said, change only the visuals and end your final message with the line numbers the teacher needs to
re-record. Keep to the house style in house_style.py. {ENDCARD_RULE.strip()} Re-render only the affected
scenes, update concat.txt if needed, re-mux video.mp4, and verify it with ffprobe. Do not launch the viewer.
"""
        code, _ = run_process(job["id"], claude_cmd(prompt, job.get("session", "")), claude=True)
        if code or not (job_dir(job["id"]) / "video.mp4").exists():
            raise RuntimeError("Revision did not produce video.mp4")
        update_job(job["id"], status="review", video_feedback="")
        return
    prompt = f"""The teacher watched video.mp4 and gives this feedback (timestamps refer to video.mp4):

{job.get('video_feedback', '')}

Apply it, following the skill's Phase 6 iteration rules: for a referenced moment, extract that frame and
look at it before changing code. If narration must change, edit narration.txt, then regenerate the voice
with exactly this command (unchanged lines are cached):
{voice}
and update script.py to the new timestamps.json. Keep to the house style in house_style.py.
{ENDCARD_RULE.strip()} Re-render only the affected scenes, update concat.txt if
needed, re-mux video.mp4, and verify it with ffprobe. Do not launch the viewer.
"""
    code, _ = run_process(job["id"], claude_cmd(prompt, job.get("session", "")), claude=True)
    if code or not (job_dir(job["id"]) / "video.mp4").exists():
        raise RuntimeError("Revision did not produce video.mp4")
    update_job(job["id"], status="review", video_feedback="")


def export_prompt(job: dict) -> str:
    s = load_settings()
    badge = s.get("badge", "").strip()
    seo = ("Titles are what students type into YouTube search: lead with the topic or skill in plain words "
           "(e.g. \"Shortest distance between skew lines\")" + (f", then \" | {badge}\"" if badge else "") + ". ")
    if job.get("source"):
        seo += f"Include the exam reference \"{job['source']}\" in at least one title and in the description. "
    seo += "At most 70 characters; never promise something the video doesn't deliver."
    desc_extra = (f" Then a line \"Question: {job['source']}\"." if job.get("source") else "") + (
        f" Then a line \"Part of the {job['series']} series.\"" if job.get("series") else "")
    steps = [
        """Render every scene in script.py at 1920x1080 and 30 fps (set config.pixel_width/pixel_height/frame_rate
   accordingly, keeping frame_width/frame_height and all timing unchanged), concatenate in the same order and
   mux with voiceover.mp3 into video_1080p.mp4. Check its duration matches video.mp4 within 0.5 s and look at
   two or three frames. Then restore script.py's original render settings.""",
        f"""Write youtube.md: the best title on the first line, a blank line, then a 2–4 sentence description for
   students (what the question is, which methods are used, the level).{desc_extra} Then a line of 8–12
   comma-separated tags (topic, method, "IB Maths", the course and level). No chapter timestamps (added
   automatically). {seo}""",
        """Write titles.json: ["…", "…", "…"], three genuinely different titles for YouTube's Test & compare (one
   leading with the skill or topic, one with the question or exam reference, one with the payoff or the trap),
   all following the title rules above. The first is youtube.md's title.""",
        """Write thumbnail.json: {"variants": [{"headline": …, "subline": …, "frame_s": …}, … three of them]}, three
   different thumbnails for Test & compare, each with its own angle and its own background frame:
   - headline: 2–4 punchy words, e.g. "Shortest distance"; subline: 2–5 words completing it, e.g. "between
     vector lines". Together they read as a title.
   - frame_s: the time of a visually striking frame (a colourful diagram, graph or 3D view rather than a page of
     equations). Extract candidate frames and look at them before choosing.""",
    ]
    if s.get("shorts"):
        steps.append("""Write shorts.json: {"start_s": …, "end_s": …, "title": "…"}: the best self-contained 30–55 second
   stretch of video.mp4 for a YouTube Short (a neat trick, a trap, or the key step) that makes sense on its own.
   Start at the beginning of a narration line and end at the end of one (use timestamps.json); never include the
   greeting. title: a 3–8 word hook shown above the clip.""")
    if s.get("worksheet"):
        steps.append("""Make the practice worksheet: copy worksheet-template.tex to worksheet.tex and fill in the marked
   parts: the title, 5–6 practice questions like this one (easiest first, the last one exam-style and harder),
   and short worked answers. Solve every question yourself to be sure the answers are right. Compile with
   `pdflatex -interaction=nonstopmode worksheet.tex` (twice) and fix any errors until worksheet.pdf exists.""")
    numbered = "\n".join(f"{i}. {text}" for i, text in enumerate(steps, 1))
    return f"The teacher approved video.mp4. Make the final YouTube version:\n{numbered}\nDo not change the narration or voice.\n"


def step_export(job: dict) -> None:
    d = job_dir(job["id"])
    for name in ("video_1080p.mp4", "titles.json", "shorts.json", "short.mp4", "worksheet.pdf"):
        (d / name).unlink(missing_ok=True)
    prepare_assets(job["id"])
    code, _ = run_process(job["id"], claude_cmd(export_prompt(job), job.get("session", "")), claude=True)
    if code or not (d / "video_1080p.mp4").exists():
        raise RuntimeError("Export did not produce video_1080p.mp4")
    if load_settings().get("endscreen_seconds", 0) > 0:
        add_endscreen(job["id"])
    else:
        update_job(job["id"], endscreen_start=None, endscreen_signoff="")
    try:
        master_audio(job["id"])
    except Exception as e:  # noqa: BLE001 — the unmastered video is still usable
        log(job["id"], f"Audio mastering skipped: {e}")
    chapters = chapter_lines(d)
    text = (d / "youtube.md").read_text().rstrip() if (d / "youtube.md").exists() else job["title"]
    if chapters:
        text += "\n\nChapters\n" + "\n".join(chapters)
    (d / "youtube.md").write_text(text + "\n")
    extras = [("Thumbnails", make_thumbnail), ("Subtitles", make_subtitles)]
    if load_settings().get("shorts") and (d / "shorts.json").exists():
        extras.append(("Short", make_short))
    for name, make in extras:
        try:
            make(job["id"])
        except Exception as e:  # noqa: BLE001 — an extra must not fail the export
            log(job["id"], f"{name} not made: {e}")
    if load_settings().get("worksheet") and not (d / "worksheet.pdf").exists():
        log(job["id"], "Worksheet not made (worksheet.pdf missing; see the log above).")
    update_job(job["id"], status="done")


def music_track() -> Path | None:
    s = load_settings()
    if s.get("music", "off") == "off":
        return None
    if s.get("music_file") and Path(s["music_file"]).exists():
        return Path(s["music_file"])
    from audio import builtin_pad
    return builtin_pad(STUDIO_DIR / "music-builtin.wav")


def greeting_end(d: Path) -> float | None:
    """When the greeting line finishes (timestamps.json), so intro music can fade out after it."""
    greeting = re.sub(r"\W+", " ", load_settings().get("greeting", "")).strip().lower()
    try:
        subs = json.loads((d / "timestamps.json").read_text())["subtitles"]
    except (OSError, ValueError, KeyError):
        return None
    for sub in subs[:3]:
        if greeting and re.sub(r"\W+", " ", sub["text"]).strip().lower() == greeting:
            return float(sub["end_s"])
    return None


def master_audio(job_id: str) -> None:
    """Voice polish, music bed (intro and end screen, or throughout) and YouTube loudness on video_1080p.mp4."""
    from audio import duration, master
    d, s, job = job_dir(job_id), load_settings(), read_job(job_id)
    src = d / "video_1080p.mp4"
    music, regions = music_track(), None
    if music and s.get("music") == "intro_outro":
        regions = []
        if (end := greeting_end(d)) is not None:
            regions.append((0.0, end + 1.0))
        if job.get("endscreen_start") is not None:
            regions.append((job["endscreen_start"] - 0.5, duration(src) + 5))
        if not regions:
            music = None
    log(job_id, "Mastering audio" + (" with music" if music else "") + " (YouTube loudness, -14 LUFS)…")
    tmp = d / "video_1080p_mastered.mp4"
    stats = master(src, tmp, music, regions, float(s.get("music_level", 30)))
    tmp.replace(src)
    log(job_id, f"Audio mastered: {stats['input_i']} LUFS → -14 LUFS.")


def add_endscreen(job_id: str) -> None:
    """Append a clean end screen (with the spoken sign-off) to video_1080p.mp4, leaving room for
    YouTube's end screen elements. The un-extended export is kept as video_1080p_main.mp4."""
    from endscreen import append
    d, s = job_dir(job_id), load_settings()
    (d / "video_1080p.mp4").replace(d / "video_1080p_main.mp4")
    voice = None
    signoff = s.get("signoff", "").strip()
    recorded = STUDIO_DIR / "signoff.wav"
    if signoff and read_job(job_id).get("voice_mode") == "record":
        if recorded.exists():
            from audio import duration
            shutil.copy(recorded, d / "endscreen-voice.wav")
            voice = d / "endscreen-voice.wav"
            (d / "endscreen-timestamps.json").write_text(json.dumps(
                {"subtitles": [{"duration_s": duration(voice), "pause_s": 0}]}))
        else:
            log(job_id, "No recorded sign-off yet (⚙ Settings → End screen), so the end screen is silent.")
    elif signoff:
        (d / "endscreen-narration.txt").write_text(f"subtitles:\n- {signoff}\n")
        code, _ = run_process(job_id, voice_command("endscreen-narration.txt", "endscreen-voice.mp3",
                                                    "endscreen-timestamps.json"), claude=False)
        voice = d / "endscreen-voice.mp3" if code == 0 and (d / "endscreen-voice.mp3").exists() else None
    log(job_id, f"Adding a {s['endscreen_seconds']:g}s end screen…")
    start = append(d / "video_1080p_main.mp4", d / "video_1080p.mp4", float(s["endscreen_seconds"]),
                   logo=s.get("logo") or None, channel=s.get("channel", ""), voice=voice)
    update_job(job_id, endscreen_start=round(start, 3), endscreen_seconds=float(s["endscreen_seconds"]),
               endscreen_signoff=signoff if voice else "")
    log(job_id, f"End screen added from {start:.1f}s: add YouTube end screen elements there.")


def make_subtitles(job_id: str) -> Path:
    """subtitles.srt for YouTube, from the rendered scenes' caption timings. Local and free."""
    from subtitles import srt_time, two_lines, write
    d = job_dir(job_id)
    out = write(d)
    job = read_job(job_id)
    if job.get("endscreen_signoff") and (d / "endscreen-timestamps.json").exists():
        speech = json.loads((d / "endscreen-timestamps.json").read_text())["subtitles"][0]
        a = job["endscreen_start"] + 0.4
        n = out.read_text().count(" --> ") + 1
        out.write_text(out.read_text().rstrip() + f"\n\n{n}\n{srt_time(a)} --> "
                       f"{srt_time(a + speech['duration_s'] - speech.get('pause_s', 0))}\n{two_lines(job['endscreen_signoff'])}\n")
    log(job_id, f"Subtitles made: {out.read_text().count(' --> ')} captions.")
    return out


THUMB_NAMES = ["thumbnail.png", "thumbnail-2.png", "thumbnail-3.png"]


def thumbnail_specs(job_id: str) -> list[dict]:
    """Up to three thumbnail designs (for YouTube's Test & compare) from thumbnail.json."""
    d = job_dir(job_id)
    default = {"headline": read_job(job_id).get("title", "")[:60], "subline": "", "frame_s": None}
    try:
        data = json.loads((d / "thumbnail.json").read_text())
    except (OSError, ValueError):
        data = {}
    variants = data.get("variants") if isinstance(data, dict) and "variants" in data else [data]
    specs = [{**default, **v} for v in variants if isinstance(v, dict) and v][:len(THUMB_NAMES)]
    return specs or [default]


def make_thumbnail(job_id: str, variant: int | None = None, **changes) -> list[Path]:
    """Render the thumbnails (or one variant, with `changes`) from thumbnail.json and the branding. Local and free."""
    d = job_dir(job_id)
    video = d / "video_1080p.mp4" if (d / "video_1080p.mp4").exists() else d / "video.mp4"
    if not video.exists():
        raise ValueError("Make the video first.")
    specs = thumbnail_specs(job_id)
    if variant is not None:
        variant = max(0, min(int(variant), len(THUMB_NAMES) - 1))
        while len(specs) <= variant:
            specs.append({**specs[0], "frame_s": None})
        specs[variant] = {**specs[variant], **{k: v for k, v in changes.items() if v is not None}}
    total = None
    s = load_settings()
    from thumbnail import render
    made = []
    for i, spec in enumerate(specs):
        if variant is not None and i != variant and (d / THUMB_NAMES[i]).exists():
            continue
        if spec.get("frame_s") in (None, ""):
            if total is None:
                out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of",
                                      "csv=p=0", str(video)], capture_output=True, text=True).stdout
                total = float(out or 0)
            spec["frame_s"] = round(total * (0.4 + 0.15 * i), 1)
        spec["frame_s"] = float(spec["frame_s"])
        render(video, d / THUMB_NAMES[i], spec["headline"], spec.get("subline", ""), spec["frame_s"],
               logo=s.get("logo") or None, channel=s.get("channel", ""), badge=s.get("badge", ""),
               accent=s.get("accent") or "#FFD23F")
        made.append(d / THUMB_NAMES[i])
        log(job_id, f"Thumbnail {i + 1}: \"{spec['headline']}\" / \"{spec.get('subline', '')}\" at {spec['frame_s']:g}s.")
    for extra in THUMB_NAMES[len(specs):]:
        (d / extra).unlink(missing_ok=True)
    (d / "thumbnail.json").write_text(json.dumps({"variants": specs}, indent=2))
    return made


def short_spec(job_id: str) -> dict:
    try:
        return json.loads((job_dir(job_id) / "shorts.json").read_text())
    except (OSError, ValueError):
        return {}


def make_short(job_id: str, **changes) -> Path:
    """short.mp4: a vertical YouTube Short cut from the final video, with big captions. Local and free."""
    d, s = job_dir(job_id), load_settings()
    video = d / "video_1080p.mp4"
    if not video.exists():
        raise ValueError("Export the video first.")
    spec = {**short_spec(job_id), **{k: v for k, v in changes.items() if v not in (None, "")}}
    if "start_s" not in spec or "end_s" not in spec:
        raise ValueError("Choose where the Short starts and ends.")
    if not (d / "subtitles.srt").exists():
        make_subtitles(job_id)
    from shorts import make
    log(job_id, f"Making the Short from {float(spec['start_s']):.1f}s to {float(spec['end_s']):.1f}s…")
    info = make(video, d / "short.mp4", float(spec["start_s"]), float(spec["end_s"]),
                spec.get("title") or read_job(job_id)["title"], d / "subtitles.srt", s.get("logo") or None,
                s.get("channel", ""), s.get("accent") or "#FFD23F")
    (d / "shorts.json").write_text(json.dumps(info, indent=2))
    log(job_id, f"Short made: {info['seconds']:g}s, \"{info['title']}\".")
    return d / "short.mp4"


def chapter_lines(d: Path) -> list[str]:
    """YouTube chapters from the scene list: first at 0:00, at least three, each at least 10 s long."""
    concat = d / "concat.txt"
    if not concat.exists():
        return []
    scenes, t = [], 0.0
    for line in concat.read_text().splitlines():
        m = re.match(r"file '(.+)'", line.strip())
        if not m:
            continue
        path = d / m.group(1)
        out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0",
                              str(path)], capture_output=True, text=True)
        try:
            dur = float(out.stdout.strip())
        except ValueError:
            continue
        name = re.sub(r"^Scene\d*_?", "", path.stem)
        name = re.sub(r"(?<=[a-z0-9])(?=[A-Z])|_", " ", name).strip() or path.stem
        scenes.append((t, dur, name))
        t += dur
    merged = [list(s) for s in scenes]
    while len(merged) > 1 and (short := next((i for i, c in enumerate(merged) if c[1] < 10), None)) is not None:
        if short:   # fold a short scene into the chapter before it
            merged[short - 1][1] += merged[short][1]
            del merged[short]
        else:       # a short opening scene joins the next one, which names the chapter
            merged[1] = [merged[0][0], merged[0][1] + merged[1][1], merged[1][2]]
            del merged[0]
    if len(merged) < 3:
        return []
    fmt = lambda s: f"{int(s // 60)}:{int(s % 60):02d}"  # noqa: E731
    return [f"{fmt(start)} {name}" for start, _, name in merged]


STEPS = {"scripting": step_script, "checking": step_check, "processing": step_takes, "voicing": step_voice, "rendering": step_video,
         "revising": step_revise, "exporting": step_export}


def worker(queue_map: dict) -> None:
    """Take the oldest job waiting in one of this worker's queues and run its step; repeat forever."""
    while True:
        job = None
        with LOCK:
            waiting = [j for j in all_jobs() if j.get("status") in queue_map]
            if waiting:
                job = min(waiting, key=lambda j: j.get("queued_at", j.get("created", 0)))
                job = update_job(job["id"], status=queue_map[job["status"]], error="")
        if not job:
            time.sleep(1)
            continue
        status = job["status"]
        log(job["id"], f"── {status.capitalize()} ──")
        started = time.time()
        try:
            STEPS[status](job)
            log(job["id"], f"Step finished in {time.time() - started:.0f}s.")
        except Exception as e:  # noqa: BLE001 — any failure is shown to the user with a Retry button
            current = read_job(job["id"])
            if current.get("status") == "cancelled":
                continue
            log(job["id"], f"FAILED: {e}")
            update_job(job["id"], status="failed", failed_step=status, error=str(e))


def recover_interrupted() -> None:
    """Steps that were running when the app stopped are re-queued on start-up."""
    back = {v: k for k, v in {**LIGHT, **HEAVY}.items()}
    for j in all_jobs():
        if j.get("status") in back:
            update_job(j["id"], status=back[j["status"]])


# ---------------------------------------------------------------- actions --
def act(job_id: str, action: str, body: dict) -> dict:
    with LOCK:
        job = read_job(job_id)
        status = job["status"]
        d = job_dir(job_id)

        def need(*ok):
            if status not in ok:
                raise ValueError(f"Can't {action} while the video is '{status}'.")

        if action == "narration":
            need("script_ready", "review")
            text = body.get("text", "")
            if "subtitles:" not in text:
                raise ValueError("narration.txt must keep its 'subtitles:' heading.")
            (d / "narration.txt").write_text(text if text.endswith("\n") else text + "\n")
            log(job_id, "Narration edited by you.")
            return read_job(job_id)
        if action == "approve":
            need("script_ready")
            if load_settings().get("voice_mode", "record") == "record":
                return update_job(job_id, status="record", voice_mode="record")
            return update_job(job_id, status="queued_voice", voice_mode="clone", queued_at=time.time())
        if action == "use_recording":
            need("takes_review")
            retime = (d / "script.py").exists() and (d / "video.mp4").exists()
            log(job_id, "Using your recording" + (": re-timing the animation to it." if retime else "."))
            return update_job(job_id, status="queued_video", retime=retime, queued_at=time.time())
        if action == "record_again":
            need("takes_review", "record", "review", "done")
            return update_job(job_id, status="record")
        if action == "subtitle":
            need("takes_review")
            from recording import set_subtitle
            set_subtitle(d, int(body.get("index", -1)), str(body.get("text", "")))
            return read_job(job_id)
        if action == "rescript":
            need("script_ready")
            return update_job(job_id, status="queued_script", script_feedback=body.get("notes", ""),
                              queued_at=time.time())
        if action == "revoice":
            need("review", "done")
            if job.get("voice_mode") == "record":
                return update_job(job_id, status="takes_review" if (d / "takes" / "lines.json").exists() else "record")
            log(job_id, "Re-recording the voice with the current settings.")
            return update_job(job_id, status="queued_voice", retime=True, queued_at=time.time())
        if action == "revise":
            need("review", "done")
            if not body.get("notes", "").strip():
                raise ValueError("Write what you'd like changed first.")
            return update_job(job_id, status="queued_revise", video_feedback=body["notes"], queued_at=time.time())
        if action == "export":
            need("review", "done")
            return update_job(job_id, status="queued_export", queued_at=time.time())
        if action == "retry":
            need("failed", "cancelled")
            back = {v: k for k, v in {**LIGHT, **HEAVY}.items()}
            return update_job(job_id, status=back.get(job.get("failed_step"), "queued_script"),
                              queued_at=time.time(), error="")
        if action == "cancel":
            proc = PROCS.get(job_id)
            if status in RUNNING or status.startswith("queued_"):
                update_job(job_id, status="cancelled", failed_step={**LIGHT, **HEAVY}.get(status, status))
            if proc:
                try:
                    os.killpg(proc.pid, signal.SIGTERM)
                except (ProcessLookupError, PermissionError):
                    proc.terminate()
            log(job_id, "Cancelled.")
            return read_job(job_id)
        if action == "thumbnail":
            need("review", "done")
            frame = body.get("frame_s")
            try:
                make_thumbnail(job_id, int(body.get("variant") or 0), headline=body.get("headline"),
                               subline=body.get("subline"), frame_s=float(frame) if frame not in (None, "") else None)
            except Exception as e:  # noqa: BLE001 — shown to the user
                raise ValueError(f"Couldn't make the thumbnail: {e}") from e
            return read_job(job_id)
        if action == "short":
            need("done")
            try:
                make_short(job_id, start_s=body.get("start_s"), end_s=body.get("end_s"), title=body.get("title"))
            except Exception as e:  # noqa: BLE001 — shown to the user
                raise ValueError(f"Couldn't make the Short: {e}") from e
            return read_job(job_id)
        if action == "check":
            need("script_ready")
            return update_job(job_id, status="queued_check", queued_at=time.time())
        if action == "subtitles":
            need("review", "done")
            try:
                make_subtitles(job_id)
            except Exception as e:  # noqa: BLE001 — shown to the user
                raise ValueError(f"Couldn't make subtitles: {e}") from e
            return read_job(job_id)
        if action == "delete":
            if status in RUNNING:
                raise ValueError("Cancel it first.")
            shutil.rmtree(d)
            return {"deleted": job_id}
    raise ValueError(f"Unknown action {action!r}")


def job_detail(job_id: str) -> dict:
    job = read_job(job_id)
    d = job_dir(job_id)
    read = lambda name: (d / name).read_text(errors="replace") if (d / name).exists() else ""  # noqa: E731
    files = {name: (d / name).stat().st_mtime_ns for name in SERVED_FILES if (d / name).exists()}
    def load(name, default):
        try:
            return json.loads(read(name)) if read(name) else default
        except ValueError:
            return default
    return {**job, "narration": read("narration.txt"), "outline": read("outline.md"),
            "youtube": read("youtube.md"), "log": tail(d / "studio.log"), "files": files, "folder": str(d),
            "thumbnails": thumbnail_specs(job_id), "titles": load("titles.json", []),
            "short": load("shorts.json", {}), "check": load("check.json", None),
            "takes": takes_detail(d), "entries": entries_detail(d)}


def takes_detail(d: Path) -> list[dict]:
    try:
        lines = json.loads((d / "takes" / "lines.json").read_text())
    except (OSError, ValueError):
        return []
    for e in lines:
        f = d / "takes" / e["file"]
        e["v"] = f.stat().st_mtime_ns if f.exists() else 0
    return lines


def entries_detail(d: Path) -> list[dict]:
    """The script as teleprompter cards: each line's text and any [pause N] hint."""
    try:
        from recording import tts
        return [{"text": t, "hint": h} for t, h in tts().parse_entries(str(d / "narration.txt"))]
    except (OSError, SystemExit, ValueError):
        return []


def health() -> dict:
    s = load_settings()
    checks = {
        "claude": bool(shutil.which(s["claude"] or "claude")),
        "ffmpeg": bool(shutil.which("ffmpeg")),
        "latex": bool(shutil.which("latex")),
        "manim": subprocess.run([sys.executable, "-c", "import manim"], capture_output=True).returncode == 0,
        "voice_sample": s.get("voice_mode", "record") == "record" or bool(s.get("voice_sample") and Path(s["voice_sample"]).exists()),
        "sherpa_onnx": subprocess.run([sys.executable, "-c", "import sherpa_onnx"], capture_output=True).returncode == 0,
    }
    return {"checks": checks, "home": str(HOME), "python": sys.executable}


# ------------------------------------------------------------ own recording --
AUDIO_EXTS = {".webm", ".m4a", ".mp4", ".mp3", ".wav", ".aac", ".ogg", ".oga", ".flac", ".mov"}


def audio_ext(name: str, default: str) -> str:
    ext = Path(name).suffix.lower() or default
    if ext not in AUDIO_EXTS:
        raise ValueError("Use an audio file (m4a, mp3, wav, webm, …).")
    return ext


def save_recording(job_id: str, data: bytes, name: str, meta: dict) -> dict:
    """The one-take narration from the teleprompter (or an uploaded file): store it and queue processing."""
    with LOCK:
        job = read_job(job_id)
        if job["status"] not in ("record", "takes_review"):
            raise ValueError(f"Can't take a recording while the video is '{job['status']}'.")
        if len(data) < 2000:
            raise ValueError("The recording is empty. Check the microphone and try again.")
        takes = job_dir(job_id) / "takes"
        takes.mkdir(exist_ok=True)
        for old in takes.glob("take-raw.*"):
            old.unlink()
        (takes / f"take-raw{audio_ext(name, '.webm')}").write_bytes(data)
        marks = [float(m) for m in meta.get("marks", [])]
        cuts = [[float(a), float(b)] for a, b in meta.get("cuts", [])]
        (takes / "marks.json").write_text(json.dumps({"marks": marks, "cuts": cuts}))
        log(job_id, f"Recording received ({len(data) / 1e6:.1f} MB).")
        return update_job(job_id, status="queued_takes", queued_at=time.time())


def save_retake(job_id: str, index: int, data: bytes, name: str) -> dict:
    """Replace one line with a new recording of it."""
    from recording import retake
    with LOCK:
        job = read_job(job_id)
        if job["status"] != "takes_review":
            raise ValueError("Re-record lines while checking the recording.")
    takes = job_dir(job_id) / "takes"
    tmp = takes / f"retake{audio_ext(name, '.webm')}"
    tmp.write_bytes(data)
    try:
        e = retake(job_dir(job_id), index, tmp)
    finally:
        tmp.unlink(missing_ok=True)
    log(job_id, f"Line {index + 1} re-recorded" + (f" ({e['flag']})" if e.get("flag") else "."))
    return read_job(job_id)


def save_signoff(data: bytes, name: str) -> dict:
    from recording import clean_clip
    STUDIO_DIR.mkdir(parents=True, exist_ok=True)
    tmp = STUDIO_DIR / f"signoff-raw{audio_ext(name, '.webm')}"
    tmp.write_bytes(data)
    try:
        seconds = clean_clip(tmp, STUDIO_DIR / "signoff.wav")
    finally:
        tmp.unlink(missing_ok=True)
    return {**load_settings(), "signoff_seconds": round(seconds, 1)}


# ------------------------------------------------------------- voice picker --
def start_voice_pick(recording: Path) -> dict:
    """Test clips from a long recording in the background; results land in PICK_DIR."""
    if voice_pick_state().get("status") == "running":
        raise ValueError("Already testing a recording; wait for it to finish.")
    PICK_DIR.mkdir(parents=True, exist_ok=True)
    for old in PICK_DIR.glob("*"):
        if old.is_file():
            old.unlink()
    s = load_settings()
    cmd = [sys.executable, str(TOOLS_DIR / "voice-pick.py"), str(recording), "--out-dir", str(PICK_DIR),
           "--speed", f"{s['speed']:g}", "--clone-guidance", f"{s['guidance']:g}"]
    for item in re.split(r"[,\s]+", s.get("say", "").strip()):
        if "=" in item:
            cmd += ["--say", item]
    (PICK_DIR / "status.json").write_text(json.dumps({"status": "running", "started": time.time()}))

    def run():
        with open(PICK_DIR / "log.txt", "w") as out:
            code = subprocess.run(cmd, stdout=out, stderr=subprocess.STDOUT, env=tool_env(), cwd=PICK_DIR).returncode
        ok = code == 0 and (PICK_DIR / "results.json").exists()
        (PICK_DIR / "status.json").write_text(json.dumps({"status": "ready" if ok else "failed",
                                                          "finished": time.time()}))
    threading.Thread(target=run, daemon=True).start()
    return voice_pick_state()


def voice_pick_state() -> dict:
    try:
        state = json.loads((PICK_DIR / "status.json").read_text())
    except (OSError, ValueError):
        return {"status": "none"}
    lines = [l for l in tail(PICK_DIR / "log.txt", 400).splitlines()
             if l.strip() and "ConvertTokensToIds" not in l and not re.fullmatch(r"\s*-?\d+\.\d+\s*", l)]
    progress = [l for l in lines if l.startswith("PROGRESS ")]
    if progress:
        done, total = progress[-1].split()[1].split("/")
        state["progress"] = [int(done), int(total)]
    state["log"] = "\n".join(l for l in lines if not l.startswith("PROGRESS "))[-3000:]
    if state.get("status") == "running" and time.time() - state.get("started", 0) > 3 * 3600:
        state["status"] = "failed"
    try:
        state["results"] = json.loads((PICK_DIR / "results.json").read_text())["results"][:5]
    except (OSError, ValueError, KeyError):
        state["results"] = []
    state["in_use"] = load_settings().get("voice_pick_used", "")
    return state


def use_voice_pick(clip_id: str) -> dict:
    results = {r["id"]: r for r in voice_pick_state().get("results", [])}
    if clip_id not in results:
        raise ValueError("That clip isn't in the latest results.")
    for old in STUDIO_DIR.glob("voice-sample.*"):
        old.unlink()
    dest = STUDIO_DIR / "voice-sample.wav"
    shutil.copy(PICK_DIR / results[clip_id]["clip"], dest)
    return save_settings({"voice_sample": str(dest), "voice_text": results[clip_id]["transcript"],
                          "voice_pick_used": clip_id})


# ------------------------------------------------------------- series plans --
SYLLABUS_PROMPT = """Plan a YouTube series of {count} explainer videos for {audience} on this topic:

{topic}

Each video is one worked question (or a short concept explainer) of about {minutes:g} minutes, made by the
channel's video app. Order them from the foundations to full exam-level questions, so a student can work
through the playlist in order, and cover the syllabus points a student needs for this topic. Write
original questions in the style of IB exam questions (with all the numbers, so each can be solved) rather
than copying past-paper text.

Write series.json in the current directory:
{{"series": "short playlist name, e.g. Vectors: lines and planes",
  "videos": [{{"title": "short video title", "question": "the full question text",
              "why": "one line: what the student learns"}}, …]}}
Write only series.json.
"""


def plan_list() -> list[dict]:
    plans = []
    for p in PLANS_DIR.glob("*/plan.json"):
        try:
            plans.append(json.loads(p.read_text()))
        except ValueError:
            pass
    return sorted(plans, key=lambda p: p.get("created", 0), reverse=True)


def plan_path(plan_id: str) -> Path:
    if not SLUG_RE.match(plan_id or ""):
        raise KeyError(plan_id)
    path = PLANS_DIR / plan_id / "plan.json"
    if not path.exists():
        raise KeyError(plan_id)
    return path


def create_plan(topic: str, count: int, audience: str) -> dict:
    topic = topic.strip()
    if not topic:
        raise ValueError("Choose or type a topic first.")
    s = load_settings()
    count = max(2, min(15, int(count or 6)))
    words = re.findall(r"[a-z0-9]+", topic.lower())[:6] or ["series"]
    base = "-".join(words)[:48].strip("-") or "series"
    plan_id, n = base, 2
    while (PLANS_DIR / plan_id).exists():
        plan_id, n = f"{base}-{n}", n + 1
    d = PLANS_DIR / plan_id
    d.mkdir(parents=True)
    plan = {"id": plan_id, "topic": topic, "count": count, "audience": audience or s["audience"],
            "status": "planning", "created": time.time(), "series": "", "videos": []}
    (d / "plan.json").write_text(json.dumps(plan, indent=2))
    prompt = SYLLABUS_PROMPT.format(count=count, audience=plan["audience"], topic=topic,
                                    minutes=float(s.get("minutes", 4)))

    def run():
        with open(d / "plan.log", "w") as out:
            try:
                subprocess.run(claude_cmd(prompt), cwd=d, stdout=out, stderr=subprocess.STDOUT, env=tool_env())
            except FileNotFoundError:
                out.write("Claude Code isn't installed or isn't on PATH.\n")
        current = json.loads((d / "plan.json").read_text())
        try:
            data = json.loads((d / "series.json").read_text())
            current["series"] = str(data.get("series") or topic)
            current["videos"] = [{"title": str(v.get("title", "")), "question": str(v.get("question", "")),
                                  "why": str(v.get("why", "")), "job": ""}
                                 for v in data.get("videos", []) if v.get("question")]
            current["status"] = "ready" if current["videos"] else "failed"
        except (OSError, ValueError, AttributeError):
            current["status"] = "failed"
        (d / "plan.json").write_text(json.dumps(current, indent=2))
    threading.Thread(target=run, daemon=True).start()
    return plan


def plan_make(plan_id: str, index) -> dict:
    """Create videos from a plan: one (index) or every one not yet made ("all")."""
    with LOCK:
        path = plan_path(plan_id)
        plan = json.loads(path.read_text())
        picks = range(len(plan["videos"])) if index == "all" else [int(index)]
        for i in picks:
            v = plan["videos"][i]
            if v.get("job") and (HOME / v["job"]).exists():
                continue
            job = create_job(v["question"], plan["audience"], 0, None, series=plan.get("series", ""))
            update_job(job["id"], title=v.get("title") or job["title"])
            v["job"] = job["id"]
        path.write_text(json.dumps(plan, indent=2))
        return plan


def plan_delete(plan_id: str) -> dict:
    shutil.rmtree(plan_path(plan_id).parent)
    return {"deleted": plan_id}


# ------------------------------------------------------------------ server --
class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def send_json(self, data, code=200):
        body = json.dumps(data).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def read_body(self) -> bytes:
        n = int(self.headers.get("Content-Length") or 0)
        if n > MAX_UPLOAD:
            raise ValueError("Upload too large.")
        return self.rfile.read(n) if n else b""

    def do_GET(self):
        path = unquote(urlparse(self.path).path)
        try:
            if path in ("/", "/index.html"):
                body = UI_FILE.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif path == "/api/jobs":
                self.send_json([{k: j.get(k) for k in ("id", "title", "status", "created", "updated", "error")}
                                for j in all_jobs()])
            elif m := re.match(r"^/api/jobs/([^/]+)$", path):
                self.send_json(job_detail(m.group(1)))
            elif path == "/api/settings":
                self.send_json(load_settings())
            elif path == "/api/logo":
                logo = load_settings().get("logo")
                if not logo or not Path(logo).exists():
                    raise KeyError(path)
                self.send_file(Path(logo))
            elif path == "/api/health":
                self.send_json(health())
            elif path == "/api/voice-pick":
                self.send_json(voice_pick_state())
            elif m := re.match(r"^/voice-pick/(clip-\d+(?:-test)?\.(?:wav|mp3))$", path):
                self.send_file(PICK_DIR / m.group(1))
            elif path == "/api/music":
                track = music_track() if load_settings().get("music") != "off" else None
                if not track:
                    raise KeyError(path)
                self.send_file(track)
            elif path == "/voice-script":
                self.send_file(STUDIO_FILES / "voice-recording-script.md")
            elif path == "/api/plans":
                self.send_json(plan_list())
            elif m := re.match(r"^/files/([^/]+)/takes/(line-\d{3}\.wav)$", path):
                self.send_file(job_dir(m.group(1)) / "takes" / m.group(2))
            elif path == "/api/signoff":
                self.send_file(STUDIO_DIR / "signoff.wav")
            elif m := re.match(r"^/files/([^/]+)/([^/]+)$", path):
                if m.group(2) not in SERVED_FILES:
                    raise KeyError(path)
                self.send_file(job_dir(m.group(1)) / m.group(2))
            else:
                self.send_error(404)
        except (KeyError, FileNotFoundError):
            self.send_error(404)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_POST(self):
        path = unquote(urlparse(self.path).path)
        try:
            if path == "/api/jobs":
                body = json.loads(self.read_body() or b"{}")
                self.send_json(create_job(body.get("question", ""), body.get("audience", ""),
                                          body.get("minutes", 0), body.get("image"), body.get("source", ""),
                                          body.get("series", "")))
            elif m := re.match(r"^/api/jobs/([^/]+)/recording$", path):
                self.send_json(save_recording(m.group(1), self.read_body(),
                                              unquote(self.headers.get("X-Filename", "take.webm")),
                                              json.loads(unquote(self.headers.get("X-Meta", "{}")))))
            elif m := re.match(r"^/api/jobs/([^/]+)/retake/(\d+)$", path):
                self.send_json(save_retake(m.group(1), int(m.group(2)), self.read_body(),
                                           unquote(self.headers.get("X-Filename", "line.webm"))))
            elif path == "/api/signoff":
                self.send_json(save_signoff(self.read_body(), unquote(self.headers.get("X-Filename", "signoff.webm"))))
            elif m := re.match(r"^/api/jobs/([^/]+)/(\w+)$", path):
                self.send_json(act(m.group(1), m.group(2), json.loads(self.read_body() or b"{}")))
            elif path == "/api/settings":
                self.send_json(save_settings(json.loads(self.read_body() or b"{}")))
            elif path == "/api/logo":
                ext = Path(unquote(self.headers.get("X-Filename", "logo.png"))).suffix.lower() or ".png"
                if ext not in {".png", ".jpg", ".jpeg", ".webp"}:
                    raise ValueError("Use a PNG (ideally with a transparent background), JPG or WebP image.")
                STUDIO_DIR.mkdir(parents=True, exist_ok=True)
                for old in STUDIO_DIR.glob("logo.*"):
                    old.unlink()
                dest = STUDIO_DIR / f"logo{ext}"
                dest.write_bytes(self.read_body())
                try:
                    from PIL import Image
                    Image.open(dest).verify()
                except Exception:
                    dest.unlink()
                    raise ValueError("That file doesn't look like an image.")
                self.send_json(save_settings({"logo": str(dest)}))
            elif path == "/api/voice":
                name = Path(unquote(self.headers.get("X-Filename", "voice.m4a"))).name
                ext = Path(name).suffix.lower() or ".m4a"
                if ext not in {".m4a", ".mp3", ".wav", ".aac", ".flac", ".ogg", ".mp4", ".mov", ".webm"}:
                    raise ValueError("Use an audio file (m4a, mp3, wav, …).")
                STUDIO_DIR.mkdir(parents=True, exist_ok=True)
                for old in STUDIO_DIR.glob("voice-sample.*"):
                    old.unlink()
                dest = STUDIO_DIR / f"voice-sample{ext}"
                dest.write_bytes(self.read_body())
                self.send_json(save_settings({"voice_sample": str(dest), "voice_text": "", "voice_pick_used": ""}))
            elif path == "/api/voice-recording":
                ext = Path(unquote(self.headers.get("X-Filename", "voice.m4a"))).suffix.lower() or ".m4a"
                if ext not in {".m4a", ".mp3", ".wav", ".aac", ".flac", ".ogg", ".mp4", ".mov", ".webm"}:
                    raise ValueError("Use an audio file (m4a, mp3, wav, …).")
                STUDIO_DIR.mkdir(parents=True, exist_ok=True)
                for old in STUDIO_DIR.glob("voice-recording.*"):
                    old.unlink()
                dest = STUDIO_DIR / f"voice-recording{ext}"
                dest.write_bytes(self.read_body())
                self.send_json(start_voice_pick(dest))
            elif path == "/api/voice-pick/use":
                self.send_json(use_voice_pick(json.loads(self.read_body() or b"{}").get("id", "")))
            elif path == "/api/music":
                ext = Path(unquote(self.headers.get("X-Filename", "music.mp3"))).suffix.lower() or ".mp3"
                if ext not in MUSIC_EXTS:
                    raise ValueError("Use an audio file (mp3, m4a, wav, …).")
                STUDIO_DIR.mkdir(parents=True, exist_ok=True)
                for old in STUDIO_DIR.glob("music.*"):
                    old.unlink()
                dest = STUDIO_DIR / f"music{ext}"
                dest.write_bytes(self.read_body())
                self.send_json(save_settings({"music_file": str(dest)}))
            elif path == "/api/music/clear":
                for old in STUDIO_DIR.glob("music.*"):
                    old.unlink()
                self.send_json(save_settings({"music_file": ""}))
            elif path == "/api/plans":
                body = json.loads(self.read_body() or b"{}")
                self.send_json(create_plan(body.get("topic", ""), body.get("count", 6), body.get("audience", "")))
            elif m := re.match(r"^/api/plans/([^/]+)/make$", path):
                self.send_json(plan_make(m.group(1), json.loads(self.read_body() or b"{}").get("index", "all")))
            elif m := re.match(r"^/api/plans/([^/]+)/delete$", path):
                self.send_json(plan_delete(m.group(1)))
            else:
                self.send_error(404)
        except KeyError:
            self.send_json({"error": "Not found"}, 404)
        except (ValueError, OSError) as e:
            self.send_json({"error": str(e)}, 400)

    def send_file(self, path: Path):
        size = path.stat().st_size
        ctype = {".mp4": "video/mp4", ".mp3": "audio/mpeg", ".wav": "audio/wav", ".m4a": "audio/mp4",
                 ".aac": "audio/aac", ".flac": "audio/flac", ".ogg": "audio/ogg", ".pdf": "application/pdf",
                 ".md": "text/plain; charset=utf-8", ".srt": "text/plain; charset=utf-8", ".png": "image/png", ".jpg": "image/jpeg",
                 ".jpeg": "image/jpeg", ".webp": "image/webp", ".gif": "image/gif"}.get(path.suffix, "application/octet-stream")
        start, end = 0, size - 1
        if (rng := self.headers.get("Range")) and (m := re.match(r"bytes=(\d+)-(\d*)", rng)):
            start, end = int(m[1]), int(m[2]) if m[2] else size - 1
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        else:
            self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(end - start + 1))
        self.end_headers()
        with open(path, "rb") as f:
            f.seek(start)
            remaining = end - start + 1
            while remaining > 0:
                chunk = f.read(min(1 << 20, remaining))
                if not chunk:
                    break
                self.wfile.write(chunk)
                remaining -= len(chunk)


class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def main() -> None:
    parser = argparse.ArgumentParser(description="Manim Studio: questions in, narrated explainer videos out")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()

    HOME.mkdir(parents=True, exist_ok=True)
    recover_interrupted()
    threading.Thread(target=worker, args=(LIGHT,), daemon=True).start()
    threading.Thread(target=worker, args=(HEAVY,), daemon=True).start()
    server = Server(("127.0.0.1", args.port), Handler)
    url = f"http://localhost:{args.port}"
    print(f"Manim Studio running at {url}  (projects in {HOME}; Ctrl+C to stop)", flush=True)
    if not args.no_browser:
        threading.Timer(0.5, webbrowser.open, (url,)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
