#!/usr/bin/env python3
"""
A YouTube Short (1080×1920, under 60 s) cut from a finished explainer: a hook title at the top, the
video in the middle, big captions underneath and the channel at the bottom.

    python studio/shorts.py video_1080p.mp4 short.mp4 --start 42.5 --end 88 \
        --title "The trap in this system" --srt subtitles.srt --logo logo.png
"""

import argparse
import re
import subprocess
import tempfile
from pathlib import Path

from PIL import Image, ImageColor, ImageDraw

from subtitles import parse_srt
from thumbnail import INK, font, wrap

W, H = 1080, 1920
VIDEO_Y, VIDEO_H = 640, 608          # 16:9 at full width
CAPTION_TOP = VIDEO_Y + VIDEO_H + 70
MAX_S = 59.0
CAPTION_FONTS = [Path("/System/Library/Fonts/Supplemental/Arial Bold.ttf"),
                 Path("/Library/Fonts/Arial Bold.ttf"),
                 Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf")]


def caption_font(size: int):
    from PIL import ImageFont

    for f in CAPTION_FONTS:
        if f.exists():
            return ImageFont.truetype(str(f), size)
    return font(size)


def snap(cues, start: float, end: float) -> tuple[float, float]:
    """Move the cut to caption boundaries (start of a line, end of a line) and keep it under a minute."""
    if cues:
        starts = [a for a, _, _ in cues]
        start = min(starts, key=lambda a: abs(a - start))
        ends = [b for _, b, _ in cues if b > start + 12]
        if ends:
            end = min(ends, key=lambda b: abs(b - end))
            fitting = [b for b in ends if b - start <= MAX_S]
            if end - start > MAX_S and fitting:
                end = max(fitting)
    end = min(end, start + MAX_S)
    return round(start, 3), round(end + 0.3, 3)


def base(title: str, logo, channel: str, accent: str) -> Image.Image:
    accent_rgb = ImageColor.getrgb(accent or "#FFD23F")
    img = Image.new("RGB", (W, H), INK)
    draw = ImageDraw.Draw(img)
    # subtle vertical glow behind the video
    for y in range(H):
        d = abs(y - (VIDEO_Y + VIDEO_H / 2)) / H
        shade = max(0, int(26 - d * 60))
        draw.line([(0, y), (W, y)], fill=(11 + shade // 2, 15 + shade // 2, 20 + shade))
    y = 110
    if logo and Path(logo).exists():
        lg = Image.open(logo).convert("RGBA")
        scale = min(110 / lg.height, 420 / lg.width)
        lg = lg.resize((max(1, int(lg.width * scale)), max(1, int(lg.height * scale))), Image.LANCZOS)
        img.paste(lg, ((W - lg.width) // 2, y), lg)
        y += lg.height + 40
    else:
        y += 40
    size, lines = 112, []
    for size in range(112, 63, -6):
        lines = wrap(title.upper(), font(size), W - 120)
        if len(lines) <= 3:
            break
    fnt = font(size)
    block_h = len(lines) * int(size * 1.08)
    y = max(y, VIDEO_Y - 50 - block_h)
    for i, line in enumerate(lines):
        x = (W - fnt.getlength(line)) / 2
        draw.text((x, y), line, font=fnt, fill=accent_rgb if i == 0 else (255, 255, 255),
                  stroke_width=4, stroke_fill=INK)
        y += int(size * 1.08)
    foot = caption_font(40)
    text = f"Full explanation on {channel}" if channel else "Full explanation on the channel"
    draw.text(((W - foot.getlength(text)) / 2, H - 230), text, font=foot, fill=(200, 205, 214))
    return img


def caption_png(text: str, path: Path, accent: str) -> Path:
    fnt = caption_font(58)
    lines = wrap(text.replace("\n", " "), fnt, W - 140)[:4]
    img = Image.new("RGBA", (W, 4 * 76 + 20), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    y = 0
    for line in lines:
        draw.text(((W - fnt.getlength(line)) / 2, y), line, font=fnt, fill="white",
                  stroke_width=5, stroke_fill=INK)
        y += 76
    img.save(path)
    return path


def make(video, out, start: float, end: float, title: str, srt=None, logo=None, channel: str = "",
         accent: str = "#FFD23F") -> dict:
    video, out = Path(video), Path(out)
    cues = parse_srt(Path(srt).read_text()) if srt and Path(srt).exists() else []
    start, end = snap(cues, start, end)
    length = end - start
    inside = [(max(a, start) - start, min(b, end) - start, re.sub(r"\s+", " ", t))
              for a, b, t in cues if b > start + 0.05 and a < end - 0.05]
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        base(title, logo, channel, accent).save(tmp / "base.png")
        inputs = ["-loop", "1", "-t", f"{length:.3f}", "-i", str(tmp / "base.png"),
                  "-ss", f"{start:.3f}", "-t", f"{length:.3f}", "-i", str(video)]
        chain = [f"[1:v]scale={W}:{VIDEO_H}:force_original_aspect_ratio=decrease,"
                 f"pad={W}:{VIDEO_H}:(ow-iw)/2:(oh-ih)/2:color=0x0B0F14[vid]",
                 f"[0:v][vid]overlay=0:{VIDEO_Y}[s0]"]
        last = "s0"
        for k, (a, b, text) in enumerate(inside):
            inputs += ["-i", str(caption_png(text, tmp / f"cap{k}.png", accent))]
            chain.append(f"[{last}][{k + 2}:v]overlay=0:{CAPTION_TOP}:enable='between(t,{a:.3f},{b:.3f})'[s{k + 1}]")
            last = f"s{k + 1}"
        chain.append(f"[{last}]format=yuv420p,fps=30[v]")
        chain.append(f"[1:a]afade=t=in:d=0.25,afade=t=out:st={max(0, length - 0.4):.3f}:d=0.4[a]")
        subprocess.run(["ffmpeg", "-v", "error", "-y", *inputs, "-filter_complex", ";".join(chain),
                        "-map", "[v]", "-map", "[a]", "-t", f"{length:.3f}", "-c:v", "libx264", "-preset",
                        "veryfast", "-crf", "20", "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart",
                        str(out)], check=True, capture_output=True)
    return {"start_s": start, "end_s": end, "seconds": round(length, 1), "title": title}


def main() -> None:
    p = argparse.ArgumentParser(description="Cut a YouTube Short from a finished video")
    p.add_argument("video")
    p.add_argument("out")
    p.add_argument("--start", type=float, required=True)
    p.add_argument("--end", type=float, required=True)
    p.add_argument("--title", required=True)
    p.add_argument("--srt")
    p.add_argument("--logo")
    p.add_argument("--channel", default="")
    p.add_argument("--accent", default="#FFD23F")
    a = p.parse_args()
    print(make(a.video, a.out, a.start, a.end, a.title, a.srt, a.logo, a.channel, a.accent))


if __name__ == "__main__":
    main()
