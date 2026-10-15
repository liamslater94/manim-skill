#!/usr/bin/env python3
"""
YouTube subtitle track (subtitles.srt) for a narrated manim-skill video.

Joins the per-scene .srt files Manim writes for add_subcaption() (exact on-screen timing) into one
track, using the rendered scene lengths from concat.txt. Falls back to timestamps.json if the scene
files are missing. Long narration lines become short, readable captions (at most two lines of
about 42 characters), and captions clear the screen during a line's thinking pause.

    python studio/subtitles.py ~/.manim-skill/<video>        # writes <video>/subtitles.srt
"""

import json
import re
import subprocess
import sys
from pathlib import Path

LINE_CHARS = 42          # YouTube / broadcast guideline per caption line
CUE_CHARS = 2 * LINE_CHARS
MIN_CUE_S = 1.0
LINGER_S = 0.6           # how long a caption stays up into a pause


def srt_time(s: float) -> str:
    ms = max(0, round(s * 1000))
    return f"{ms // 3600000:02d}:{ms // 60000 % 60:02d}:{ms // 1000 % 60:02d},{ms % 1000:03d}"


def parse_time(t: str) -> float:
    h, m, rest = t.strip().replace(".", ",").split(":")
    sec, ms = rest.split(",")
    return int(h) * 3600 + int(m) * 60 + int(sec) + int(ms) / 1000


def parse_srt(text: str) -> list[tuple[float, float, str]]:
    cues = []
    for block in re.split(r"\n\s*\n", text.strip()):
        lines = block.strip().splitlines()
        timing = next((i for i, l in enumerate(lines) if "-->" in l), None)
        if timing is None:
            continue
        a, b = lines[timing].split("-->")
        body = " ".join(l.strip() for l in lines[timing + 1:] if l.strip())
        if body:
            cues.append((parse_time(a), parse_time(b), body))
    return cues


def duration(path: Path) -> float:
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0",
                          str(path)], capture_output=True, text=True).stdout
    return float(out.strip() or 0)


def scene_cues(d: Path) -> list[tuple[float, float, str]]:
    """Narration cues on the whole-video timeline, from the rendered scenes' .srt files."""
    concat = d / "concat.txt"
    if not concat.exists():
        return []
    cues, offset = [], 0.0
    for line in concat.read_text().splitlines():
        m = re.match(r"file '(.+)'", line.strip())
        if not m:
            continue
        scene = d / m.group(1)
        srt = scene.with_suffix(".srt")
        if srt.exists():
            cues += [(offset + a, offset + b, t) for a, b, t in parse_srt(srt.read_text())]
        offset += duration(scene)
    return cues


def timestamp_cues(d: Path) -> list[tuple[float, float, str]]:
    try:
        subs = json.loads((d / "timestamps.json").read_text())["subtitles"]
    except (OSError, ValueError, KeyError):
        return []
    return [(s["start_s"], s["end_s"], s["text"]) for s in subs]


def pauses(d: Path) -> list[float]:
    try:
        return [float(s.get("pause_s", 0)) for s in json.loads((d / "timestamps.json").read_text())["subtitles"]]
    except (OSError, ValueError, KeyError):
        return []


def chunks(text: str) -> list[str]:
    """Split a narration line into caption-sized pieces at sentence, then clause, then word breaks."""
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= CUE_CHARS:
        return [text]
    for pattern in (r"(?<=[.?!])\s+", r"(?<=[,;:])\s+", r"\s+"):
        parts = [p for p in re.split(pattern, text) if p]
        if len(parts) > 1:
            out, cur = [], ""
            for p in parts:
                trial = f"{cur} {p}".strip()
                if len(trial) <= CUE_CHARS or not cur:
                    cur = trial
                else:
                    out.append(cur)
                    cur = p
            out.append(cur)
            if all(len(c) <= CUE_CHARS for c in out):
                return out
            return [piece for c in out for piece in (chunks(c) if len(c) > CUE_CHARS else [c])]
    return [text]


def two_lines(text: str) -> str:
    """Break a caption into at most two balanced lines."""
    if len(text) <= LINE_CHARS:
        return text
    words, best = text.split(), None
    for i in range(1, len(words)):
        a, b = " ".join(words[:i]), " ".join(words[i:])
        score = max(len(a), len(b))
        if best is None or score < best[0]:
            best = (score, f"{a}\n{b}")
    return best[1] if best else text


def build(d: Path) -> list[tuple[float, float, str]]:
    cues = scene_cues(d) or timestamp_cues(d)
    gaps = pauses(d)
    out = []
    for i, (start, end, text) in enumerate(cues):
        if len(gaps) == len(cues) and gaps[i] > LINGER_S:
            end = max(start + MIN_CUE_S, end - gaps[i] + LINGER_S)   # clear the screen during the pause
        pieces = chunks(text)
        total = sum(len(p) for p in pieces) or 1
        t = start
        for p in pieces:
            span = (end - start) * len(p) / total
            out.append((t, t + span, two_lines(p)))
            t += span
    return out


def write(d: Path, out: Path | None = None) -> Path:
    d = Path(d).expanduser()
    cues = build(d)
    cues = [(a, min(b, cues[i + 1][0]) if i + 1 < len(cues) else b, t) for i, (a, b, t) in enumerate(cues)]
    if not cues:
        raise ValueError("No narration timings found (concat.txt + scene .srt files, or timestamps.json).")
    out = out or d / "subtitles.srt"
    out.write_text("\n".join(f"{i}\n{srt_time(a)} --> {srt_time(b - 0.001)}\n{t}\n"
                             for i, (a, b, t) in enumerate(cues, 1)))
    return out


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    print(write(Path(sys.argv[1])))
