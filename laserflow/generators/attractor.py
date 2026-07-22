"""De Jong attractor — a slowly-evolving organic swirl. Good for smoke-like or
meditative drifting scenes. The four coefficients breathe over time so the
figure is never static.

Rendered as one continuous polyline (the attractor orbit), which the galvos can
trace without blanking — efficient and smooth.
"""

from __future__ import annotations

import numpy as np

from ..geometry import Path, Frame
from .base import Generator, register


@register("attractor")
class DeJong(Generator):
    defaults = dict(
        points=600,
        a=1.4, b=-2.3, c=2.4, d=-2.1,
        drift=0.15,     # how much the coefficients wander over time
        speed=0.1,
        hue=0.72,
        scale=0.42,
    )

    def render(self, t: float, p: dict) -> Frame:
        tt = t * p["speed"]
        a = p["a"] + p["drift"] * np.sin(0.31 * tt)
        b = p["b"] + p["drift"] * np.cos(0.27 * tt)
        c = p["c"] + p["drift"] * np.sin(0.19 * tt + 1.0)
        d = p["d"] + p["drift"] * np.cos(0.23 * tt + 2.0)

        n = max(16, int(p["points"]))
        xs = np.empty(n, np.float32)
        ys = np.empty(n, np.float32)
        x = y = 0.0
        for i in range(n):
            x, y = np.sin(a * y) - np.cos(b * x), np.sin(c * x) - np.cos(d * y)
            xs[i], ys[i] = x, y
        # de Jong lives roughly in [-2,2]; scale into normalized space
        pts = np.stack([xs, ys], axis=1) * p["scale"] * 0.5
        return [Path(pts, _hue(p["hue"]))]


def _hue(h: float):
    h = h % 1.0
    r = max(0.0, 1 - abs(h - 0.0) * 3, 1 - abs(h - 1.0) * 3)
    g = max(0.0, 1 - abs(h - 0.33) * 3)
    b = max(0.0, 1 - abs(h - 0.66) * 3)
    return (min(1, r), min(1, g), min(1, b))
