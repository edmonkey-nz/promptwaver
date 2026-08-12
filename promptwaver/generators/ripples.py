"""Ripples — expanding concentric rings that fade and respawn. Reads as
dripping water or rain landing on a pool. Each ring is a closed circle whose
radius grows with age; brightness (via colour scaling) falls as it expands.
"""

from __future__ import annotations

import numpy as np

from ..color import hue_rgb as _hue
from ..geometry import Path, Frame
from .base import Generator, register


@register("ripples")
class Ripples(Generator):
    description = "expanding concentric rings — rain, droplets, sonar"
    defaults = dict(
        rings=5,
        segments=64,
        speed=0.25,     # ring growth rate
        spawn=0.9,      # spacing between ring birth phases
        hue=0.58,
    )
    param_meta = {
        "rings": (1, 24, 1),
        "segments": (8, 128, 1),
        "speed": (0.0, 1.0, 0.01),
        "spawn": (0.1, 2.0, 0.01),
        "hue": (0.0, 1.0, 0.01),
    }

    def render(self, t: float, p: dict) -> Frame:
        n = max(1, int(p["rings"]))
        seg = max(8, int(p["segments"]))
        theta = np.linspace(0, 2 * np.pi, seg, endpoint=True)
        cos, sin = np.cos(theta), np.sin(theta)
        base = _hue(p["hue"])
        out: Frame = []
        for k in range(n):
            # each ring is offset in phase so they emerge in sequence
            age = ((t * p["speed"] + k * p["spawn"]) % 1.0)
            radius = age  # 0 -> 1 across the field
            fade = max(0.0, 1.0 - age)  # dim as it expands
            col = tuple(c * fade for c in base)
            pts = np.stack([cos * radius, sin * radius], axis=1).astype(np.float32)
            out.append(Path(pts, col, closed=True))
        return out
