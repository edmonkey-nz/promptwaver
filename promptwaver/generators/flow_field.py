"""Flow field — advected particle streaks. Reads as flowing water, drifting
cloud, or wind depending on turbulence and speed. Curved strokes suit galvos
well.

The field is a cheap pseudo-noise built from summed sines (no external noise
dependency). Particles are re-seeded on a fixed grid each frame and traced for
a few steps, so the figure stays a stable point budget regardless of time.
"""

from __future__ import annotations

import numpy as np

from ..color import hue_rgb as _hue_to_rgb
from ..geometry import Path, Frame
from .base import Generator, register


def _field(x, y, t, turb):
    # summed-sine flow angle; turbulence adds higher-frequency curl
    a = np.sin(1.3 * x + 0.7 * t) + np.cos(1.7 * y - 0.5 * t)
    a += turb * (np.sin(3.1 * y + 1.1 * t) + np.cos(2.9 * x - 0.9 * t))
    return a * np.pi


@register("flow_field")
class FlowField(Generator):
    description = "drifting particle streaks — water, cloud, smoke, wind"
    defaults = dict(
        particles=48,      # seeds per axis is derived; total ~particles
        steps=10,          # points per streak
        step_len=0.06,     # stroke length per step
        speed=0.2,         # time advance rate
        turbulence=0.35,
        hue=0.55,          # 0..1 mapped to an RGB below
    )
    # `particles` is floored at 9 because the seed grid is sqrt(n) per axis —
    # below that the grid collapses to a single column and the field reads as
    # a line rather than a flow.
    param_meta = {
        "particles": (9, 400, 1),
        "steps": (2, 40, 1),
        "step_len": (0.01, 0.25, 0.005),
        "speed": (0.0, 1.5, 0.01),
        "turbulence": (0.0, 1.5, 0.01),
        "hue": (0.0, 1.0, 0.01),
    }

    def render(self, t: float, p: dict) -> Frame:
        n = max(4, int(p["particles"]))
        cols = int(np.sqrt(n))
        gx, gy = np.meshgrid(
            np.linspace(-0.9, 0.9, cols),
            np.linspace(-0.9, 0.9, cols),
        )
        xs = gx.ravel()
        ys = gy.ravel()
        tt = t * p["speed"]
        col = _hue_to_rgb(p["hue"])
        steps = max(2, int(p["steps"]))
        out: Frame = []
        for x0, y0 in zip(xs, ys):
            pts = np.empty((steps, 2), np.float32)
            x, y = float(x0), float(y0)
            for i in range(steps):
                pts[i] = (x, y)
                ang = _field(x, y, tt, p["turbulence"])
                x += np.cos(ang) * p["step_len"]
                y += np.sin(ang) * p["step_len"]
            out.append(Path(pts, col))
        return out
