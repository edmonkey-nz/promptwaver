"""Flat pattern grammar — the vocabulary a `pattern2d` scene is authored in.

`shapes.py` is the 3D equivalent: it expands authored ops into world-space
line-art for a camera to project. This module does the same job for scenes
that have no camera at all — mandalas, kaleidoscopes, neon lattices — where
the pattern is composed directly in the frame and never moves through space.

Three layers, deliberately separate:

  OPS         author a motif as local 2D line-art (line, arc, ngon, star, ...)
  SPACE       how that motif's own coordinates are read — "cart" as authored,
              or "polar" where a point is (radius, angle) so a straight line
              becomes an arc or a spiral
  COMBINATORS repeat and symmetry, applied to the whole motif: parallel
              offsets (the banded neon look), concentric scaling, radial
              arrays, mirrors

Splitting SPACE (per-motif, local) from COMBINATORS (per-node, global) is what
lets one pattern mix both idioms — Cartesian mirrored cross-arms and
concentric polar diamonds in the same image — which a single global
"polar mode" flag could not express.

ANGLES ARE IN TURNS (0..1), not radians, everywhere in this module: authored
symmetry is nearly always a simple fraction of a circle, and 0.25 survives a
round-trip through JSON and a language model's arithmetic far better than
1.5707963. Radians appear only inside the maths.
"""

from __future__ import annotations

import numpy as np

TAU = 2 * np.pi


def _pt(c, default=(0.0, 0.0)) -> np.ndarray:
    return np.asarray(c if c is not None else default, np.float32).reshape(2)


# --- ops: author a motif in local 2D ----------------------------------------
# Each returns a list of (N,2) float arrays. Nothing here knows about colour,
# placement or repetition — those are applied later, so every op stays a few
# lines and new ones cost nothing.

def _op_line(a, b, **_):
    return [np.array([_pt(a), _pt(b)], np.float32)]


def _op_polyline(pts, closed=False, **_):
    P = np.asarray(pts, np.float32).reshape(-1, 2)
    if closed and len(P) > 1:
        P = np.vstack([P, P[:1]])
    return [P]


def _op_circle(r=1.0, c=None, seg=48, **_):
    a = np.linspace(0, TAU, max(3, int(seg)), endpoint=True)
    return [(np.stack([np.cos(a) * r, np.sin(a) * r], 1) + _pt(c)).astype(np.float32)]


def _op_arc(r=1.0, a0=0.0, a1=0.5, c=None, seg=32, **_):
    a = np.linspace(a0 * TAU, a1 * TAU, max(2, int(seg)))
    return [(np.stack([np.cos(a) * r, np.sin(a) * r], 1) + _pt(c)).astype(np.float32)]


def _op_rect(w=1.0, h=1.0, c=None, **_):
    hw, hh = w / 2, h / 2
    P = np.array([[-hw, -hh], [hw, -hh], [hw, hh], [-hw, hh], [-hw, -hh]], np.float32)
    return [(P + _pt(c)).astype(np.float32)]


def _op_ngon(n=4, r=1.0, c=None, rot=0.0, **_):
    """Regular polygon. n=3 triangle, n=4 diamond, n=6 hexagon."""
    k = max(3, int(n))
    a = np.linspace(0, TAU, k + 1) + rot * TAU
    return [(np.stack([np.cos(a) * r, np.sin(a) * r], 1) + _pt(c)).astype(np.float32)]


def _op_star(n=5, r1=1.0, r2=0.45, c=None, rot=0.0, **_):
    """n-pointed star alternating between outer radius r1 and inner r2."""
    k = max(3, int(n))
    a = np.linspace(0, TAU, 2 * k + 1) + rot * TAU
    rr = np.empty(2 * k + 1, np.float32)
    rr[0::2], rr[1::2] = r1, r2
    return [(np.stack([np.cos(a) * rr, np.sin(a) * rr], 1) + _pt(c)).astype(np.float32)]


def _op_grid(w=2.0, h=2.0, nx=4, ny=4, c=None, **_):
    o = _pt(c)
    xs = np.linspace(-w / 2, w / 2, max(2, int(nx)))
    ys = np.linspace(-h / 2, h / 2, max(2, int(ny)))
    out = [np.array([[x, ys[0]], [x, ys[-1]]], np.float32) + o for x in xs]
    out += [np.array([[xs[0], y], [xs[-1], y]], np.float32) + o for y in ys]
    return out


_OPS = {
    "line": _op_line, "polyline": _op_polyline, "circle": _op_circle,
    "arc": _op_arc, "rect": _op_rect, "ngon": _op_ngon, "star": _op_star,
    "grid": _op_grid,
}


def available_ops() -> list[str]:
    return sorted(_OPS)


# --- space: how a motif's own coordinates are read ---------------------------

def _polar_to_cart(P: np.ndarray, seg: int = 12) -> np.ndarray:
    """Read (u, v) as (radius, angle-in-turns) and convert to Cartesian.

    Each authored segment is subdivided BEFORE conversion — that is the whole
    point of polar space. A straight line between two polar points is an arc
    or a spiral, and without subdividing it would render as the chord: the
    curve would silently vanish and polar space would look broken rather than
    subtle.
    """
    if len(P) < 2:
        if len(P) == 1:
            r, th = P[0]
            return np.array([[r * np.cos(th * TAU), r * np.sin(th * TAU)]], np.float32)
        return P.astype(np.float32)
    k = max(2, int(seg))
    a = P[:-1]
    b = P[1:]
    t = np.linspace(0.0, 1.0, k, endpoint=False)[None, :, None]   # (1,k,1)
    mid = a[:, None, :] * (1 - t) + b[:, None, :] * t             # (S,k,2)
    flat = np.concatenate([mid.reshape(-1, 2), P[-1:]], axis=0)
    r, th = flat[:, 0], flat[:, 1] * TAU
    return np.stack([r * np.cos(th), r * np.sin(th)], 1).astype(np.float32)


def build_def(d) -> list[np.ndarray]:
    """Expand one motif definition into local 2D polylines.

    Accepts either `{"space":..., "ops":[...]}` or a bare list of ops, so a
    Cartesian motif — much the commoner case — needs no wrapper.
    """
    if isinstance(d, dict):
        ops = d.get("ops") or []
        space = d.get("space", "cart")
        seg = int(d.get("seg", 12))
    else:
        ops, space, seg = (d or []), "cart", 12

    out: list[np.ndarray] = []
    for op in ops:
        fn = _OPS.get((op or {}).get("op"))
        if fn is None:
            continue
        args = {k: v for k, v in op.items() if k != "op"}
        try:
            out.extend(fn(**args))
        except Exception:
            continue            # a malformed op never kills the scene
    if space == "polar":
        out = [_polar_to_cart(P, seg) for P in out]
    return [P for P in out if len(P) >= 2]


# --- combinators: repeat and symmetry ---------------------------------------
# These work on "items" — (points, hue_delta) pairs. Carrying the hue offset
# alongside the geometry (rather than resolving colour here) is what lets a
# repeat and a symmetry compose without either needing to know the node's
# authored colour.

Item = tuple[np.ndarray, float]


def _rot_matrix(turns: float) -> np.ndarray:
    a = turns * TAU
    ca, sa = np.cos(a), np.sin(a)
    return np.array([[ca, -sa], [sa, ca]], np.float32)


def _offset_dir(P: np.ndarray) -> np.ndarray:
    """Unit normal to a polyline's overall direction.

    Uses first->last rather than a true polygon offset: it is predictable,
    costs nothing, and is exactly right for the straight and chevron motifs
    that parallel banding is actually used for. A closed or degenerate motif
    has no meaningful direction, so it falls back to +y.
    """
    d = P[-1] - P[0]
    n = float(np.hypot(*d))
    if n < 1e-6:
        return np.array([0.0, 1.0], np.float32)
    return np.array([-d[1] / n, d[0] / n], np.float32)


def apply_repeat(items: list[Item], rep: dict | None) -> list[Item]:
    if not rep:
        return items
    kind = rep.get("kind", "offset")
    n = max(1, int(rep.get("n", 1)))
    hue_step = float(rep.get("hue_step", 0.0))
    out: list[Item] = []

    if kind == "offset":
        d = float(rep.get("d", 0.04))
        axis = rep.get("axis", "normal")
        for P, h in items:
            if axis == "x":
                vec = np.array([1.0, 0.0], np.float32)
            elif axis == "y":
                vec = np.array([0.0, 1.0], np.float32)
            else:
                vec = _offset_dir(P)
            for i in range(n):
                out.append((P + vec * (d * i), h + hue_step * i))
    elif kind == "scale":
        f = float(rep.get("factor", 1.3))
        for P, h in items:
            for i in range(n):
                out.append((P * (f ** i), h + hue_step * i))
    elif kind == "radial":
        for P, h in items:
            for i in range(n):
                out.append((P @ _rot_matrix(i / n).T, h + hue_step * i))
    elif kind == "ring":
        r = float(rep.get("r", 0.5))
        spin = bool(rep.get("spin", True))
        for P, h in items:
            for i in range(n):
                turns = i / n
                Q = P @ _rot_matrix(turns).T if spin else P
                c = np.array([r * np.cos(turns * TAU), r * np.sin(turns * TAU)], np.float32)
                out.append((Q + c, h + hue_step * i))
    elif kind == "grid":
        nx, ny = max(1, int(rep.get("nx", 2))), max(1, int(rep.get("ny", 2)))
        sx, sy = (rep.get("step") or [0.4, 0.4])[:2]
        for P, h in items:
            for iy in range(ny):
                for ix in range(nx):
                    c = np.array([(ix - (nx - 1) / 2) * sx,
                                  (iy - (ny - 1) / 2) * sy], np.float32)
                    out.append((P + c, h + hue_step * (ix + iy)))
    else:
        return items
    return out


def apply_symmetry(items: list[Item], sym: dict | None) -> list[Item]:
    """Fold the whole node. `mirror` is "x" (left-right), "y" (up-down) or
    "xy" (both, giving 4-fold); `radial` is an n-fold rotational array.

    Mirrors run before radial so "mirror then rotate" reads as the alternating
    reflected wedges a kaleidoscope actually produces.
    """
    if not sym:
        return items
    out = list(items)
    m = sym.get("mirror")
    if m in ("x", "xy"):
        out += [(P * np.array([-1.0, 1.0], np.float32), h) for P, h in out]
    if m in ("y", "xy"):
        out += [(P * np.array([1.0, -1.0], np.float32), h) for P, h in out]
    n = int(sym.get("radial", 0) or 0)
    if n > 1:
        base = list(out)
        out = []
        hue_step = float(sym.get("hue_step", 0.0))
        for i in range(n):
            R = _rot_matrix(i / n).T
            out += [(P @ R, h + hue_step * i) for P, h in base]
    return out
