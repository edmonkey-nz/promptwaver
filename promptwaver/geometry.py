"""Geometry primitives.

Everything the visual engine produces is a list of `Path`s. A Path is a
polyline in normalized coordinates (x, y in [-1, 1]) plus an RGB colour in
[0, 1]. The laser is a *vector* device — we draw strokes, never pixels — so
this is the natural unit throughout PromptWaver.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np


@dataclass
class Path:
    """A single continuous stroke.

    Attributes:
        points: float array of shape (N, 2), coordinates in [-1, 1].
        color:  (r, g, b) in [0, 1]. Applied to the whole stroke for now;
                per-point colour is a later extension.
        closed: if True the renderer joins the last point back to the first.
    """

    points: np.ndarray
    color: tuple[float, float, float] = (1.0, 1.0, 1.0)
    closed: bool = False

    def __post_init__(self) -> None:
        self.points = np.asarray(self.points, dtype=np.float32).reshape(-1, 2)

    def __len__(self) -> int:
        return len(self.points)


@dataclass
class Path3D:
    """A stroke in world space: points of shape (N, 3) in world units, plus a
    base colour. The camera projects these to `Path`s. Used by 3D generators."""

    points: np.ndarray
    color: tuple[float, float, float] = (1.0, 1.0, 1.0)
    closed: bool = False
    lod: int = 0   # 0 = always draw; higher = drop first when far / over budget

    def __post_init__(self) -> None:
        self.points = np.asarray(self.points, dtype=np.float32).reshape(-1, 3)

    def __len__(self) -> int:
        return len(self.points)


Frame = list[Path]  # what a Generator returns for a given instant


def clamp_frame(frame: Frame) -> Frame:
    """Clamp all coordinates into [-1, 1] so nothing drives the galvos past
    their safe travel. Cheap insurance before the frame hits the DAC."""
    for p in frame:
        np.clip(p.points, -1.0, 1.0, out=p.points)
    return frame
