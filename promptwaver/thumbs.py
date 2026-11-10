"""Shape thumbnails for the control page's Shapes pane.

Each entry in a layer's `defs` is expanded with the same interpreter the
generator uses (`shapes.build_def` for `world`, `patterns2d.build_def` for
`pattern2d`) and flattened to 2D polylines in [-1,1], fitted to the shape's own
bounding box so a 0.1-unit bolt and a 40-unit hall both fill their tile.

3D defs are seen from a fixed three-quarter view (yawed and looking slightly
down), orthographic, because a straight-on view collapses every box to a square
and every lathe to a rectangle. Each stroke carries a depth shade so the near
side of a wireframe reads in front of the far side.

Answered on request (the `shape_thumbs` websocket command), never in
`state()`: it builds every def once and the reply is tens of kB.
"""

from __future__ import annotations

import numpy as np

from . import shapes as shapes3d
from . import patterns2d

_YAW = np.radians(35.0)
_PITCH = np.radians(-22.0)          # camera above, looking down
_cy, _sy = np.cos(_YAW), np.sin(_YAW)
_cp, _sp = np.cos(_PITCH), np.sin(_PITCH)
# yaw about y, then pitch about x: columns are the view's x, y, z (z = depth)
_VIEW = (np.array([[1, 0, 0], [0, _cp, -_sp], [0, _sp, _cp]])
         @ np.array([[_cy, 0, _sy], [0, 1, 0], [-_sy, 0, _cy]])).T

MAX_POINTS = 1500                   # per shape, after decimation
_FILL = 0.88                        # leave a margin inside the tile


def _closed(pts: np.ndarray, closed: bool) -> np.ndarray:
    if closed and len(pts) > 2 and not np.allclose(pts[0], pts[-1]):
        return np.vstack([pts, pts[:1]])
    return pts


def _thumb_3d(ops) -> tuple[list[np.ndarray], list[float]]:
    paths = shapes3d.build_def(ops if isinstance(ops, list) else [])
    flat, depth = [], []
    for p in paths:
        pts = np.asarray(p.points, np.float64)
        if pts.ndim != 2 or pts.shape[1] != 3 or len(pts) < 2:
            continue
        v = _closed(pts, p.closed) @ _VIEW
        flat.append(v[:, :2])
        depth.append(float(v[:, 2].mean()))
    return flat, depth


def _thumb_2d(d) -> tuple[list[np.ndarray], list[float]]:
    out = [np.asarray(P, np.float64) for P in patterns2d.build_def(d)]
    out = [P for P in out if P.ndim == 2 and P.shape[1] == 2 and len(P) >= 2]
    return out, [0.0] * len(out)


def _fit(paths: list[np.ndarray]) -> list[np.ndarray]:
    allp = np.vstack(paths)
    lo, hi = allp.min(axis=0), allp.max(axis=0)
    centre = (lo + hi) / 2
    half = float(max(hi[0] - lo[0], hi[1] - lo[1])) / 2
    s = _FILL / half if half > 1e-9 else 1.0
    return [(P - centre) * s for P in paths]


def _decimate(paths: list[np.ndarray]) -> list[np.ndarray]:
    total = sum(len(P) for P in paths)
    if total <= MAX_POINTS:
        return paths
    step = int(np.ceil(total / MAX_POINTS))
    # keep each stroke's last point so a decimated outline still closes
    return [np.vstack([P[::step], P[-1:]]) if len(P) > 2 else P for P in paths]


def _shade(depth: list[float]) -> list[float]:
    """0.45 (far) .. 1.0 (near) per stroke; flat when there is no depth."""
    if not depth:
        return []
    d = np.asarray(depth)
    span = float(d.max() - d.min())
    if span < 1e-9:
        return [1.0] * len(depth)
    # view z grows away from the camera after the pitch, so near = low z
    return [round(1.0 - 0.55 * (x - d.min()) / span, 2) for x in d]


def build(layers: list[dict]) -> list[dict]:
    """`layers` is [{generator, defs, nodes}] — plain data, already copied off
    the live spec by the caller. Returns one entry per def, sorted by name
    within each layer (the order the Shape scale picker lists them in)."""
    out = []
    for li, layer in enumerate(layers):
        gen = layer.get("generator")
        if gen not in ("world", "pattern2d"):
            continue
        defs = layer.get("defs") or {}
        nodes = layer.get("nodes") or []
        key = "shape" if gen == "world" else "def"
        uses: dict[str, int] = {}
        colour: dict[str, list] = {}
        for n in nodes:
            if not isinstance(n, dict):
                continue
            name = n.get(key) or (n.get("shape") if gen == "pattern2d" else None)
            if name is None:
                continue
            uses[name] = uses.get(name, 0) + 1
            if name not in colour and isinstance(n.get("color"), (list, tuple)):
                colour[name] = [round(float(c), 3) for c in n["color"][:3]]
        for name in sorted(defs):
            try:
                paths, depth = (_thumb_3d if gen == "world" else _thumb_2d)(defs[name])
            except Exception:
                paths, depth = [], []
            entry = {"name": name, "layer": li, "kind": "3d" if gen == "world" else "2d",
                     "uses": uses.get(name, 0),
                     "color": colour.get(name, [1.0, 1.0, 1.0]),
                     "paths": [], "shade": []}
            if paths:
                paths = _decimate(_fit(paths))
                entry["paths"] = [np.round(P, 3).tolist() for P in paths]
                entry["shade"] = _shade(depth)
            out.append(entry)
    return out
