"""De Jong attractor — a slowly-evolving organic swirl. Good for smoke-like or
meditative drifting scenes. The four coefficients breathe over time so the
figure is never static.

Rendered as one continuous polyline (the attractor orbit), which the galvos can
trace without blanking — efficient and smooth.
"""

from __future__ import annotations

import numpy as np

from ..color import hue_rgb as _hue
from ..geometry import Path, Frame
from .base import Generator, register


@register("attractor")
class DeJong(Generator):
    description = "de Jong attractor — a slow organic swirl, smoke or nebula"
    defaults = dict(
        points=600,
        a=1.4, b=-2.3, c=2.4, d=-2.1,
        drift=0.15,     # how much the coefficients wander over time
        speed=0.1,
        hue=0.72,
        scale=0.42,
    )
    # The four coefficients need to swing either side of zero — the figure's
    # whole character lives in that range, and inference would clamp b/d to
    # their own negative half.
    param_meta = {
        "points": (64, 2000, 1),
        "a": (-3.0, 3.0, 0.01), "b": (-3.0, 3.0, 0.01),
        "c": (-3.0, 3.0, 0.01), "d": (-3.0, 3.0, 0.01),
        "drift": (0.0, 1.0, 0.01),
        "speed": (0.0, 1.0, 0.01),
        "hue": (0.0, 1.0, 0.01),
        "scale": (0.05, 1.0, 0.01),
    }

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
