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
  "eq": {"low": 0.0, "mid": 0.0, "high": 0.0},
  "swell_amount": 0.0, "swell_period": 24.0,
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

import math
import random

import numpy as np

from ..modulation import Envelope

SR = 44100
VOICE_TYPES = ("pad", "pluck", "noise", "sub", "osc")
WAVEFORMS = ("sine", "saw", "square", "triangle")


def midi_to_hz(n: float) -> float:
    return 440.0 * 2.0 ** ((n - 69) / 12.0)


def _osc_raw(phase: np.ndarray, waveform: str) -> np.ndarray:
    """The naive oscillator: exact shape, infinite harmonics. Kept for the
    sub-oscillator (always sine, so nothing to alias) and as the reference
    the bandlimited tables are built to match."""
    if waveform == "saw":
        return 2.0 * (phase % 1.0) - 1.0
    if waveform == "square":
        return np.where((phase % 1.0) < 0.5, 1.0, -1.0)
    if waveform == "triangle":
        return 4.0 * np.abs((phase % 1.0) - 0.5) - 1.0
    return np.sin(2.0 * np.pi * phase)          # sine (default)


# --- bandlimited oscillators with a brightness control ------------------------
#
# The naive shapes above have two problems that together are most of what
# reads as "cheap digital" rather than "warm synth":
#
#   1. Infinite harmonics at a finite sample rate means everything above
#      Nyquist folds back down as inharmonic tones. That is not a musical
#      choice, it is a sampling artefact, and no amount of prompting removes it.
#   2. No brightness control at all. `tone` was only ever read by `pad` and
#      `noise`; on `osc` and `pluck` — the voices a director reaches for when
#      asked for bass and leads — writing `tone` did nothing whatsoever.
#
# The module's founding constraint (see the docstring at the top) is no
# per-sample IIR recursion, because pure numpy cannot vectorise one. A
# resonant lowpass is therefore off the table. But the same warmth can be had
# additively: build one cycle of the waveform from a truncated harmonic series
# with a rolloff, and the result is bandlimited AND has a brightness knob.
#
# Doing that sum every block would be far too expensive (measured: 32 partials
# across a 3-note chord is ~11% of the audio callback budget, and unison
# multiplies it). Instead the cycle is built ONCE into a small table and read
# back by phase, so the per-block cost stays a couple of array ops — the same
# order as the naive oscillator it replaces.
_TABLE_N = 2048            # samples per cycle; quantisation noise is ~-66dB here
_MAX_PARTIALS = 64
_TABLE_CACHE: dict = {}


def _harmonic_amps(waveform: str, n: int) -> np.ndarray:
    """Amplitude of each sine partial for an ideal waveform, k = 1..n."""
    ks = np.arange(1, n + 1, dtype=np.float64)
    odd = (ks % 2 == 1)
    if waveform == "saw":
        return 1.0 / ks
    if waveform == "square":
        return np.where(odd, 1.0 / ks, 0.0)
    if waveform == "triangle":
        # odd harmonics, 1/k^2, alternating sign
        sign = np.where(((ks - 1) // 2) % 2 == 0, 1.0, -1.0)
        return np.where(odd, sign / (ks * ks), 0.0)
    a = np.zeros(n)
    a[0] = 1.0
    return a                                     # sine


def _partial_count(waveform: str, f0: float, sr: int) -> int:
    """How many partials fit under Nyquist at this pitch. Purely an
    anti-aliasing limit — brightness is the rolloff's job (see `_wavetable`),
    not truncation's, so this doesn't depend on `tone`. Keeping the two
    separate also means far fewer distinct tables to cache."""
    if waveform == "sine":
        return 1
    fits = int(sr * 0.5 / max(f0, 1.0))
    return int(max(1, min(fits, _MAX_PARTIALS)))


def _wavetable(waveform: str, tone_q: int, n: int) -> np.ndarray:
    """One normalised cycle. Cached: the key space is small in practice (a
    handful of waveforms x quantised tone x partial count), and a scene's
    voices ask for the same table on every block for as long as it plays."""
    key = (waveform, tone_q, n)
    tbl = _TABLE_CACHE.get(key)
    if tbl is not None:
        return tbl
    tone = tone_q / 100.0
    ks = np.arange(1, n + 1, dtype=np.float64)
    # Rolloff shaped like a lowpass sweeping through the harmonic series,
    # rather than a per-partial decay. A `tone ** (k-1)` curve (which is what
    # `pad` uses, and gets away with because it caps at 8 partials) collapses
    # almost immediately across 64: measured, everything from tone 0.15 to 0.6
    # came out identically dark, so five sixths of the control did nothing.
    # A cutoff in harmonic number spreads the range evenly and behaves the way
    # a filter knob is expected to.
    kc = 1.0 + tone * (_MAX_PARTIALS - 1)        # cutoff, in harmonic number
    rolloff = 1.0 / (1.0 + (ks / kc) ** 4)
    amps = _harmonic_amps(waveform, n) * rolloff
    x = np.arange(_TABLE_N, dtype=np.float64) / _TABLE_N
    tbl = (amps[:, None] * np.sin(2.0 * np.pi * ks[:, None] * x[None, :])).sum(axis=0)
    peak = float(np.max(np.abs(tbl)))
    # Normalise so lowering `tone` darkens without also turning the voice
    # down — a brightness control that loses level would just get compensated
    # for with `level`, which is not what it is for.
    if peak > 1e-9:
        tbl = tbl / peak
    tbl = tbl.astype(np.float32)
    if len(_TABLE_CACHE) < 512:                  # bounded; keys are few in practice
        _TABLE_CACHE[key] = tbl
    return tbl


def _osc(phase: np.ndarray, waveform: str, tone: float = 1.0,
         f0: float = 0.0, sr: int = SR) -> np.ndarray:
    """Bandlimited oscillator from phase in cycles, with a brightness control.

    `tone` 1.0 is the full waveform (every harmonic that fits under Nyquist);
    lower values roll the upper partials off for a warmer, rounder tone.
    `f0` is the fundamental, needed to know how many harmonics fit — pass 0
    to fall back to the naive shape (only used where there is nothing to
    alias, i.e. a pure sine).
    """
    if waveform == "sine" or f0 <= 0.0:
        return _osc_raw(phase, waveform)
    n = _partial_count(waveform, f0, sr)
    if n <= 1:
        return np.sin(2.0 * np.pi * phase)
    tbl = _wavetable(waveform, int(round(np.clip(tone, 0.0, 1.0) * 100)), n)
    # Linear interpolation between table entries — nearest-neighbour would
    # add its own broadband quantisation hiss, which is the sort of grit this
    # is meant to be removing.
    x = (phase % 1.0) * _TABLE_N
    i0 = x.astype(np.int32)
    frac = (x - i0).astype(np.float32)
    i1 = (i0 + 1) & (_TABLE_N - 1)               # _TABLE_N is a power of two
    return tbl[i0] * (1.0 - frac) + tbl[i1] * frac


def _eq_gain_db(freqs: np.ndarray, low_db: float, mid_db: float, high_db: float,
                low_x: float = 250.0, high_x: float = 4000.0) -> np.ndarray:
    """Per-bin gain (dB) for a 3-band EQ, as a smooth crossfade between three
    bands (~1 octave transition) rather than hard cuts — avoids ringing at
    the crossover points."""
    f = np.maximum(freqs, 1.0)                     # guard log2(0) at DC
    lf = np.log2(f)
    l1, l2 = np.log2(low_x), np.log2(high_x)
    low_w = 1.0 / (1.0 + np.exp(lf - l1))
    high_w = 1.0 / (1.0 + np.exp(l2 - lf))
    mid_w = np.clip(1.0 - low_w - high_w, 0.0, 1.0)
    return low_w * low_db + mid_w * mid_db + high_w * high_db


def _apply_eq(mix: np.ndarray, eq: dict, sr: int) -> np.ndarray:
    """3-band low/mid/high EQ (gains in dB), applied as a per-block frequency
    -domain gain curve (rFFT -> scale bins -> irFFT). Pure numpy, fully
    vectorised — no per-sample recursion, matching the rest of this module's
    realtime-safety approach. Like `Delay`, this is block-granular (the curve
    is exact for the block but not continuous across block boundaries via
    overlap-add) — acceptable here for the same reason: blocks are large
    (thousands of samples) and the material is slow ambient pads, not
    transient-heavy material where that would be audible."""
    low_db = float(eq.get("low", 0.0))
    mid_db = float(eq.get("mid", 0.0))
    high_db = float(eq.get("high", 0.0))
    n = mix.shape[0]
    freqs = np.fft.rfftfreq(n, d=1.0 / sr)
    gain_db = _eq_gain_db(freqs, low_db, mid_db, high_db)
    gain = 10.0 ** (gain_db / 20.0)
    spec = np.fft.rfft(mix, axis=0)
    spec *= gain[:, None]
    return np.fft.irfft(spec, n=n, axis=0).astype(np.float32)


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
        self._mute_gain = 1.0     # currently-applied smoothed mute multiplier (0..1)
        self._mute_from = 1.0
        self._mute_to = 1.0
        self._mute_fade_dur = 0.0
        self._mute_fade_pos = 0.0
        self.last_peak = 0.0      # post-master output peak of the last rendered block (VU meter)
        self.last_clip = False    # last_peak reached the clip threshold
        # "orchestration" swell: a slow, continuous per-voice level LFO (long
        # period, independent random phase per voice) layered on top of each
        # voice's own level — the whole point is that voices *don't* all
        # swell in lockstep, so the mix feels arranged rather than uniformly
        # breathing. Separate from the ADSR envelope above, which only fires
        # once on trigger/mute; this runs continuously for as long as a voice
        # plays. Off by default (swell_amount=0) so existing scenes/tests are
        # unaffected — see SCENE_SIZE-style "evolution" character slider in
        # the director, which is what turns this on for new generations.
        self._swell_phase: dict[str, float] = {}
        self._swell_period: dict[str, float] = {}
        self.set_spec(spec or default_soundscape())

    def set_muted(self, muted: bool, fade: float = 0.0):
        """Mute/unmute, optionally ramped over `fade` seconds (e.g. a "Disable
        Audio" button) rather than the instant snap plain `muted = True` gives
        (used for the safety-critical Start/Stop and Blank gates, which must
        not lag behind the click)."""
        self.muted = bool(muted)
        target = 0.0 if muted else 1.0
        fade = max(0.0, float(fade or 0.0))
        if fade <= 0.0:
            self._mute_gain = target
            self._mute_from = target
            self._mute_to = target
            self._mute_fade_dur = 0.0
            self._mute_fade_pos = 0.0
        else:
            self._mute_from = self._mute_gain   # start from wherever it is now, so a
            self._mute_to = target               # rapid re-toggle mid-fade doesn't jump
            self._mute_fade_dur = fade
            self._mute_fade_pos = 0.0

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
            # assign each voice its own random phase/period once, the first
            # time its name is seen — preserved across live param tweaks
            # (which don't call set_spec) so the swell keeps its place
            # instead of jumping every time a knob moves.
            name = v["name"]
            if name not in self._swell_phase:
                self._swell_phase[name] = random.uniform(0.0, 2 * math.pi)
                base_period = float(self.spec.get("swell_period", 24.0))
                self._swell_period[name] = base_period * random.uniform(0.7, 1.3)

    def set_param(self, path: str, value):
        """Live update. Paths: 'master', 'tempo', 'distortion', 'swell_amount',
        'swell_period', 'delay.time|feedback|mix', 'eq.low|mid|high',
        'voice.<name>.<field>'."""
        s = self.spec
        parts = path.split(".")
        if len(parts) == 1 and parts[0] in ("master", "tempo", "distortion",
                                             "swell_amount", "swell_period"):
            s[parts[0]] = _coerce(parts[0], value)
        elif parts[0] == "delay" and len(parts) == 2:
            s["delay"][parts[1]] = float(value)
        elif parts[0] == "eq" and len(parts) == 2:
            s.setdefault("eq", {})[parts[1]] = float(value)
        elif parts[0] == "voice" and len(parts) == 3:
            for v in s["voices"]:
                if v["name"] == parts[1]:
                    v[parts[2]] = _coerce(parts[2], value)
                    if parts[2] == "env" and v["name"] in self._voice_env:
                        e = v["env"]
                        env = self._voice_env[v["name"]]
                        env.a, env.d, env.s, env.r = e.get("attack", env.a), \
                            e.get("decay", env.d), e.get("sustain", env.s), e.get("release", env.r)

    def _swell_gain(self, name: str, block_t: float) -> float:
        """This voice's slow orchestration multiplier at this block's start
        time — 1.0 (unattenuated) at the peak of its cycle, down to
        `1 - swell_amount` at the trough. Block-granular (evaluated once per
        render() call, not per-sample) is plenty for a period measured in
        tens of seconds — matches the precision `Delay`/`_apply_eq` already
        use for similarly slow-moving effects."""
        amount = float(self.spec.get("swell_amount", 0.0))
        if amount <= 0.0:
            return 1.0
        period = self._swell_period.get(name) or float(self.spec.get("swell_period", 24.0))
        phase = self._swell_phase.get(name, 0.0)
        w = math.sin(2 * math.pi * (block_t / period) + phase)   # -1..1
        return 1.0 - amount * (0.5 - 0.5 * w)                     # 1.0 at peak, 1-amount at trough

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
            mono = mono * self._swell_gain(name, block_t)
            pan = float(v.get("pan", 0.0))
            l = mono * np.sqrt(0.5 * (1 - pan))
            r = mono * np.sqrt(0.5 * (1 + pan))
            mix[:, 0] += l
            mix[:, 1] += r

        eq = self.spec.get("eq")
        if eq and (eq.get("low") or eq.get("mid") or eq.get("high")):
            mix = _apply_eq(mix, eq, self.sr)

        d = self.spec["delay"]
        if d.get("mix", 0) > 0:
            mix = self._delay.process(mix, d["time"], d["feedback"], d["mix"])

        drive = float(self.spec.get("distortion", 0.0))
        if drive > 0:
            g = 1.0 + drive * 8.0
            mix = np.tanh(mix * g) / np.tanh(g if g > 1 else 1)

        master = float(self.spec.get("master", 0.8))
        if self._mute_fade_pos < self._mute_fade_dur:
            prog = (self._mute_fade_pos + np.arange(frames) / self.sr) / self._mute_fade_dur
            prog = np.clip(prog, 0.0, 1.0)
            mute_gain = self._mute_from + (self._mute_to - self._mute_from) * prog
            self._mute_fade_pos += frames / self.sr
            self._mute_gain = float(mute_gain[-1])
            mix *= master * mute_gain[:, None]
        else:
            mix *= master * self._mute_gain
        np.tanh(mix, out=mix)                     # gentle safety limiter
        self._clock += frames
        mix = mix.astype(np.float32)
        # VU meter source: measured post-master, post-limiter — moving the
        # master knob (or muting) is directly visible on the meter.
        self.last_peak = float(np.max(np.abs(mix))) if mix.size else 0.0
        self.last_clip = self.last_peak >= 0.98
        return mix

    # --- voice renderers ---------------------------------------------------
    def _render_pad(self, v, t, n0, frames):
        """Additive: `n_partials` sine harmonics per (chord note x detune
        layer). Was a Python triple-nested loop calling `_osc` once per
        partial — up to len(chord)*2*n_partials separate numpy calls per
        audio callback (e.g. a 4-note chord at full brightness: 64 calls,
        PER sustained voice, every ~185ms block at 8192/44100). Each numpy
        call has fixed dispatch overhead independent of its size, and this
        runs inside the realtime audio callback where the GIL is held for
        the duration — more voices/instruments in a soundscape meant
        proportionally more of these tiny calls stacking up, which is
        exactly the "bigger scenes with more instruments" case where visual
        frame drops got worse (the render thread was losing GIL time to
        this). Batched into one (partials x frames) vectorised call —
        verified numerically equivalent to the old loop (max abs diff
        ~6e-7, pure float32 rounding) before landing this."""
        chord = v.get("chord", [0])
        root = v.get("note", 36)
        wf = v.get("waveform", "saw")
        tone = float(v.get("tone", 0.4))          # brightness 0..1
        detune = float(v.get("detune", 0.01))
        n_partials = 1 if wf == "sine" else max(1, int(2 + tone * 6))
        ks = np.arange(1, n_partials + 1, dtype=np.float64)
        amps = (tone ** (ks - 1)) / ks                          # rolloff -> warmth, per partial
        freqs = [midi_to_hz(root + semi) * (1 + dt)
                 for semi in chord for dt in (-detune, detune)]  # chord x detune layers
        freqs = np.asarray(freqs, dtype=np.float64)
        fk = (freqs[:, None] * ks[None, :]).ravel()             # every (layer, partial) frequency
        ak = np.tile(amps, len(freqs))                          # matching amplitude per fk entry
        ph = fk[:, None] * t[None, :]
        out = (ak[:, None] * _osc(ph, "sine")).sum(axis=0).astype(np.float32)
        out /= max(1, len(chord) * 2)
        return out          # attack/release now handled by the voice's ADSR envelope

    def _render_osc(self, v, t, n0, frames):
        """A classic unison multi-oscillator voice — distinct from `pad`
        (which builds warmth from harmonic partials): this stacks detuned
        copies of the SAME waveform for thickness, with an optional
        one-octave-down sub mixed in. Good for a lead/bass-style texture.

        Batched the same way as `_render_pad` above (one (chord x unison,
        frames) vectorised call instead of a nested Python loop of small
        `_osc` calls) — same GIL-contention reasoning, verified numerically
        equivalent first."""
        chord = v.get("chord") or [0]
        root = v.get("note", 48)
        wf = v.get("waveform", "saw")
        unison = int(np.clip(v.get("unison", 1), 1, 7))
        detune = float(v.get("detune", 0.01))
        sub = float(np.clip(v.get("sub", 0.0), 0.0, 1.0))
        # `tone` used to be silently ignored here — an osc voice had no
        # brightness control at all, which is most of why leads and basses
        # came out thin and buzzy. 1.0 keeps the full waveform.
        tone = float(np.clip(v.get("tone", 1.0), 0.0, 1.0))
        spread = np.linspace(-detune, detune, unison) if unison > 1 else np.array([0.0])
        freqs = np.array([midi_to_hz(root + semi) for semi in chord], dtype=np.float64)
        fu = (freqs[:, None] * (1.0 + spread[None, :])).ravel()     # every (chord note, unison layer)
        ph = fu[:, None] * t[None, :]
        # One table for the whole stack, sized off the HIGHEST note in the
        # chord so nothing in it can alias. A chord spanning more than an
        # octave costs its lowest note some upper harmonics; erring that way
        # is right, since the alternative is folded inharmonic tones.
        out = _osc(ph, wf, tone, float(fu.max()), self.sr).sum(axis=0) / unison
        if sub > 0:
            sub_ph = (freqs / 2.0)[:, None] * t[None, :]
            out = out + sub * _osc(sub_ph, "sine").sum(axis=0)
        out = (out / max(1, len(chord))).astype(np.float32)
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

    def _schedule_notes(self, voice_name, n0, frames, interval, freq_fn, wf, decay,
                        tone=1.0):
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
                start=onset, freq=freq_fn(step), wf=wf, decay=decay,
                voice=voice_name, tone=tone))
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
            # Per-note table: notes are already rendered one at a time here,
            # so each gets partials sized to its own pitch — a low pluck keeps
            # its harmonics instead of being capped by the highest note.
            out += (env * _osc(ph, note["wf"], note.get("tone", 1.0),
                               note["freq"], self.sr)).astype(np.float32)
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

        # `tone` reaches pluck for the first time here — previously it was
        # read only by pad/noise, so a "warm" brief could ask for a mellow
        # pluck and get the same bright one regardless.
        tone = float(min(1.0, max(0.0, v.get("tone", 1.0))))

        def freq_fn(step):
            semi = scale[step % len(scale)] + 12 * ((step // len(scale)) % 2)
            return midi_to_hz(root + semi)

        self._schedule_notes(v["name"], n0, frames, interval, freq_fn, wf, decay, tone)
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

        tone = float(min(1.0, max(0.0, v.get("tone", 1.0))))

        def freq_fn(step):
            return midi_to_hz(root + self._arp_note(chord, mode, step))

        self._schedule_notes(v["name"], n0, frames, interval, freq_fn, wf, decay, tone)
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


class SoundscapeMixer:
    """Owns the currently-playing `Soundscape` and, during a scene switch,
    sequences a fade-out/swap/fade-in rather than crossfading two live
    instances at once. Pure DSP, no device I/O, so it renders and tests the
    same as `Soundscape`.

    A prior version rendered the outgoing AND incoming `Soundscape` on every
    block for the whole switch (a true simultaneous crossfade) — correct
    sounding, but it doubles the per-callback DSP cost (every voice, on both
    sides) for the entire fade. On a several-voice soundscape that doubled
    cost was measured pushing a single realtime audio callback well over its
    own budget (300%+ observed), which is real, audible underruns/glitching
    — independent of anything visual, and not something further micro-
    optimising either side's render cost alone fixes, since the problem is
    literally "two full renders where the callback only has time for one."

    So: fade the outgoing soundscape to silence over half the requested
    duration, THEN build the incoming one and fade it up over the other
    half — only ONE `Soundscape` is ever being rendered at a time, so the
    doubled cost is gone entirely. The swap itself lands exactly at the
    silent point between the two halves, so there's no discontinuity to
    hear. The visual scene crossfade (`SceneManager.render`) is unaffected
    by this and still overlaps both scenes smoothly — visuals are cheap
    enough after the camera-projection fix (see scene3d.py) to afford that;
    audio, even after vectorising the DSP, generally isn't once a soundscape
    has more than a couple of voices.
    """

    def __init__(self, spec: dict | None = None, sr: int = SR):
        self.sr = sr
        self.current = Soundscape(spec, sr=sr)
        self._pending_spec: dict | None = None
        self._pending_muted = False
        self._phase: str | None = None   # None | "out" | "in"
        self._fade_dur = 0.0             # duration of the CURRENT half (out or in)
        self._fade_pos = 0.0
        self._peak = 0.0
        self._clip = False

    def set_spec(self, spec: dict, fade: float = 0.0):
        fade = max(0.0, float(fade or 0.0))
        if fade <= 0.0:
            self.current.set_spec(spec)
            self._phase = None
            return
        self._pending_spec = spec
        self._pending_muted = self.current.muted
        self._fade_dur = fade / 2.0
        self._fade_pos = 0.0
        self._phase = "out"

    def set_param(self, path: str, value):
        self.current.set_param(path, value)   # live tweaks always target the incoming/current one

    @property
    def spec(self) -> dict:
        return self.current.spec

    @property
    def last_peak(self) -> float:
        return self._peak

    @property
    def last_clip(self) -> bool:
        return self._clip

    @property
    def muted(self) -> bool:
        return self.current.muted

    def set_muted(self, muted: bool, fade: float = 0.0):
        self.current.set_muted(muted, fade)
        self._pending_muted = muted   # in case a fade-out is in flight when this lands

    def render(self, frames: int) -> np.ndarray:
        if self._phase is None:
            out = self.current.render(frames)
            self._peak = self.current.last_peak
            self._clip = self.current.last_clip
            return out

        block = self.current.render(frames)
        prog = (self._fade_pos + np.arange(frames) / self.sr) / max(self._fade_dur, 1e-6)
        prog = np.clip(prog, 0.0, 1.0)[:, None]
        gain = (1.0 - prog) if self._phase == "out" else prog
        out = (block * gain).astype(np.float32)
        # same "gentle safety limiter" pattern Soundscape.render applies to
        # its own mix — cheap, and guards against anything unexpected still
        # pushing this scaled blend out of range.
        np.tanh(out, out=out)

        self._fade_pos += frames / self.sr
        if self._fade_pos >= self._fade_dur:
            if self._phase == "out":
                # silent now — swap in the new soundscape and fade it up
                self.current = Soundscape(self._pending_spec, sr=self.sr)
                self.current.set_muted(self._pending_muted)
                self._pending_spec = None
                self._phase = "in"
                self._fade_pos = 0.0
            else:
                self._phase = None

        self._peak = float(np.max(np.abs(out))) if out.size else 0.0
        self._clip = self._peak >= 0.98
        return out


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
    eq = dict(s.get("eq", {}))
    eq["low"] = _clamp(eq.get("low"), -24.0, 24.0, 0.0)
    eq["mid"] = _clamp(eq.get("mid"), -24.0, 24.0, 0.0)
    eq["high"] = _clamp(eq.get("high"), -24.0, 24.0, 0.0)
    s["eq"] = eq
    s["swell_amount"] = _clamp(s.get("swell_amount"), 0.0, 1.0, 0.0)
    s["swell_period"] = _clamp(s.get("swell_period"), 5.0, 120.0, 24.0)
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
            env["attack"] = _clamp(env.get("attack"), 0.01, 15.0, 3.0)
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
        "eq": {"low": 0.0, "mid": 0.0, "high": 0.0},
        "swell_amount": 0.0, "swell_period": 24.0,
        "voices": [
            {"name": "drone", "type": "pad", "waveform": "saw", "note": 36,
             "chord": [0, 7, 12, 19], "level": 0.5, "tone": 0.35, "detune": 0.012, "pan": 0.0},
            {"name": "bells", "type": "pluck", "waveform": "sine", "note": 72,
             "scale": [0, 3, 7, 10], "level": 0.28, "rate": 0.5, "decay": 1.6, "pan": 0.25},
            {"name": "air", "type": "noise", "level": 0.12, "tone": 0.4, "pan": -0.15},
        ],
    }
