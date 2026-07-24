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
    """

    def __init__(self, initial: float = 0.0, smooth: float = 0.0):
        self.current = initial
        self.smooth = smooth        # 0 = none, ->1 = heavy slew
        self._v = initial

    def sample(self, t: float, dt: float) -> float:
        if self.smooth > 0:
            k = min(1.0, dt / max(self.smooth, 1e-4))
            self._v += (self.current - self._v) * k
        else:
            self._v = self.current
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
