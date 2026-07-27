"""Realtime audio diagnostics.

sounddevice's callback receives a `status` object with hardware-reported xrun
flags (output_underflow etc.) — that's ground truth from PortAudio, not a
guess. Combined with timing each callback's actual render duration against its
real-time budget, this tells us WHERE a glitch is coming from:

  - underruns == 0 but duration regularly near/over budget -> our render is
    too slow for the chosen blocksize (raise blocksize, or it's a real DSP
    cost problem).
  - underruns > 0 with duration comfortably under budget -> something below
    us is stalling the callback (OS scheduling, another app holding the
    device, a sandboxed/virtual audio layer such as PulseAudio-via-snap
    adding jitter). No amount of optimizing render() fixes this class.
  - irregular callback *intervals* much larger than blocksize/sr, independent
    of duration -> the callback itself is being delayed before it even starts
    (scheduling/OS-level), also not a render-cost problem.

This module stays dependency-light (stdlib only) so it works even when the
rest of the audio stack is struggling.
"""

from __future__ import annotations

import collections
import time


class CallbackStats:
    def __init__(self, sr: int, blocksize: int, window: int = 400):
        self.sr = sr
        self.blocksize = blocksize
        self.expected_interval = blocksize / sr
        self.durations = collections.deque(maxlen=window)
        self.intervals = collections.deque(maxlen=window)
        self.underrun_events = collections.deque(maxlen=25)
        self.count = 0
        self.underrun_count = 0
        # A soundscape fade (SoundscapeMixer._phase is not None — its own
        # fade-out/swap/fade-in, independent of and often longer-lived than
        # the visual scene crossfade) still renders a live Soundscape and
        # scales its output during this window; tallied separately so a
        # heavier scene's fade lasting longer than the visual one is
        # visible in the numbers instead of assumed. (A prior version
        # rendered the outgoing AND incoming soundscape simultaneously here,
        # doubling the per-callback cost for the whole fade — see
        # SoundscapeMixer's docstring in dsp.py for why that changed.)
        # instead of guessed at.
        self.xfade_count = 0
        self.xfade_over_budget = 0
        self._last_start = None
        self._t0 = time.monotonic()

    def record(self, duration: float, status, crossfading: bool = False) -> None:
        now = time.monotonic()
        self.count += 1
        self.durations.append(duration)
        if self._last_start is not None:
            self.intervals.append(now - self._last_start)
        self._last_start = now

        if crossfading:
            self.xfade_count += 1
            if duration * 1000 > self.expected_interval * 1000:
                self.xfade_over_budget += 1

        flags = [name for name in
                 ("output_underflow", "input_underflow",
                  "output_overflow", "input_overflow", "priming_output")
                 if getattr(status, name, False)]
        if flags:
            self.underrun_count += 1
            self.underrun_events.append({"t": round(now - self._t0, 3), "flags": flags,
                                         "crossfade": crossfading})

    def summary(self) -> dict:
        d = list(self.durations)
        iv = list(self.intervals)
        budget_ms = self.expected_interval * 1000
        return {
            "sr": self.sr,
            "blocksize": self.blocksize,
            "expected_interval_ms": round(budget_ms, 2),
            "callbacks": self.count,
            "underruns": self.underrun_count,
            # plain sum()/len(), not statistics.mean() — see perf.py's
            # summary() for why (mean() is ~100x slower here for no benefit,
            # and this runs on the same contended broadcaster thread).
            "avg_duration_ms": round(sum(d) / len(d) * 1000, 3) if d else 0.0,
            "max_duration_ms": round(max(d) * 1000, 3) if d else 0.0,
            "duration_budget_pct": round((max(d) * 1000 / budget_ms * 100), 1) if d and budget_ms else 0.0,
            "avg_interval_ms": round(sum(iv) / len(iv) * 1000, 3) if iv else 0.0,
            "max_interval_ms": round(max(iv) * 1000, 3) if iv else 0.0,
            "xfade_callbacks": self.xfade_count,
            "xfade_over_budget": self.xfade_over_budget,
            "recent_underruns": list(self.underrun_events),
        }


def list_devices() -> dict:
    """Enumerate output-capable audio devices. Safe to call even if no stream
    is currently open; returns an error string rather than raising."""
    try:
        import sounddevice as sd
        devices = sd.query_devices()
        hostapis = sd.query_hostapis()
        out = []
        for i, d in enumerate(devices):
            if d.get("max_output_channels", 0) > 0:
                out.append({
                    "index": i,
                    "name": d.get("name", f"device {i}"),
                    "hostapi": hostapis[d["hostapi"]]["name"] if d.get("hostapi") is not None else "",
                    "default_sr": d.get("default_samplerate"),
                    "max_out": d.get("max_output_channels"),
                })
        default = sd.default.device
        # sd.default.device can be a plain int, a list/tuple, or (on some
        # sounddevice versions) a `_InputOutputPair` object that supports
        # indexing but isn't a list/tuple instance — isinstance() misses it,
        # and that whole non-plain object then fails to JSON-serialize. Index
        # into it defensively and coerce to a plain int so this can never
        # break the state broadcast again regardless of sounddevice's type.
        try:
            default_out = default[1]
        except (TypeError, IndexError, KeyError):
            default_out = default
        try:
            default_out = int(default_out)
        except (TypeError, ValueError):
            default_out = None
        return {"ok": True, "devices": out, "default_output": default_out, "error": None}
    except Exception as e:
        return {"ok": False, "devices": [], "default_output": None, "error": str(e)}
