#!/usr/bin/env python3
"""
Append a clean YouTube end screen to a finished video: the last frame, softly blurred and darkened,
with the channel logo and "Thanks for watching" across the top and the rest of the frame left
empty for YouTube Studio's end screen elements (videos, playlist, subscribe). Optional sign-off
audio plays over it; room tone fills the rest.

    python studio/endscreen.py video_main.mp4 video.mp4 --seconds 20 --logo logo.png --voice signoff.mp3
"""

import argparse
import subprocess
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

from thumbnail import INK, font

W, H, FPS = 1920, 1080, 30
FADE = 0.8   # crossfade from the last frame of the video into the end screen


def duration(path: Path, stream: str = "") -> float:
    args = ["ffprobe", "-v", "error"] + (["-select_streams", stream] if stream else []) + [
        "-show_entries", "format=duration" if not stream else "stream=duration", "-of", "csv=p=0", str(path)]
    out = subprocess.run(args, capture_output=True, text=True).stdout.strip().splitlines()
    return float(out[0]) if out and out[0] not in ("", "N/A") else 0.0


def card(video: Path, out: Path, logo=None, channel: str = "", heading: str = "Thanks for watching") -> Path:
    """The end screen still: blurred last frame, logo and heading at the top, empty below."""
    png = subprocess.run(["ffmpeg", "-v", "error", "-sseof", "-0.5", "-i", str(video), "-frames:v", "1",
                          "-vf", f"scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H}",
                          "-f", "image2pipe", "-vcodec", "png", "-"], capture_output=True).stdout
    import io
    bg = Image.open(io.BytesIO(png)).convert("RGB") if png else Image.new("RGB", (W, H), INK)
    bg = Image.blend(bg.filter(ImageFilter.GaussianBlur(28)), Image.new("RGB", (W, H), INK), 0.62)
    img = bg.convert("RGBA")
    draw = ImageDraw.Draw(img)
    y = 70
    if logo and Path(logo).exists():
        lg = Image.open(logo).convert("RGBA")
        scale = min(130 / lg.height, 520 / lg.width)
        lg = lg.resize((max(1, int(lg.width * scale)), max(1, int(lg.height * scale))), Image.LANCZOS)
        img.paste(lg, ((W - lg.width) // 2, y), lg)
        y += lg.height + 28
    elif channel:
        f = font(56)
        draw.text(((W - f.getlength(channel.upper())) // 2, y), channel.upper(), font=f, fill="white")
        y += 90
    if heading:
        f = font(76)
        text = heading.upper()
        draw.text(((W - f.getlength(text)) // 2, y), text, font=f, fill="white",
                  stroke_width=3, stroke_fill=INK)
    img.convert("RGB").save(out)
    return out


def append(main: Path, out: Path, seconds: float = 20.0, logo=None, channel: str = "",
           heading: str = "Thanks for watching", voice: Path | None = None) -> float:
    """Write `out` = `main` + a `seconds`-long end screen. Returns where the end screen starts (s)."""
    main, out = Path(main), Path(out)
    with tempfile.TemporaryDirectory() as tmp:
        still = card(main, Path(tmp) / "end.png", logo, channel, heading)
        start = max(0.0, duration(main) - FADE)
        tail = seconds - FADE
        voice_in = ["-i", str(voice)] if voice and Path(voice).exists() else []
        audio_mix = ("[2:a]adelay=400|400,aresample=48000,aformat=channel_layouts=stereo[vo];"
                     "[room][vo]amix=inputs=2:normalize=0:duration=first[tail]") if voice_in else "[room]anull[tail]"
        graph = ";".join([
            f"[0:v]fps={FPS},scale={W}:{H}:force_original_aspect_ratio=decrease,pad={W}:{H}:(ow-iw)/2:(oh-ih)/2,"
            f"format=yuv420p,settb=AVTB[mv]",
            f"[1:v]fps={FPS},format=yuv420p,settb=AVTB[ev]",
            f"[mv][ev]xfade=transition=fade:duration={FADE}:offset={start:.3f}[v]",
            f"anoisesrc=color=pink:amplitude=0.0012:sample_rate=48000:duration={tail:.3f},"
            f"aformat=channel_layouts=stereo[room]",
            audio_mix,
            "[0:a]aresample=48000,aformat=channel_layouts=stereo[ma]",
            "[ma][tail]concat=n=2:v=0:a=1[a]",
        ])
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(main),
                        "-loop", "1", "-t", f"{seconds:.3f}", "-i", str(still), *voice_in,
                        "-filter_complex", graph, "-map", "[v]", "-map", "[a]",
                        "-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-pix_fmt", "yuv420p",
                        "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", str(out)],
                       check=True, capture_output=True)
    return start + FADE


def main() -> None:
    p = argparse.ArgumentParser(description="Append a clean YouTube end screen to a video")
    p.add_argument("main")
    p.add_argument("out")
    p.add_argument("--seconds", type=float, default=20.0)
    p.add_argument("--logo")
    p.add_argument("--channel", default="")
    p.add_argument("--heading", default="Thanks for watching")
    p.add_argument("--voice", help="Sign-off audio to play at the start of the end screen")
    a = p.parse_args()
    print(f"end screen starts at {append(Path(a.main), Path(a.out), a.seconds, a.logo, a.channel, a.heading, a.voice):.2f}s")


if __name__ == "__main__":
    main()
