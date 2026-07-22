"""Low-poly primitive kit — the vocabulary a scene graph is built from.

Each builder returns a list of `Path3D` in LOCAL space: centred on the origin,
roughly unit-scale, wireframe, and deliberately sparse so a laser can draw a
whole sceneful. The scene graph (see generators/world.py) places, scales,
colours and animates these. Claude composes scenes by choosing primitives and
transforms — it never has to emit raw geometry, so every object stays clean and
budget-safe.

Add a primitive = add a @register function here; it's then available to both the
director and the local fallback.
"""

from __future__ import annotations

import numpy as np

from .geometry import Path3D

_KIT: dict[str, callable] = {}
TAU = 2 * np.pi


def register(name):
    def deco(fn):
        _KIT[name] = fn
        return fn
    return deco


def build(name: str, params: dict | None = None) -> list[Path3D]:
    if name not in _KIT:
        raise KeyError(f"unknown primitive {name!r}; have {sorted(_KIT)}")
    return _KIT[name](**(params or {}))


def available() -> list[str]:
    return sorted(_KIT)


def _circle(n=20):
    a = np.linspace(0, TAU, n, endpoint=True)
    return np.cos(a), np.sin(a)


@register("planet")
def planet(lat=3, lon=4, seg=18, **_):
    """A wireframe globe: a few latitude rings + full longitude meridian circles.
    Meridians are complete great circles (both hemispheres) so the globe reads
    evenly all the way around, not just on the side facing the camera."""
    out = []
    c, s = _circle(seg)
    for i in range(1, lat + 1):
        phi = (i / (lat + 1) - 0.5) * np.pi        # -pi/2..pi/2
        r = np.cos(phi)
        y = np.sin(phi)
        pts = np.stack([c * r, np.full_like(c, y), s * r], axis=1).astype(np.float32)
        out.append(Path3D(pts, (1, 1, 1), closed=True, lod=1))
    for j in range(lon):
        th = j / lon * np.pi                        # th and th+pi are the same circle
        a = np.linspace(0, TAU, seg, endpoint=True)
        pts = np.stack([np.cos(a) * np.cos(th), np.sin(a), np.cos(a) * np.sin(th)],
                       axis=1).astype(np.float32)
        out.append(Path3D(pts, (1, 1, 1), closed=True, lod=0))
    return out


@register("ring")
def ring(inner=1.3, outer=1.7, tilt=0.4, seg=28, **_):
    """A flat ring (Saturn-style), tilted. Two concentric ellipses."""
    c, s = _circle(seg)
    ct, st = np.cos(tilt), np.sin(tilt)
    out = []
    for r in (inner, outer):
        x, z = c * r, s * r
        y = z * st
        z = z * ct
        pts = np.stack([x, y, z], axis=1).astype(np.float32)
        out.append(Path3D(pts, (1, 1, 1), closed=True, lod=0))
    return out


@register("ball")
def ball(seg=20, **_):
    """A light sphere read from three orthogonal rings — cheap, clear, 3 strokes."""
    c, s = _circle(seg)
    z0 = np.zeros_like(c)
    rings = [
        np.stack([c, s, z0], axis=1),
        np.stack([c, z0, s], axis=1),
        np.stack([z0, c, s], axis=1),
    ]
    return [Path3D(p.astype(np.float32), (1, 1, 1), closed=True, lod=(i > 0))
            for i, p in enumerate(rings)]


@register("starfield")
def starfield(count=36, spread=8.0, seed=7, size=0.05, **_):
    """A scatter of tiny star ticks filling a volume — the backdrop."""
    rng = np.random.default_rng(seed)
    pos = (rng.random((int(count), 3)) - 0.5) * 2 * spread
    out = []
    for p in pos:
        # a tiny cross so each star reads as a point without a dwell hotspot
        seg = np.array([p + [size, 0, 0], p - [size, 0, 0]], np.float32)
        out.append(Path3D(seg, (1, 1, 1), lod=2))
    return out


@register("jellyfish")
def jellyfish(tentacles=6, seg=16, t=0.0, **_):
    """A drifting jellyfish: a domed bell + wavy trailing tentacles.
    `t` (time) animates the tentacle wave — passed by the scene graph."""
    out = []
    # bell: two crossed arcs over the top
    a = np.linspace(0, np.pi, seg)
    for th in (0.0, np.pi / 2):
        pts = np.stack([np.cos(a) * np.cos(th), np.sin(a) * 0.7,
                        np.cos(a) * np.sin(th)], axis=1).astype(np.float32)
        out.append(Path3D(pts, (1, 1, 1), lod=0))
    # rim
    c, s = _circle(seg)
    out.append(Path3D(np.stack([c, np.zeros_like(c), s], axis=1).astype(np.float32),
                      (1, 1, 1), closed=True, lod=1))
    # tentacles hang down and sway
    for k in range(int(tentacles)):
        ang = k / tentacles * TAU
        base = np.array([np.cos(ang) * 0.8, 0.0, np.sin(ang) * 0.8])
        ys = np.linspace(0, -1.6, 8)
        wave = np.sin(ys * 3 + t * 2 + k) * 0.12
        pts = np.stack([base[0] + wave, ys, base[2] + wave * 0.5], axis=1).astype(np.float32)
        out.append(Path3D(pts, (1, 1, 1), lod=2))
    return out


@register("torus")
def torus(rings=5, seg=16, tube=0.35, **_):
    """A wireframe donut — a few tube loops around the ring."""
    out = []
    for i in range(int(rings)):
        phi = i / rings * TAU
        cx, cz = np.cos(phi), np.sin(phi)
        a = np.linspace(0, TAU, seg, endpoint=True)
        px = (1 + tube * np.cos(a)) * cx
        pz = (1 + tube * np.cos(a)) * cz
        py = tube * np.sin(a)
        out.append(Path3D(np.stack([px, py, pz], axis=1).astype(np.float32),
                          (1, 1, 1), closed=True, lod=(i % 2)))
    return out


@register("crystal")
def crystal(**_):
    """An octahedron — a faceted gem/asteroid, 8 cheap edges."""
    v = np.array([[1, 0, 0], [-1, 0, 0], [0, 1, 0],
                  [0, -1, 0], [0, 0, 1], [0, 0, -1]], np.float32)
    top, bot = v[2], v[3]
    eq = [v[0], v[4], v[1], v[5]]
    out = [Path3D(np.array([*eq, eq[0]], np.float32), (1, 1, 1), lod=0)]
    for e in eq:
        out.append(Path3D(np.array([top, e, bot], np.float32), (1, 1, 1), lod=1))
    return out
