"""
House style for the channel's Manim videos: one palette, one layout, and a few building blocks that
make videos easy to watch along to. Manim Studio copies this file (plus house_style.json with the
channel's accent colour) into each video folder; script.py starts with

    from manim import *
    from house_style import *

Palette and type
    BG, INK, SOFT, PANEL, ACCENT, GOOD, WARN      colours
    VAR_COLORS                                    x, y, z, parameters... always the same colour
    TITLE_SIZE, BODY_SIZE, SMALL_SIZE             font sizes
    SAFE_BOTTOM                                   keep content above this y (captions, player bar)

Building blocks (every helper that plays animations takes run_time and uses exactly that long)
    mt("3 x + 2 y = a")              MathTex split at spaces (outside braces) and colour-coded
    paint(mob)                       colour-code an existing MathTex built from separate parts
    title_card("Title", "subtitle")  big title with an accent bar
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

from manim import (DOWN, LEFT, ORIGIN, RIGHT, UL, UP, UR, Arc, Circle, FadeIn, FadeOut, GrowFromCenter,
                   ImageMobject, Indicate, Integer, Line, MathTex, RoundedRectangle, SurroundingRectangle, Tex, Text,
                   ValueTracker, VGroup, always_redraw, config, linear)

_settings = {}
try:
    _settings = json.loads((Path(__file__).with_name("house_style.json")).read_text())
except (OSError, ValueError):
    pass

BG = "#0F1419"
INK = "#F4F1EA"
SOFT = "#8A94A6"
PANEL = "#1A2430"
ACCENT = _settings.get("accent", "#FFD23F")
GOOD = "#7BD389"
WARN = "#FF6B6B"

VAR_COLORS = {
    "x": "#58C4DD", "y": "#F2C14E", "z": "#F78272",
    "a": "#7BD389", "k": "#7BD389", "m": "#7BD389", "p": "#7BD389",
    "t": "#C792EA", r"\lambda": "#C792EA", r"\mu": "#C792EA", "s": "#C792EA", r"\theta": "#C792EA",
}
VAR_COLORS.update(_settings.get("var_colors", {}))

TITLE_SIZE, BODY_SIZE, SMALL_SIZE = 56, 40, 30
MARGIN = 0.45
SAFE_BOTTOM = -config.frame_height / 2 + 1.1

config.background_color = BG


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
def title_card(title: str, subtitle: str = "", width: float = 11.0):
    head = Tex(title, font_size=TITLE_SIZE, color=INK)
    if head.width > width:
        head.scale_to_fit_width(width)
    bar = Line(LEFT, RIGHT, color=ACCENT, stroke_width=6).set_width(min(head.width, 4.5))
    bar.next_to(head, DOWN, buff=0.25)
    group = VGroup(head, bar)
    if subtitle:
        sub = Tex(subtitle, font_size=SMALL_SIZE + 4, color=SOFT).next_to(bar, DOWN, buff=0.3)
        group.add(sub)
    return group.move_to(ORIGIN)


def chip(text: str, color=None, font_size: float = 24):
    label = Text(text, font_size=font_size, color=BG if color is None else INK, weight="BOLD")
    box = RoundedRectangle(corner_radius=0.16, width=label.width + 0.45, height=label.height + 0.3,
                           stroke_width=0, fill_color=color or ACCENT, fill_opacity=1)
    return VGroup(box, label.move_to(box))


def step_chip(text: str, n: int | None = None):
    """'STEP 2 · Row reduce' label for the top-right corner."""
    words = f"STEP {n} · {text}" if n is not None else text
    return chip(words.upper(), PANEL, 22).to_corner(UR, buff=MARGIN)


def callout(text: str, color=WARN, font_size: float = 26):
    return chip(text, color, font_size)


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
                                 stroke_color=SOFT, stroke_width=1.5, fill_color=PANEL, fill_opacity=0.94)
        tab = chip(label, ACCENT, 20)
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
    ring_bg = Circle(radius=0.42, stroke_color=PANEL, stroke_width=8)

    def ring():
        frac = max(0.001, tracker.get_value() / seconds)
        return Arc(radius=0.42, start_angle=1.5708, angle=-6.2832 * frac, arc_center=ring_bg.get_center(),
                   stroke_color=ACCENT, stroke_width=8)

    arc = always_redraw(ring)
    count = always_redraw(lambda: Integer(max(0, int(tracker.get_value() + 0.999)), font_size=34, color=INK)
                          .move_to(ring_bg))
    label = chip(text, ACCENT, 24).next_to(ring_bg, LEFT, buff=0.25)
    badge = VGroup(label, ring_bg)
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

