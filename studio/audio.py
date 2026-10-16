#!/usr/bin/env python3
"""
Final audio for a finished video: a gentle voice polish (rumble cut, a touch of presence, light
de-essing and compression), an optional music bed that ducks under the voice, and loudness
normalised to YouTube's -14 LUFS (two-pass, so every video plays at the same volume).

    python studio/audio.py video.mp4 out.mp4 --music track.mp3 --region 0:9 --region 182:202

Without --music the voice is only polished and normalised. Without --region the music plays
throughout. `builtin_pad()` synthesises a soft, royalty-free pad if you have no track of your own.
"""

import argparse
import json
import subprocess
import tempfile
from pathlib import Path

TARGET_LUFS = -14.0
VOICE_CHAIN = ("highpass=f=70,equalizer=f=250:t=q:w=1:g=-1.5,equalizer=f=3500:t=q:w=1.2:g=1.5,"
               "deesser=i=0.25,acompressor=threshold=0.125:ratio=2.5:attack=15:release=250:makeup=1.4")


def duration(path: Path) -> float:
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0",
                          str(path)], capture_output=True, text=True).stdout.strip()
    return float(out or 0)


def music_gain_db(level: float) -> float:
    """Music level 0-100 (a slider) -> gain in dB under a loudness-matched voice."""
    return -32 + 0.22 * max(0.0, min(100.0, level))


def region_expr(regions, fade_in: float = 1.2, fade_out: float = 2.5) -> str:
    terms = [f"clip((t-{a:.3f})/{fade_in},0,1)*clip(({b:.3f}-t)/{fade_out},0,1)" for a, b in regions]
    return "min(1," + "+".join(terms) + ")" if terms else "1"


def graph(music: bool, total: float, regions, level: float) -> str:
    if not music:
        return f"[0:a]aresample=48000,{VOICE_CHAIN}[mix]"
    vol = region_expr(regions) if regions else "1"
    return ";".join([
        f"[0:a]aresample=48000,aformat=channel_layouts=stereo,{VOICE_CHAIN},asplit=2[v][sc]",
        f"[1:a]aresample=48000,aformat=channel_layouts=stereo,atrim=0:{total:.3f},asetpts=N/SR/TB,"
        f"loudnorm=I=-16:TP=-2,volume={music_gain_db(level):.1f}dB,volume='{vol}':eval=frame[m0]",
        "[m0][sc]sidechaincompress=threshold=0.02:ratio=8:attack=30:release=500[m]",
        "[v][m]amix=inputs=2:normalize=0:duration=first[mix]",
    ])


def master(src, dst, music=None, regions=None, level: float = 30.0) -> dict:
    """Write dst = src with mastered audio (video stream copied). Returns the loudness measured."""
    src, dst = Path(src), Path(dst)
    total = duration(src)
    use_music = bool(music and Path(music).exists())
    inputs = ["-i", str(src)] + (["-stream_loop", "-1", "-i", str(music)] if use_music else [])
    g = graph(use_music, total, regions or [], level)
    measured = subprocess.run(["ffmpeg", "-hide_banner", "-nostats", *inputs, "-filter_complex",
                               g + f";[mix]loudnorm=I={TARGET_LUFS}:TP=-1.5:LRA=11:print_format=json[out]",
                               "-map", "[out]", "-t", f"{total:.3f}", "-f", "null", "-"],
                              capture_output=True, text=True)
    text = measured.stderr
    try:
        stats = json.loads(text[text.rindex("{"):text.rindex("}") + 1])
    except ValueError as e:
        raise RuntimeError("Couldn't measure loudness:\n" + text[-800:]) from e
    norm = (f"loudnorm=I={TARGET_LUFS}:TP=-1.5:LRA=11:measured_I={stats['input_i']}:"
            f"measured_TP={stats['input_tp']}:measured_LRA={stats['input_lra']}:"
            f"measured_thresh={stats['input_thresh']}:offset={stats['target_offset']}:linear=true")
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / dst.name
        subprocess.run(["ffmpeg", "-v", "error", "-y", *inputs, "-filter_complex",
                        g + f";[mix]{norm},aresample=48000[out]", "-map", "0:v", "-map", "[out]",
                        "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-t", f"{total:.3f}",
                        "-movflags", "+faststart", str(out)], check=True, capture_output=True)
        out.replace(dst)
    return stats


def builtin_pad(out, seconds: float = 64.0, sr: int = 48000) -> Path:
    """A soft, slow, royalty-free chord pad (Cmaj7 - Am7 - Fmaj7 - G6), made here from sine tones."""
    import numpy as np
    import soundfile as sf

    out = Path(out)
    if out.exists():
        return out
    note = lambda name: 440.0 * 2 ** ((name - 69) / 12)   # noqa: E731  (MIDI number -> Hz)
    chords = [[48, 52, 55, 59, 64], [45, 48, 52, 55, 60], [41, 45, 48, 52, 57], [43, 47, 50, 52, 59]]
    bar = seconds / 8
    n = int(seconds * sr)
    t = np.arange(n) / sr
    left, right = np.zeros(n), np.zeros(n)
    for k in range(8):
        chord = chords[k % 4]
        start, length = k * bar, bar + 2.5          # each chord rings into the next
        env = np.clip((t - start) / 2.0, 0, 1) * np.clip((start + length - t) / 2.5, 0, 1)
        env = env ** 1.5
        for i, m in enumerate(chord):
            f = note(m)
            amp = 0.16 / (1 + 0.35 * i)
            for detune, ch in ((0.9985, left), (1.0015, right)):
                ph = 2 * np.pi * f * detune * t
                ch += amp * env * (np.sin(ph) + 0.18 * np.sin(2 * ph) + 0.05 * np.sin(3 * ph))
    stereo = np.stack([left, right], axis=1)
    # loop seamlessly: fold the tail into the head
    tail = int(2.5 * sr)
    stereo[:tail] += stereo[-tail:] * np.linspace(1, 0, tail)[:, None]
    stereo *= 0.25 / max(1e-9, np.sqrt(np.mean(stereo ** 2))) * 0.35
    out.parent.mkdir(parents=True, exist_ok=True)
    raw = out.with_name(out.stem + "-raw.wav")
    sf.write(raw, stereo.astype(np.float32), sr)
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(raw), "-af", "lowpass=f=1800,lowpass=f=3000",
                    str(out)], check=True)   # soften
    raw.unlink()
    return out


def main() -> None:
    p = argparse.ArgumentParser(description="Polish, add music and normalise a video's audio for YouTube")
    p.add_argument("src")
    p.add_argument("dst")
    p.add_argument("--music", help="Music file (loops); 'builtin' for the built-in pad")
    p.add_argument("--region", action="append", default=[], metavar="START:END",
                   help="Where the music plays (repeatable); default throughout")
    p.add_argument("--level", type=float, default=30.0, help="Music level 0-100 (default 30)")
    a = p.parse_args()
    music = a.music
    if music == "builtin":
        music = builtin_pad(Path(tempfile.gettempdir()) / "manim-studio-pad.wav")
    regions = [tuple(map(float, r.split(":"))) for r in a.region]
    stats = master(a.src, a.dst, music, regions, a.level)
    print(f"Loudness before: {stats['input_i']} LUFS -> {TARGET_LUFS} LUFS")


if __name__ == "__main__":
    main()
