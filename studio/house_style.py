"""
House style for the channel's Manim videos: one palette, one layout, and a few building blocks that
make videos easy to watch along to. Manim Studio copies this file (plus house_style.json with the
channel's accent colour) into each video folder; script.py starts with

    from manim import *
    from house_style import *

The channel's brand (the Mr. Liam IB Maths logo and website): near-black ground, cream ink, sage green
and gold; headlines in Newsreader (serif), body text in Inter, labels in letter-spaced gold Montserrat
capitals. The fonts ship in brand-fonts/ next to this file and are registered on import.

Palette and type
    BG, INK, SOFT, MUTED, PANEL, LINE, GRID       ground, cream ink, secondary text, panels, rules
    SAGE, SAGE_DEEP, GOLD, BLUE, CLAY, LILAC      brand colours and their companions
    ACCENT (= GOLD), GOOD (= SAGE), WARN          highlight, right, careful
    VAR_COLORS                                    x, y, z, parameters... always the same colour
    TITLE_SIZE, BODY_SIZE, SMALL_SIZE             font sizes
    SAFE_BOTTOM                                   keep content above this y (captions, player bar)

Words on screen (never plain Text/Tex, which look like any other Manim video)
    heading("Stationary points")     Newsreader serif headline, cream
    prose("Set the derivative to 0") Inter body text (wraps at `width`)
    label("Topic 5 · Calculus")      letter-spaced Montserrat capitals in gold (eyebrows, tags)

Building blocks (every helper that plays animations takes run_time and uses exactly that long)
    mt("3 x + 2 y = a")              MathTex split at spaces (outside braces) and colour-coded
    paint(mob)                       colour-code an existing MathTex built from separate parts
    brand_axes(...)                  Axes in the brand style; plot curves in SAGE, highlight in GOLD
    title_card("Title", "subtitle", eyebrow="Topic 5 · Calculus")  serif title with a gold rule
    step_chip("Row reduce", 2)       small "STEP 2 · Row reduce" label, top right
    QuestionPin(question_mob)        compact copy of the question, pinned top left
    spotlight(mob) / Spotlight       soft accent box behind what is being talked about
    emphasise(mob)                   brief pulse in the accent colour (an animation)
    result_box(mob)                  accent frame around a final answer
    callout("Watch out!", WARN)      small rounded label for a warning or tip
    pause_and_try(self, seconds)     "PAUSE & TRY" badge with a countdown ring
    logo_open(self, seconds)         channel logo (channel-logo.png) for the greeting
"""

import json
from pathlib import Path

from manim import (DOWN, LEFT, ORIGIN, RIGHT, UL, UP, UR, Arc, Axes, Circle, FadeIn, FadeOut, GrowFromCenter,
                   ImageMobject, Indicate, Integer, Line, MarkupText, MathTex, RoundedRectangle, SurroundingRectangle,
                   Text, ValueTracker, VGroup, always_redraw, config, linear)

_settings = {}
try:
    _settings = json.loads((Path(__file__).with_name("house_style.json")).read_text())
except (OSError, ValueError):
    pass

# brand palette (from the logo) and the supporting shades the website uses
BG = "#0D1317"          # near-black ground
INK = "#F3F1EB"         # cream
SOFT = "#9AA19D"        # secondary text
MUTED = "#B7BCB7"       # quieter body text
PANEL = "#141B20"       # cards and pinned panels
LINE = "#232D34"        # panel borders, rules
GRID = "#1E272D"        # background grid
AXIS = "#5A6870"        # axes and ticks
SAGE = "#72AC8B"
SAGE_DEEP = "#3F7558"
GOLD = "#CCAB57"
BLUE = "#7FA6C9"        # companions in the same muted, mid-light family
CLAY = "#D98B6C"
LILAC = "#AE9CCB"
ACCENT = GOLD if _settings.get("accent", GOLD).upper() in ("#FFD23F", "") else _settings["accent"]
GOOD = SAGE
WARN = "#E07A5F"

VAR_COLORS = {
    "x": SAGE, "y": GOLD, "z": BLUE,
    "a": CLAY, "k": CLAY, "m": CLAY, "p": CLAY,
    "t": LILAC, r"\lambda": LILAC, r"\mu": LILAC, "s": LILAC, r"\theta": LILAC,
}
VAR_COLORS.update(_settings.get("var_colors", {}))

SERIF, SERIF_MEDIUM, SANS, SANS_BOLD = "Newsreader", "Newsreader Medium", "Inter", "Inter SemiBold"
CAPS = "Montserrat SemiBold"
TITLE_SIZE, BODY_SIZE, SMALL_SIZE = 56, 40, 30
MARGIN = 0.45
SAFE_BOTTOM = -config.frame_height / 2 + 1.1

config.background_color = BG


def _register_fonts() -> None:
    try:
        import manimpango
    except ImportError:
        return
    for folder in (Path(__file__).with_name("brand-fonts"), Path(__file__).resolve().parent / "fonts" / "brand"):
        for ttf in sorted(folder.glob("*.ttf")) if folder.exists() else []:
            try:
                manimpango.register_font(str(ttf))
            except Exception:  # noqa: BLE001 — a missing font falls back to the system's
                pass


_register_fonts()


# ------------------------------------------------------------------- words --
def heading(text: str, font_size: float = TITLE_SIZE, color=None, **kwargs):
    """Newsreader serif headline (cream by default)."""
    return Text(text, font=SERIF_MEDIUM, font_size=font_size, color=color or INK, **kwargs)


def prose(text: str, font_size: float = 30, color=None, width: float | None = None, **kwargs):
    """Inter body text; wraps to `width` (scene units) when given."""
    mob = Text(text, font=SANS, font_size=font_size, color=color or MUTED, line_spacing=0.9, **kwargs)
    if width and mob.width > width:
        words, lines, cur = text.split(), [], ""
        for w in words:
            trial = f"{cur} {w}".strip()
            if cur and Text(trial, font=SANS, font_size=font_size).width > width:
                lines.append(cur)
                cur = w
            else:
                cur = trial
        lines.append(cur)
        mob = Text("\n".join(lines), font=SANS, font_size=font_size, color=color or MUTED, line_spacing=0.9, **kwargs)
    return mob


def label(text: str, font_size: float = 20, color=None, spacing: float = 0.22):
    """Letter-spaced Montserrat capitals (gold by default), like the website's eyebrows and tags."""
    pts = int(font_size * spacing * 1024 * 0.2)   # Pango units at Manim's internal text scale
    safe = text.upper().replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return MarkupText(f'<span letter_spacing="{pts}">{safe}</span>', font=CAPS, font_size=font_size,
                      color=color or ACCENT)


def brand_axes(x_range=(-1, 6, 1), y_range=(-1, 4, 1), x_length=10, y_length=5.5, **kwargs):
    """Axes in the brand style: soft grey-blue lines and ticks, Inter numbers."""
    opts = {"color": AXIS, "stroke_width": 2.5, "include_tip": False}
    opts.update(kwargs.pop("axis_config", {}))
    return Axes(x_range=list(x_range), y_range=list(y_range), x_length=x_length, y_length=y_length,
                axis_config=opts, **kwargs)


# ------------------------------------------------------------------ maths text --
def _split_top_level(tex: str) -> list[str]:
    parts, depth, cur = [], 0, ""
    for ch in tex:
        depth += ch == "{"
        depth -= ch == "}"
        if ch.isspace() and depth == 0:
            if cur:
                parts.append(cur)
            cur = ""
        else:
            cur += ch
    if cur:
        parts.append(cur)
    return parts


def paint(mob, colors: dict | None = None):
    """Colour every part of a MathTex whose text is exactly a variable in VAR_COLORS (or `colors`)."""
    table = {**VAR_COLORS, **(colors or {})}
    for sub in getattr(mob, "submobjects", []):
        key = getattr(sub, "tex_string", None)
        if isinstance(key, str) and key.strip() in table:
            sub.set_color(table[key.strip()])
        elif getattr(sub, "submobjects", None) and not hasattr(sub, "tex_string"):
            paint(sub, colors)
    return mob


def mt(tex: str, font_size: float = BODY_SIZE, colors: dict | None = None, **kwargs):
    """MathTex from space-separated tokens ('3 x + 2 y = 7'), colour-coded. Spaces inside {...} are kept,
    so '\\frac{a + 1}{2}' stays one token. Keep \\left( ... \\right) out of mt (use MathTex + paint)."""
    parts = _split_top_level(tex)
    return paint(MathTex(*parts, font_size=font_size, color=INK, **kwargs), colors)


# --------------------------------------------------------------- layout pieces --
def title_card(title: str, subtitle: str = "", width: float = 11.0, eyebrow: str = ""):
    """Serif title with a gold rule; optional gold caps eyebrow above and a quieter subtitle below."""
    head = heading(title)
    if head.width > width:
        head.scale_to_fit_width(width)
    bar = Line(LEFT, RIGHT, color=ACCENT, stroke_width=4).set_width(min(head.width, 3.2))
    bar.next_to(head, DOWN, buff=0.3)
    group = VGroup(head, bar)
    if subtitle:
        group.add(prose(subtitle, font_size=30, color=SOFT).next_to(bar, DOWN, buff=0.3))
    if eyebrow:
        group.add(label(eyebrow, color=SAGE).next_to(head, UP, buff=0.35))
    return group.move_to(ORIGIN)


def chip(text: str, color=None, font_size: float = 20, ink=None):
    """Rounded tag with letter-spaced caps. Filled chips use dark text; dark chips use gold text."""
    fill = color or ACCENT
    dark = fill in (PANEL, BG, LINE, GRID)
    words = label(text, font_size=font_size, color=ink or (ACCENT if dark else BG), spacing=0.14)
    box = RoundedRectangle(corner_radius=0.14, width=words.width + 0.5, height=words.height + 0.32,
                           stroke_width=1.2 if dark else 0, stroke_color=LINE, fill_color=fill, fill_opacity=1)
    return VGroup(box, words.move_to(box))


def step_chip(text: str, n: int | None = None):
    """'STEP 2 · Row reduce' label for the top-right corner."""
    words = f"Step {n} · {text}" if n is not None else text
    return chip(words, PANEL, 18).to_corner(UR, buff=MARGIN)


def callout(text: str, color=WARN, font_size: float = 24):
    """A short warning or tip in a rounded tag, in sentence case (maths stays readable)."""
    words = Text(text, font=SANS_BOLD, font_size=font_size, color=BG)
    box = RoundedRectangle(corner_radius=0.14, width=words.width + 0.5, height=words.height + 0.34,
                           stroke_width=0, fill_color=color, fill_opacity=1)
    return VGroup(box, words.move_to(box))


class QuestionPin(VGroup):
    """A compact copy of the question, kept in the top-left corner while the working happens."""

    def __init__(self, content, max_width: float = 5.4, max_height: float = 1.7, label: str = "Q", **kwargs):
        super().__init__(**kwargs)
        body = content.copy()
        if body.width > max_width:
            body.scale_to_fit_width(max_width)
        if body.height > max_height:
            body.scale_to_fit_height(max_height)
        panel = RoundedRectangle(corner_radius=0.18, width=body.width + 0.6, height=body.height + 0.5,
                                 stroke_color=LINE, stroke_width=1.5, fill_color=PANEL, fill_opacity=0.96)
        tab = chip(label, ACCENT, 16)
        body.move_to(panel)
        tab.move_to(panel.get_corner(UL) + RIGHT * (tab.width / 2 + 0.2))   # a tag sitting on the top edge
        self.add(panel, body, tab)
        self.to_corner(UL, buff=MARGIN).shift(DOWN * 0.12)


class Spotlight(VGroup):
    """Soft accent box behind a mobject: shows which term or step the narration is talking about."""

    def __init__(self, mob, color=None, buff: float = 0.14, **kwargs):
        super().__init__(**kwargs)
        box = SurroundingRectangle(mob, buff=buff, corner_radius=0.12, color=color or ACCENT,
                                   stroke_width=2.5, fill_color=color or ACCENT, fill_opacity=0.14)
        self.add(box)


def spotlight(mob, color=None, buff: float = 0.14):
    return Spotlight(mob, color, buff)


def emphasise(mob, run_time: float = 0.8):
    return Indicate(mob, color=ACCENT, scale_factor=1.06, run_time=run_time)


def result_box(mob, color=None, buff: float = 0.2):
    return SurroundingRectangle(mob, buff=buff, corner_radius=0.1, color=color or ACCENT, stroke_width=4)


# ------------------------------------------------------------ timed sequences --
def pause_and_try(scene, seconds: float, text: str = "PAUSE & TRY", position=None):
    """Badge with a countdown ring for a 'pause the video and try it' moment. Takes exactly `seconds`."""
    seconds = max(1.6, float(seconds))
    tracker = ValueTracker(seconds)
    ring_bg = Circle(radius=0.42, stroke_color=LINE, stroke_width=8, fill_color=PANEL, fill_opacity=1)

    def ring():
        frac = max(0.001, tracker.get_value() / seconds)
        return Arc(radius=0.42, start_angle=1.5708, angle=-6.2832 * frac, arc_center=ring_bg.get_center(),
                   stroke_color=ACCENT, stroke_width=8)

    arc = always_redraw(ring)
    count = always_redraw(lambda: Integer(max(0, int(tracker.get_value() + 0.999)), font_size=34, color=INK)
                          .move_to(ring_bg))
    tag = chip(text, ACCENT, 18).next_to(ring_bg, LEFT, buff=0.25)
    badge = VGroup(tag, ring_bg)
    if position is not None:
        badge.move_to(position)
    else:
        badge.to_edge(RIGHT, buff=MARGIN).shift(DOWN * 1.2)
    arc.update()
    count.update()
    scene.play(FadeIn(badge, shift=UP * 0.2), FadeIn(arc), FadeIn(count), run_time=0.4)
    scene.play(tracker.animate.set_value(0), run_time=seconds - 0.8, rate_func=linear)
    scene.play(FadeOut(badge), FadeOut(arc), FadeOut(count), run_time=0.4)


def logo_open(scene, seconds: float, path: str = "channel-logo.png", width: float | None = None):
    """Channel logo, centred, for the greeting line. Takes exactly `seconds`; returns the logo
    (fade it out or shrink it with the next animation)."""
    width = width or config.frame_width / 3
    if Path(path).exists():
        logo = ImageMobject(path)
        logo.scale_to_fit_width(width)
        if logo.height > config.frame_height * 0.45:
            logo.scale_to_fit_height(config.frame_height * 0.45)
    else:
        logo = title_card(_settings.get("channel", ""))
    grow = min(0.9, seconds / 3)
    scene.play(GrowFromCenter(logo) if isinstance(logo, ImageMobject) else FadeIn(logo), run_time=grow)
    scene.wait(max(0, seconds - grow))
    return logo

