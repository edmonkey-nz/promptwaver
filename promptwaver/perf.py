"""Realtime render-loop diagnostics — the visual-side counterpart to
`audio/diagnostics.py`'s `CallbackStats`.

The engine's render loop (`Engine._loop`) is a fixed-rate thread: render the
scene, write it to the output, sleep off whatever's left of the frame budget.
When something's slow, the *symptom* (stutter, glitching during a scene fade)
doesn't say WHERE the cost is — this measures each stage so that's visible
instead of guessed at:

  - `render_ms`  time spent building the frame (generators + camera projection
                 + crossfade compositing) — the part scene complexity affects.
  - `output_ms`  time spent handing the frame to the output (DAC write, or the
                 null-output's point-count pass).
  - `interval_ms` wall-clock time between one tick's start and the next —
                 if this runs well above the nominal frame period even though
                 render+output are comfortably under budget, something outside
                 this loop (OS scheduling, GIL contention with the audio
                 callback thread) is stealing time, not the render itself.
  - dropped ticks: a tick whose total work exceeded the frame budget, so it
                 skipped its sleep entirely — the direct measurement of "the
                 engine fell behind its target FPS".

Crossfades render two full scenes for the transition's duration, so ticks
during a crossfade are tallied separately — this is what lets "lag right
when scenes fade" be confirmed or ruled out from the numbers instead of a
guess.
"""

from __future__ import annotations

import collections
import time


class LoopStats:
    def __init__(self, fps: int, window: int = 300):
        self.fps = fps
        self.period = 1.0 / fps
        self.render_ms = collections.deque(maxlen=window)
        self.output_ms = collections.deque(maxlen=window)
        self.interval_ms = collections.deque(maxlen=window)
        self.points = collections.deque(maxlen=window)
        self.tick_count = 0
        self.dropped_count = 0          # ticks with no time left to sleep
        self.xfade_ticks = 0
        self.xfade_dropped = 0
        self.drop_events = collections.deque(maxlen=25)
        self._t0 = time.monotonic()
        self._last_start = None

    def set_fps(self, fps: int):
        self.fps = fps
        self.period = 1.0 / fps if fps else self.period

    def tick(self):
        """Cheap, always-safe-to-call interval tracking — one timestamp
        diff and a deque append, nothing else. This is what keeps the FPS
        readout working even with the fuller instrumentation switched off
        (Settings > Diagnostics / --no-diag): the detailed per-stage timing
        in `record()` below costs a bit more (a handful of extra
        `time.monotonic()` calls per tick) and is the part that's actually
        optional; a live FPS number is cheap enough to just always have."""
        now = time.monotonic()
        self.tick_count += 1
        if self._last_start is not None:
            self.interval_ms.append((now - self._last_start) * 1000)
        self._last_start = now

    def record(self, *, render_s: float, output_s: float, total_s: float,
               n_points: int, crossfading: bool):
        """Full per-tick detail — render/output timing, dropped-tick and
        crossfade correlation, point counts. Call `tick()` first each
        iteration; this adds the detailed fields on top of it."""
        now = time.monotonic()
        self.render_ms.append(render_s * 1000)
        self.output_ms.append(output_s * 1000)
        self.points.append(n_points)

        dropped = total_s > self.period
        if crossfading:
            self.xfade_ticks += 1
        if dropped:
            self.dropped_count += 1
            if crossfading:
                self.xfade_dropped += 1
            self.drop_events.append({
                "t": round(now - self._t0, 3),
                "over_ms": round((total_s - self.period) * 1000, 1),
                "crossfade": crossfading,
            })

    def summary(self) -> dict:
        r, o, iv, pts = list(self.render_ms), list(self.output_ms), list(self.interval_ms), list(self.points)
        # plain sum()/len(), not statistics.mean(): mean() does exact-precision
        # rational arithmetic internally (via Fraction) — ~100x slower than a
        # float division for a list this size (benchmarked: ~500us vs ~5us
        # per call), total overkill for a performance counter, and this
        # summary runs on the same broadcaster thread that's already
        # competing with the render/audio threads for the GIL at ~20Hz.
        avg_interval = sum(iv) / len(iv) if iv else self.period * 1000
        return {
            "target_fps": self.fps,
            "fps": round(1000.0 / avg_interval, 1) if avg_interval else 0.0,
            "ticks": self.tick_count,
            "avg_render_ms": round(sum(r) / len(r), 2) if r else 0.0,
            "max_render_ms": round(max(r), 2) if r else 0.0,
            "avg_output_ms": round(sum(o) / len(o), 2) if o else 0.0,
            "max_output_ms": round(max(o), 2) if o else 0.0,
            "avg_interval_ms": round(avg_interval, 2),
            "max_interval_ms": round(max(iv), 2) if iv else 0.0,
            "budget_ms": round(self.period * 1000, 2),
            "dropped": self.dropped_count,
            "dropped_pct": round(100.0 * self.dropped_count / self.tick_count, 1) if self.tick_count else 0.0,
            "xfade_ticks": self.xfade_ticks,
            "xfade_dropped": self.xfade_dropped,
            "avg_points": round(sum(pts) / len(pts), 0) if pts else 0,
            "max_points": max(pts) if pts else 0,
            "recent_drops": list(self.drop_events),
        }
