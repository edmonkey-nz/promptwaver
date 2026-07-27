"""3D for a vector laser — a camera and a projection pipeline, not a game engine.

Keeps the world as 3D polylines, flies a slow camera through it, projects to the
same normalized 2D `Path`s everything downstream already speaks. Because a laser
can only draw a few hundred strokes per frame, the far plane does double duty:
it culls distant geometry (performance) and *is* the fog.

Depth cueing for on/off lasers
------------------------------
Many units (e.g. TTL RGB) can't fade brightness — a beam is on or off. So depth
is shown with COLOUR, not brightness. Configurable via the scene's `camera.depth`:

    mode = "hue"  : lerp near_color -> far_color by distance (optionally snapped
                    to TTL-clean primaries so channels are cleanly on/off)
    mode = "cull" : hard-drop anything past `far` (pure depth culling)
    mode = "both" : hue AND cull

Near-plane clipping is mandatory: segments crossing behind the camera are cut,
otherwise the perspective divide blows up (the classic laser-3D failure).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

from .geometry import Path, Path3D, Frame


@dataclass
class DepthCue:
    mode: str = "hue"                 # "hue" | "cull" | "both"
    near_color: tuple = (0.4, 0.9, 1.0)
    far_color: tuple = (0.1, 0.2, 0.5)
    ttl_quantize: bool = False        # snap channels to 0/1 for TTL RGB lasers

    def color_for(self, depth: float, near: float, far: float):
        t = min(1.0, max(0.0, (depth - near) / max(far - near, 1e-4)))
        c = tuple(a + (b - a) * t for a, b in zip(self.near_color, self.far_color))
        if self.ttl_quantize:
            c = tuple(1.0 if v >= 0.5 else 0.0 for v in c)
        return c


class Camera:
    """A drifting fly-through camera. Position advances along +Z; gentle yaw/pitch
    sway gives the floating feel. `speed` is read from the modulation matrix each
    frame, so audio/LFOs can steer the flythrough."""

    def __init__(self, *, fov=60.0, near=0.4, far=14.0, speed=0.6,
                 height=1.0, sway=0.12, depth: DepthCue | None = None,
                 max_strokes=90, aspect=1.0, mode="fly",
                 target=(0.0, 0.0, 0.0), orbit_radius=9.0, orbit_height=1.5):
        self.fov = fov
        self.near = near
        self.far = far
        self.base_speed = speed
        self.height = height
        self.sway = sway
        self.depth = depth or DepthCue()
        self.max_strokes = max_strokes
        self.aspect = aspect
        self.mode = mode                       # "fly" | "orbit" | "drift"
        self.target = np.asarray(target, np.float32)
        self.orbit_radius = orbit_radius
        self.orbit_height = orbit_height
        self.z = 0.0                           # travelled distance (fly mode)
        self._yaw = 0.0
        self._pitch = 0.0
        self.pos = np.array([0.0, 0.0, -orbit_radius], np.float32)
        self._angle = 0.0
        self._drift_t = 0.0                    # integrated phase (drift mode)

    def update(self, t: float, dt: float, matrix=None):
        speed = self.base_speed
        if matrix is not None:
            speed = matrix.value("camera.speed", self.base_speed)
        if self.mode == "fly":
            self.z += speed * dt
            self._yaw = math.sin(t * 0.11) * self.sway
            self._pitch = math.sin(t * 0.07 + 1.0) * self.sway * 0.4
        elif self.mode == "orbit":
            self._angle += speed * dt * 0.15
            r = self.orbit_radius
            self.pos = self.target + np.array([
                math.cos(self._angle) * r,
                self.orbit_height + math.sin(t * 0.13) * 0.6,
                math.sin(self._angle) * r], np.float32)
        else:  # drift: gentle bounded wander, always looking at the scene
            # Integrate an internal phase from speed rather than multiplying
            # absolute time by the (live-modulated) speed directly. Modulation
            # routes like audio_level -> camera.speed change `speed` every
            # frame; multiplying raw t by a changing speed jumps the sin/cos
            # phase discontinuously each tick (visible as jerkiness). Integrating
            # means a speed change only alters the *rate* of drift — the
            # position itself stays continuous.
            self._drift_t += speed * dt
            p = self._drift_t
            r = self.orbit_radius
            self.pos = self.target + np.array([
                math.sin(p * 0.11) * r,
                self.orbit_height + math.sin(p * 0.09) * r * 0.4,
                math.cos(p * 0.13) * r], np.float32)

    def _focal(self):
        return 1.0 / math.tan(math.radians(self.fov) * 0.5)

    def project(self, paths3d: list[Path3D], field_depth: float) -> Frame:
        """Project world paths to 2D. Fly mode wraps geometry in Z for an endless
        field; orbit/drift use a look-at view of a bounded, composed scene."""
        if self.mode != "fly":
            return self._project_lookat(paths3d)
        f = self._focal()
        cy, sy = math.cos(self._yaw), math.sin(self._yaw)
        cp, sp = math.cos(self._pitch), math.sin(self._pitch)

        # depth-sort strokes so the budget keeps near geometry, drops far first.
        # `.sum()/len()` instead of `.points[:,2].mean()`: numpy's generic
        # `mean()` (dtype/where-handling machinery on top of the reduction)
        # costs real time when called once per stroke per frame — profiled at
        # ~2x a scene's whole frame budget for a stroke-rich scene, entirely
        # in this kind of per-stroke bookkeeping, not the actual drawing.
        scored = [((float(p3.points[:, 2].sum()) / len(p3.points) - self.z) % field_depth, p3)
                  for p3 in paths3d]
        scored.sort(key=lambda s: s[0])

        cull = self.depth.mode in ("cull", "both")
        out: Frame = []
        for zc, p3 in scored:
            if len(out) >= self.max_strokes:
                break
            # LOD: past 60% of far, drop higher-LOD detail strokes
            if p3.lod > 0 and zc > self.far * 0.6:
                continue
            pts = p3.points.copy()
            # camera-relative wrap on Z, then yaw/pitch sway, then translate height
            pts[:, 2] = ((pts[:, 2] - self.z) % field_depth)
            x = pts[:, 0]
            y = pts[:, 1] - self.height
            z = pts[:, 2]
            # apply sway rotations (small angles) about Y then X
            x, z = x * cy + z * sy, -x * sy + z * cy
            y, z = y * cp - z * sp, y * sp + z * cp

            # Exact (not approximate) skip: if every point of this stroke is
            # already behind the near plane, or (when culling) beyond far,
            # `_clip_and_project`'s own visibility mask would exclude all of
            # them and it'd return nothing — so there's no need to pay for
            # its per-point clip/run-finding work to find that out. A scene
            # with more background geometry than its own `max_strokes`
            # budget (so the strokes-found early-exit above never triggers)
            # was paying full clip cost for every one of those strokes every
            # frame regardless of whether any of them were ever visible —
            # profiled as the dominant render cost for such a scene.
            if z.max() <= self.near or (cull and z.min() >= self.far):
                continue

            seg = _clip_and_project(x, y, z, f, self.near, self.far,
                                    self.aspect, self.depth, cull=cull)
            for xy, depth in seg:
                if len(xy) < 2:
                    continue
                if self.depth.mode in ("hue", "both"):
                    col = self.depth.color_for(depth, self.near, self.far)
                else:
                    col = p3.color
                out.append(Path(xy, col))
        return out

    def _project_lookat(self, paths3d: list[Path3D]) -> Frame:
        """Look-at projection for a bounded, composed scene (orbit/drift)."""
        f = self._focal()
        fwd = self.target - self.pos
        n = np.linalg.norm(fwd)
        if n < 1e-6:
            return []
        fwd = fwd / n
        up0 = np.array([0.0, 1.0, 0.0], np.float32)
        right = np.cross(fwd, up0)
        rn = np.linalg.norm(right)
        right = right / rn if rn > 1e-6 else np.array([1.0, 0.0, 0.0], np.float32)
        up = np.cross(right, fwd)

        # depth-sort strokes; keep nearest, drop far first for the budget.
        # Same reasoning as the fly-mode branch above: `.mean(axis=0)` +
        # `np.linalg.norm` are each general-purpose numpy functions with
        # their own dispatch overhead, paid once per stroke per frame — for
        # a several-dozen-stroke scene that's the single biggest line item
        # in the whole render (profiled). A plain `.sum(axis=0)` plus a
        # 3-term Euclidean distance in pure Python is exactly the same math,
        # far cheaper per call for arrays this small.
        px, py, pz = float(self.pos[0]), float(self.pos[1]), float(self.pos[2])
        scored = []
        for p3 in paths3d:
            pts = p3.points
            s = pts.sum(axis=0)
            n_pts = len(pts)
            cx = s[0] / n_pts - px
            cy = s[1] / n_pts - py
            cz = s[2] / n_pts - pz
            scored.append(((cx * cx + cy * cy + cz * cz) ** 0.5, p3))
        scored.sort(key=lambda s: s[0])

        cull = self.depth.mode in ("cull", "both")
        out: Frame = []
        for d, p3 in scored:
            if len(out) >= self.max_strokes:
                break
            rel = p3.points - self.pos
            x = rel @ right
            y = rel @ up
            z = rel @ fwd                      # +Z in front of the camera

            # Exact skip — see the matching comment in `project()` (fly mode)
            # above: if nothing in this stroke can pass the visibility test,
            # skip the clip pipeline entirely instead of running it to
            # discover that. This is the branch scenes with `max_strokes`
            # set above their own raw geometry count hit hardest, since the
            # strokes-found early-exit above never triggers for them either.
            if z.max() <= self.near or (cull and z.min() >= self.far):
                continue

            seg = _clip_and_project(x, y, z, f, self.near, self.far,
                                    self.aspect, self.depth, cull=cull)
            for xy, depth in seg:
                if len(xy) < 2:
                    continue
                if self.depth.mode in ("hue", "both"):
                    col = self.depth.color_for(depth, self.near, self.far)
                else:
                    col = p3.color
                out.append(Path(xy, col))
        return out


def _clip_and_project(x, y, z, f, near, far, aspect, depth, cull):
    """Clip a polyline against the near plane, project surviving segments, and
    return a list of (Nx2 array, mean_depth). Splits the stroke where it crosses
    the near plane so nothing wraps around behind the camera.

    Was a per-point Python loop (`for i in range(n)`) calling a `proj(i)`
    closure once per point and accumulating into plain lists — profiled as
    the single hottest spot in the whole render path for a stroke-rich
    scene, entirely from Python-level overhead (a laser stroke's point count
    is small, so the *math* here is trivial; it's the interpreter overhead of
    doing it one point at a time, 45 times a second, across every stroke,
    that adds up). Visibility, run-finding, and projection are now batch
    numpy ops over the whole stroke at once; only `_clip_frame`'s box-clip
    below (genuinely sequential — it merges clipped segments into
    continuous runs) is still a Python loop, and it now runs over far fewer,
    already-culled points."""
    if cull:
        visible = (z > near) & (z < far)
    else:
        visible = z > near
    if not visible.any():
        return []

    # Runs of >=2 consecutive visible points, via edge-detection on the
    # boolean mask rather than an explicit per-point accumulate/flush loop.
    padded = np.concatenate(([False], visible, [False]))
    edges = np.flatnonzero(padded[1:] != padded[:-1])
    starts, ends = edges[0::2], edges[1::2]      # [start, end) index pairs, end exclusive

    zc = np.maximum(z, 1e-3)
    px = x / zc * f * aspect
    py = y / zc * f

    # A laser stroke's point count is small (a handful to a few dozen), so
    # numpy's per-call dispatch overhead dominates any array method called
    # per run — confirmed by benchmark: plain Python min()/max() on a list
    # slice runs ~40% faster than the equivalent numpy calls at this size.
    # One `.tolist()` per stroke, then plain Python for the per-run bounds
    # check below.
    pxl, pyl, zl = px.tolist(), py.tolist(), z.tolist()

    result = []
    for s, e in zip(starts, ends):
        n = e - s
        if n < 2:
            continue
        rx, ry = pxl[s:e], pyl[s:e]
        # Exact skip: [-1,1] is a convex box, so if every point in this run
        # is on the far side of any one of its four edges, the whole
        # (piecewise-linear) run is too — `_clip_frame` would clip all of it
        # away. A scene whose geometry extends well beyond the camera's
        # field of view at any given moment (composed 3D scenes tend to —
        # you're only ever looking at part of them) was paying full
        # box-clip cost, per run, to rediscover that same "entirely
        # off-screen" answer every single frame. Profiled on one such scene:
        # over half its on-screen-candidate strokes were fully outside the
        # frame this way.
        if (min(rx) > 1.0 or max(rx) < -1.0 or min(ry) > 1.0 or max(ry) < -1.0):
            continue
        xy = np.stack([px[s:e], py[s:e]], axis=1).astype(np.float32)
        d = sum(zl[s:e]) / n
        # clip to the [-1,1] frame: cut lines at the edge rather than clamping
        # them onto it (which draws ugly border-hugging strokes and wastes points)
        for sub in _clip_frame(xy):
            if len(sub) >= 2:
                result.append((np.asarray(sub, np.float32), d))
    return result


def _lb_clip(a, b):
    """Liang-Barsky clip of segment a->b against the box [-1,1]^2.
    Returns (p0, p1) inside the box, or None if the segment misses it."""
    x0, y0 = float(a[0]), float(a[1])
    x1, y1 = float(b[0]), float(b[1])
    dx, dy = x1 - x0, y1 - y0
    t0, t1 = 0.0, 1.0
    for p, q in ((-dx, x0 + 1), (dx, 1 - x0), (-dy, y0 + 1), (dy, 1 - y0)):
        if p == 0:
            if q < 0:
                return None            # parallel and outside this edge
        else:
            r = q / p
            if p < 0:
                if r > t1:
                    return None
                if r > t0:
                    t0 = r
            else:
                if r < t0:
                    return None
                if r < t1:
                    t1 = r
    if t0 > t1:
        return None
    return ((x0 + t0 * dx, y0 + t0 * dy), (x0 + t1 * dx, y0 + t1 * dy))


def _clip_frame(pts):
    """Split a projected polyline into in-frame sub-polylines, cut cleanly at the
    box edge. Segments fully outside are dropped entirely."""
    runs, cur = [], []
    for i in range(len(pts) - 1):
        seg = _lb_clip(pts[i], pts[i + 1])
        if seg is None:
            if len(cur) >= 2:
                runs.append(cur)
            cur = []
            continue
        p0, p1 = seg
        if not cur:
            cur = [p0, p1]
        elif abs(cur[-1][0] - p0[0]) < 1e-4 and abs(cur[-1][1] - p0[1]) < 1e-4:
            cur.append(p1)             # continuous with the previous segment
        else:
            if len(cur) >= 2:
                runs.append(cur)
            cur = [p0, p1]             # re-entered the frame: start a new run
    if len(cur) >= 2:
        runs.append(cur)
    return runs


def make_camera(spec: dict) -> Camera:
    d = spec.get("depth", {}) if spec else {}
    depth = DepthCue(
        mode=d.get("mode", "hue"),
        near_color=tuple(d.get("near_color", (0.4, 0.9, 1.0))),
        far_color=tuple(d.get("far_color", (0.1, 0.2, 0.5))),
        ttl_quantize=bool(d.get("ttl_quantize", False)),
    )
    spec = spec or {}
    return Camera(
        fov=spec.get("fov", 60.0),
        near=spec.get("near", 0.4),
        far=spec.get("far", 14.0),
        speed=spec.get("speed", 0.6),
        height=spec.get("height", 1.0),
        sway=spec.get("sway", 0.12),
        mode=spec.get("mode", "fly"),
        target=tuple(spec.get("target", (0.0, 0.0, 0.0))),
        orbit_radius=spec.get("orbit_radius", 9.0),
        orbit_height=spec.get("orbit_height", 1.5),
        max_strokes=spec.get("max_strokes", 90),
        depth=depth,
    )
