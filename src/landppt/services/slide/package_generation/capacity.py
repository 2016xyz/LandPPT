"""Estimate how many characters each text slot holds at the smallest accepted size.

Declared ``max_chars`` is a hard ceiling, but AI-designed components often declare far
more than their boxes can show. Writers are given the measured budget so pages fit on
the first render instead of needing slow model repairs.
"""

from __future__ import annotations

import functools
import math

from ..svg_page.constants import (
    BOX_HEIGHT_ATTR,
    BOX_WIDTH_ATTR,
    FONT_FLOOR,
    FONT_SHRINK_STEP,
    LINE_HEIGHT,
    MAX_LINES_ATTR,
)
from ..svg_page.metrics import get_text_measurer
from ..svg_page.sanitize import parse_svg

#: Line-end waste from punctuation that sticks to the previous character.
WRAP_SAFETY = 0.9
#: Mirrors the renderer: text may not shrink below this size or 75% of the original.
MIN_READABLE_SIZE = 18


def _number(node, attribute, *, positive=True):
    try:
        value = float(node.get(attribute))
    except (TypeError, ValueError):
        return None
    if not math.isfinite(value) or (value <= 0 if positive else value < 0):
        return None
    return value


def _fits(count, box, max_lines, below, ascent, descent):
    """Mirror the layout loop: it keeps the largest size whose lines fit the box."""
    x, y, size, width, height = box
    floor = max(MIN_READABLE_SIZE, size * 0.75)
    current = size
    while True:
        # CJK glyphs are 1em wide; Latin text is narrower, so this is conservative.
        per_line = int(width // current * WRAP_SAFETY)
        lines = math.ceil(count / per_line) if per_line else math.inf
        fits = current + (lines - 1) * current * LINE_HEIGHT <= height + 0.5 and (
            not max_lines or lines <= max_lines
        )
        if fits or current - FONT_SHRINK_STEP < FONT_FLOOR:
            break
        current -= FONT_SHRINK_STEP
    if not fits or current < floor:
        return False
    bottom = y + (lines - 1) * current * LINE_HEIGHT + descent * current
    return all(bottom <= top for top in below)


@functools.lru_cache(maxsize=256)
def _measure(svg, slots):
    measurer = get_text_measurer()
    ascent, descent = measurer.ascent(1.0), measurer.descent(1.0)
    nodes = {n.get("id"): n for n in parse_svg(svg).iter() if n.get("id")}
    boxes = {}
    for slot in slots:
        node = nodes.get(slot.node_id)
        if slot.kind != "text" or node is None:
            continue
        values = (
            _number(node, "x", positive=False),
            _number(node, "y", positive=False),
            _number(node, "font-size"),
            _number(node, BOX_WIDTH_ATTR),
            _number(node, BOX_HEIGHT_ATTR),
        )
        if None not in values:
            boxes[slot.field] = (values, _number(node, MAX_LINES_ATTR), slot.max_chars)
    limits = {}
    for field, (box, max_lines, declared) in boxes.items():
        x, y, _, width, _ = box
        # Generated designs may let a box reach into the slot below it; short text
        # keeps a large size, wraps further and collides with that slot's text.
        below = [
            oy - ascent * osize
            for other, ((ox, oy, osize, owidth, _), _, _) in boxes.items()
            if other != field and oy > y and ox < x + width and x < ox + owidth
        ]
        # Shorter copy must fit too: the size choice makes this non-monotonic.
        count = 0
        while count < declared and _fits(
            count + 1, box, max_lines, below, ascent, descent
        ):
            count += 1
        limits[field] = max(1, count)
    return limits


def slot_limits(component) -> dict[str, int]:
    """Field -> writing budget, never above the declared ``max_chars``."""
    try:
        measured = _measure(component.svg, component.slots)
    except ValueError:
        measured = {}
    return {
        slot.field: min(slot.max_chars, measured.get(slot.field, slot.max_chars))
        for slot in component.slots
        if slot.kind == "text"
    }
