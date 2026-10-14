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
MAX_UPLOAD = 40 * 1024 * 1024

DEFAULT_SETTINGS = {
    "audience": "IB Mathematics: Analysis and Approaches HL students",
    "minutes": 4,
    "voice_sample": "",
    "say": "z=zed",
    "speed": 1.1,
    "pause": 1.0,
    "guidance": 1.5,
    "claude": "claude",
    "model": "",
}

# Status values. "queued_*" statuses are picked up by a worker; the rest are waiting on the user or done.
LIGHT = {"queued_script": "scripting"}
HEAVY = {"queued_voice": "voicing", "queued_video": "rendering",
         "queued_revise": "revising", "queued_export": "exporting"}
RUNNING = set(LIGHT.values()) | set(HEAVY.values())
SERVED_FILES = {"video.mp4", "video_1080p.mp4", "voiceover.mp3", "question.png", "question.jpg",
                "question.jpeg", "question.webp", "question.gif"}

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
            current[key] = type(default)(values[key]) if not isinstance(default, str) else str(values[key])
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


def create_job(question: str, audience: str, minutes: float, image: dict | None) -> dict:
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


def run_process(job_id: str, cmd: list[str], *, claude: bool) -> tuple[int, dict]:
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
            if event.get("session_id"):
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


def voice_command() -> list[str]:
    s = load_settings()
    cmd = [sys.executable, str(TOOLS_DIR / "tts-generate.py"), "--narration", "narration.txt",
           "--pause", f"{s['pause']:g}"]
    sample = s.get("voice_sample")
    if sample and Path(sample).exists():
        cmd += ["--clone", sample, "--qa", "--speed", f"{s['speed']:g}", "--clone-guidance", f"{s['guidance']:g}"]
        for item in re.split(r"[,\s]+", s.get("say", "").strip()):
            if "=" in item:
                cmd += ["--say", item]
    return cmd


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
        prompt = skill_preamble(job) + f"""
This step is Phase 1 (Narration) only. The question or topic is in question.md:

<question>
{job['question']}
</question>
{image}
Write narration.txt for a video of about {job['minutes']:g} minutes, in the skill's SubtitleSpec format:
- Teach it the way an excellent teacher would at this level: motivate, explain each step, connect to the
  geometry or intuition, and finish with a short summary. Be mathematically exact.
- End a line with [pause 2.5] after every worked step or key result so viewers can take it in; the
  narrator adds a 1 s breath after other lines automatically.
- The narration is read by a cloned human voice: write maths as it should be spoken
  ("x squared", "a minus nine", "the integral from zero to one of …"), never symbols or LaTeX.
- Keep lines to one or two sentences each.

Also write outline.md: the scene-by-scene plan (Scene class names and what appears on screen, matching the
narration lines) followed by a complete worked solution, so the teacher can check the mathematics.

Do not generate audio, write animation code or render. Stop once narration.txt and outline.md are written.
"""
        session = ""
    code, _ = run_process(job["id"], claude_cmd(prompt, session), claude=True)
    if code or not (job_dir(job["id"]) / "narration.txt").exists():
        raise RuntimeError("Script step did not produce narration.txt")
    update_job(job["id"], status="script_ready", script_feedback="")


def step_voice(job: dict) -> None:
    for name in ("voiceover.mp3", "timestamps.json"):
        (job_dir(job["id"]) / name).unlink(missing_ok=True)
    code, _ = run_process(job["id"], voice_command(), claude=False)
    if code or not (job_dir(job["id"]) / "timestamps.json").exists():
        raise RuntimeError("Voice step failed (see log)")
    update_job(job["id"], status="queued_video")


def step_video(job: dict) -> None:
    prompt = skill_preamble(job) + """
narration.txt has been approved and voiced: voiceover.mp3 and timestamps.json are in the current directory,
and outline.md has the scene plan. Do NOT run tts-generate.py and do not edit narration.txt.
Now do Phases 3–5 of the skill: write script.py timed to timestamps.json (each duration_s already includes
that line's pause), lint it, render every scene, write concat.txt, and mux video.mp4 with voiceover.mp3.
Then check it: extract frames at several points in every scene and look at them; fix overlapping or
clipped text, unreadable labels and anything mathematically wrong, re-render those scenes and re-mux.
Finish when video.mp4 is complete and verified with ffprobe.
"""
    if job.get("retime") and (job_dir(job["id"]) / "script.py").exists():
        prompt = """The narration was re-recorded with the same wording: voiceover.mp3 and timestamps.json are new.
Do NOT run tts-generate.py or edit narration.txt. Update script.py so every segment's duration matches the new
timestamps.json (keep the visuals), lint it, re-render every scene, rewrite concat.txt if needed and re-mux
video.mp4 with the new voiceover.mp3. Verify it with ffprobe and check a few frames. Do not launch the viewer.
"""
    (job_dir(job["id"]) / "video.mp4").unlink(missing_ok=True)
    code, _ = run_process(job["id"], claude_cmd(prompt, job.get("session", "")), claude=True)
    if code or not (job_dir(job["id"]) / "video.mp4").exists():
        raise RuntimeError("Video step did not produce video.mp4")
    update_job(job["id"], retime=False)
    update_job(job["id"], status="review")


def step_revise(job: dict) -> None:
    voice = " ".join(f'"{c}"' if " " in c else c for c in voice_command())
    prompt = f"""The teacher watched video.mp4 and gives this feedback (timestamps refer to video.mp4):

{job.get('video_feedback', '')}

Apply it, following the skill's Phase 6 iteration rules: for a referenced moment, extract that frame and
look at it before changing code. If narration must change, edit narration.txt, then regenerate the voice
with exactly this command (unchanged lines are cached):
{voice}
and update script.py to the new timestamps.json. Re-render only the affected scenes, update concat.txt if
needed, re-mux video.mp4, and verify it with ffprobe. Do not launch the viewer.
"""
    code, _ = run_process(job["id"], claude_cmd(prompt, job.get("session", "")), claude=True)
    if code or not (job_dir(job["id"]) / "video.mp4").exists():
        raise RuntimeError("Revision did not produce video.mp4")
    update_job(job["id"], status="review", video_feedback="")


def step_export(job: dict) -> None:
    d = job_dir(job["id"])
    (d / "video_1080p.mp4").unlink(missing_ok=True)
    prompt = """The teacher approved video.mp4. Make the final YouTube version:
1. Render every scene in script.py at 1920x1080 and 30 fps (set config.pixel_width/pixel_height/frame_rate
   accordingly, keeping frame_width/frame_height and all timing unchanged), concatenate in the same order and
   mux with voiceover.mp3 into video_1080p.mp4. Check its duration matches video.mp4 within 0.5 s and look at
   two or three frames. Then restore script.py's original render settings.
2. Write youtube.md with: a clear, searchable title (max 70 characters) on the first line, a blank line, then
   a 2–4 sentence description for students (what the question is, which methods are used, the level), then a
   line of 5–10 comma-separated tags. Do not write chapter timestamps — they are added automatically.
Do not change the narration or voice.
"""
    code, _ = run_process(job["id"], claude_cmd(prompt, job.get("session", "")), claude=True)
    if code or not (d / "video_1080p.mp4").exists():
        raise RuntimeError("Export did not produce video_1080p.mp4")
    chapters = chapter_lines(d)
    text = (d / "youtube.md").read_text().rstrip() if (d / "youtube.md").exists() else job["title"]
    if chapters:
        text += "\n\nChapters\n" + "\n".join(chapters)
    (d / "youtube.md").write_text(text + "\n")
    update_job(job["id"], status="done")


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


STEPS = {"scripting": step_script, "voicing": step_voice, "rendering": step_video,
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
            return update_job(job_id, status="queued_voice", queued_at=time.time())
        if action == "rescript":
            need("script_ready")
            return update_job(job_id, status="queued_script", script_feedback=body.get("notes", ""),
                              queued_at=time.time())
        if action == "revoice":
            need("review", "done")
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
    files = {name: int((d / name).stat().st_mtime) for name in SERVED_FILES if (d / name).exists()}
    return {**job, "narration": read("narration.txt"), "outline": read("outline.md"),
            "youtube": read("youtube.md"), "log": tail(d / "studio.log"), "files": files, "folder": str(d)}


def health() -> dict:
    s = load_settings()
    checks = {
        "claude": bool(shutil.which(s["claude"] or "claude")),
        "ffmpeg": bool(shutil.which("ffmpeg")),
        "latex": bool(shutil.which("latex")),
        "manim": subprocess.run([sys.executable, "-c", "import manim"], capture_output=True).returncode == 0,
        "voice_sample": bool(s.get("voice_sample") and Path(s["voice_sample"]).exists()),
        "sherpa_onnx": subprocess.run([sys.executable, "-c", "import sherpa_onnx"], capture_output=True).returncode == 0,
    }
    return {"checks": checks, "home": str(HOME), "python": sys.executable}


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
            elif path == "/api/health":
                self.send_json(health())
            elif m := re.match(r"^/files/([^/]+)/([^/]+)$", path):
                if m.group(2) not in SERVED_FILES:
                    raise KeyError(path)
                self.send_file(job_dir(m.group(1)) / m.group(2))
            else:
                self.send_error(404)
        except KeyError:
            self.send_error(404)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_POST(self):
        path = unquote(urlparse(self.path).path)
        try:
            if path == "/api/jobs":
                body = json.loads(self.read_body() or b"{}")
                self.send_json(create_job(body.get("question", ""), body.get("audience", ""),
                                          body.get("minutes", 0), body.get("image")))
            elif m := re.match(r"^/api/jobs/([^/]+)/(\w+)$", path):
                self.send_json(act(m.group(1), m.group(2), json.loads(self.read_body() or b"{}")))
            elif path == "/api/settings":
                self.send_json(save_settings(json.loads(self.read_body() or b"{}")))
            elif path == "/api/voice":
                name = Path(self.headers.get("X-Filename", "voice.m4a")).name
                ext = Path(name).suffix.lower() or ".m4a"
                if ext not in {".m4a", ".mp3", ".wav", ".aac", ".flac", ".ogg", ".mp4", ".mov", ".webm"}:
                    raise ValueError("Use an audio file (m4a, mp3, wav, …).")
                STUDIO_DIR.mkdir(parents=True, exist_ok=True)
                for old in STUDIO_DIR.glob("voice-sample.*"):
                    old.unlink()
                dest = STUDIO_DIR / f"voice-sample{ext}"
                dest.write_bytes(self.read_body())
                self.send_json(save_settings({"voice_sample": str(dest)}))
            else:
                self.send_error(404)
        except KeyError:
            self.send_json({"error": "Not found"}, 404)
        except (ValueError, OSError) as e:
            self.send_json({"error": str(e)}, 400)

    def send_file(self, path: Path):
        size = path.stat().st_size
        ctype = {".mp4": "video/mp4", ".mp3": "audio/mpeg", ".png": "image/png", ".jpg": "image/jpeg",
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
