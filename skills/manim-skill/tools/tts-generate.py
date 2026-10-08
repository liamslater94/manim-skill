#!/usr/bin/env python3
"""
TTS generator for the Manim voiceover pipeline (local Kokoro or a cloned voice).

Reads the SubtitleSpec from narration.txt, synthesizes one MP3 per subtitle,
measures exact durations with ffprobe, concatenates them into voiceover.mp3,
and writes per-subtitle timings to timestamps.json.

    python tts-generate.py --narration narration.txt            # cloned voice if set up, else Kokoro
    python tts-generate.py --narration narration.txt --engine kokoro --voice am_adam
    python tts-generate.py --narration narration.txt --engine clone
    python tts-generate.py --narration narration.txt --bust 2   # drop one cached clip, then exit

Engines:
  kokoro  Kokoro-82M preset voices. Requires: uv add "kokoro>=0.9.4" soundfile
  clone   Zero-shot voice cloning with Qwen3-TTS from a reference recording
          (voice/reference.wav + voice/reference.txt next to tools/, or
          $MANIM_VOICE_DIR). Requires: uv add qwen-tts torch soundfile
  auto    (default) clone when a reference recording is found, else kokoro.

Spoken text is passed through British pronunciation fixes by default (a lone
letter z is said "zed"); subtitles keep the original text. Disable with
--pronunciation none.

Per-line audio is cached in .tts-cache/, so re-runs only regenerate changed
lines. The model is loaded once per run (in-process); if every line is a cache
hit it is not loaded at all. Requires ffmpeg/ffprobe.
"""

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

DEFAULT_VOICE = "af_heart"
KOKORO_MODEL_ID = "hexgrad/Kokoro-82M"
KOKORO_LANG_CODES = {"a", "b", "e", "f", "h", "i", "j", "p", "z"}
CLONE_MODEL_IDS = {"1.7B": "Qwen/Qwen3-TTS-12Hz-1.7B-Base", "0.6B": "Qwen/Qwen3-TTS-12Hz-0.6B-Base"}
DEFAULT_VOICE_DIR = Path(__file__).resolve().parent.parent / "voice"
CACHE_DIR = Path(".tts-cache")
_PIPELINES = {}
_CLONE = {}

# A lone letter z (optionally with a numeric coefficient: "3z", "8z") — not part of a word.
_LONE_Z = re.compile(r"(?<![A-Za-z])(\d*)[zZ](?![A-Za-z])")


def to_spoken(text: str, pronunciation: str) -> str:
    """Rewrite subtitle text into what the TTS engine should actually say."""
    if pronunciation == "british":
        text = _LONE_Z.sub(lambda m: (m.group(1) + " " if m.group(1) else "") + "zed", text)
    return text


def voice_dir() -> Path:
    return Path(os.environ.get("MANIM_VOICE_DIR", DEFAULT_VOICE_DIR))


def clone_reference() -> tuple[Path, str] | None:
    """Return (reference audio, its exact transcript) if a cloned voice is set up."""
    d = voice_dir()
    txt = d / "reference.txt"
    audio = next((d / f"reference{ext}" for ext in (".wav", ".mp3", ".flac", ".m4a")
                  if (d / f"reference{ext}").exists()), None)
    if audio is None or not txt.exists():
        return None
    return audio, txt.read_text().strip()


def cache_path(text: str, voice_id: str, cache_dir: Path = CACHE_DIR) -> Path:
    obj = {"model_id": KOKORO_MODEL_ID, "text": text, "voice_id": voice_id,
           "speed": os.environ.get("KOKORO_SPEED", "1")}
    if voice_id.startswith("clone:"):
        obj = {"model_id": voice_id, "text": text}
    key = hashlib.sha256(json.dumps(obj, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return cache_dir / f"{key}.mp3"


def parse_subtitles(narration_path: str) -> list[str]:
    """
    Parse the SubtitleSpec from narration.txt: lines prefixed with `- ` under a
    `subtitles:` heading (inside or outside a fenced code block).
    """
    text = Path(narration_path).read_text()
    stripped = re.sub(r"```[^\n]*\n", "", text).replace("```", "")
    match = re.search(r"^subtitles:\s*\n(.*?)(?=\n[^\s\-\n]|\Z)", stripped, re.MULTILINE | re.DOTALL)
    if not match:
        sys.exit(f"Error: No 'subtitles:' block found in {narration_path}")
    items = [re.sub(r"^[ \t]*-\s*", "", line).strip()
             for line in match.group(1).splitlines()
             if re.match(r"^[ \t]*-", line)]
    if not items:
        sys.exit("Error: Empty subtitles list in narration.txt")
    return items


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


def write_mp3(audio_chunks, out_path: Path, sample_rate: int = 24000) -> None:
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
        sf.write(wav_path, audio_out, sample_rate)
        subprocess.run(
            ["ffmpeg", "-y", "-i", wav_path, "-codec:a", "libmp3lame", "-q:a", "2", str(out_path)],
            check=True, capture_output=True,
        )
    finally:
        if os.path.exists(wav_path):
            os.unlink(wav_path)


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


def get_clone_model(model_size: str, ref_audio: Path, ref_text: str):
    key = (model_size, str(ref_audio))
    if key in _CLONE:
        return _CLONE[key]
    try:
        import torch
        from qwen_tts import Qwen3TTSModel
    except ImportError as e:
        raise RuntimeError("Voice cloning is not installed. Install it with: uv add qwen-tts torch soundfile") from e
    if torch.cuda.is_available():
        model = Qwen3TTSModel.from_pretrained(CLONE_MODEL_IDS[model_size], device_map="cuda",
                                              dtype=torch.bfloat16)
    else:
        torch.set_num_threads(os.cpu_count() or 4)
        model = Qwen3TTSModel.from_pretrained(CLONE_MODEL_IDS[model_size], dtype=torch.float32,
                                              low_cpu_mem_usage=False)
    prompt = model.create_voice_clone_prompt(ref_audio=str(ref_audio), ref_text=ref_text,
                                             x_vector_only_mode=False)
    _CLONE[key] = (model, prompt)
    return _CLONE[key]


def trim_silence(audio, sample_rate: int, lead_s: float = 0.05, tail_s: float = 0.15):
    """Trim leading/trailing near-silence, keeping a short natural margin."""
    import numpy as np

    level = np.abs(audio)
    loud = np.nonzero(level > max(level.max() * 0.02, 1e-4))[0]
    if loud.size == 0:
        return audio
    a = max(0, loud[0] - int(lead_s * sample_rate))
    b = min(len(audio), loud[-1] + int(tail_s * sample_rate))
    return audio[a:b]


def synthesize_clone(items: list[tuple[int, str, Path]], model_size: str,
                     ref_audio: Path, ref_text: str, seed_offset: int = 0) -> dict[int, str]:
    """Synthesize each (index, spoken text, out_path) in the cloned voice."""
    if not items:
        return {}
    import numpy as np
    import torch

    model, prompt = get_clone_model(model_size, ref_audio, ref_text)
    paths = {}
    for index, text, out_path in items:
        # Seed from the text so a given line always renders the same take;
        # --bust plus --seed-offset gives a different take of one line.
        seed = int(hashlib.sha256(text.encode()).hexdigest()[:8], 16) + seed_offset
        torch.manual_seed(seed)
        with torch.inference_mode():
            wavs, sr = model.generate_voice_clone(text=text, voice_clone_prompt=prompt, language="English")
        audio = trim_silence(np.asarray(wavs[0], dtype=np.float32), sr)
        write_mp3([audio], out_path, sr)
        paths[index] = str(out_path)
        print(f"  [{index}] done ({len(audio) / sr:.1f}s)", flush=True)
    return paths


def ffprobe_duration(mp3_path: str) -> float:
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", mp3_path],
        capture_output=True, text=True, check=True,
    )
    return float(result.stdout.strip())


def concat_mp3s(parts: list[str], out_path: str) -> None:
    # Stream-copying MP3 packets accumulates encoder padding at every join.
    # Decode first, preserve each measured timing budget, and encode only once.
    inputs = [arg for part in parts for arg in ("-i", part)]
    filters = [
        f"[{i}:a]apad,atrim=duration={ffprobe_duration(part):.9f},"
        f"asetpts=PTS-STARTPTS[a{i}]"
        for i, part in enumerate(parts)
    ]
    filters.append("".join(f"[a{i}]" for i in range(len(parts)))
                   + f"concat=n={len(parts)}:v=0:a=1[audio]")
    subprocess.run(
        ["ffmpeg", "-y", *inputs, "-filter_complex", ";".join(filters),
         "-map", "[audio]", "-codec:a", "libmp3lame", "-q:a", "2", out_path],
        check=True, capture_output=True,
    )


def bust_cache(subtitles: list[str], spoken: list[str], target_arg: str, voice_id: str) -> None:
    try:
        i = int(target_arg)
        target = subtitles[i]
    except (ValueError, IndexError):
        matches = [i for i, s in enumerate(subtitles) if target_arg.lower() in s.lower()]
        if not matches:
            sys.exit(f"Error: no subtitle matching {target_arg!r}")
        i = matches[0]
        target = subtitles[i]
    cp = cache_path(spoken[i], voice_id)
    if cp.exists():
        cp.unlink()
        print(f"Busted cache for: {target!r}")
    else:
        print(f"No cache entry for: {target!r}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate TTS voiceover from narration.txt SubtitleSpec")
    parser.add_argument("--narration", "--plan", dest="narration", default="narration.txt",
                        help="Spoken-line input (default: narration.txt; --plan is a compatibility alias)")
    parser.add_argument("--engine", choices=["auto", "kokoro", "clone"], default="auto",
                        help="auto (default): cloned voice if voice/reference.* exists, else Kokoro")
    parser.add_argument("--voice", default=DEFAULT_VOICE, help="Kokoro voice name (default: af_heart)")
    parser.add_argument("--clone-model", choices=sorted(CLONE_MODEL_IDS), default="1.7B",
                        help="Qwen3-TTS size for --engine clone (default: 1.7B; 0.6B is faster)")
    parser.add_argument("--pronunciation", choices=["british", "none"], default="british",
                        help="Spoken-text fixes, e.g. a lone z is said 'zed' (default: british)")
    parser.add_argument("--seed-offset", type=int, default=0,
                        help="Clone engine: shift the per-line seed for a different take (use with --bust)")
    parser.add_argument("--out-audio", default="voiceover.mp3")
    parser.add_argument("--out-timestamps", default="timestamps.json")
    parser.add_argument("--bust", metavar="INDEX_OR_TEXT",
                        help="Drop the cached clip for a subtitle by 0-based index or text match, then exit")
    args = parser.parse_args()

    subtitles = parse_subtitles(args.narration)
    spoken = [to_spoken(t, args.pronunciation) for t in subtitles]

    reference = clone_reference()
    engine = args.engine
    if engine == "auto":
        engine = "clone" if reference else "kokoro"
    if engine == "clone":
        if reference is None:
            sys.exit(f"Error: --engine clone needs reference.wav (or .mp3) and reference.txt in {voice_dir()}")
        ref_audio, ref_text = reference
        # The seed offset is deliberately not in the cache key: --bust one line,
        # then re-run with --seed-offset N to re-roll only that line.
        ref_key = hashlib.sha256(ref_audio.read_bytes() + ref_text.encode()).hexdigest()[:16]
        voice_id = f"clone:{args.clone_model}:{ref_key}"
    else:
        voice_id = args.voice

    if args.bust is not None:
        bust_cache(subtitles, spoken, args.bust, voice_id)
        return

    print(f"Found {len(subtitles)} subtitle(s). Engine: {engine}. Voice: {voice_id}. Generating TTS...")
    parts = [""] * len(subtitles)
    missing: list[tuple[int, str, Path]] = []
    hits = 0
    for i, text in enumerate(spoken):
        cached = cache_path(text, voice_id)
        if cached.exists():
            parts[i] = str(cached)
            hits += 1
            print(f"  [{i}] cached")
        else:
            missing.append((i, text, cached))

    started = time.perf_counter()
    if engine == "clone":
        generated = synthesize_clone(missing, args.clone_model, ref_audio, ref_text, args.seed_offset)
    else:
        generated = synthesize(missing, voice_id)
        for idx in generated:
            print(f"  [{idx}] done")
    for idx, path in generated.items():
        parts[idx] = path
    print(f"  {hits} cached, {len(missing)} generated in {time.perf_counter() - started:.1f}s")

    print("Measuring durations...")
    durations = [ffprobe_duration(p) for p in parts]

    print(f"Concatenating {len(parts)} clips → {args.out_audio}")
    concat_mp3s(parts, args.out_audio)

    start = 0.0
    entries = []
    for i, (text, dur) in enumerate(zip(subtitles, durations)):
        entries.append({"index": i, "text": text,
                        "start_s": round(start, 3), "end_s": round(start + dur, 3),
                        "duration_s": round(dur, 3)})
        start += dur
    Path(args.out_timestamps).write_text(
        json.dumps({"total_duration_s": round(start, 3), "subtitles": entries}, indent=2))
    print(f"Done. {args.out_audio} + {args.out_timestamps} written. Total: {start:.1f}s audio.")


if __name__ == "__main__":
    main()
