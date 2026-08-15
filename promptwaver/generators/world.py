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

import math
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


#: Shared identity rotation. Every motion type except `spin` leaves the node
#: unrotated, and building a fresh `np.eye(3)` for each of those was measurable
#: on its own (~6% of frame time on a 690-node world). Returned by reference,
#: never written to — `_emit` reads it and additionally uses `is _EYE3` to skip
#: the no-op matmul entirely.
_EYE3 = np.eye(3, dtype=np.float32)
_EYE3.flags.writeable = False

_ZERO3 = np.zeros(3, np.float32)
_ZERO3.flags.writeable = False


def _motion(node_motion: dict, t: float):
    """Return (extra_scale, rot_matrix, offset) for this node at time t."""
    m = node_motion or {}
    kind = m.get("type", "none")
    if kind == "none" or not m:
        return 1.0, _EYE3, _ZERO3
    speed = float(m.get("speed", 0.3))
    amp = float(m.get("amp", 0.3))
    axis = m.get("axis", "y")
    scale, rot, offset = 1.0, _EYE3, _ZERO3
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

# How much geometry to transform, as a multiple of the camera's stroke budget,
# before stopping (see `render3d`). The camera keeps `max_strokes` strokes and
# discards the rest, but it discards them for reasons this generator can't see
# (behind the near plane, clipped off-frame), so transforming exactly
# `max_strokes` would starve scenes where much of the nearby geometry happens
# to be out of shot. 3x is comfortably above the worst ratio measured across
# the shipped library while still bounding the work.
_BUDGET_SLACK = 3.0

# Safety margin on the view-cone reject. The cone has to CONTAIN the frustum,
# which is a rectangle — so the half-angle is measured to the frustum's corner
# (see `_cone_cos`), not to the middle of its edge, and this is just a little
# slack on top. Wrongly keeping a node costs one node's transform; wrongly
# dropping one is a visible hole, so the asymmetry is deliberate.
_CONE_MARGIN = 1.08


def _cone_cos(fov_deg: float, aspect: float) -> float:
    """cos of the half-angle of the smallest cone about the view axis that
    still contains the whole view frustum.

    The camera projects with `f = 1/tan(fov/2)` and keeps `|x/z*f/aspect| <= 1`
    and `|y/z*f| <= 1` — a rectangle. Its most distant direction is the corner,
    at `tan(half) = tan(fov/2) * sqrt(1 + aspect^2)`. Testing against `fov/2`
    alone would cut the corners off the frame — and a wide output ratio makes
    that far worse, since the cone has to reach further sideways."""
    t = math.tan(math.radians(fov_deg) * 0.5)
    a = aspect if aspect else 1.0
    corner = math.atan(t * math.sqrt(1.0 + a * a))
    return math.cos(min(math.pi * 0.5 - 1e-6, corner * _CONE_MARGIN))


@register("world")
class World(Generator3D):
    description = "composed scene graph — authored defs placed as nodes"
    field_depth = 1000.0        # bounded scene; effectively no Z-wrap
    # `nodes`/`defs` are authored data rather than knobs, so they get no
    # param_meta and produce no sliders (schema() only exposes int/float/bool).
    # `shape_speed` is the one genuine knob: a scalar, so it becomes a slider
    # and a `visual.shape_speed` modulation destination for free.
    defaults = dict(nodes=[], defs={}, shape_speed=1.0)
    param_meta = {"shape_speed": (0.0, 1.0, 0.01)}

    def __init__(self, **params):
        super().__init__(**params)
        self._def_cache = {}    # name -> (paths, radius) — defs are static
        self._prim_cache = {}   # (name, frozen params) -> (paths, radius)
        # Shape motion runs on its own accumulated clock — see _shape_time.
        self._shape_t = 0.0
        self._last_t = None

    def _shape_time(self, t: float, rate: float) -> float:
        """The clock node motion reads, advanced at `rate` x the scene clock.

        Accumulated rather than computed as `t * rate`, for the same reason
        `Engine._scene_t` is: scaling absolute time makes every change to the
        rate teleport the phase. A shape spinning at t=200 sits at phase 200;
        halve the rate and `t * rate` snaps it to phase 100 — a visible jump,
        and an unusable one when the rate is being dragged on a slider or
        driven from the modulation matrix. Integrating the rate instead means
        the shape slows from wherever it currently is.

        Only the CAMERA is untouched by this. The camera advances in
        `Scene.render` off the unscaled scene clock, which is the whole point
        of the control: the walk through the world keeps its pace while the
        things in the world calm down.
        """
        prev, self._last_t = self._last_t, t
        if prev is None:
            return self._shape_t
        dt = t - prev
        # A backwards or implausibly large step is a clock reset (scene load,
        # crossfade, seek), not elapsed time — advancing on it would lurch
        # every shape forward by however long the gap was. Freeze already
        # arrives here as dt == 0 and correctly stops shape motion too.
        if 0.0 < dt < 1.0:
            self._shape_t += dt * rate
        return self._shape_t

    def _geom_for(self, node: dict, defs: dict, t: float):
        """Local (untransformed) geometry for a node, plus the radius of its
        bounding sphere about the local origin.

        The radius is cached alongside the geometry rather than recomputed:
        it's only needed for the visibility pre-pass in `render3d`, it's a
        pure function of the same inputs the geometry is, and computing it
        per node per frame would reintroduce exactly the per-frame
        world-sized cost that pre-pass exists to remove."""
        # 1) scene-authored shape (Claude's per-scene geometry, stored in JSON)
        shape = node.get("shape")
        if shape is not None and shape in defs:
            hit = self._def_cache.get(shape)
            if hit is None:
                hit = _with_radius(shapes.build_def(defs[shape]))
                self._def_cache[shape] = hit
            return hit
        # 2) built-in primitive kit (convenience defaults)
        prim = node.get("primitive")
        if prim is not None:
            params = dict(node.get("params", {}))
            if prim in _ANIMATED_PRIMITIVES:
                params.setdefault("t", t)      # time-aware primitives (jellyfish)
                try:
                    return _with_radius(primitives.build(prim, params))
                except KeyError:
                    return ([], 0.0)
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
                    return _with_radius(primitives.build(prim, params))
                except KeyError:
                    return ([], 0.0)
            cached = self._prim_cache.get(key)
            if cached is None:
                try:
                    cached = _with_radius(primitives.build(prim, params))
                except KeyError:
                    cached = ([], 0.0)
                self._prim_cache[key] = cached
            return cached
        return ([], 0.0)

    def render3d(self, t: float, p: dict):
        nodes = p.get("nodes", [])
        defs = p.get("defs", {})
        disable_plane = p.get("_disable_plane", False)
        cam = p.get("_camera")
        # Everything below this line sees shape time, not scene time. `t` is
        # used for exactly two things down there — animated primitives and
        # node motion — so substituting it is the whole implementation.
        t = self._shape_time(t, float(p.get("shape_speed", 1.0)))

        # Fly mode is excluded deliberately: it wraps geometry in Z against
        # `field_depth` and tracks the camera as a travelled distance
        # (`camera.z`) rather than a position — `camera.pos` is left at its
        # initial value there, so the distance/direction maths below would be
        # measuring from the wrong place. Every scene the director composes
        # uses orbit/drift; fly falls through to the unbounded path.
        if cam is None or getattr(cam, "mode", "fly") == "fly":
            return self._render_all(t, nodes, defs, disable_plane)
        return self._render_budgeted(t, nodes, defs, disable_plane, cam)

    def _render_all(self, t, nodes, defs, disable_plane):
        """Transform every node — the original behaviour, kept for fly mode
        and for any caller with no camera to cull against."""
        out = []
        for node in nodes:
            if _skip_plane(node, disable_plane):
                continue
            local, _ = self._geom_for(node, defs, t)
            if not local:
                continue
            mscale, rot, offset = _motion(node.get("motion"), t)
            pos = np.asarray(node.get("pos", [0, 0, 0]), np.float32)
            _emit(out, node, local, float(node.get("scale", 1.0)) * mscale,
                  rot, pos + offset)
        return out

    def _render_budgeted(self, t, nodes, defs, disable_plane, cam):
        """Transform only as much geometry as the camera can actually draw.

        The old path built world-space strokes for the whole scene every
        frame and handed the lot to the camera, which depth-sorted it, kept
        `max_strokes`, and threw the rest away — so render cost scaled with
        how big the world IS rather than with how much of it can be drawn.
        On a scene 10x the size of the heaviest shipped one that was ~92% of
        the transform work wasted, and enough to miss the frame budget
        outright (measured 39.9ms against a 22.2ms budget at 45fps).

        A node's bounding sphere is one cached radius and one position, so
        visibility and distance can be decided per NODE, before any
        per-stroke work happens. Sorting on the sphere's nearest point (not
        its centre) and filling near-to-far means the budget is spent on the
        geometry the camera's own depth sort would have kept anyway, and a
        large node like a floor plane still sorts early on the strength of
        the part of it that's close. Same picture out, cost roughly flat in
        world size (10x: 39.9 -> 21.5ms, 20x: 69.3 -> 25.9ms)."""
        cam_pos = cam.pos
        fwd = cam.target - cam_pos
        fn = float(np.linalg.norm(fwd))
        # Degenerate look-at (camera sitting exactly on its target) — the
        # camera itself bails out of projection in this case, so skip the
        # direction-dependent culling rather than divide by ~zero.
        fwd = fwd / fn if fn > 1e-6 else None
        cos_half = _cone_cos(cam.fov, getattr(cam, "aspect", 1.0))
        far = cam.far
        px, py, pz = float(cam_pos[0]), float(cam_pos[1]), float(cam_pos[2])

        cand = []
        for node in nodes:
            if _skip_plane(node, disable_plane):
                continue
            local, radius = self._geom_for(node, defs, t)
            if not local:
                continue
            # Motion is deliberately NOT evaluated here. It used to be, and it
            # was the single largest avoidable cost in a big world: on a
            # 690-node scene `_motion` ran 690 times a frame (each call
            # building a 3x3 numpy rotation via np.eye) to place ~40 nodes
            # that survived the cull — ~94% of the work discarded, 20% of
            # frame time. It now runs once per node that is actually drawn,
            # below.
            #
            # Culling therefore tests the node's RESTING position, with the
            # sphere inflated to cover wherever motion could carry it: `amp`
            # bounds the bob/drift offset and pulse scales by at most
            # 1 + amp*0.5 (see _motion). Spin rotates about the local origin,
            # which the sphere is centred on, so it can't move it at all. The
            # bound is conservative by construction, so this keeps a few nodes
            # it could have dropped and drops none it should have kept.
            s = float(node.get("scale", 1.0))
            m = node.get("motion")
            if m and m.get("type", "none") != "none":
                amp = abs(float(m.get("amp", 0.3)))
                r = radius * abs(s) * (1.0 + amp * 0.5) + amp
            else:
                amp, r = 0.0, radius * abs(s)
            pos = node.get("pos") or (0.0, 0.0, 0.0)
            rx = float(pos[0]) - px
            ry = float(pos[1]) - py
            rz = float(pos[2]) - pz
            d = (rx * rx + ry * ry + rz * rz) ** 0.5
            near_edge = d - r
            if fwd is not None:
                # Depth is measured ALONG THE VIEW AXIS, matching what the
                # camera culls on (`z = rel @ fwd` in _project_lookat) — not
                # Euclidean distance, which is always the larger of the two
                # off-axis and so would cull geometry the camera would have
                # kept. Getting this wrong is invisible on a scene that fills
                # the view and shows up as strokes going missing at the edges
                # on one that doesn't.
                along = rx * float(fwd[0]) + ry * float(fwd[1]) + rz * float(fwd[2])
                if along - r > far:
                    continue                   # entirely beyond the fog
                if d > r:
                    # Camera outside the sphere: compare the angle to the node
                    # against the half-FOV, shrunk by the node's own angular
                    # radius so big nearby objects aren't rejected for having a
                    # centre that's off-axis. (Inside the sphere there is no
                    # meaningful direction to test — always keep it.)
                    ang = (math.acos(max(-1.0, min(1.0, along / d)))
                           - math.asin(min(1.0, r / d)))
                    if ang > 0.0 and math.cos(ang) < cos_half:
                        continue               # outside the view cone
            elif near_edge > far:
                continue
            cand.append((near_edge, len(local), node, local, s, pos))

        cand.sort(key=lambda c: c[0])
        limit = max(1, int(cam.max_strokes * _BUDGET_SLACK))
        out, n = [], 0
        for _, n_strokes, node, local, s, pos in cand:
            if n >= limit:
                break
            # Only now, for nodes that will actually be drawn.
            mscale, rot, offset = _motion(node.get("motion"), t)
            world_pos = np.asarray(pos, np.float32) + offset
            _emit(out, node, local, s * mscale, rot, world_pos)
            n += n_strokes
        return out


def _with_radius(paths):
    """Pair local geometry with the radius of its bounding sphere about the
    local origin — the exact max point norm, not a max-coordinate bound, so
    the cone reject in `_render_budgeted` stays tight enough to be worth
    running."""
    r = 0.0
    for path in paths:
        pts = path.points
        if len(pts):
            r = max(r, float(np.sqrt((pts.astype(np.float32) ** 2).sum(axis=1)).max()))
    return (paths, r)


def _skip_plane(node: dict, disable_plane: bool) -> bool:
    if not disable_plane:
        return False
    name = node.get("shape") or node.get("primitive") or ""
    return bool(_PLANE_NAME_RE.search(name))


def _emit(out, node, local, s, rot, world_pos):
    color = tuple(node.get("color", [1.0, 1.0, 1.0]))
    spun = rot is not _EYE3          # skip an identity matmul per stroke
    for path in local:
        pts = path.points * s
        if spun:
            pts = pts @ rot.T
        pts = pts + world_pos
        out.append(Path3D(pts.astype(np.float32), color, closed=path.closed,
                          lod=path.lod))
