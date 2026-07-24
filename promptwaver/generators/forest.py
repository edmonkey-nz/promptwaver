"""A forest to float through. Trees are instanced at fixed (x, z) positions off
to either side of the flight path; the camera-relative Z wrap in the projector
makes them recede and re-approach endlessly. Near trees draw a trunk plus a few
branches; distant trees (LOD) draw just a trunk, protecting the point budget.

Trees are simple by design — a laser reads a suggestion of a tree better than a
botanically correct one, and strokes are precious.
"""

from __future__ import annotations

import numpy as np

from ..geometry import Path3D
from .base import Generator3D, register


def _tree(x, z, height, seed):
    rng = np.random.default_rng(seed)
    trunk = np.array([[x, 0.0, z], [x, height, z]], np.float32)
    parts = [Path3D(trunk, (0.2, 0.8, 0.4), lod=0)]
    # a few branches as short strokes near the top (higher LOD -> dropped when far)
    n = rng.integers(3, 6)
    for _ in range(n):
        h = height * rng.uniform(0.55, 0.95)
        ang = rng.uniform(0, 2 * np.pi)
        ln = rng.uniform(0.25, 0.5)
        tip = [x + np.cos(ang) * ln, h + rng.uniform(0.1, 0.3), z + np.sin(ang) * ln]
        parts.append(Path3D(np.array([[x, h, z], tip], np.float32),
                            (0.3, 0.9, 0.5), lod=2))
    return parts


@register("forest")
class Forest(Generator3D):
    field_depth = 18.0
    defaults = dict(
        trees=14,
        spread=5.0,      # how far left/right trees sit from the path
        clearance=1.2,   # keep a corridor clear so you fly *between* trees
        hue=0.33,
        sway=0.04,       # gentle canopy sway
    )

    def render3d(self, t: float, p: dict):
        n = int(p["trees"])
        D = self.field_depth
        rng = np.random.default_rng(1234)   # stable layout
        out = []
        for i in range(n):
            side = -1 if i % 2 == 0 else 1
            x = side * (p["clearance"] + rng.uniform(0, p["spread"]))
            z = (i / n) * D + rng.uniform(-0.4, 0.4)
            height = rng.uniform(1.4, 2.6)
            # gentle sway animates the whole tree horizontally
            x += np.sin(t * 0.5 + i) * p["sway"]
            out.extend(_tree(float(x), float(z), float(height), seed=i))
        return out
