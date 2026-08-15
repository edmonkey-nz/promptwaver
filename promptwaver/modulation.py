"""The modulation matrix — PromptWaver's shared nervous system.

Sources (LFOs, ADSR envelopes, audio level, the transport clock) are sampled
once per tick into a flat dict of named values. Routes then add scaled source
values onto named destination parameters. Crucially, destinations live in
*both* domains: an audio-level source can open a visual turbulence param while
an LFO opens a synth filter. That bidirectional coupling is what makes this one
instrument rather than two apps side by side.

Destination keys are namespaced strings, e.g. "visual.speed", "audio.cutoff",
"scene.morph". A generator or the synth simply calls `matrix.value("visual.speed",
base)` to read its modulated parameter.
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, field


# --- sources -----------------------------------------------------------------

class Source:
    """Base class. `sample(t, dt)` returns the current value."""

    def sample(self, t: float, dt: float) -> float:  # pragma: no cover
        raise NotImplementedError


class LFO(Source):
    """Low-frequency oscillator. Output range depends on `unipolar`."""

    SHAPES = ("sine", "triangle", "saw", "square")

    def __init__(self, rate: float = 0.1, shape: str = "sine",
                 phase: float = 0.0, unipolar: bool = True):
        self.rate = rate            # Hz
        self.shape = shape
        self.phase = phase          # 0..1
        self.unipolar = unipolar    # True -> 0..1, False -> -1..1

    def sample(self, t: float, dt: float) -> float:
        x = (t * self.rate + self.phase) % 1.0
        if self.shape == "sine":
            v = math.sin(2 * math.pi * x)
        elif self.shape == "triangle":
            v = 4 * abs(x - 0.5) - 1
        elif self.shape == "saw":
            v = 2 * x - 1
        else:  # square
            v = 1.0 if x < 0.5 else -1.0
        return (v + 1) * 0.5 if self.unipolar else v


class Envelope(Source):
    """A minimal ADSR. Call `trigger()` to start, `release()` to release.

    Feeds both the synth (amplitude/filter) and, if routed, visual params —
    so a note swell can bloom the visuals too.
    """

    def __init__(self, attack=1.0, decay=0.5, sustain=0.7, release=3.0):
        self.a, self.d, self.s, self.r = attack, decay, sustain, release
        self._stage = "idle"        # idle | attack | decay | sustain | release
        self._level = 0.0
        self._rel_from = 0.0

    def trigger(self):
        self._stage = "attack"

    def release(self):
        self._rel_from = self._level
        self._stage = "release"

    def sample(self, t: float, dt: float) -> float:
        if self._stage == "attack":
            self._level += dt / max(self.a, 1e-4)
            if self._level >= 1.0:
                self._level, self._stage = 1.0, "decay"
        elif self._stage == "decay":
            self._level -= dt * (1 - self.s) / max(self.d, 1e-4)
            if self._level <= self.s:
                self._level, self._stage = self.s, "sustain"
        elif self._stage == "release":
            self._level -= dt * self._rel_from / max(self.r, 1e-4)
            if self._level <= 0.0:
                self._level, self._stage = 0.0, "idle"
        return self._level


class Value(Source):
    """A plain externally-driven value, e.g. the live audio RMS level.

    The audio analysis thread writes `.current`; the matrix reads it. This is
    the audio -> visual bridge.

    `delay` holds the value back by that many seconds before it is used, which
    exists to fix a lead rather than to create an effect. The synth measures
    each block's energy at the moment it *generates* the block
    (`Soundscape._update_bands`), and that block is then handed to the output
    device and heard one stream-latency later — so without this, visuals
    driven by the engine's own sound arrive before the sound does. Measured on
    this machine at blocksize 8192: 186ms of stream latency plus half a block,
    against ~90ms of slew and tick lag pulling the other way, for a net visual
    lead of nearly 0.2s.

    Only the *synth* path can be corrected this way, and only because we know
    the audio before it is audible. `mic_level` has the opposite problem — it
    measures sound that has already played — and no amount of buffering can
    advance a signal, so it is left alone.
    """

    def __init__(self, initial: float = 0.0, smooth: float = 0.0, delay: float = 0.0):
        self.current = initial
        self.smooth = smooth        # 0 = none, ->1 = heavy slew
        self.delay = delay          # seconds to hold the value back
        self._v = initial
        # Own clock, accumulated from REAL dt. Deliberately not the `t` passed
        # to sample(): that is the scene clock, which Freeze ramps to a
        # standstill — a delay line running on it would stall mid-buffer and
        # never deliver, while the whole point of Freeze is that audio
        # reactivity keeps working.
        self._clock = 0.0
        self._hist: deque[tuple[float, float]] = deque()

    def sample(self, t: float, dt: float) -> float:
        self._clock += dt
        target = self.current
        if self.delay > 0:
            self._hist.append((self._clock, self.current))
            cutoff = self._clock - self.delay
            # Keep exactly one sample older than the cutoff so there is always
            # a pair to interpolate between.
            while len(self._hist) > 1 and self._hist[1][0] <= cutoff:
                self._hist.popleft()
            t0, v0 = self._hist[0]
            if len(self._hist) > 1 and cutoff > t0:
                t1, v1 = self._hist[1]
                span = t1 - t0
                # Interpolated, not nearest: the render tick is ~22ms and the
                # delay is set from audio timing, so snapping to the nearest
                # stored sample would quantise the correction to the frame
                # rate and reintroduce its own stepping.
                target = v0 + (v1 - v0) * ((cutoff - t0) / span) if span > 1e-9 else v1
            else:
                target = v0
        if self.smooth > 0:
            k = min(1.0, dt / max(self.smooth, 1e-4))
            self._v += (target - self._v) * k
        else:
            self._v = target
        return self._v


# --- routing -----------------------------------------------------------------

@dataclass
class Route:
    source: str          # source name
    dest: str            # destination key, e.g. "visual.speed"
    depth: float = 1.0   # how much of the source to add
    bias: float = 0.0    # constant offset added to the source before scaling


class ModMatrix:
    def __init__(self):
        self.sources: dict[str, Source] = {}
        self.routes: list[Route] = []
        self._values: dict[str, float] = {}
        # per-source multiplier — the "level effect": turn a whole source's
        # influence up or down (e.g. all audio_level-driven routes at once)
        # without touching each route's individually-authored depth.
        self.source_scale: dict[str, float] = {}

    def set_source_scale(self, source: str, scale: float):
        self.source_scale[source] = scale

    # setup
    def add_source(self, name: str, source: Source) -> Source:
        self.sources[name] = source
        return source

    def add_route(self, source: str, dest: str, depth: float = 1.0, bias: float = 0.0):
        self.routes.append(Route(source, dest, depth, bias))

    def clear_routes(self):
        self.routes.clear()

    # per-tick
    def update(self, t: float, dt: float):
        self._values = {name: s.sample(t, dt) for name, s in self.sources.items()}

    def source_value(self, name: str, default: float = 0.0) -> float:
        return self._values.get(name, default)

    def value(self, dest: str, base: float = 0.0) -> float:
        """Base parameter plus the sum of all routes targeting `dest`."""
        v = base
        for r in self.routes:
            if r.dest == dest:
                scale = self.source_scale.get(r.source, 1.0)
                v += (self._values.get(r.source, 0.0) + r.bias) * r.depth * scale
        return v
