"""A receding ground grid — the floor you float over. Longitudinal rails plus
transverse rungs on the y=0 plane, wrapping in Z so it never ends. Gives the eye
a strong sense of motion and depth, which is most of the immersion.
"""

from __future__ import annotations

import numpy as np

from ..geometry import Path3D
from .base import Generator3D, register


@register("ground_grid")
class GroundGrid(Generator3D):
    field_depth = 16.0
    defaults = dict(
        width=6.0,       # half-width of the floor
        rails=7,         # longitudinal lines
        rungs=16,        # transverse lines across the field
        hue=0.55,
    )

    def render3d(self, t: float, p: dict):
        w = p["width"]
        rails = int(p["rails"])
        rungs = int(p["rungs"])
        D = self.field_depth
        out = []
        # longitudinal rails run the full field depth (one long stroke each)
        zs = np.linspace(0.0, D, 40, dtype=np.float32)
        for xi in np.linspace(-w, w, rails):
            pts = np.stack([np.full_like(zs, xi), np.zeros_like(zs), zs], axis=1)
            out.append(Path3D(pts, (0.5, 0.9, 1.0), lod=0))
        # transverse rungs, wrapping in Z (higher LOD so they thin out when far)
        xs = np.linspace(-w, w, 12, dtype=np.float32)
        for k in range(rungs):
            z = (k / rungs) * D
            pts = np.stack([xs, np.zeros_like(xs), np.full_like(xs, z)], axis=1)
            out.append(Path3D(pts, (0.3, 0.7, 1.0), lod=1))
        return out
