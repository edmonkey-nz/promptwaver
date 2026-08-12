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
        glow:   0..1 per-stroke bloom, MONITOR ONLY. The laser has no such
                control — its per-point intensity channel is on/off (see
                output/ilda.py), so brightness there is carried by RGB. This
                rides alongside the geometry rather than in the display-filter
                block because it is authored per shape by the scene, unlike
                the global glow slider which applies to the whole frame.
                Default 0 means every existing generator is unaffected.
    """

    points: np.ndarray
    color: tuple[float, float, float] = (1.0, 1.0, 1.0)
    closed: bool = False
    glow: float = 0.0

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


def test_pattern_frame() -> Frame:
    """A static keystone-calibration pattern: outer border, corner-to-corner
    diagonals, a centre crosshair, and an inner rule-of-thirds-ish box —
    straight lines and right angles make it easy to see exactly what a
    keystone correction is doing to the frame's geometry, in either
    direction, at a glance. Bright white/cyan so it's unambiguous against
    whatever palette a scene happens to use.

    Deliberately independent of any generator/camera — this bypasses the
    live scene entirely (see Engine._loop's test_pattern_on handling, which
    substitutes this for BOTH the real output and self._last_frame, so
    every viewer — laser, visualiser, output windows — gets the same
    pattern through their normal rendering path, each with its own
    keystone/flip already applied) so calibration doesn't depend on
    picking a scene with the right geometry to see the effect clearly.
    """
    white = (1.0, 1.0, 1.0)
    cyan = (0.3, 0.9, 1.0)
    b = 0.96   # inset slightly from the exact [-1,1] edge: a border stroke
               # drawn exactly ON the edge gets half its width clipped by the
               # canvas/DAC boundary, rendering as a near-invisible sliver
    return [
        Path(np.array([[-b, -b], [b, -b], [b, b], [-b, b], [-b, -b]], np.float32), white, closed=True),
        Path(np.array([[-0.5, -0.5], [0.5, -0.5], [0.5, 0.5], [-0.5, 0.5], [-0.5, -0.5]], np.float32),
             cyan, closed=True),
        Path(np.array([[-1, 0], [1, 0]], np.float32), white),
        Path(np.array([[0, -1], [0, 1]], np.float32), white),
        Path(np.array([[-1, -1], [1, 1]], np.float32), cyan),
        Path(np.array([[-1, 1], [1, -1]], np.float32), cyan),
    ]
