"""Soundscape DSP core — pure numpy, no C extensions, no audio I/O.

This is the testable heart of the synth: given a soundscape spec, `Soundscape`
renders blocks of stereo float samples. `synth.py` wraps this with sounddevice
for realtime output; keeping the DSP separate means it can be rendered and
checked offline (finite, bounded, non-silent, delay produces echoes, etc.)
without any audio device.

Design choices that keep it fast and click-free in pure Python:
- Additive tone generation (summed partials with a brightness rolloff) instead
  of per-sample IIR filters — fully vectorised per block.
- Phase derived from an absolute sample clock, so tones stay continuous across
  block boundaries with no stored per-oscillator phase.
- A block-granular delay line (delay >= one block) so feedback needs no
  per-sample recursion.

A soundscape spec (JSON, stored in the scene):
{
  "tempo": 60, "master": 0.8, "distortion": 0.0,
  "delay": {"time": 0.4, "feedback": 0.35, "mix": 0.3},
  "voices": [
    {"name":"drone","type":"pad","waveform":"saw","note":36,"chord":[0,7,12],
     "level":0.5,"tone":0.4,"detune":0.01,"pan":0.0,"mute":false},
    {"name":"bells","type":"pluck","waveform":"sine","note":72,
     "scale":[0,3,7,10],"level":0.3,"rate":1.0,"decay":1.2,"pan":0.2,"mute":false},
    {"name":"air","type":"noise","level":0.15,"tone":0.5,"pan":0.0,"mute":false}
  ]
}
"""

from __future__ import annotations

import random

import numpy as np

from ..modulation import Envelope

SR = 44100
VOICE_TYPES = ("pad", "pluck", "noise", "sub", "osc")
WAVEFORMS = ("sine", "saw", "square", "triangle")


def midi_to_hz(n: float) -> float:
    return 440.0 * 2.0 ** ((n - 69) / 12.0)


def _osc(phase: np.ndarray, waveform: str) -> np.ndarray:
    """Oscillator from phase in [0,1). Vectorised."""
    if waveform == "saw":
        return 2.0 * (phase % 1.0) - 1.0
    if waveform == "square":
        return np.where((phase % 1.0) < 0.5, 1.0, -1.0)
    if waveform == "triangle":
        return 4.0 * np.abs((phase % 1.0) - 0.5) - 1.0
    return np.sin(2.0 * np.pi * phase)          # sine (default)


class Delay:
    """Block-granular stereo delay/echo. Delay time is clamped to >= one block
    so read/write ranges never overlap within a block — this lets the whole
    block be processed as vectorised numpy ops with no per-sample Python loop,
    which matters because this runs inside a realtime audio callback where a
    slow block is an audible glitch, not just a slow frame."""

    def __init__(self, sr=SR, max_seconds=2.0):
        self.sr = sr
        self.buf = np.zeros((int(sr * max_seconds), 2), np.float32)
        self.buflen = len(self.buf)
        self.w = 0

    def process(self, block, time_s, feedback, mix):
        n = len(block)
        d = int(np.clip(time_s, n / self.sr, self.buflen / self.sr - 0.01) * self.sr)
        d = max(d, n)                              # guarantee no in-block overlap
        read_idx = (self.w - d + np.arange(n)) % self.buflen
        write_idx = (self.w + np.arange(n)) % self.buflen

        delayed = self.buf[read_idx]
        wet = block + feedback * delayed
        self.buf[write_idx] = wet
        out = block * (1 - mix) + delayed * mix

        self.w = (self.w + n) % self.buflen
        return out


class Soundscape:
    def __init__(self, spec: dict | None = None, sr: int = SR):
        self.sr = sr
        self._clock = 0                          # absolute sample index
        self._delay = Delay(sr)
        self._active_notes: list[dict] = []      # for pluck/arp voices
        self._voice_env: dict[str, Envelope] = {}     # ADSR per pad/osc/sub voice
        self._voice_env_on: dict[str, bool] = {}       # last known trigger state
        self.spec = {}
        self.muted = False        # engine-level gate; independent of spec['master']
        self.set_spec(spec or default_soundscape())

    # --- spec / params -----------------------------------------------------
    def set_spec(self, spec: dict):
        self.spec = _normalise(spec)
        self._active_notes.clear()
        self._voice_env = {}
        self._voice_env_on = {}
        for v in self.spec["voices"]:
            if v.get("type") in ("pad", "sub", "osc"):
                e = v.get("env", {})
                self._voice_env[v["name"]] = Envelope(
                    attack=e.get("attack", 3.0), decay=e.get("decay", 1.2),
                    sustain=e.get("sustain", 0.85), release=e.get("release", 2.5))
                self._voice_env_on[v["name"]] = False

    def set_param(self, path: str, value):
        """Live update. Paths: 'master', 'tempo', 'distortion',
        'delay.time|feedback|mix', 'voice.<name>.<field>'."""
        s = self.spec
        parts = path.split(".")
        if len(parts) == 1 and parts[0] in ("master", "tempo", "distortion"):
            s[parts[0]] = _coerce(parts[0], value)
        elif parts[0] == "delay" and len(parts) == 2:
            s["delay"][parts[1]] = float(value)
        elif parts[0] == "voice" and len(parts) == 3:
            for v in s["voices"]:
                if v["name"] == parts[1]:
                    v[parts[2]] = _coerce(parts[2], value)
                    if parts[2] == "env" and v["name"] in self._voice_env:
                        e = v["env"]
                        env = self._voice_env[v["name"]]
                        env.a, env.d, env.s, env.r = e.get("attack", env.a), \
                            e.get("decay", env.d), e.get("sustain", env.s), e.get("release", env.r)

    # --- rendering ---------------------------------------------------------
    def render(self, frames: int) -> np.ndarray:
        n0 = self._clock
        idx = np.arange(n0, n0 + frames)
        t = idx / self.sr
        block_t = n0 / self.sr
        block_dt = frames / self.sr
        mix = np.zeros((frames, 2), np.float32)

        for v in self.spec["voices"]:
            vt = v.get("type", "pad")
            name = v["name"]
            is_sustained = vt in ("pad", "sub", "osc")

            if is_sustained:
                # ADSR gate: attack/decay/sustain while unmuted, a genuine
                # release fade (not a hard cut) once muted. Previously, muting
                # a pad/osc voice skipped rendering it entirely — an instant
                # cut with no release at all.
                env = self._voice_env[name]
                on = not v.get("mute")
                if on != self._voice_env_on.get(name):
                    (env.trigger if on else env.release)()
                    self._voice_env_on[name] = on
                gain = env.sample(block_t, block_dt)
                if gain <= 1e-4 and not on:
                    continue          # fully released and off — nothing to render
            elif v.get("mute"):
                continue
            else:
                gain = 1.0

            arp = v.get("arp") or {}
            if arp.get("on") and vt in ("pad", "osc"):
                mono = self._render_arp(v, n0, frames)
            elif vt == "pad" or vt == "sub":
                mono = self._render_pad(v, t, n0, frames)
            elif vt == "osc":
                mono = self._render_osc(v, t, n0, frames)
            elif vt == "pluck":
                mono = self._render_pluck(v, n0, frames)
            elif vt == "noise":
                mono = self._render_noise(v, frames)
            else:
                continue
            mono = mono * gain
            mono = mono * float(v.get("level", 0.4))
            pan = float(v.get("pan", 0.0))
            l = mono * np.sqrt(0.5 * (1 - pan))
            r = mono * np.sqrt(0.5 * (1 + pan))
            mix[:, 0] += l
            mix[:, 1] += r

        d = self.spec["delay"]
        if d.get("mix", 0) > 0:
            mix = self._delay.process(mix, d["time"], d["feedback"], d["mix"])

        drive = float(self.spec.get("distortion", 0.0))
        if drive > 0:
            g = 1.0 + drive * 8.0
            mix = np.tanh(mix * g) / np.tanh(g if g > 1 else 1)

        mix *= float(self.spec.get("master", 0.8)) * (0.0 if self.muted else 1.0)
        np.tanh(mix, out=mix)                     # gentle safety limiter
        self._clock += frames
        return mix.astype(np.float32)

    # --- voice renderers ---------------------------------------------------
    def _render_pad(self, v, t, n0, frames):
        chord = v.get("chord", [0])
        root = v.get("note", 36)
        wf = v.get("waveform", "saw")
        tone = float(v.get("tone", 0.4))          # brightness 0..1
        detune = float(v.get("detune", 0.01))
        n_partials = 1 if wf == "sine" else max(1, int(2 + tone * 6))
        out = np.zeros(frames, np.float32)
        for semi in chord:
            f = midi_to_hz(root + semi)
            for layer, dt in enumerate((-detune, detune)):
                fl = f * (1 + dt)
                for k in range(1, n_partials + 1):
                    amp = (tone ** (k - 1)) / k    # rolloff -> warmth
                    ph = fl * k * t
                    out += amp * _osc(ph, "sine")
        out /= max(1, len(chord) * 2)
        return out          # attack/release now handled by the voice's ADSR envelope

    def _render_osc(self, v, t, n0, frames):
        """A classic unison multi-oscillator voice — distinct from `pad`
        (which builds warmth from harmonic partials): this stacks detuned
        copies of the SAME waveform for thickness, with an optional
        one-octave-down sub mixed in. Good for a lead/bass-style texture."""
        chord = v.get("chord") or [0]
        root = v.get("note", 48)
        wf = v.get("waveform", "saw")
        unison = int(np.clip(v.get("unison", 1), 1, 7))
        detune = float(v.get("detune", 0.01))
        sub = float(np.clip(v.get("sub", 0.0), 0.0, 1.0))
        spread = np.linspace(-detune, detune, unison) if unison > 1 else [0.0]
        out = np.zeros(frames, np.float32)
        for semi in chord:
            f = midi_to_hz(root + semi)
            for d in spread:
                out += _osc(f * (1 + d) * t, wf) / unison
            if sub > 0:
                out += sub * _osc((f / 2.0) * t, "sine")
        out /= max(1, len(chord))
        return out          # attack/release now handled by the voice's ADSR envelope


    # Hard ceiling on simultaneously-active plucked notes, shared across all
    # pluck voices. Without this, a soundscape with a high tempo/rate (very
    # plausible from a "complicated" or energetic AI-generated brief) can
    # schedule notes faster than they decay, growing unboundedly — each note
    # costs a full block-length numpy pass per tick, so render time climbs
    # with the pile-up until it blows the realtime budget. Confirmed: 3
    # moderate pluck voices reached 1248 active notes and 100ms+ renders
    # (against a ~93ms budget) within 30 seconds without this cap.
    MAX_ACTIVE_NOTES = 96
    # Floor on the gap between scheduled onsets (in samples) — stops a
    # pathological tempo*rate combination from scheduling a burst of
    # thousands of notes within a single block in the first place.
    MIN_ONSET_INTERVAL_S = 0.04       # ~25 onsets/sec ceiling, plenty for ambient

    def _schedule_notes(self, voice_name, n0, frames, interval, freq_fn, wf, decay):
        """Append onsets landing in this block for `voice_name`, then enforce
        the shared MAX_ACTIVE_NOTES cap. `freq_fn(step)` returns the Hz for
        onset index `step`. Shared by both `pluck` (indexes a scale) and `arp`
        (indexes a chord in a pattern) so both get the same overrun
        protection for free."""
        interval = max(interval, int(self.sr * self.MIN_ONSET_INTERVAL_S))
        end = n0 + frames
        if interval <= 0:
            return
        first = ((n0 + interval - 1) // interval) * interval
        for onset in range(first, end, interval):
            step = onset // interval
            self._active_notes.append(dict(
                start=onset, freq=freq_fn(step), wf=wf, decay=decay, voice=voice_name))
        if len(self._active_notes) > self.MAX_ACTIVE_NOTES:
            # evict oldest first — inaudible in an ambient context, and far
            # cheaper than letting render cost keep climbing
            self._active_notes = self._active_notes[-self.MAX_ACTIVE_NOTES:]

    def _render_note_events(self, voice_name, n0, frames):
        """Render + age out all currently-active enveloped notes belonging to
        `voice_name`. Shared by `pluck` and `arp` — they differ only in how
        notes get scheduled (see `_schedule_notes`), not in how a scheduled
        note sounds or decays."""
        out = np.zeros(frames, np.float32)
        idx = np.arange(n0, n0 + frames)
        alive = []
        for note in self._active_notes:
            if note["voice"] != voice_name:
                alive.append(note)
                continue
            age = (idx - note["start"]) / self.sr
            env = np.where(age >= 0, np.exp(-age / max(0.05, note["decay"])), 0.0)
            if age[-1] <= note["decay"] * 6:
                alive.append(note)
            ph = note["freq"] * (idx / self.sr)
            out += (env * _osc(ph, note["wf"])).astype(np.float32)
        self._active_notes = alive
        return out * 0.6

    def _render_pluck(self, v, n0, frames):
        tempo = float(self.spec.get("tempo", 60))
        rate = float(v.get("rate", 1.0))          # notes per beat
        interval = int(self.sr * 60.0 / max(1e-3, tempo * rate))
        scale = v.get("scale") or [0, 3, 7, 10]
        root = v.get("note", 72)
        wf = v.get("waveform", "sine")
        decay = float(v.get("decay", 1.2))

        def freq_fn(step):
            semi = scale[step % len(scale)] + 12 * ((step // len(scale)) % 2)
            return midi_to_hz(root + semi)

        self._schedule_notes(v["name"], n0, frames, interval, freq_fn, wf, decay)
        return self._render_note_events(v["name"], n0, frames)

    @staticmethod
    def _arp_note(chord, mode, step):
        n = len(chord)
        if n == 0:
            return 0
        if mode == "down":
            return chord[(n - 1) - (step % n)]
        if mode == "updown" and n > 1:
            cycle = 2 * n - 2
            pos = step % cycle
            return chord[pos] if pos < n else chord[cycle - pos]
        if mode == "random":
            # Deterministic pseudo-random index from the step number (no RNG
            # state needed, so scheduling stays a pure function of position).
            # A plain multiplicative hash's low bits repeat with a short
            # period against small n (e.g. mod 4 can degenerate to the
            # identity permutation) — seed a fresh Random per step instead,
            # cheap at one call per scheduled note and properly distributed
            # regardless of chord length.
            return chord[random.Random(step).randrange(n)]
        return chord[step % n]           # "up" (default)

    def _render_arp(self, v, n0, frames):
        """Arpeggiate this voice's `chord` instead of sustaining it — steps
        through the chord in `arp.mode` order at `arp.rate` notes/beat, each
        note a short enveloped pluck. Reuses the pluck note-lifecycle
        machinery (and its overrun protection) unchanged."""
        tempo = float(self.spec.get("tempo", 60))
        arp = v.get("arp") or {}
        rate = float(arp.get("rate", v.get("rate", 2.0)))
        interval = int(self.sr * 60.0 / max(1e-3, tempo * rate))
        chord = v.get("chord") or [0]
        root = v.get("note", 60)
        wf = v.get("waveform", "sine")
        decay = float(arp.get("decay", 0.35))
        mode = arp.get("mode", "up")

        def freq_fn(step):
            return midi_to_hz(root + self._arp_note(chord, mode, step))

        self._schedule_notes(v["name"], n0, frames, interval, freq_fn, wf, decay)
        return self._render_note_events(v["name"], n0, frames)
        self._active_notes = alive
        return out * 0.6

    def _render_noise(self, v, frames):
        tone = float(v.get("tone", 0.5))
        w = np.random.default_rng().standard_normal(frames).astype(np.float32)
        # lowpass by moving average (kernel grows as tone falls -> darker)
        k = max(1, int((1.0 - tone) * 40) + 1)
        if k > 1:
            w = np.convolve(w, np.ones(k, np.float32) / k, mode="same")
        return w * 0.5


# --- spec helpers ----------------------------------------------------------

def _coerce(field, value):
    if field == "mute":
        return bool(value)
    if field == "waveform":
        return str(value)
    if field == "tempo":
        return float(value)
    if field in ("arp", "chord", "scale", "env"):
        return value            # dict / list — passed through, clamped by _normalise
    return float(value)


def _clamp(v, lo, hi, default):
    try:
        v = float(v)
    except (TypeError, ValueError):
        return default
    if v != v:              # NaN
        return default
    return max(lo, min(hi, v))


def _normalise(spec: dict) -> dict:
    """Sanity-clamp every field to an ambient-appropriate range. This is the
    one place all soundscapes pass through — AI-generated, hand-authored, or
    GUI-edited — so it's the right place to guard against a value that's
    merely unusual (e.g. a very high tempo/rate combo) turning into a runaway
    render cost (see MAX_ACTIVE_NOTES / MIN_ONSET_INTERVAL_S in _render_pluck
    for the specific failure this prevents)."""
    s = dict(spec or {})
    s["tempo"] = _clamp(s.get("tempo"), 20, 200, 60.0)
    s["master"] = _clamp(s.get("master"), 0.0, 1.0, 0.8)
    s["distortion"] = _clamp(s.get("distortion"), 0.0, 1.0, 0.0)
    dl = dict(s.get("delay", {}))
    dl["time"] = _clamp(dl.get("time"), 0.02, 1.9, 0.4)
    dl["feedback"] = _clamp(dl.get("feedback"), 0.0, 0.92, 0.3)
    dl["mix"] = _clamp(dl.get("mix"), 0.0, 0.95, 0.25)
    s["delay"] = dl
    voices = []
    for i, v in enumerate(s.get("voices", [])):
        v = dict(v)
        v.setdefault("name", f"voice{i+1}")
        v.setdefault("type", "pad")
        v["level"] = _clamp(v.get("level"), 0.0, 1.0, 0.4)
        v["pan"] = _clamp(v.get("pan"), -1.0, 1.0, 0.0)
        v["mute"] = bool(v.get("mute", False))
        v["tone"] = _clamp(v.get("tone"), 0.0, 1.0, 0.4)
        v["detune"] = _clamp(v.get("detune"), 0.0, 0.05, 0.01)
        v["rate"] = _clamp(v.get("rate"), 0.05, 8.0, 0.5)
        v["decay"] = _clamp(v.get("decay"), 0.05, 6.0, 1.2)
        v["unison"] = int(_clamp(v.get("unison"), 1, 7, 1))
        v["sub"] = _clamp(v.get("sub"), 0.0, 1.0, 0.0)
        arp = v.get("arp")
        if isinstance(arp, dict):
            arp = dict(arp)
            arp["on"] = bool(arp.get("on", False))
            arp["mode"] = arp.get("mode") if arp.get("mode") in ("up", "down", "updown", "random") else "up"
            arp["rate"] = _clamp(arp.get("rate"), 0.1, 8.0, 2.0)
            arp["decay"] = _clamp(arp.get("decay"), 0.05, 3.0, 0.35)
            v["arp"] = arp
        else:
            v.pop("arp", None)
        env = v.get("env")
        if isinstance(env, dict):
            env = dict(env)
            env["attack"] = _clamp(env.get("attack"), 0.01, 10.0, 3.0)
            env["decay"] = _clamp(env.get("decay"), 0.01, 10.0, 1.2)
            env["sustain"] = _clamp(env.get("sustain"), 0.0, 1.0, 0.85)
            env["release"] = _clamp(env.get("release"), 0.05, 15.0, 2.5)
            v["env"] = env
        else:
            v.pop("env", None)
        # a malformed/empty chord or scale would also divide-by-zero downstream
        chord = v.get("chord") or [0]
        v["chord"] = [int(n) for n in chord[:8]] if isinstance(chord, list) else [0]
        scale = v.get("scale") or [0, 3, 7, 10]
        v["scale"] = [int(n) for n in scale[:8]] if isinstance(scale, list) else [0, 3, 7, 10]
        voices.append(v)
    s["voices"] = voices or default_soundscape()["voices"]
    return s


def default_soundscape() -> dict:
    """A calm default so audio works offline with no AI call."""
    return {
        "tempo": 60, "master": 0.8, "distortion": 0.05,
        "delay": {"time": 0.45, "feedback": 0.35, "mix": 0.3},
        "voices": [
            {"name": "drone", "type": "pad", "waveform": "saw", "note": 36,
             "chord": [0, 7, 12, 19], "level": 0.5, "tone": 0.35, "detune": 0.012, "pan": 0.0},
            {"name": "bells", "type": "pluck", "waveform": "sine", "note": 72,
             "scale": [0, 3, 7, 10], "level": 0.28, "rate": 0.5, "decay": 1.6, "pan": 0.25},
            {"name": "air", "type": "noise", "level": 0.12, "tone": 0.4, "pan": -0.15},
        ],
    }
