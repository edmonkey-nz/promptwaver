"""Shared colour helpers.

Three generators each carried their own byte-identical copy of the same hue
ramp (`attractor._hue`, `ripples._hue`, `flow_field._hue_to_rgb`). They live
here now, so a change to how this instrument maps hue happens once.

Two distinct jobs:

  `hue_rgb(h)`   the original ramp — one scalar `hue` param to an RGB. Cheap,
                 fully saturated, and what every legacy generator means by
                 "hue". Kept exactly as it was so no saved scene shifts colour.

  `hue_shift(rgb, d)`  rotate an AUTHORED colour's hue while keeping its
                 saturation and value. This is what the neon bands in a 2D
                 pattern need: successive offset copies of one motif stepping
                 through the spectrum without any of them turning into a
                 different brightness than the colour the scene asked for.
"""

from __future__ import annotations

import colorsys


def hue_rgb(h: float) -> tuple[float, float, float]:
    """0..1 hue -> a saturated RGB. Piecewise-linear, no saturation control.

    Deliberately NOT colorsys.hsv_to_rgb: this ramp's channel overlap is what
    gives the legacy generators their particular look, and every scene in the
    library was authored against it.
    """
    h = h % 1.0
    r = max(0.0, 1 - abs(h - 0.0) * 3, 1 - abs(h - 1.0) * 3)
    g = max(0.0, 1 - abs(h - 0.33) * 3)
    b = max(0.0, 1 - abs(h - 0.66) * 3)
    return (min(1.0, r), min(1.0, g), min(1.0, b))


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
