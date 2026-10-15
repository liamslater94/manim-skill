#!/usr/bin/env python3
"""
YouTube thumbnail (1280×720) for a Manim Studio video: a striking frame from the video as the
background, the title in large bold lettering, the channel logo and a level badge.

    python studio/thumbnail.py video.mp4 thumbnail.png --headline "Shortest distance" \
        --subline "between vector lines" --frame 42 --logo logo.png --badge "IB Maths AA HL"
"""

import argparse
import io
import subprocess
from pathlib import Path

from PIL import Image, ImageColor, ImageDraw, ImageFilter, ImageFont

W, H = 1280, 720
MARGIN = 64
FONT_FILES = [
    Path(__file__).resolve().parent / "fonts" / "Anton-Regular.ttf",
    Path("/System/Library/Fonts/Supplemental/Arial Black.ttf"),
    Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
]
INK = (11, 15, 20)


def font(size: int) -> ImageFont.FreeTypeFont:
    for f in FONT_FILES:
        if f.exists():
            return ImageFont.truetype(str(f), size)
    return ImageFont.load_default(size)


def video_frame(video: Path, at: float) -> Image.Image:
    """The frame at `at` seconds, enlarged and shifted right so its centre sits beside the title."""
    def grab(t):
        return subprocess.run(
            ["ffmpeg", "-v", "error", "-ss", f"{max(0.0, t):.2f}", "-i", str(video), "-frames:v", "1",
             "-f", "image2pipe", "-vcodec", "png", "-"], capture_output=True).stdout
    canvas = Image.new("RGB", (W, H), INK)
    png = grab(at) or grab(0)
    if not png:
        return canvas
    frame = Image.open(io.BytesIO(png)).convert("RGB")
    scale = max(W / frame.width, H / frame.height) * 1.12
    frame = frame.resize((int(frame.width * scale), int(frame.height * scale)), Image.LANCZOS)
    canvas.paste(frame, (int(W * 0.70 - frame.width / 2), (H - frame.height) // 2))
    return canvas


def wrap(text: str, fnt: ImageFont.FreeTypeFont, width: int) -> list[str]:
    lines, line = [], ""
    for word in text.split():
        trial = f"{line} {word}".strip()
        if fnt.getlength(trial) <= width or not line:
            line = trial
        else:
            lines.append(line)
            line = word
    return lines + ([line] if line else [])


def fit(text: str, width: int, max_lines: int, start: int, smallest: int):
    """Largest font size at which `text` wraps into at most `max_lines` lines of `width`."""
    for size in range(start, smallest - 1, -4):
        lines = wrap(text, font(size), width)
        if len(lines) <= max_lines and all(font(size).getlength(l) <= width for l in lines):
            return size, lines
    return smallest, wrap(text, font(smallest), width)


def shade(img: Image.Image) -> Image.Image:
    """Dark gradient from the left (behind the text) plus a gentle vignette, so the title always reads."""
    grad = Image.new("L", (W, 1))
    for x in range(W):
        t = x / (W * 0.78)
        grad.putpixel((x, 0), int(255 * max(0.0, 1 - t) ** 1.2) if t < 1 else 0)
    grad = grad.resize((W, H))
    dark = Image.new("RGB", (W, H), INK)
    img = Image.blend(img, dark, 0.18)
    img = Image.composite(dark, img, grad)
    vignette = Image.new("L", (W, H), 0)
    ImageDraw.Draw(vignette).rectangle([0, int(H * 0.82), W, H], fill=120)
    return Image.composite(dark, img, vignette.filter(ImageFilter.GaussianBlur(60)))


def paste_logo(img: Image.Image, logo_path: Path, box_h: int = 120, max_w: int = 380) -> int:
    """Logo top-left with a soft shadow; returns the y below it."""
    logo = Image.open(logo_path).convert("RGBA")
    scale = min(box_h / logo.height, max_w / logo.width)
    logo = logo.resize((max(1, int(logo.width * scale)), max(1, int(logo.height * scale))), Image.LANCZOS)
    shadow = Image.new("RGBA", (logo.width + 40, logo.height + 40), (0, 0, 0, 0))
    shadow.paste((0, 0, 0, 170), (20, 24), logo.split()[3])
    shadow = shadow.filter(ImageFilter.GaussianBlur(10))
    img.paste(shadow, (MARGIN - 20, MARGIN - 20), shadow)
    img.paste(logo, (MARGIN, MARGIN), logo)
    return MARGIN + logo.height


def pill(draw: ImageDraw.ImageDraw, xy, text: str, size: int, fill, ink) -> tuple[int, int]:
    fnt = font(size)
    x, y = xy
    pad_x, pad_y = int(size * 0.55), int(size * 0.28)
    w = int(fnt.getlength(text)) + 2 * pad_x
    asc, desc = fnt.getmetrics()
    h = asc + desc + 2 * pad_y
    draw.rounded_rectangle([x, y, x + w, y + h], radius=h // 2, fill=fill)
    draw.text((x + pad_x, y + pad_y), text, font=fnt, fill=ink)
    return w, h


def render(video, out, headline: str, subline: str = "", frame_s: float = 0.0, logo=None,
           channel: str = "", badge: str = "", accent: str = "#FFD23F") -> Path:
    accent_rgb = ImageColor.getrgb(accent or "#FFD23F")
    img = shade(video_frame(Path(video), frame_s)).convert("RGBA")
    draw = ImageDraw.Draw(img)

    top = MARGIN
    if logo and Path(logo).exists():
        top = paste_logo(img, Path(logo))
    elif channel:
        _, h = pill(draw, (MARGIN, MARGIN), channel, 34, (255, 255, 255, 235), INK)
        top = MARGIN + h

    bottom = H - MARGIN
    if badge:
        size = 30
        asc, desc = font(size).getmetrics()
        bh = asc + desc + 2 * int(size * 0.28)
        pill(draw, (MARGIN, H - MARGIN - bh), badge.upper(), size, accent_rgb, INK)
        bottom = H - MARGIN - bh - 24

    text_w = int(W * 0.60)
    room = bottom - top - 40
    for start in range(168, 55, -8):
        h_size, h_lines = fit(headline.upper(), text_w, 3, start, 56)
        s_size, s_lines = (fit(subline.upper(), text_w, 2, max(34, int(h_size * 0.5)), 30) if subline else (0, []))
        h_lh, s_lh = int(h_size * 1.02), int(s_size * 1.08)
        block = h_lh * len(h_lines) + (int(s_size * 0.35) + s_lh * len(s_lines) if s_lines else 0)
        if block <= room:
            break
    y = top + max(28, ((bottom - top) - block) // 2)

    stroke = max(3, h_size // 18)
    for line in h_lines:
        draw.text((MARGIN + 4, y + 6), line, font=font(h_size), fill=(0, 0, 0, 150))
        draw.text((MARGIN, y), line, font=font(h_size), fill="white", stroke_width=stroke, stroke_fill=INK)
        y += h_lh
    if s_lines:
        y += int(s_size * 0.35)
        for line in s_lines:
            draw.text((MARGIN, y), line, font=font(s_size), fill=accent_rgb,
                      stroke_width=max(2, s_size // 16), stroke_fill=INK)
            y += s_lh

    out = Path(out)
    img.convert("RGB").save(out, "PNG", optimize=True)
    return out


def main() -> None:
    p = argparse.ArgumentParser(description="Render a YouTube thumbnail from a video frame")
    p.add_argument("video")
    p.add_argument("out")
    p.add_argument("--headline", required=True)
    p.add_argument("--subline", default="")
    p.add_argument("--frame", type=float, default=0.0, help="Seconds into the video for the background")
    p.add_argument("--logo")
    p.add_argument("--channel", default="")
    p.add_argument("--badge", default="")
    p.add_argument("--accent", default="#FFD23F")
    a = p.parse_args()
    print(render(a.video, a.out, a.headline, a.subline, a.frame, a.logo, a.channel, a.badge, a.accent))


if __name__ == "__main__":
    main()
