#!/usr/bin/env python3
"""
The teacher's own narration: one take read from the teleprompter, split into script lines.

While recording, the teacher taps Next as each new line starts ("marks") and Redo to restart a line
("cuts" remove the fluffed attempt). This module cleans the take (rumble cut, noise reduction, even
level), mutes the key clicks, snaps every mark to where that line's speech really begins, checks each
line against the script with Whisper, and writes what the rest of the pipeline expects:

    takes/line-NNN.wav     each line, with the pause after it (padded to any [pause N] in the script)
    takes/lines.json       per-line timing, the subtitle text, what Whisper heard, and flags
    voiceover.mp3          all lines joined
    timestamps.json        the same timing format tts-generate.py writes

    python studio/recording.py <video folder> take.webm --marks 0,4.1,9.8 [--cuts 12.0:15.2]
"""

import argparse
import importlib.util
import json
import re
import subprocess
from pathlib import Path

import numpy as np

SR = 48000
HOP = 480                      # 10 ms frames
CLEAN = "highpass=f=80,afftdn=nf=-28:tn=1,lowpass=f=15000"
TARGET_DB = -19.0              # speech level after levelling (dBFS RMS of voiced frames)
LEAD_S = 0.15                  # kept before each line's first word
TOOLS = Path(__file__).resolve().parent.parent / "skills" / "manim-skill" / "tools"
_tts = None


def tts():
    """tts-generate.py as a module (parse_entries, Transcriber, accuracy)."""
    global _tts
    if _tts is None:
        spec = importlib.util.spec_from_file_location("tts_generate", TOOLS / "tts-generate.py")
        _tts = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(_tts)
    return _tts


# ------------------------------------------------------------------ audio --
def decode(path, clean: bool = True) -> np.ndarray:
    args = ["ffmpeg", "-v", "error", "-i", str(path), "-ac", "1", "-ar", str(SR)]
    if clean:
        args += ["-af", CLEAN]
    pcm = subprocess.run(args + ["-f", "f32le", "-"], capture_output=True, check=True).stdout
    return np.frombuffer(pcm, np.float32).copy()


def levels(audio: np.ndarray) -> np.ndarray:
    n = max(1, len(audio) // HOP)
    frames = audio[:n * HOP].reshape(n, HOP) if len(audio) >= HOP else np.zeros((1, HOP), np.float32)
    return 20 * np.log10(np.sqrt(np.mean(frames ** 2, axis=1)) + 1e-9)


def speech_mask(db: np.ndarray) -> np.ndarray:
    """Voiced frames: above a threshold between the room's noise floor and the speech level,
    with short gaps (inside words) filled and isolated blips (clicks, bumps) dropped."""
    floor, loud = np.percentile(db, 10), np.percentile(db, 95)
    on = db > max(floor + 0.35 * (loud - floor), loud - 32)
    out = on.copy()
    # fill gaps under 120 ms
    i = 0
    while i < len(out):
        if not out[i]:
            j = i
            while j < len(out) and not out[j]:
                j += 1
            if 0 < i and j < len(out) and j - i < 12:
                out[i:j] = True
            i = j
        else:
            i += 1
    # drop runs under 80 ms
    i = 0
    while i < len(out):
        if out[i]:
            j = i
            while j < len(out) and out[j]:
                j += 1
            if j - i < 8:
                out[i:j] = False
            i = j
        else:
            i += 1
    return out


def runs(mask: np.ndarray) -> list[tuple[float, float]]:
    """(start_s, end_s) of each voiced run."""
    out, i = [], 0
    while i < len(mask):
        if mask[i]:
            j = i
            while j < len(mask) and mask[j]:
                j += 1
            out.append((i * HOP / SR, j * HOP / SR))
            i = j
        else:
            i += 1
    return out


def level(audio: np.ndarray, mask: np.ndarray | None = None) -> np.ndarray:
    """Bring voiced frames to TARGET_DB, with a soft limit on peaks."""
    db = levels(audio)
    mask = speech_mask(db) if mask is None else mask
    voiced = db[mask[:len(db)]] if mask.any() else db
    gain = 10 ** ((TARGET_DB - float(np.mean(voiced))) / 20) if len(voiced) else 1.0
    out = audio * min(gain, 30.0)
    return (np.tanh(out * 1.1) / np.tanh(1.1)).astype(np.float32)


def room_tone(audio: np.ndarray, seconds: float) -> np.ndarray:
    """The quietest 300 ms of the take, looped, to fill pauses without dead silence."""
    n = int(seconds * SR)
    if n <= 0:
        return np.zeros(0, np.float32)
    db = levels(audio)
    win = 30
    if len(db) <= win:
        return np.zeros(n, np.float32)
    k = int(np.argmin(np.convolve(db, np.ones(win), mode="valid")))
    piece = audio[k * HOP:(k + win) * HOP].copy()
    if not len(piece):
        return np.zeros(n, np.float32)
    reps = int(np.ceil(n / len(piece))) + 1
    loop = np.concatenate([piece[::(-1) ** r] for r in range(reps)])   # mirror to avoid seams
    return loop[:n].astype(np.float32)


def fade(audio: np.ndarray, ms: float = 6) -> np.ndarray:
    n = min(len(audio) // 2, int(SR * ms / 1000))
    if n:
        audio = audio.copy()
        audio[:n] *= np.linspace(0, 1, n, dtype=np.float32)
        audio[-n:] *= np.linspace(1, 0, n, dtype=np.float32)
    return audio


def write_wav(path: Path, audio: np.ndarray) -> None:
    import soundfile as sf
    sf.write(path, audio.astype(np.float32), SR)


def read_wav(path: Path) -> np.ndarray:
    import soundfile as sf
    audio, _ = sf.read(path, dtype="float32")
    return audio if audio.ndim == 1 else audio.mean(axis=1)


# --------------------------------------------------------------- the take --
def remove_cuts(audio: np.ndarray, cuts, marks):
    """Drop the redone attempts; move the marks onto the shortened timeline."""
    cuts = sorted((max(0.0, a), max(a, b)) for a, b in cuts if b > a)
    keep, t = [], 0.0
    for a, b in cuts:
        if a > t:
            keep.append((t, a))
        t = max(t, b)
    keep.append((t, len(audio) / SR))
    out = np.concatenate([audio[int(a * SR):int(b * SR)] for a, b in keep if b > a] or [audio[:0]])

    def shift(x):
        removed = 0.0
        for a, b in cuts:
            if x >= b:
                removed += b - a
            elif x > a:
                removed += x - a
        return x - removed
    return out, [shift(m) for m in marks]


def mute_clicks(audio: np.ndarray, presses) -> np.ndarray:
    """Soften the key or trackpad click at each press, unless someone is talking over it."""
    audio = audio.copy()
    db = levels(audio)
    mask = speech_mask(db)
    for t in presses:
        a, b = max(0, int((t - 0.12) / (HOP / SR))), min(len(db), int((t + 0.2) / (HOP / SR)))
        if b <= a or mask[a:b].any():
            continue          # someone is speaking there: leave it
        i, j = a * HOP, b * HOP
        floor = room_tone(audio, (j - i) / SR)
        ramp = np.ones(j - i, np.float32)
        k = min(len(ramp) // 2, int(0.01 * SR))
        if k:
            ramp[:k] = np.linspace(1, 0, k)
            ramp[-k:] = np.linspace(0, 1, k)
            ramp[k:-k] = 0
        audio[i:j] = audio[i:j] * ramp + floor[:j - i] * (1 - ramp)
    return audio


def soft_start(db: np.ndarray, t: float, back: float = 0.4) -> float:
    """Walk back from a detected onset while the sound is still above the room's noise floor, so soft
    starts ("h", "f", "s") aren't clipped."""
    floor = np.percentile(db, 10)
    i = int(t * SR / HOP)
    lo = max(0, i - int(back * SR / HOP))
    while i > lo and db[i - 1] > floor + 8:
        i -= 1
    return i * HOP / SR


def soft_end(db: np.ndarray, t: float, ahead: float = 0.4) -> float:
    floor = np.percentile(db, 10)
    i = min(len(db), int(t * SR / HOP))
    hi = min(len(db), i + int(ahead * SR / HOP))
    while i < hi and db[i] > floor + 8:
        i += 1
    return i * HOP / SR


def boundaries(mask: np.ndarray, marks: list[float], total: float, db: np.ndarray | None = None) -> list[float]:
    """Where each line starts: the speech onset nearest the moment Next was pressed."""
    voiced = [r for r in runs(mask) if r[1] - r[0] >= 0.12]       # ignore breaths and clicks
    gaps = {a: (a - voiced[k - 1][1]) if k else 9.0 for k, (a, _) in enumerate(voiced)}   # the first always counts
    onsets = [a for a in gaps if gaps[a] >= 0.15]
    if not onsets:
        return [m for m in marks] + [total]
    soft = (lambda o: soft_start(db, o)) if db is not None else (lambda o: o)
    starts = [max(0.0, soft(onsets[0]) - LEAD_S)]
    for m in marks[1:]:
        # Next is usually pressed in the pause before a line or just as it starts, but can be a couple of
        # seconds late; a real line break is also a longer pause than a comma inside a line.
        near = [o for o in onsets if m - 3.5 <= o <= m + 1.5 and soft(o) - LEAD_S > starts[-1] + 0.3]
        score = lambda o: abs(o - (m + 0.1)) - 1.2 * min(gaps[o], 1.5)   # noqa: E731
        o = soft(min(near, key=score)) if near else m
        starts.append(max(starts[-1] + 0.3, o - LEAD_S))
    last_end = voiced[-1][1] if voiced else total
    return starts + [min(total, max(starts[-1] + 0.5, last_end + 0.6))]


def auto_marks(mask: np.ndarray, entries) -> list[float]:
    """No Next taps (an uploaded file): guess where each line starts from how long its text is, then
    snap each guess to the nearest clear pause."""
    voiced = runs(mask)
    if not voiced:
        return [0.0] * len(entries)
    first, last = voiced[0][0], voiced[-1][1]
    words = [max(1, len(t.split())) for t, _ in entries]
    total, acc, guesses = sum(words), 0, []
    for w in words:
        guesses.append(first + (last - first) * acc / total)
        acc += w
    gaps = [(a, a - voiced[k - 1][1]) for k, (a, _) in enumerate(voiced) if k and a - voiced[k - 1][1] >= 0.3]
    marks = [first]
    for g in guesses[1:]:
        options = [a for a, gap in gaps if a > marks[-1] + 0.5]
        marks.append(min(options, key=lambda a: abs(a - g) - 0.5 * min(1.0, dict(gaps)[a])) if options else g)
    return marks


def speech_end(mask: np.ndarray, a: float, b: float) -> float:
    i, j = int(a * SR / HOP), int(b * SR / HOP)
    idx = np.where(mask[i:j])[0]
    return min(b, a + (idx[-1] + 1) * HOP / SR + 0.06) if len(idx) else a


# ---------------------------------------------------------------- outputs --
def check_line(entry: dict, audio: np.ndarray, transcriber) -> None:
    lo = int(LEAD_S * SR * 0.5)
    hi = int((entry["speech_end_s"] - entry["start_s"]) * SR)
    heard = transcriber(audio[lo:max(lo + 1, hi)], SR) if hi - lo > SR * 0.3 else ""
    acc = tts().accuracy(entry["text"], heard) if heard else 0.0
    entry["heard"], entry["accuracy"] = heard, round(acc, 3)
    entry["flag"] = ("No speech found: check where this line starts." if not heard else
                     "Sounds different from the script: listen, then keep it, use what you said, or re-record."
                     if acc < 0.6 else "")


def build(d: Path, lines: list[dict]) -> None:
    """voiceover.mp3, timestamps.json and narration.txt from the line files."""
    takes = d / "takes"
    parts, subs, t = [], [], 0.0
    for e in lines:
        audio = read_wav(takes / e["file"])
        dur = len(audio) / SR
        parts.append(audio)
        speech = e["speech_end_s"] - e["start_s"]
        subs.append({"index": e["index"], "text": e["subtitle"], "start_s": round(t, 3), "end_s": round(t + dur, 3),
                     "duration_s": round(dur, 3), "pause_s": round(max(0.0, dur - speech), 3)})
        t += dur
    full = np.concatenate(parts) if parts else np.zeros(0, np.float32)
    wav = takes / "voiceover.wav"
    write_wav(wav, full)
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(wav), "-c:a", "libmp3lame", "-b:a", "192k",
                    str(d / "voiceover.mp3")], check=True)
    wav.unlink()
    (d / "timestamps.json").write_text(json.dumps({"total_duration_s": round(t, 3), "subtitles": subs}, indent=2))
    narration = "subtitles:\n" + "".join(
        f"- {e['subtitle']}" + (f" [pause {e['hint']:g}]" if e.get("hint") else "") + "\n" for e in lines)
    (d / "narration.txt").write_text(narration)
    (takes / "lines.json").write_text(json.dumps(lines, indent=2))


def line_audio(clean: np.ndarray, a: float, b: float, end: float, hint) -> np.ndarray:
    audio = clean[int(a * SR):int(b * SR)]
    pause = b - end
    if hint and pause < hint:
        audio = np.concatenate([audio, room_tone(clean, hint - pause)])
    return fade(audio)


def process(d: Path, take: Path, marks: list[float], cuts=(), transcriber=None) -> list[dict]:
    """Split the whole take into lines. Returns lines.json's content."""
    d = Path(d)
    takes = d / "takes"
    takes.mkdir(exist_ok=True)
    entries = tts().parse_entries(str(d / "narration.txt"))
    raw = decode(take)
    if len(raw) < SR:
        raise ValueError("The recording is less than a second long.")
    raw = mute_clicks(raw, [m for m in marks[1:]] + [b for _, b in cuts])
    audio, marks = remove_cuts(raw, cuts, marks)
    mask = speech_mask(levels(audio))
    if not mask.any():
        raise ValueError("No speech found in the recording. Check the microphone and try again.")
    if len(marks) < 2 and len(entries) > 1:
        marks = auto_marks(mask, entries)
    marks = (list(marks) + [marks[-1] + 1.0] * len(entries))[:len(entries)]
    audio = level(audio, mask)
    total = len(audio) / SR
    db = levels(audio)
    starts = boundaries(mask, marks, total, db)
    transcriber = transcriber or tts().Transcriber()
    for old in takes.glob("line-*.wav"):
        old.unlink()
    lines = []
    for i, (text, hint) in enumerate(entries):
        a, b = starts[i], starts[i + 1]
        end = min(b, soft_end(db, speech_end(mask, a, b)))
        e = {"index": i, "text": text, "subtitle": text, "hint": hint, "start_s": round(a, 3),
             "speech_end_s": round(end, 3), "end_s": round(b, 3), "file": f"line-{i + 1:03d}.wav", "retake": False}
        clip = line_audio(audio, a, b, end, hint)
        write_wav(takes / e["file"], clip)
        lines.append(e)
    for e in lines:   # transcribe from each line's own audio
        clip = read_wav(takes / e["file"])
        local = {**e, "start_s": 0.0, "speech_end_s": e["speech_end_s"] - e["start_s"]}
        check_line(local, clip, transcriber)
        e.update(heard=local["heard"], accuracy=local["accuracy"], flag=local["flag"])
    write_wav(takes / "room.wav", room_tone(audio, 2.0))
    build(d, lines)
    return lines


def retake(d: Path, index: int, take: Path, transcriber=None) -> dict:
    """Replace one line with a new recording of it, keeping the pause that followed it."""
    d = Path(d)
    takes = d / "takes"
    lines = json.loads((takes / "lines.json").read_text())
    e = lines[index]
    audio = decode(take)
    mask = speech_mask(levels(audio))
    voiced = runs(mask)
    if not voiced:
        raise ValueError("No speech found in that recording. Check the microphone and try again.")
    a, b = max(0.0, voiced[0][0] - LEAD_S), min(len(audio) / SR, voiced[-1][1] + 0.06)
    speech = level(audio[int(a * SR):int(b * SR)], mask[int(a * SR / HOP):int(b * SR / HOP)])
    old_pause = max(0.25, e["end_s"] - e["speech_end_s"])
    room = read_wav(takes / "room.wav") if (takes / "room.wav").exists() else np.zeros(SR, np.float32)
    tail = np.resize(room, int(old_pause * SR)) if len(room) else np.zeros(int(old_pause * SR), np.float32)
    clip = fade(np.concatenate([speech, tail]))
    write_wav(takes / e["file"], clip)
    e.update(retake=True, start_s=0.0, speech_end_s=round(len(speech) / SR, 3), end_s=round(len(clip) / SR, 3))
    local = {**e}
    check_line(local, clip, transcriber or tts().Transcriber())
    e.update(heard=local["heard"], accuracy=local["accuracy"], flag=local["flag"])
    build(d, lines)
    return e


def set_subtitle(d: Path, index: int, text: str) -> dict:
    d = Path(d)
    lines = json.loads((d / "takes" / "lines.json").read_text())
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        raise ValueError("A line's subtitle can't be empty.")
    lines[index]["subtitle"] = text
    build(d, lines)
    return lines[index]


def clean_clip(src: Path, out: Path) -> float:
    """A single cleaned, trimmed, levelled clip (the end-screen sign-off). Returns its length."""
    audio = decode(src)
    mask = speech_mask(levels(audio))
    voiced = runs(mask)
    if not voiced:
        raise ValueError("No speech found in that recording.")
    a, b = max(0.0, voiced[0][0] - 0.1), min(len(audio) / SR, voiced[-1][1] + 0.15)
    clip = fade(level(audio[int(a * SR):int(b * SR)], mask[int(a * SR / HOP):int(b * SR / HOP)]))
    write_wav(out, clip)
    return len(clip) / SR


def main() -> None:
    p = argparse.ArgumentParser(description="Split a one-take narration recording into script lines")
    p.add_argument("folder")
    p.add_argument("take")
    p.add_argument("--marks", required=True, help="Comma-separated times (s) when each line was started")
    p.add_argument("--cuts", default="", help="Comma-separated start:end ranges (s) to remove")
    a = p.parse_args()
    marks = [float(x) for x in a.marks.split(",") if x.strip()]
    cuts = [tuple(map(float, c.split(":"))) for c in a.cuts.split(",") if c.strip()]
    for e in process(Path(a.folder), Path(a.take), marks, cuts):
        print(f"{e['index'] + 1:>3}  {e['start_s']:7.2f}-{e['end_s']:7.2f}s  {e['accuracy']:.0%}  {e['flag'] or 'ok'}")


if __name__ == "__main__":
    main()
