"""Shared colour helpers.

  `hue_shift(rgb, d)`  rotate an AUTHORED colour's hue while keeping its
                 saturation and value. This is what the neon bands in a 2D
                 pattern need: successive offset copies of one motif stepping
                 through the spectrum without any of them turning into a
                 different brightness than the colour the scene asked for.
"""

from __future__ import annotations

import colorsys


def hue_shift(rgb, d: float) -> tuple[float, float, float]:
    """Rotate `rgb`'s hue by `d` turns, preserving saturation and value.

    A grey or black input has no hue to rotate, so it is returned untouched
    rather than being invented into a colour.
    """
    if not d:
        return tuple(float(c) for c in rgb)
    r, g, b = (max(0.0, min(1.0, float(c))) for c in rgb)
    h, s, v = colorsys.rgb_to_hsv(r, g, b)
    if s <= 1e-6:
        return (r, g, b)
    return colorsys.hsv_to_rgb((h + d) % 1.0, s, v)
