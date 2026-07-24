"""ILDA point streaming and Helios DAC output.

A Frame (list of Paths in normalized coords) becomes a flat point stream the
galvos can scan: each path is resampled to a max step size, blanked travel
points are inserted between paths, and a few dwell points are added at path
ends so corners don't overshoot. Coordinates map [-1,1] -> 12-bit [0,4095].

`HeliosOutput` wraps the official Helios SDK via ctypes (drop your existing
laserx3 wrapper in here if you prefer). If the shared library isn't present it
degrades to `NullOutput`, so PromptWaver runs and previews with no hardware.

SAFETY: keystone correction and invert are applied to the DAC output only.
Blanking failures leave a stationary hot spot — always bench-test with the
laser at minimum current and wear appropriate eyewear.
"""

from __future__ import annotations

import ctypes
import os
import time

import numpy as np

from ..geometry import Frame

_MAX = 4095


def _hex_to_rgb(h: str):
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) / 255.0 for i in (0, 2, 4))


class HeliosPoint(ctypes.Structure):
    _fields_ = [("x", ctypes.c_uint16), ("y", ctypes.c_uint16),
                ("r", ctypes.c_uint8), ("g", ctypes.c_uint8),
                ("b", ctypes.c_uint8), ("i", ctypes.c_uint8)]


_POINT_DTYPE = np.dtype([("x", "<u2"), ("y", "<u2"), ("r", "u1"),
                          ("g", "u1"), ("b", "u1"), ("i", "u1")])


class PathPlanner:
    """Turns a Frame into a HeliosPoint array.

    Vectorised end to end: arc-length resampling per path uses np.interp
    (replacing a per-point Python loop), the DAC coordinate transform
    (keystone + clip + scale) runs once over the whole frame's points as numpy
    array ops, and the result is handed to ctypes as a zero-copy buffer view
    rather than constructed one Python object at a time.

    This matters beyond raw speed: the old per-point Python loop held the GIL
    almost continuously while it ran, competing directly with the realtime
    audio callback thread for scheduling — a rich scene (hundreds of strokes)
    could visibly worsen audio glitching purely from this, independent of
    anything in the audio DSP itself (confirmed: lowering max_strokes measurably
    reduced audio underruns even though max_strokes is a purely visual knob).
    """

    def __init__(self, max_step=0.03, invert_x=False, invert_y=False,
                 keystone_h=0.0, keystone_v=0.0, blank_dwell=3, corner_dwell=2):
        self.max_step = max_step
        self.invert_x = invert_x
        self.invert_y = invert_y
        self.keystone_h = keystone_h
        self.keystone_v = keystone_v
        self.blank_dwell = blank_dwell
        self.corner_dwell = corner_dwell

    def _to_dac_vec(self, xy: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        """Vectorised version of the old per-point `_to_dac`: keystone, clip,
        scale to 12-bit DAC range, applied to a whole (N,2) array at once."""
        x = xy[:, 0].astype(np.float64, copy=True)
        y = xy[:, 1].astype(np.float64, copy=True)
        if self.invert_x:
            x = -x
        if self.invert_y:
            y = -y
        x = x * (1.0 + self.keystone_h * y)
        y = y * (1.0 + self.keystone_v * x)
        np.clip(x, -1.0, 1.0, out=x)
        np.clip(y, -1.0, 1.0, out=y)
        ix = ((x + 1.0) * 0.5 * _MAX).astype(np.uint16)
        iy = ((y + 1.0) * 0.5 * _MAX).astype(np.uint16)
        return ix, iy

    def _resample_path(self, P: np.ndarray) -> np.ndarray:
        """Arc-length resample a path to ~max_step spacing using np.interp —
        one vectorised call per path instead of a Python loop per output point."""
        if len(P) == 1:
            return P
        d = np.hypot(*np.diff(P, axis=0).T)
        cum = np.concatenate([[0.0], np.cumsum(d)])
        total = cum[-1]
        if total <= 1e-9:
            return P[:1]
        n_samples = max(1, int(total / self.max_step))
        sample_d = np.linspace(0.0, total, n_samples, endpoint=False)
        x = np.interp(sample_d, cum, P[:, 0])
        y = np.interp(sample_d, cum, P[:, 1])
        return np.stack([x, y], axis=1)

    def plan_arrays(self, frame: Frame):
        """Build the flat (x, y, r, g, b, i) arrays for a frame.

        Collects LOCAL-space (untransformed) coordinates across the *whole*
        frame first, then does exactly one batched DAC transform at the end —
        not one per stroke. Fixed-size per-stroke segments (blank travel,
        corner dwell — 2-3 points each) are built as plain Python lists rather
        than numpy calls: at that size, numpy's per-call dispatch overhead
        costs more than the operation itself, and a scene with ~100 strokes
        means ~100x that overhead if done per-stroke. Only the genuinely
        variable-length resampling step still calls numpy per stroke (that's
        where vectorisation actually pays for itself).
        """
        if not frame:
            return None
        lx_all: list[float] = []
        ly_all: list[float] = []
        r_all: list[int] = []
        g_all: list[int] = []
        b_all: list[int] = []
        i_all: list[int] = []
        last_local = None

        for path in frame:
            P = path.points
            if len(P) == 0:
                continue
            if path.closed and len(P) > 1:
                P = np.vstack([P, P[:1]])
            r, g, b = (int(c * 255) for c in path.color)
            resampled = self._resample_path(P)
            ex, ey = float(P[-1, 0]), float(P[-1, 1])
            n = len(resampled)
            sx, sy = (float(resampled[0, 0]), float(resampled[0, 1])) if n else (ex, ey)

            if last_local is not None:
                lx, ly = last_local
                lx_all += [lx] * self.blank_dwell; ly_all += [ly] * self.blank_dwell
                r_all += [0] * self.blank_dwell; g_all += [0] * self.blank_dwell
                b_all += [0] * self.blank_dwell; i_all += [0] * self.blank_dwell
                lx_all += [sx] * self.blank_dwell; ly_all += [sy] * self.blank_dwell
                r_all += [0] * self.blank_dwell; g_all += [0] * self.blank_dwell
                b_all += [0] * self.blank_dwell; i_all += [0] * self.blank_dwell

            if n:
                lx_all.extend(resampled[:, 0].tolist())
                ly_all.extend(resampled[:, 1].tolist())
                r_all += [r] * n; g_all += [g] * n; b_all += [b] * n; i_all += [255] * n

            lx_all += [ex] * self.corner_dwell; ly_all += [ey] * self.corner_dwell
            r_all += [r] * self.corner_dwell; g_all += [g] * self.corner_dwell
            b_all += [b] * self.corner_dwell; i_all += [255] * self.corner_dwell
            last_local = (ex, ey)

        if not lx_all:
            return None

        xy_local = np.array([lx_all, ly_all], dtype=np.float64).T
        ix, iy = self._to_dac_vec(xy_local)
        return (ix, iy, np.array(r_all, dtype=np.uint8), np.array(g_all, dtype=np.uint8),
                np.array(b_all, dtype=np.uint8), np.array(i_all, dtype=np.uint8))

    def point_count(self, frame: Frame) -> int:
        """Cheap point count for UI/no-hardware use — same vectorised path as
        `plan()` minus the ctypes buffer construction."""
        arrays = self.plan_arrays(frame)
        return 0 if arrays is None else len(arrays[0])

    def plan(self, frame: Frame):
        """Returns a ctypes array of HeliosPoint, built as a zero-copy view
        over a numpy structured array rather than one Python object per point."""
        arrays = self.plan_arrays(frame)
        if arrays is None:
            return []
        x, y, r, g, b, i = arrays
        n = len(x)
        buf = np.empty(n, dtype=_POINT_DTYPE)
        buf["x"] = x; buf["y"] = y
        buf["r"] = r; buf["g"] = g; buf["b"] = b; buf["i"] = i
        try:
            return (HeliosPoint * n).from_buffer(buf)
        except (TypeError, ValueError):
            # non-contiguous or otherwise unshareable buffer — fall back to a
            # still-fast bulk construction rather than crashing
            return (HeliosPoint * n)(*[HeliosPoint(*row) for row in buf.tolist()])


class NullOutput:
    """No hardware: just tracks the last frame's point count for the UI."""

    name = "null"

    def __init__(self, **planner_kw):
        self.planner = PathPlanner(**planner_kw)
        self.last_points = 0

    def write(self, frame: Frame, pps: int):
        # Previously called the full plan() (including ctypes buffer
        # construction) purely to get a length for a UI counter, on every
        # tick, with no hardware to actually send it to. point_count() does
        # the same vectorised resampling/transform but skips the ctypes step.
        self.last_points = self.planner.point_count(frame)

    def blank(self):
        """No hardware beam to kill, but keep the point counter honest."""
        self.last_points = 0

    def close(self):
        pass


class HeliosOutput:
    """Streams frames to a Helios DAC over the official SDK (ctypes)."""

    name = "helios"

    def __init__(self, lib_path: str | None = None, device: int = 0, **planner_kw):
        self.planner = PathPlanner(**planner_kw)
        self.device = device
        self.last_points = 0
        path = lib_path or os.environ.get("HELIOS_LIB", "libHeliosDacAPI.so")
        self.lib = ctypes.cdll.LoadLibrary(path)
        n = self.lib.OpenDevices()
        if n <= 0:
            raise RuntimeError("no Helios DAC found")

    def _wait_ready(self):
        # Wait until the DAC is ready for the next frame. This used to be a
        # naked `while ...: pass` spin loop — with the GIL, a tight spin like
        # that reacquires/releases it at very high frequency, which can starve
        # other threads (notably the realtime audio callback) of scheduling
        # time even though each individual ctypes call releases the GIL while
        # it runs. Sleeping briefly between polls fixes that at the cost of a
        # sub-millisecond wait, which is irrelevant next to frame timing.
        # A bounded timeout also prevents a wedged DAC from hanging the whole
        # render thread forever.
        deadline = time.monotonic() + 0.5
        while self.lib.GetStatus(self.device) != 1:
            if time.monotonic() > deadline:
                print("[promptwaver] Helios DAC not ready after 0.5s; sending anyway")
                break
            time.sleep(0.0003)

    def _write_points(self, points, pps):
        if not points:
            return
        n = len(points)
        # plan() already returns a proper ctypes Array (zero-copy view over a
        # numpy buffer) — reconstructing it here via (HeliosPoint*n)(*points)
        # would unpack every point through Python again, throwing away exactly
        # the optimisation plan() exists to provide. Only build a fresh array
        # for the other caller (blank()), which hands over a plain list.
        arr = points if isinstance(points, ctypes.Array) else (HeliosPoint * n)(*points)
        self._wait_ready()
        # flags 0; last arg is frame flag per SDK
        self.lib.WriteFrame(self.device, pps, 0, ctypes.byref(arr), n)

    def write(self, frame: Frame, pps: int):
        points = self.planner.plan(frame)
        self.last_points = len(points)
        self._write_points(points, pps)

    def blank(self):
        """Send an explicit zero-intensity frame — a real 'beam off' command to
        the DAC, not just skipping a write (which would leave the last frame
        looping on the device's own buffer). Bypasses the scene planner
        entirely so it works even if the scene/engine state is in a bad way."""
        pts = [HeliosPoint(2047, 2047, 0, 0, 0, 0) for _ in range(4)]
        self.last_points = 0
        self._write_points(pts, 2000)

    def close(self):
        try:
            self.lib.CloseDevices()
        except Exception:
            pass


def make_output(enable_laser: bool, **planner_kw):
    """Return a HeliosOutput if requested and available, else NullOutput."""
    if enable_laser:
        try:
            return HeliosOutput(**planner_kw)
        except Exception as e:  # missing lib or no device
            print(f"[promptwaver] laser output unavailable ({e}); using null output")
    return NullOutput(**planner_kw)
