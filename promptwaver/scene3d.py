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


class PathSpline:
    """A closed Catmull-Rom spline through waypoints, parameterised by ARC
    LENGTH rather than by the raw spline parameter.

    Arc length matters: a Catmull-Rom segment is not traversed at uniform
    speed for uniform `t`, so parameterising by `t` makes the camera visibly
    accelerate wherever the author happened to space waypoints further apart.
    That reads as the path being wrong, not the spacing being uneven. The
    table is built once — a scene's waypoints don't change at runtime.

    Closed via wraparound control points, so position AND tangent are
    continuous across the seam. Merely repeating the first waypoint at the
    end joins the position but not the heading, which shows up as a flick of
    the view once per lap — the one artefact a looping journey can't hide.

    The camera aims at a point a fixed arc length further along the path
    (`Camera.lookahead`) rather than down the straight tangent, so the view
    leads into turns instead of swinging wide of them. How far ahead is a
    dial between walking and orbiting, and how much it matters depends
    entirely on how the scene's geometry is arranged. Two measurements,
    both against a 110-stroke budget:

      A 13-node room authored for a drifting camera (geometry in the middle,
      path wandering through it) — strokes on screen:

        lookahead   2 -> 20    4 -> 30    8 -> 49    11 -> 76    14 -> 102

      A 265-node circuit with geometry distributed ALONG the path, which is
      what a journey scene actually looks like:

        every lookahead from 2 to 45 -> 110 (saturated), 0% empty frames

    So coverage is a property of the scene, not of the camera: in a scene
    built to be walked through, "look where you're going" is perfectly full
    and the setting is purely about feel. It only becomes a survival
    question when a path is run through a scene composed for drift, where a
    short lookahead stares into the empty middle of the room.

    The same pairing runs the other way — drift on that 265-node circuit
    manages 1 stroke a frame and 91% empty, because it orbits the middle of
    a ring. Camera mode and geometry layout have to be chosen together.
    """

    def __init__(self, points, samples_per_seg: int = 32):
        p = np.asarray(points, np.float32).reshape(-1, 3)
        # Drop coincident neighbours, including a final point repeating the
        # first. The loop is closed by wraparound indexing, so an explicitly
        # repeated endpoint is a duplicate control point: it makes a
        # zero-length segment, and the tangent through a doubled point is
        # ill-defined. Authors close loops by hand all the time (the director
        # does), so this has to be tolerated rather than assumed away.
        keep = [0]
        for i in range(1, len(p)):
            if float(np.linalg.norm(p[i] - p[keep[-1]])) > 1e-4:
                keep.append(i)
        if len(keep) > 2 and float(np.linalg.norm(p[keep[-1]] - p[keep[0]])) <= 1e-4:
            keep.pop()
        p = p[keep]
        self.points = p
        self.n = len(p)
        # Sample every segment, accumulating chord length, and keep a global
        # parameter u = segment + local_t alongside it. u is continuous
        # across segment boundaries (i + 1.0 is the same place as (i+1) +
        # 0.0), which is what makes it safe to interpolate across them.
        cum = [0.0]
        us = [0.0]
        prev = self._eval(0, 0.0)[0]
        for i in range(self.n):
            for k in range(1, samples_per_seg + 1):
                t = k / samples_per_seg
                pos = self._eval(i, t)[0]
                d = float(np.linalg.norm(pos - prev))
                cum.append(cum[-1] + d)
                us.append(i + t)
                prev = pos
        self._cum = np.asarray(cum, np.float64)
        self._us = np.asarray(us, np.float64)
        self.length = float(self._cum[-1])

    def _eval(self, i: int, t: float):
        """Position and tangent on segment `i` at local parameter `t`."""
        n = self.n
        p0 = self.points[(i - 1) % n]
        p1 = self.points[i % n]
        p2 = self.points[(i + 1) % n]
        p3 = self.points[(i + 2) % n]
        t2 = t * t
        t3 = t2 * t
        a = 2.0 * p1
        b = -p0 + p2
        c = 2.0 * p0 - 5.0 * p1 + 4.0 * p2 - p3
        d = -p0 + 3.0 * p1 - 3.0 * p2 + p3
        pos = 0.5 * (a + b * t + c * t2 + d * t3)
        tan = 0.5 * (b + 2.0 * c * t + 3.0 * d * t2)
        return pos, tan

    def at(self, s: float):
        """(position, unit tangent, u) at arc-length `s`, wrapping the loop."""
        if self.length <= 1e-6:
            return self.points[0].copy(), np.array([0, 0, 1], np.float32), 0.0
        s = s % self.length
        j = int(np.searchsorted(self._cum, s, side="right"))
        j = min(max(j, 1), len(self._cum) - 1)
        c0, c1 = self._cum[j - 1], self._cum[j]
        frac = 0.0 if c1 - c0 <= 1e-9 else (s - c0) / (c1 - c0)
        u = self._us[j - 1] + (self._us[j] - self._us[j - 1]) * frac
        i = int(u) % self.n
        pos, tan = self._eval(i, u - int(u))
        ln = float(np.linalg.norm(tan))
        tan = tan / ln if ln > 1e-9 else np.array([0, 0, 1], np.float32)
        return pos, tan, u


def _basis(fwd):
    """A stable right/up basis around a forward vector. Falls back to a
    different world-up when the path is heading straight up or down, where
    the usual cross product degenerates."""
    world_up = np.array([0.0, 1.0, 0.0], np.float32)
    if abs(float(fwd @ world_up)) > 0.999:
        world_up = np.array([0.0, 0.0, 1.0], np.float32)
    right = np.cross(fwd, world_up)
    rn = float(np.linalg.norm(right))
    right = right / rn if rn > 1e-9 else np.array([1.0, 0.0, 0.0], np.float32)
    return right, np.cross(right, fwd)


def _drift_axis(base: float, p: float, f2: float, phase: float, wander: float) -> float:
    """One drift axis: the original sinusoid, optionally roughened.

    At `wander` 0 this returns `base` untouched, so every scene saved before
    this existed keeps exactly the motion it was tuned with — the drift
    rates are part of how those scenes look, not an implementation detail to
    quietly improve. Above 0 it mixes in a second, much slower component at
    an unrelated rate: the three original rates (0.11/0.09/0.13) are close
    enough to a simple ratio that the wander visibly retraces itself within a
    minute or two, which matters a lot more on something left running than on
    something glanced at. Renormalised so raising `wander` widens the path
    without also inflating its radius.
    """
    if wander <= 0.0:
        return base
    return (base + wander * 0.6 * math.sin(p * f2 + phase)) / (1.0 + wander * 0.6)


def _wander(t: float, phase, f0: float, f1: float) -> float:
    """Two sinusoids at deliberately unrelated rates, summed to roughly
    [-1, 1]. Their combined period is long enough not to read as a loop —
    a single sinusoid (or two at a simple ratio) is recognisably periodic
    within a minute or so, which is exactly the length of thing this has to
    stay interesting across."""
    return (math.sin(t * f0 + phase[0]) + 0.6 * math.sin(t * f1 + phase[1])) / 1.6


class Camera:
    """A drifting fly-through camera. Position advances along +Z; gentle yaw/pitch
    sway gives the floating feel. `speed` is read from the modulation matrix each
    frame, so audio/LFOs can steer the flythrough."""

    def __init__(self, *, fov=60.0, near=0.4, far=14.0, speed=0.6,
                 height=1.0, sway=0.12, depth: DepthCue | None = None,
                 max_strokes=90, aspect=1.0, mode="fly",
                 target=(0.0, 0.0, 0.0), orbit_radius=9.0, orbit_height=1.5,
                 waypoints=None, look_at=None, lookahead=3.0, seed=0, wander=0.0):
        self.fov = fov
        self.near = near
        self.far = far
        self.base_speed = speed
        self.height = height
        self.sway = sway
        self.depth = depth or DepthCue()
        self.max_strokes = max_strokes
        self.aspect = aspect
        self.mode = mode                       # "fly" | "orbit" | "drift" | "path"
        self.target = np.asarray(target, np.float32)
        self.orbit_radius = orbit_radius
        self.orbit_height = orbit_height
        self.z = 0.0                           # travelled distance (fly mode)
        self._yaw = 0.0
        self._pitch = 0.0
        self.pos = np.array([0.0, 0.0, -orbit_radius], np.float32)
        self._angle = 0.0
        self._drift_t = 0.0                    # integrated phase (drift mode)

        # 0 keeps drift bit-identical to how it has always behaved; see
        # _drift_axis. Path mode's own sway is separate and always on.
        self.wander = float(wander)

        # --- path mode ---
        self.look_at = list(look_at) if look_at else None
        self.path = None
        self._path_s = 0.0                     # arc length travelled
        self.lookahead = float(lookahead) if lookahead is not None else None
        if waypoints and len(waypoints) >= 3:
            self.path = PathSpline(waypoints)
            if self.lookahead is None:
                # A fraction of the LAP rather than an absolute distance,
                # because scene scale varies hugely — 4 units is a third of
                # the way across a small room and two paces in a castle.
                # A tenth reads as walking, which is what this mode is for.
                # Raise it toward 0.25 for a scene whose geometry sits in the
                # middle rather than along the path (see the class docstring);
                # push it to 0.5 and you are looking straight across the
                # circuit, which is orbit with extra steps.
                self.lookahead = self.path.length * 0.10
        else:
            if self.lookahead is None:
                self.lookahead = 3.0
            if self.mode == "path":
                # A path scene with no usable waypoints would otherwise
                # render from wherever `pos` was initialised and never move —
                # silently broken. Drift still shows the scene.
                print("[promptwaver] camera mode 'path' needs >=3 waypoints "
                      "— falling back to drift")
                self.mode = "drift"
        # Per-camera phase offsets so two scenes never wander in step. Fixed
        # `seed` (not time) keeps a saved journey reproducible frame for frame.
        rng = np.random.default_rng(int(seed) & 0xFFFFFFFF)
        self._phase = rng.uniform(0.0, 2 * math.pi, 8).tolist()

    def update(self, t: float, dt: float, matrix=None):
        speed = self.base_speed
        if matrix is not None:
            speed = matrix.value("camera.speed", self.base_speed)
        if self.mode == "fly":
            self.z += speed * dt
            self._yaw = math.sin(t * 0.11) * self.sway
            self._pitch = math.sin(t * 0.07 + 1.0) * self.sway * 0.4
        elif self.mode == "path" and self.path is not None:
            # Integrate DISTANCE from speed, for the same reason drift
            # integrates phase (see below): a modulation route driving
            # `camera.speed` changes it every frame, and multiplying raw `t`
            # by a moving speed jumps position discontinuously. Integrating
            # means audio changes the rate of travel, never the place.
            #
            # Because it's arc length into a closed loop, this also needs no
            # end handling at all — `PathSpline.at` wraps, so the walk simply
            # continues round the circuit.
            self._path_s += speed * dt
            on_path, fwd, u = self.path.at(self._path_s)
            right, up = _basis(fwd)

            # Sway rides on TIME, not on distance travelled: it's the float of
            # the body, not the walk. Tied to distance it would freeze solid
            # whenever audio drove the speed to zero, which is exactly when a
            # held, still frame most wants to keep breathing.
            lat = _wander(t, self._phase[0:2], 0.0731, 0.1373)
            ver = _wander(t, self._phase[2:4], 0.0587, 0.1094)
            self.pos = (on_path + right * (lat * self.sway * 3.0)
                        + up * (ver * self.sway * 1.5))

            # Aim at a point further ALONG THE PATH, not along the straight
            # tangent. On a closed circuit this is what decides whether
            # anything is on screen at all: a short lookahead stares into
            # whatever is directly in front (measured at ~14 strokes of a
            # 110 budget, i.e. a nearly dark beam), while a lookahead of
            # roughly a third of a lap aims across the space and keeps the
            # scene's bulk in the cone (~100 strokes). Following the curve
            # also means the view leads into turns instead of swinging wide
            # of them.
            ahead = self.path.at(self._path_s + self.lookahead)[0]
            tgt = ahead
            if self.look_at:
                i = int(u) % self.path.n
                frac = u - int(u)
                a = self.look_at[i] if i < len(self.look_at) else None
                b_i = (i + 1) % self.path.n
                b = self.look_at[b_i] if b_i < len(self.look_at) else None
                ta = np.asarray(a, np.float32) if a is not None else ahead
                tb = np.asarray(b, np.float32) if b is not None else ahead
                tgt = ta + (tb - ta) * frac
            # A little independent wobble on the aim, so the head turns
            # slightly rather than the whole view sliding rigidly sideways.
            self.target = (tgt
                           + right * (_wander(t, self._phase[4:6], 0.0431, 0.0917)
                                      * self.sway * 1.2)
                           + up * (_wander(t, self._phase[6:8], 0.0367, 0.0813)
                                   * self.sway * 0.6))
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
            w = self.wander
            self.pos = self.target + np.array([
                _drift_axis(math.sin(p * 0.11), p, 0.043, self._phase[0], w) * r,
                self.orbit_height
                + _drift_axis(math.sin(p * 0.09), p, 0.037, self._phase[1], w) * r * 0.4,
                _drift_axis(math.cos(p * 0.13), p, 0.051, self._phase[2], w) * r], np.float32)

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
    # DIVIDE by aspect, not multiply. `aspect` is width/height of the output
    # viewport, and x in [-1,1] has to span that whole width — so a wider
    # viewport must show MORE world horizontally, not less, or everything
    # comes out stretched. (Aspect was hardcoded to 1.0 until output ratios
    # existed, where the two conventions are identical, which is how this sat
    # backwards unnoticed.) `fov` stays the VERTICAL field of view, so
    # changing the ratio widens the view rather than cropping it.
    px = x / zc * f / aspect
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
        # Path mode (mode="path"): a closed circuit of >=3 waypoints. `look_at`
        # is a parallel list whose entries are either a point to watch while
        # passing that waypoint, or null to just look where you're going.
        waypoints=spec.get("waypoints"),
        look_at=spec.get("look_at"),
        # None (the default) means "a quarter of the lap" — see Camera.__init__
        lookahead=spec.get("lookahead"),
        wander=spec.get("wander", 0.0),
        seed=spec.get("seed", 0),
    )
