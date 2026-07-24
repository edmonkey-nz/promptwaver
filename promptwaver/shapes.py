"""Shape grammar — the open-ended geometry vocabulary Claude authors scenes with.

Instead of a fixed bucket of named objects, a scene's JSON carries a `defs`
block: each object is a list of geometric OPS that this interpreter expands into
`Path3D` line-art in local space. Claude composes any object (easel, jar, window,
fish) from these ops; the app never needs a matching primitive baked in.

Ops (each a dict with an "op" key):
  line     {a:[x,y,z], b:[x,y,z]}
  polyline {pts:[[x,y,z],...], closed?:bool}          # raw escape hatch
  circle   {r, plane:"xy|xz|yz", c:[x,y,z], seg?}
  rect     {w, h, plane, c}
  box      {size:[w,h,d], c}                            # wireframe cuboid
  arc      {r, a0, a1, plane, c, seg?}
  grid     {w, h, nx, ny, plane, c}                     # floor/wall lattices
  lathe    {profile:[[r,y],...], seg?, meridians?}      # revolve -> vases, jars

Any op may carry "lod" (0 always drawn, higher dropped first when far/over budget).
Coordinates are local (the scene graph then places/scales/rotates each object).
"""

from __future__ import annotations

import numpy as np

from .geometry import Path3D

TAU = 2 * np.pi
_PLANE = {  # (axis for u, axis for v): builds a point from (u, v) in a plane
    "xy": lambda u, v: np.stack([u, v, np.zeros_like(u)], axis=-1),
    "xz": lambda u, v: np.stack([u, np.zeros_like(u), v], axis=-1),
    "yz": lambda u, v: np.stack([np.zeros_like(u), u, v], axis=-1),
}


def _c(c):
    return np.asarray(c if c is not None else [0, 0, 0], np.float32)


def _op_line(a, b, lod=0, **_):
    return [Path3D(np.array([a, b], np.float32), (1, 1, 1), lod=lod)]


def _op_polyline(pts, closed=False, lod=0, **_):
    return [Path3D(np.asarray(pts, np.float32), (1, 1, 1), closed=closed, lod=lod)]


def _op_circle(r=1.0, plane="xy", c=None, seg=20, lod=0, **_):
    a = np.linspace(0, TAU, int(seg), endpoint=True)
    u, v = np.cos(a) * r, np.sin(a) * r
    pts = _PLANE.get(plane, _PLANE["xy"])(u, v) + _c(c)
    return [Path3D(pts.astype(np.float32), (1, 1, 1), closed=True, lod=lod)]


def _op_rect(w=1.0, h=1.0, plane="xy", c=None, lod=0, **_):
    hw, hh = w / 2, h / 2
    u = np.array([-hw, hw, hw, -hw, -hw])
    v = np.array([-hh, -hh, hh, hh, -hh])
    pts = _PLANE.get(plane, _PLANE["xy"])(u, v) + _c(c)
    return [Path3D(pts.astype(np.float32), (1, 1, 1), lod=lod)]


def _op_arc(r=1.0, a0=0.0, a1=np.pi, plane="xy", c=None, seg=16, lod=0, **_):
    a = np.linspace(a0, a1, int(seg))
    u, v = np.cos(a) * r, np.sin(a) * r
    pts = _PLANE.get(plane, _PLANE["xy"])(u, v) + _c(c)
    return [Path3D(pts.astype(np.float32), (1, 1, 1), lod=lod)]


def _op_box(size=(1, 1, 1), c=None, lod=0, **_):
    w, h, d = [s / 2 for s in size]
    o = _c(c)
    # bottom + top rectangles (2 closed strokes) + 4 vertical edges = 6 strokes
    bot = np.array([[-w, -h, -d], [w, -h, -d], [w, -h, d], [-w, -h, d], [-w, -h, -d]], np.float32)
    top = bot.copy(); top[:, 1] = h
    out = [Path3D(bot + o, (1, 1, 1), lod=lod), Path3D(top + o, (1, 1, 1), lod=lod)]
    for cx, cz in ((-w, -d), (w, -d), (w, d), (-w, d)):
        out.append(Path3D(np.array([[cx, -h, cz], [cx, h, cz]], np.float32) + o,
                          (1, 1, 1), lod=lod + 1))
    return out


def _op_grid(w=2.0, h=2.0, nx=4, ny=4, plane="xz", c=None, lod=1, **_):
    build = _PLANE.get(plane, _PLANE["xz"])
    out = []
    xs = np.linspace(-w / 2, w / 2, int(nx))
    ys = np.linspace(-h / 2, h / 2, int(ny))
    for x in xs:
        u = np.array([x, x]); v = np.array([ys[0], ys[-1]])
        out.append(Path3D((build(u, v) + _c(c)).astype(np.float32), (1, 1, 1), lod=lod))
    for y in ys:
        u = np.array([xs[0], xs[-1]]); v = np.array([y, y])
        out.append(Path3D((build(u, v) + _c(c)).astype(np.float32), (1, 1, 1), lod=lod))
    return out


def _op_lathe(profile, seg=18, meridians=4, c=None, lod=0, **_):
    """Revolve a 2D profile [[r, y], ...] around the Y axis: horizontal rings at
    each profile height + a few vertical meridians. Vases, jars, bottles, lamps."""
    prof = np.asarray(profile, np.float32)
    o = _c(c)
    out = []
    a = np.linspace(0, TAU, int(seg), endpoint=True)
    ca, sa = np.cos(a), np.sin(a)
    for r, y in prof:
        if r < 0.01:
            continue
        pts = np.stack([ca * r, np.full_like(ca, y), sa * r], axis=1) + o
        out.append(Path3D(pts.astype(np.float32), (1, 1, 1), closed=True, lod=lod + 1))
    for k in range(int(meridians)):
        th = k / meridians * TAU
        pts = np.stack([prof[:, 0] * np.cos(th), prof[:, 1], prof[:, 0] * np.sin(th)],
                       axis=1) + o
        out.append(Path3D(pts.astype(np.float32), (1, 1, 1), lod=lod))
    return out


_OPS = {
    "line": _op_line, "polyline": _op_polyline, "circle": _op_circle,
    "rect": _op_rect, "arc": _op_arc, "box": _op_box, "grid": _op_grid,
    "lathe": _op_lathe,
}


def build_op(op: dict) -> list[Path3D]:
    kind = op.get("op")
    fn = _OPS.get(kind)
    if fn is None:
        return []
    args = {k: v for k, v in op.items() if k != "op"}
    try:
        return fn(**args)
    except Exception:
        return []                       # a malformed op never kills the scene


def build_def(ops: list) -> list[Path3D]:
    """Expand a whole object definition (list of ops) into local line-art."""
    out = []
    for op in ops or []:
        out.extend(build_op(op))
    return out


def available_ops() -> list[str]:
    return sorted(_OPS)
