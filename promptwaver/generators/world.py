"""World generator — turns a scene graph into animated world-space geometry.

A scene graph is a list of nodes; each node picks a primitive, places it with a
transform (pos / scale / rotation), colours it, and optionally animates it
(spin, bob, drift, pulse). This is what Claude (or the local fallback) emits from
a prompt: it composes a 3D world from the primitive kit, and the camera flies or
orbits through it.

    {"primitive": "planet", "pos": [0,0,0], "scale": 3.0, "color": [0.4,0.7,1.0],
     "params": {"lat": 3, "lon": 5}, "motion": {"type": "spin", "speed": 0.2}}
"""

from __future__ import annotations

import re

import numpy as np

from ..geometry import Path3D
from .. import primitives, shapes
from .base import Generator3D, register

# "Disable scene plane" (Camera controls, UI): scenes almost always name
# their floor/backdrop node something from this small vocabulary — matched
# against the node's `shape` or `primitive` name, not its content, so it's a
# cheap loose convention rather than a real geometry classifier. Doesn't
# catch every possible floor (an oddly-named one slips through), but this is
# what's actually seen across the shipped/generated scene library.
#
# The `(?![a-z])` guard matters: "plane" is also the first five letters of
# the very common "planet" primitive (see this module's own docstring
# example) — without it, disabling the scene plane would also delete every
# planet in the scene. Requiring the match not be immediately followed by
# another letter keeps "floor"/"cave_floor"/"seafloor"/"ocean_grid" matching
# while leaving "planet" (and similar incidental collisions) alone.
_PLANE_NAME_RE = re.compile(r"(?:floor|ground|plane|grid)(?![a-z])", re.IGNORECASE)


def _rot_axis(axis: str, ang: float) -> np.ndarray:
    c, s = np.cos(ang), np.sin(ang)
    if axis == "x":
        return np.array([[1, 0, 0], [0, c, -s], [0, s, c]], np.float32)
    if axis == "z":
        return np.array([[c, -s, 0], [s, c, 0], [0, 0, 1]], np.float32)
    return np.array([[c, 0, s], [0, 1, 0], [-s, 0, c]], np.float32)   # y


def _motion(node_motion: dict, t: float):
    """Return (extra_scale, rot_matrix, offset) for this node at time t."""
    m = node_motion or {}
    kind = m.get("type", "none")
    speed = float(m.get("speed", 0.3))
    amp = float(m.get("amp", 0.3))
    axis = m.get("axis", "y")
    scale, rot, offset = 1.0, np.eye(3, dtype=np.float32), np.zeros(3, np.float32)
    if kind == "spin":
        rot = _rot_axis(axis, t * speed)
    elif kind == "bob":
        offset = np.array([0.0, np.sin(t * speed) * amp, 0.0], np.float32)
    elif kind == "drift":
        offset = np.array([np.sin(t * speed) * amp,
                           np.sin(t * speed * 0.7 + 1) * amp * 0.5,
                           np.cos(t * speed * 0.9) * amp], np.float32)
    elif kind == "pulse":
        scale = 1.0 + np.sin(t * speed) * amp * 0.5
    return scale, rot, offset


# Primitives whose local geometry genuinely depends on time (e.g. jellyfish's
# tentacle wave) and so can't be cached like the rest of the kit below.
_ANIMATED_PRIMITIVES = {"jellyfish"}


@register("world")
class World(Generator3D):
    field_depth = 1000.0        # bounded scene; effectively no Z-wrap
    defaults = dict(nodes=[], defs={})

    def __init__(self, **params):
        super().__init__(**params)
        self._def_cache = {}    # name -> built local geometry (defs are static)
        self._prim_cache = {}   # (name, frozen params) -> built local geometry

    def _local_for(self, node: dict, defs: dict, t: float):
        # 1) scene-authored shape (Claude's per-scene geometry, stored in JSON)
        shape = node.get("shape")
        if shape is not None and shape in defs:
            if shape not in self._def_cache:
                self._def_cache[shape] = shapes.build_def(defs[shape])
            return self._def_cache[shape]
        # 2) built-in primitive kit (convenience defaults)
        prim = node.get("primitive")
        if prim is not None:
            params = dict(node.get("params", {}))
            if prim in _ANIMATED_PRIMITIVES:
                params.setdefault("t", t)      # time-aware primitives (jellyfish)
                try:
                    return primitives.build(prim, params)
                except KeyError:
                    return []
            # Every other primitive (planet/ring/ball/torus/crystal/starfield)
            # ignores `t` entirely, so its local geometry is a pure function
            # of its params — was being rebuilt from raw numpy (trig, RNG for
            # starfield) on EVERY frame regardless, the single biggest
            # avoidable render-loop cost in scenes that lean on the primitive
            # kit rather than authored `defs` (which already had this cache).
            try:
                key = (prim, tuple(sorted(params.items())))
            except TypeError:
                # an unhashable param value (e.g. a list) — build uncached
                # rather than crash; every shipped primitive only takes
                # scalar params, so this is a defensive fallback, not the
                # normal path.
                try:
                    return primitives.build(prim, params)
                except KeyError:
                    return []
            cached = self._prim_cache.get(key)
            if cached is None:
                try:
                    cached = primitives.build(prim, params)
                except KeyError:
                    cached = []
                self._prim_cache[key] = cached
            return cached
        return []

    def render3d(self, t: float, p: dict):
        nodes = p.get("nodes", [])
        defs = p.get("defs", {})
        disable_plane = p.get("_disable_plane", False)
        out = []
        for node in nodes:
            if disable_plane:
                name = node.get("shape") or node.get("primitive") or ""
                if _PLANE_NAME_RE.search(name):
                    continue
            local = self._local_for(node, defs, t)
            if not local:
                continue

            pos = np.asarray(node.get("pos", [0, 0, 0]), np.float32)
            scale = float(node.get("scale", 1.0))
            color = tuple(node.get("color", [1.0, 1.0, 1.0]))
            mscale, rot, offset = _motion(node.get("motion"), t)
            s = scale * mscale

            for path in local:
                pts = path.points * s
                pts = pts @ rot.T
                pts = pts + pos + offset
                out.append(Path3D(pts.astype(np.float32), color, closed=path.closed,
                                  lod=path.lod))
        return out
