#!/usr/bin/env python3
"""
TTS generator for the Manim voiceover pipeline (local Kokoro, or a local clone of your own voice).

Reads the SubtitleSpec from narration.txt, synthesizes one MP3 per subtitle,
measures exact durations with ffprobe, concatenates them into voiceover.mp3,
and writes per-subtitle timings to timestamps.json.

    python tts-generate.py --narration narration.txt            # Kokoro, default voice af_heart
    python tts-generate.py --narration narration.txt --voice am_adam
    python tts-generate.py --clone my-voice.m4a --qa            # narrate in a cloned voice, check every take
    python tts-generate.py --narration narration.txt --bust 2   # drop one cached clip, then exit

A narration line may end with a pause hint, e.g. `- That gives minus one. [pause 2.5]`:
that much silence follows the line (default `--pause`, 0) and counts toward its
duration_s, giving viewers time to take in the maths. The hint is not part of the subtitle.

Voice cloning (`--clone`) runs ZipVoice zero-shot TTS locally through sherpa-onnx.
Give it 5-30 s of clean, expressive speech; the clone copies the sample's voice,
tone and pace. The sample is trimmed to ~12 s at a pause and transcribed
automatically (or pass `--clone-text`). Models are downloaded from GitHub on first
use into ~/.cache/manim-skill/models (override with MANIM_SKILL_MODELS).

Per-line audio is cached in .tts-cache/, so re-runs only regenerate changed
lines; models load only when there's something to generate. Requires ffmpeg/ffprobe and:
    uv add "kokoro>=0.9.4" soundfile        # Kokoro
    uv add sherpa-onnx soundfile            # --clone / --qa
"""

import argparse
import difflib
import hashlib
import json
import os
import re
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request
from pathlib import Path

DEFAULT_VOICE = "af_heart"
KOKORO_MODEL_ID = "hexgrad/Kokoro-82M"
KOKORO_LANG_CODES = {"a", "b", "e", "f", "h", "i", "j", "p", "z"}
CACHE_DIR = Path(".tts-cache")
_PIPELINES = {}

SHERPA_RELEASES = "https://github.com/k2-fsa/sherpa-onnx/releases/download"
ZIPVOICE_MODEL = "sherpa-onnx-zipvoice-distill-zh-en-emilia"
WHISPER_MODEL = "sherpa-onnx-whisper-base.en"
MODELS_DIR = Path(os.environ.get("MANIM_SKILL_MODELS", Path.home() / ".cache" / "manim-skill" / "models"))
CLONE_SR = 24000
QA_MIN_ACCURACY = 0.9

PAUSE_HINT = re.compile(r"\s*\[pause[\s=:]*([0-9]*\.?[0-9]+)\s*s?\]\s*$", re.IGNORECASE)


def cache_path(text: str, voice_id: str, cache_dir: Path = CACHE_DIR, extra: dict | None = None) -> Path:
    obj = {"model_id": KOKORO_MODEL_ID, "text": text, "voice_id": voice_id,
           "speed": os.environ.get("KOKORO_SPEED", "1")}
    if extra:
        obj.update(extra)
    key = hashlib.sha256(json.dumps(obj, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return cache_dir / f"{key}.mp3"


def parse_entries(narration_path: str) -> list[tuple[str, float | None]]:
    """
    Parse the SubtitleSpec from narration.txt: lines prefixed with `- ` under a
    `subtitles:` heading (inside or outside a fenced code block). Returns
    (subtitle text, pause hint in seconds or None) per line.
    """
    text = Path(narration_path).read_text()
    stripped = re.sub(r"```[^\n]*\n", "", text).replace("```", "")
    match = re.search(r"^subtitles:\s*\n(.*?)(?=\n[^\s\-\n]|\Z)", stripped, re.MULTILINE | re.DOTALL)
    if not match:
        sys.exit(f"Error: No 'subtitles:' block found in {narration_path}")
    entries = []
    for line in match.group(1).splitlines():
        if not re.match(r"^[ \t]*-", line):
            continue
        item = re.sub(r"^[ \t]*-\s*", "", line).strip()
        hint = PAUSE_HINT.search(item)
        entries.append((PAUSE_HINT.sub("", item).strip(), float(hint.group(1)) if hint else None))
    if not entries:
        sys.exit("Error: Empty subtitles list in narration.txt")
    return entries


def parse_subtitles(narration_path: str) -> list[str]:
    return [text for text, _ in parse_entries(narration_path)]


# ---------------------------------------------------------------- audio io ---
def decode(path, sr: int):
    """Any audio file -> mono float32 at `sr` (via ffmpeg)."""
    import numpy as np

    pcm = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-ac", "1", "-ar", str(sr),
                          "-f", "f32le", "-"], capture_output=True, check=True).stdout
    return np.frombuffer(pcm, np.float32).copy()


def frame_db(audio, sr: int, win: float = 0.02):
    import numpy as np

    hop = int(sr * win / 2)
    return np.array([20 * np.log10(np.sqrt(np.mean(audio[i:i + 2 * hop] ** 2)) + 1e-9)
                     for i in range(0, max(1, len(audio) - 2 * hop), hop)]), hop


def tidy(audio, sr: int, lead: float = 0.15):
    """Trim silence/breath noise at both ends, fade the edges, even out loudness."""
    import numpy as np

    db, hop = frame_db(audio, sr)
    loud = np.where(db > db.max() - 40)[0]
    if len(loud):
        audio = audio[max(0, (loud[0] - 8) * hop):min(len(audio), (loud[-1] + 14) * hop)].copy()
    fade = min(len(audio) // 2, int(0.01 * sr))
    if fade:
        audio[:fade] *= np.linspace(0, 1, fade)
        audio[-fade:] *= np.linspace(1, 0, fade)
    audio *= 0.89 / max(1e-6, float(np.abs(audio).max()))
    return np.concatenate([np.zeros(int(lead * sr), np.float32), audio.astype(np.float32)])


def write_mp3(audio_chunks, out_path: Path, sr: int = 24000) -> None:
    import numpy as np
    import soundfile as sf

    if not audio_chunks:
        raise RuntimeError("TTS returned no audio")
    chunks = []
    for audio in audio_chunks:
        if hasattr(audio, "detach"):
            audio = audio.detach().cpu().numpy()
        chunks.append(np.asarray(audio, dtype=np.float32))
    audio_out = chunks[0] if len(chunks) == 1 else np.concatenate(chunks)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
        wav_path = f.name
    try:
        sf.write(wav_path, audio_out, sr)
        subprocess.run(
            ["ffmpeg", "-y", "-i", wav_path, "-codec:a", "libmp3lame", "-q:a", "2", str(out_path)],
            check=True, capture_output=True,
        )
    finally:
        if os.path.exists(wav_path):
            os.unlink(wav_path)


# ---------------------------------------------------------------- kokoro ----
def get_pipeline(voice_id: str):
    lang_code = voice_id[:1].lower()
    if lang_code not in KOKORO_LANG_CODES:
        lang_code = "a"
    pipeline = _PIPELINES.get(lang_code)
    if pipeline is not None:
        return pipeline
    try:
        from kokoro import KPipeline
    except ImportError as e:
        raise RuntimeError(
            'Kokoro is not installed. Install it with: uv add "kokoro>=0.9.4" soundfile'
        ) from e
    pipeline = KPipeline(lang_code=lang_code, repo_id=KOKORO_MODEL_ID, device="cpu")
    _PIPELINES[lang_code] = pipeline
    return pipeline


def synthesize(items: list[tuple[int, str, Path]], voice_id: str) -> dict[int, str]:
    """Synthesize each (index, text, out_path) with a single shared pipeline pass."""
    if not items:
        return {}
    pipeline = get_pipeline(voice_id)
    speed = float(os.environ.get("KOKORO_SPEED", "1"))
    joined = "\n".join(text for _, text, _ in items)
    audio_by_pos: dict[int, list] = {i: [] for i in range(len(items))}
    for result in pipeline(joined, voice=voice_id, speed=speed, split_pattern=r"\n+"):
        audio_by_pos[result.text_index].append(result.audio)

    paths = {}
    for pos, (index, _, out_path) in enumerate(items):
        write_mp3(audio_by_pos[pos], out_path)
        paths[index] = str(out_path)
    return paths


# ------------------------------------------------------- models (sherpa) ----
def ensure_model(release: str, name: str) -> Path:
    """Download + unpack a sherpa-onnx release archive (or single .onnx file) once."""
    dest = MODELS_DIR / name
    if dest.exists():
        return dest
    MODELS_DIR.mkdir(parents=True, exist_ok=True)
    archive = not name.endswith(".onnx")
    url = f"{SHERPA_RELEASES}/{release}/{name}{'.tar.bz2' if archive else ''}"
    tmp = dest.with_name(dest.name + ".part")
    print(f"  downloading {name} (first use only) ...", flush=True)
    urllib.request.urlretrieve(url, tmp)
    if archive:
        with tarfile.open(tmp, "r:bz2") as tar:
            try:
                tar.extractall(MODELS_DIR, filter="data")
            except TypeError:  # Python < 3.12 without the extraction-filter backport
                tar.extractall(MODELS_DIR)
        tmp.unlink()
    else:
        tmp.rename(dest)
    return dest


def sherpa():
    try:
        import sherpa_onnx
    except ImportError as e:
        raise RuntimeError("Voice cloning / --qa need sherpa-onnx: uv add sherpa-onnx soundfile") from e
    return sherpa_onnx


class Transcriber:
    """Local Whisper (base.en) for transcribing voice samples and checking takes."""

    def __init__(self):
        so = sherpa()
        d = ensure_model("asr-models", WHISPER_MODEL)
        self.rec = so.OfflineRecognizer.from_whisper(
            encoder=str(d / "base.en-encoder.int8.onnx"), decoder=str(d / "base.en-decoder.int8.onnx"),
            tokens=str(d / "base.en-tokens.txt"), language="en", task="transcribe", num_threads=4)

    def __call__(self, audio, sr: int) -> str:
        import numpy as np

        audio16 = audio if sr == 16000 else np.interp(
            np.arange(0, len(audio) * 16000 // sr) * sr / 16000, np.arange(len(audio)), audio).astype(np.float32)
        s = self.rec.create_stream()
        s.accept_waveform(16000, audio16)
        self.rec.decode_stream(s)
        return s.result.text.strip()


_NUMBERS = "zero one two three four five six seven eight nine ten".split()


def _words(text: str) -> list[str]:
    """Normalise to spoken words; the recogniser writes maths as symbols ("5x + y = -1")."""
    text = text.lower()
    text = re.sub(r"(\d)([a-z])", r"\1 \2", text)                       # 5x -> 5 x
    text = text.replace("=", " equals ").replace("+", " plus ").replace("/", " over ")
    text = re.sub(r"(?<![a-z])-(?=\s*[\da-z(])", " minus ", text)          # minus sign, not a hyphen
    text = text.replace("-", " ")
    text = re.sub(r"\d+", lambda m: number_words(m.group()), text)
    text = re.sub(r"[^a-z ]", " ", text)
    same = {"x": "ex", "y": "why", "z": "zee", "t": "tee", "uh": "a", "eigh": "a", "summarise": "summarize"}
    return [same.get(w, w) for w in text.split()]


def number_words(digits: str) -> str:
    n = int(digits)
    return _NUMBERS[n] if n <= 10 else " ".join(_NUMBERS[int(c)] for c in digits)


def accuracy(expected: str, heard: str) -> float:
    """Rough word-level match between the script line and what the recogniser heard."""
    return difflib.SequenceMatcher(None, _words(expected), _words(heard)).ratio()


# ------------------------------------------------------- voice cloning -----
MATH_FOLLOWERS = r"minus|plus|times|over|equals?|is|isn't|squared|cubed|can|could|gives?|slides|tends|approaches"
LETTER_WORDS = {"x": "ex", "y": "why", "z": "zee", "t": "tee", "b": "bee", "c": "see", "k": "kay", "n": "en"}


def speakable(text: str) -> str:
    """
    Text actually sent to the cloned voice (subtitles keep the original).
    ZipVoice is a Chinese/English model, so a lone "a" is read as the pinyin
    syllable; single maths letters are easy for it to slur.
    """
    text = re.sub(rf"\ba\b(?=\s(?:{MATH_FOLLOWERS})\b)", "eigh,", text)   # the variable a ("ay", short beat)
    text = re.sub(r"\b(constant|parameter|variable) a\b", r"\1 eigh", text)
    text = re.sub(r"\ba\b(?=\s*(?:[.,;:!?)]|$))", "eigh", text)
    text = re.sub(r"\ba\b", "uh", text)                                      # the article
    text = re.sub(r"\b([xyztbckn])\b", lambda m: LETTER_WORDS[m.group(1)], text)
    return text


def prepare_voice(sample: str, transcript: str | None, max_s: float = 12.0):
    """Cut the sample to <= max_s at its quietest moment and transcribe it (cached by content)."""
    import numpy as np
    import soundfile as sf

    audio = decode(sample, CLONE_SR)
    if len(audio) / CLONE_SR > max_s:
        db, hop = frame_db(audio, CLONE_SR)
        lo, hi = int(max_s * 0.6 * CLONE_SR / hop), int(max_s * CLONE_SR / hop)
        cut = (lo + int(np.argmin(np.convolve(db[lo:hi], np.ones(10) / 10, mode="same")))) * hop
        audio = audio[:cut]
        if transcript:
            print("  note: sample trimmed to fit; --clone-text should match the first "
                  f"{cut / CLONE_SR:.1f}s of the sample", flush=True)
    audio = tidy(audio, CLONE_SR, lead=0.0)
    key = hashlib.sha256(audio.tobytes()).hexdigest()[:16]
    store = CACHE_DIR / "voices"
    store.mkdir(parents=True, exist_ok=True)
    txt = store / f"{key}.txt"
    if transcript:
        txt.write_text(transcript.strip())
    elif not txt.exists():
        print("  transcribing voice sample ...", flush=True)
        txt.write_text(Transcriber()(audio, CLONE_SR))
    sf.write(store / f"{key}.wav", audio, CLONE_SR)
    print(f"  voice sample: {len(audio) / CLONE_SR:.1f}s, \"{txt.read_text()[:80]}\"", flush=True)
    return audio, txt.read_text(), key


def load_zipvoice(guidance: float):
    so = sherpa()
    d = ensure_model("tts-models", ZIPVOICE_MODEL)
    vocoder = ensure_model("vocoder-models", "vocos_24khz.onnx")
    return so.OfflineTts(so.OfflineTtsConfig(model=so.OfflineTtsModelConfig(
        zipvoice=so.OfflineTtsZipvoiceModelConfig(
            tokens=str(d / "tokens.txt"), encoder=str(d / "text_encoder.onnx"),
            decoder=str(d / "fm_decoder.onnx"), vocoder=str(vocoder),
            data_dir=str(d / "espeak-ng-data"), lexicon=str(d / "pinyin.raw"), guidance_scale=guidance),
        num_threads=os.cpu_count() or 4, provider="cpu")))


def synthesize_clone(items, voice, opts, transcriber=None) -> dict[int, str]:
    """ZipVoice zero-shot TTS. With a transcriber, retake lines that come out garbled; keep the best."""
    import numpy as np

    if not items:
        return {}
    prompt, prompt_text, _ = voice
    model = load_zipvoice(opts.clone_guidance)
    paths = {}
    for index, text, out_path in items:
        best, best_acc = None, -1.0
        for take in range(1 + (opts.retakes if transcriber else 0)):
            gen = model.generate(speakable(text), prompt_text, prompt.tolist(), CLONE_SR,
                                 speed=opts.speed, num_steps=opts.clone_steps)
            audio = tidy(np.asarray(gen.samples, np.float32), gen.sample_rate)
            if transcriber is None:
                best = audio
                break
            acc = accuracy(text, transcriber(audio, gen.sample_rate))
            if acc > best_acc:
                best, best_acc = audio, acc
            if acc >= QA_MIN_ACCURACY:
                break
            print(f"  [{index}] take {take + 1} heard as {acc:.0%} of the line, retaking ...", flush=True)
        write_mp3([best], out_path, CLONE_SR)
        paths[index] = str(out_path)
        print(f"  [{index}] done" + (f" (match {best_acc:.0%})" if transcriber else ""), flush=True)
        if transcriber and best_acc < QA_MIN_ACCURACY:
            print(f"  [{index}] WARNING: best take only matched {best_acc:.0%}; listen to it, rephrase the line "
                  f"or --bust {index} to retry", flush=True)
    return paths


# ------------------------------------------------------------- assembly -----
def ffprobe_duration(mp3_path: str) -> float:
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", mp3_path],
        capture_output=True, text=True, check=True,
    )
    return float(result.stdout.strip())


def concat_mp3s(parts: list[str], out_path: str, durations: list[float] | None = None) -> None:
    # Stream-copying MP3 packets accumulates encoder padding at every join.
    # Decode first, hold each clip to its timing budget (padding with silence
    # for pauses), and encode only once.
    durations = durations or [ffprobe_duration(part) for part in parts]
    inputs = [arg for part in parts for arg in ("-i", part)]
    filters = [
        f"[{i}:a]apad,atrim=duration={durations[i]:.9f},asetpts=PTS-STARTPTS[a{i}]"
        for i in range(len(parts))
    ]
    filters.append("".join(f"[a{i}]" for i in range(len(parts)))
                   + f"concat=n={len(parts)}:v=0:a=1[audio]")
    subprocess.run(
        ["ffmpeg", "-y", *inputs, "-filter_complex", ";".join(filters),
         "-map", "[audio]", "-codec:a", "libmp3lame", "-q:a", "2", out_path],
        check=True, capture_output=True,
    )


def bust_cache(subtitles: list[str], target_arg: str, key_for) -> None:
    try:
        target = subtitles[int(target_arg)]
    except (ValueError, IndexError):
        matches = [s for s in subtitles if target_arg.lower() in s.lower()]
        if not matches:
            sys.exit(f"Error: no subtitle matching {target_arg!r}")
        target = matches[0]
    cp = key_for(target)
    if cp.exists():
        cp.unlink()
        print(f"Busted cache for: {target!r}")
    else:
        print(f"No cache entry for: {target!r}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate TTS voiceover from narration.txt SubtitleSpec")
    parser.add_argument("--narration", "--plan", dest="narration", default="narration.txt",
                        help="Spoken-line input (default: narration.txt; --plan is a compatibility alias)")
    parser.add_argument("--voice", default=DEFAULT_VOICE, help="Kokoro voice name (default: af_heart)")
    parser.add_argument("--clone", metavar="SAMPLE",
                        help="Narrate in the voice of this recording (5-30 s of clean speech; any audio format)")
    parser.add_argument("--clone-text", help="Exact words spoken in the sample (default: transcribed automatically)")
    parser.add_argument("--clone-guidance", type=float, default=1.5,
                        help="How strongly the clone follows the sample's voice and expression (default 1.5)")
    parser.add_argument("--clone-steps", type=int, default=4, help="ZipVoice sampling steps (default 4)")
    parser.add_argument("--speed", type=float, default=None,
                        help="Speaking-rate multiplier (default 1; Kokoro also honours KOKORO_SPEED)")
    parser.add_argument("--pause", type=float, default=0.0,
                        help="Seconds of silence after every line without a [pause N] hint (default 0)")
    parser.add_argument("--qa", action="store_true",
                        help="Transcribe each new clip and retake cloned lines that don't match the script")
    parser.add_argument("--retakes", type=int, default=2, help="Max extra takes per line with --qa (default 2)")
    parser.add_argument("--out-audio", default="voiceover.mp3")
    parser.add_argument("--out-timestamps", default="timestamps.json")
    parser.add_argument("--bust", metavar="INDEX_OR_TEXT",
                        help="Drop the cached clip for a subtitle by 0-based index or text match, then exit")
    args = parser.parse_args()

    if args.speed is not None and not args.clone:
        os.environ["KOKORO_SPEED"] = f"{args.speed:g}"
    args.speed = args.speed if args.speed is not None else 1.0
    entries = parse_entries(args.narration)
    subtitles = [text for text, _ in entries]

    voice = None
    if args.clone:
        if not Path(args.clone).exists():
            sys.exit(f"Error: voice sample not found: {args.clone}")
        voice = prepare_voice(args.clone, args.clone_text)
        extra = {"engine": "zipvoice", "model_id": ZIPVOICE_MODEL, "speed": f"{args.speed:g}",
                 "guidance": args.clone_guidance, "steps": args.clone_steps, "pronounce": 2}
        voice_id, label = f"clone:{voice[2]}", f"cloned from {Path(args.clone).name}"
        key_for = lambda text: cache_path(text, voice_id, extra=extra)  # noqa: E731
    else:
        voice_id, label = args.voice, f"Kokoro {args.voice}"
        key_for = lambda text: cache_path(text, voice_id)  # noqa: E731

    if args.bust is not None:
        bust_cache(subtitles, args.bust, key_for)
        return

    print(f"Found {len(subtitles)} subtitle(s). Voice: {label}. Generating TTS...")
    parts = [""] * len(subtitles)
    missing: list[tuple[int, str, Path]] = []
    hits = 0
    for i, text in enumerate(subtitles):
        cached = key_for(text)
        if cached.exists():
            parts[i] = str(cached)
            hits += 1
            print(f"  [{i}] cached")
        else:
            missing.append((i, text, cached))

    started = time.perf_counter()
    if voice:
        made = synthesize_clone(missing, voice, args, Transcriber() if args.qa and missing else None)
    else:
        made = synthesize(missing, voice_id)
        for idx in made:
            print(f"  [{idx}] done")
    for idx, path in made.items():
        parts[idx] = path
    print(f"  {hits} cached, {len(missing)} generated in {time.perf_counter() - started:.1f}s")

    if args.qa and missing and not voice:
        check = Transcriber()
        for idx, text, _ in missing:
            acc = accuracy(text, check(decode(parts[idx], 16000), 16000))
            if acc < QA_MIN_ACCURACY:
                print(f"  [{idx}] WARNING: heard as only {acc:.0%} of the line; listen to it")

    print("Measuring durations...")
    speech = [ffprobe_duration(p) for p in parts]
    pauses = [args.pause if hint is None else hint for _, hint in entries]
    durations = [s + p for s, p in zip(speech, pauses)]

    print(f"Concatenating {len(parts)} clips → {args.out_audio}")
    concat_mp3s(parts, args.out_audio, durations)

    start = 0.0
    out = []
    for i, (text, dur, pause) in enumerate(zip(subtitles, durations, pauses)):
        out.append({"index": i, "text": text,
                    "start_s": round(start, 3), "end_s": round(start + dur, 3),
                    "duration_s": round(dur, 3), "pause_s": round(pause, 3)})
        start += dur
    Path(args.out_timestamps).write_text(
        json.dumps({"total_duration_s": round(start, 3), "subtitles": out}, indent=2))
    print(f"Done. {args.out_audio} + {args.out_timestamps} written. Total: {start:.1f}s audio.")


if __name__ == "__main__":
    main()
