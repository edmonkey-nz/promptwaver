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
     "level":0.5,"tone":0.4,"detune":0.01,"pan":0.0,"mute":false,
     "distortion":0.0,"compress":{"on":false,"threshold":0.6,"ratio":4.0},
     "lfo":{"on":false,"dest":"level","shape":"sine","rate":0.06,"depth":0.5,"phase":0.0}},
    {"name":"bells","type":"bell","note":72,
     "scale":[0,3,7,10],"level":0.3,"rate":1.0,"decay":1.2,"tone":0.7,"pan":0.2,
     "mute":false,"distortion":0.0},
    {"name":"air","type":"noise","level":0.15,"tone":0.5,"pan":0.0,"mute":false,
     "distortion":0.0}
  ]
}
"""

from __future__ import annotations

import math
import random

import numpy as np

from ..modulation import Envelope

SR = 44100
VOICE_TYPES = ("pad", "pluck", "noise", "sub", "osc", "bell", "harp")
WAVEFORMS = ("sine", "saw", "square", "triangle")

# Fixed inharmonic partial bank for `bell` — ratios are NOT integer multiples
# of the fundamental (that's what makes it read as a bell/chime rather than a
# plucked string), so they can't be derived by a formula like `_harmonic_amps`
# and aren't cacheable in `_wavetable`'s per-cycle table either. A tasteful
# starting point rather than a physically-modelled real bell casting; easy to
# re-season by ear. Amplitude decreases with partial number so the fundamental
# still reads as the pitch, with the upper partials as shimmer on top.
#
# 5 partials, not 8: measured directly (see MAX_ACTIVE_BELL_NOTES below) —
# sin()/exp() on this class of hardware is expensive enough that 8 partials
# at a modest note count alone pushed average render time past the 8192-frame
# block budget. Cost scales linearly with partial count, so this is the
# lowest-risk lever: fewer partials, same batching technique, same headroom
# margin restored without touching the note cap further.
BELL_PARTIAL_RATIOS = np.array([1.0, 1.41, 2.0, 2.37, 3.0])
BELL_PARTIAL_AMPS = np.array([1.0, 0.55, 0.4, 0.32, 0.22])
BELL_PARTIAL_AMP_SUM = float(BELL_PARTIAL_AMPS.sum())

# --- harp ------------------------------------------------------------------
# `harp` is `bell`'s renderer with both of bell's defining choices inverted.
# The partials are HARMONIC (integer multiples of the fundamental, so it reads
# as a string rather than a struck bar), and — the half that actually matters —
# every partial gets its OWN decay instead of sharing the note's.
#
# That second point is the voice. On a real string, damping rises with
# frequency, so a plucked note darkens as it rings. `pluck` applies one
# envelope to a full waveform, which holds its brightness for the note's whole
# life; over the multi-second ring times this voice exists for, that reads as
# an organ, not a harp.
#
# It is also nearly free in this batching shape. `_render_bell_notes` already
# flattens to (note x partial) rows and then repeats one per-note envelope
# across each note's partials; giving every ROW its own time constant instead
# is the same array, the same single `_osc()` call, and no extra passes.
#
# 5 partials for the same measured reason as BELL_PARTIAL_RATIOS — cost scales
# linearly with partial count, and the note budget below is worth more to this
# voice than a 5th harmonic that is -17dB down and damps fastest of all.
HARP_PARTIALS = 5
HARP_PARTIAL_K = np.arange(1, HARP_PARTIALS + 1, dtype=np.float64)
#: ~1/k^1.2 rather than the ideal plucked string's 1/k: on a bank of pure
#: sines the textbook spectrum came out glassy. `tone` rolls this off further.
HARP_PARTIAL_AMPS = HARP_PARTIAL_K ** -1.2
HARP_PARTIAL_AMP_SUM = float(HARP_PARTIAL_AMPS.sum())

#: Exponent on the per-partial damping: partial k decays over
#: `decay / k**damp` seconds. 0 makes every partial decay together (a pluck
#: with extra harmonics bolted on); ~0.7 is harp/guitar; past ~1.2 the upper
#: partials are gone almost immediately, which reads as a muted or felted
#: string. Authorable per voice as "damp".
HARP_DAMP_DEFAULT = 0.7

#: Ceiling on a harp voice's "decay". Every other note voice is capped at 6s
#: in `_normalise`; a harp's whole point is ringing longer than that, and
#: raising the shared limit would let a runaway pluck schedule notes that
#: never age out. Its own ceiling keeps that failure mode contained.
HARP_MAX_DECAY = 20.0

#: A harp note is dropped once it is `decay * HARP_NOTE_LIFETIME` old.
#: `pluck`/`bell` use *6 (exp(-6), about -52dB), a sane margin at their ~1s
#: decays and absurd at a harp's: at decay=12s it would hold notes for 72
#: seconds. exp(-4) is about -35dB — inaudible under a mix — and cuts the
#: resident note count by a third, which is the budget this voice is short of.
HARP_NOTE_LIFETIME = 4.0

#: Age (in multiples of the note's own decay) past which a note is rendered
#: with HARP_PARTIALS_TAIL partials instead of the full bank. Not an
#: approximation for its own sake: per-partial damping has ALREADY silenced
#: the upper partials by here. At age 2*decay with damp 0.7, partial 3 sits
#: exp(-2 * 3**0.7) = -37dB below its own onset, which is itself -14dB below
#: the fundamental, so the step at the boundary is under -45dB. Cuts the cost
#: of a long tail to a third without changing what it sounds like.
HARP_TAIL_AFTER = 2.0
HARP_PARTIALS_TAIL = 2

#: Release ramp applied when a note is retired to stay under the note cap.
#: `pluck`/`bell` drop evicted notes outright, which is fine for them: by the
#: time one of their notes is the oldest thing alive it has decayed to nothing.
#: A harp's hasn't. Measured at a dense setting (rate 1.0, roll 8, decay 14s)
#: the retired note is only 1.6s old and still at -1dB of its onset amplitude,
#: so cutting it is a step, not a fade-out. Ramping vs. cutting changes the
#: output by -19dB relative to the signal — small, but it is the difference
#: between a click and no click.
HARP_RETIRE_FADE_S = 0.35

#: Octave span a harp's scale walks before wrapping back to the root.
HARP_OCTAVES = 2

#: Notes per roll by default. Deliberately NOT 1: the roll is the gesture that
#: makes this voice a harp rather than a long pluck, and HARP_OUTPUT_GAIN below
#: is calibrated for the polyphony a roll builds up — so a bare
#: {"type":"harp"} at roll=1 renders correctly but ~7x quieter than a pluck,
#: which reads as broken. One constant so `_normalise`, the scheduler fallback
#: and the UI knob can't drift apart.
HARP_ROLL_DEFAULT = 6

#: Output trim, standing where `pluck`/`bell` use a flat 0.6.
#:
#: Those voices can use a constant because their notes are gone in about a
#: second, so only a handful ever overlap. A harp's whole premise is that they
#: DON'T: measured at a typical roll (decay 9-14s, roll 6-8) the voice peaked
#: at 2.0-2.5 against pluck's 0.5 at the same "level", which slams the master
#: tanh and turns the ring into distortion.
#:
#: 0.6/sqrt(12) — calibrated so a dozen simultaneously-ringing notes land in
#: the same range as a pluck. Deliberately a CONSTANT and not a divide by the
#: live note count: dividing would duck the whole voice by ~3dB every time a
#: roll fires, which is audible pumping on exactly the gesture this voice
#: exists to play. Sparse settings (roll=1) come out quiet by design and want
#: their "level" raised.
HARP_OUTPUT_GAIN = 0.17

#: Ceiling on a per-voice LFO's rate, in Hz. One cycle every two seconds is
#: already brisk for ambient — the useful range in practice is an order of
#: magnitude below this. Shared by the DSP clamp, the MIDI range table and the
#: UI knob so all three agree on what the control means.
LFO_MAX_RATE = 0.5

#: Whole-mix "filter sweep": a slow, single-phase sine added to the static
#: eq.high dB value, reusing _apply_eq's existing FFT machinery untouched —
#: only the "high" figure it's called with varies over time. Unlike swell
#: (per-voice, independently randomised phase so voices don't move in
#: lockstep) this is one global effect with nothing to decorrelate against,
#: so it runs on a single deterministic phase tied to the scene clock rather
#: than needing per-instance random state. At amount=1.0 the high band swings
#: +-8dB around whatever eq.high is set to — audible without being able to
#: exceed the existing +-10dB EQ ceiling by much even from an extreme static
#: setting (clamped below regardless).
FILTER_SWEEP_MAX_DB = 8.0

#: How far a per-voice `sweep` of 1.0 swings that voice's own `tone`, either
#: side of its authored value.
#:
#: The whole-mix sweep above cannot be scaled per instrument: it is one EQ
#: curve over the summed mix, so excluding a voice from it would mean giving
#: that voice its own filter, and a per-voice filter is either per-sample
#: recursion (banned here) or its own FFT per voice per block (affordable
#: once, over the mix — not once per voice).
#:
#: So the per-voice control sweeps the thing each voice ALREADY has a cheap
#: brightness handle for: `tone`, which selects a bandlimited wavetable.
#: Moving it costs nothing — `_wavetable` quantises tone to 1/100 and caches,
#: so a sweeping voice just warms ~100 tables once and then hits the cache —
#: and it is block-granular, which is exactly the granularity the whole-mix
#: sweep already runs at. Same phase as the global sweep, so a scene where
#: both are up moves as one gesture rather than two beating against each
#: other. 0.4 gives an unmistakable swing without spending most of the range
#: pinned at either end of `tone`.
VOICE_SWEEP_TONE_RANGE = 0.4

#: Output channel layouts this mixer can pan into, as
#: `(front L, front R, rear L, rear R)` channel indices.
#:
#: The panning model is QUADRAPHONIC — a front pair and a rear pair — whatever
#: the device's channel count happens to be:
#:
#:   2  stereo        FL FR                 rear absent, `depth` has no effect
#:   4  quad          FL FR RL RR
#:   6  5.1 carrier   FL FR FC LFE SL SR    FC and LFE left SILENT
#:
#: 6 is here only because a 5.1 sink may not accept a 4-channel stream — on
#: this machine's HDMI output both open, but that is a property of one
#: PulseAudio sink, not a guarantee. At 6 this is the same quad image carried
#: on a 5.1 stream, **not** a 5.1 mix: a real centre (ambient music with no
#: dialogue has nothing obvious to put there) and a real LFE (which needs a
#: crossover — a per-block filter, and this file forbids the per-sample
#: recursion the cheap version of one wants) are deliberately still open
#: questions. See future.md rather than guessing at them here.
SURROUND_LAYOUTS = {
    2: (0, 1, None, None),
    4: (0, 1, 2, 3),
    6: (0, 1, 4, 5),
}

#: Channel counts the output stream may be opened with. Anything else falls
#: back to stereo rather than panning into a layout nothing describes.
SUPPORTED_CHANNELS = tuple(sorted(SURROUND_LAYOUTS))


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


def _pan_gains(pan, depth, channels: int):
    """Per-channel gain for one voice at (pan, depth).

    Returns a `(channels,)` vector for scalar inputs, or `(frames, channels)`
    when either axis is a per-sample LFO array — the caller broadcasts it
    against the voice's mono buffer in one array op. Deliberately one term
    rather than a per-channel Python loop: this runs once per voice per block
    on the audio producer thread, and this file's rule is one vectorised op
    per voice, not one numpy call per channel.

    **The stereo path is bit-identical to the pre-surround code.** `depth` is
    ignored outright at 2 channels rather than folded down to a distance cue,
    so turning surround off — or loading a surround-authored scene on a stereo
    rig — reproduces exactly the mix that was there before this existed. A
    fold-down would want attenuation *and* high-frequency damping to read as
    distance, and per-voice damping is the expensive half (see future.md); a
    bare attenuation on its own mostly just makes a voice quieter for no
    audible reason.

    Gains are left at float64 on purpose. Casting them to float32 first would
    turn the old `float32 * float64` product into a `float32 * float32` one —
    inaudible, but no longer bit-for-bit the same numbers, which is the
    property that makes "surround off changes nothing" checkable rather than
    merely plausible.
    """
    left = np.sqrt(0.5 * (1.0 - pan))
    right = np.sqrt(0.5 * (1.0 + pan))
    fl, fr, rl, rr = SURROUND_LAYOUTS.get(channels, SURROUND_LAYOUTS[2])
    if rl is None:
        cols = {fl: left, fr: right}
    else:
        # Equal-power front/rear crossfade — the same law as the L/R pan
        # above, applied to the other axis. depth 0 is the front pair alone,
        # so a voice that was never given a depth sits exactly where it sat in
        # stereo; depth 1 is the rear pair alone.
        front = np.cos(depth * (math.pi / 2))
        back = np.sin(depth * (math.pi / 2))
        cols = {fl: left * front, fr: right * front,
                rl: left * back, rr: right * back}
    return np.stack(np.broadcast_arrays(
        *[cols.get(i, 0.0) for i in range(channels)]), axis=-1)


class Delay:
    """Block-granular delay/echo. Delay time is clamped to >= one block
    so read/write ranges never overlap within a block — this lets the whole
    block be processed as vectorised numpy ops with no per-sample Python loop,
    which matters because this runs inside a realtime audio callback where a
    slow block is an audible glitch, not just a slow frame.

    Width-agnostic: the buffer is as wide as the mix it is handed, so the echo
    follows a voice into the rears instead of collapsing it to the front pair.
    """

    def __init__(self, sr=SR, max_seconds=2.0, channels: int = 2):
        self.sr = sr
        self.channels = int(channels)
        self.buf = np.zeros((int(sr * max_seconds), self.channels), np.float32)
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
    def __init__(self, spec: dict | None = None, sr: int = SR, channels: int = 2):
        self.sr = sr
        # Output width. A rig property, not a scene one — it comes from the
        # audio settings and is reapplied to whatever soundscape is loaded,
        # the same way `output_ratio` is reapplied to whatever scene is.
        self.channels = channels if channels in SURROUND_LAYOUTS else 2
        self._clock = 0                          # absolute sample index
        self._delay = Delay(sr, channels=self.channels)
        self._active_notes: list[dict] = []      # for pluck/arp voices
        self._voice_env: dict[str, Envelope] = {}     # ADSR per enveloped voice
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
        # Three-band energy of the last block — modulation sources, so a scene
        # can be driven by its own soundscape rather than only by the mic.
        self.band_low = 0.0
        self.band_mid = 0.0
        self.band_high = 0.0
        self.voice_peaks: dict[str, float] = {}   # per-voice output level
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

    def set_channels(self, channels: int):
        """Re-point at a different output width, live.

        The delay line is rebuilt rather than resized because its contents are
        interleaved per channel — reshaping it would smear the tail of the
        echo across the new channel map. Losing up to two seconds of echo tail
        on a device change is the cheaper wrong answer.

        Only ever called with the stream stopped (see
        `SoundscapeSynth.reconfigure`): the render buffer's width has to match
        what the callback copies into, so a change to it mid-block would hand
        PortAudio a wrongly-shaped array on the realtime thread.
        """
        channels = channels if channels in SURROUND_LAYOUTS else 2
        if channels == self.channels:
            return
        self.channels = channels
        self._delay = Delay(self.sr, channels=channels)

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
            if v.get("type") in self.ENVELOPED_TYPES:
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
        'swell_period', 'filter_sweep_amount', 'filter_sweep_period',
        'delay.time|feedback|mix', 'eq.low|mid|high', 'voice.<name>.<field>'."""
        s = self.spec
        parts = path.split(".")
        if len(parts) == 1 and parts[0] in ("master", "tempo", "distortion",
                                             "swell_amount", "swell_period",
                                             "swell_depth_amount",
                                             "filter_sweep_amount", "filter_sweep_period"):
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

    # --- per-voice LFO ------------------------------------------------------
    #
    # Destinations split into two groups, and the split is forced by the block
    # size rather than chosen: a block is 190-370ms at the blocksizes this
    # runs at (8192-16384). Anything evaluated once per block therefore can't
    # represent an LFO faster than roughly 0.3-0.6Hz without visibly stepping,
    # and which end of that you get depends on a setting the user can change.
    #
    #   PER-SAMPLE  level, pan, depth — applied as arrays over the block, so
    #               they stay smooth at any rate. Tremolo, auto-pan and a
    #               voice circling front-to-back work up to audio rate if you
    #               want them to.
    #   PER-BLOCK   tone, detune, sub, waveform, rate — these select a
    #               wavetable, a set of frequencies or a note schedule *before*
    #               the block is rendered, so they can only change between
    #               blocks. Fine for the slow sweeps they're for; above ~0.5Hz
    #               they will audibly step, which is why the director is told
    #               to keep those slow.
    #: `depth` is listed unconditionally, not only when surround is running.
    #: The LFO config is scene data: a scene authored on a quad rig has to
    #: round-trip unchanged through a stereo session, and dropping the
    #: destination on a 2-channel rig would rewrite it to something else the
    #: moment that scene was saved.
    LFO_DESTS_SMOOTH = ("level", "pan", "depth", "distortion")
    LFO_DESTS_STEPPED = ("tone", "detune", "sub", "waveform", "rate")
    LFO_DESTS = LFO_DESTS_SMOOTH + LFO_DESTS_STEPPED
    LFO_SHAPES = ("sine", "triangle", "saw", "square", "random")

    def _lfo_wave(self, x: np.ndarray | float, shape: str):
        """Bipolar -1..1 from a phase in cycles. `x` may be an array (the
        per-sample path) or a float (the per-block one)."""
        if shape == "triangle":
            return 4.0 * np.abs((x % 1.0) - 0.5) - 1.0
        if shape == "saw":
            return 2.0 * (x % 1.0) - 1.0
        if shape == "square":
            return np.where((x % 1.0) < 0.5, 1.0, -1.0)
        if shape == "random":
            # Sample & hold: one value per cycle, from a hash of the cycle
            # number. Deterministic, so a scene sounds the same on every
            # playback — no RNG state to drift.
            step = np.floor(x)
            h = np.sin(step * 12.9898) * 43758.5453
            return 2.0 * (h - np.floor(h)) - 1.0
        return np.sin(2.0 * np.pi * x)              # sine (default)

    def _lfo(self, v: dict, n0: int, frames: int):
        """(per-sample array, mid-block scalar, config) for this voice's LFO,
        or None when it has none.

        Phase comes from the absolute sample clock for the same reason the
        oscillators' does — it stays continuous across block boundaries with
        no stored state, so live edits and block-size changes can't make it
        jump. The scalar is sampled at the block's MIDPOINT rather than its
        start, which halves the timing error for the stepped destinations."""
        lfo = v.get("lfo") or {}
        if not lfo.get("on"):
            return None
        dest = lfo.get("dest", "level")
        if dest not in self.LFO_DESTS:
            return None
        # Capped at 0.5Hz — one cycle every two seconds — because this is an
        # ambient instrument and anything faster reads as an effect rather
        # than as the scene breathing. Clamped here as well as in the UI so a
        # hand-edited or model-authored scene file can't sit outside the range
        # the controls can express. 0 is allowed and means "frozen": the LFO
        # holds at its phase offset, which is a useful way to park one.
        rate = float(min(LFO_MAX_RATE, max(0.0, lfo.get("rate", 0.06))))
        depth = float(min(1.0, max(0.0, lfo.get("depth", 0.5))))
        if depth <= 0.0:
            return None
        shape = lfo.get("shape", "sine")
        phase = float(lfo.get("phase", 0.0))
        t = (n0 + np.arange(frames, dtype=np.float64)) / self.sr
        x = t * rate + phase
        arr = self._lfo_wave(x, shape)
        mid = float(np.asarray(
            self._lfo_wave((n0 + frames * 0.5) / self.sr * rate + phase, shape)))
        return arr, mid, dest, depth

    def _lfo_apply_stepped(self, v: dict, mid: float, dest: str, depth: float) -> dict:
        """A shallow copy of the voice with one stepped param displaced by the
        LFO. Copied rather than mutated so the spec the UI reads back (and
        'Save Soundscape settings' writes out) keeps the authored value, not
        whatever the LFO happened to be doing at that instant."""
        ev = dict(v)
        if dest == "tone":
            ev["tone"] = float(min(1.0, max(0.0, v.get("tone", 1.0) + depth * mid)))
        elif dest == "detune":
            ev["detune"] = float(min(0.05, max(0.0,
                                               v.get("detune", 0.01) + depth * 0.02 * mid)))
        elif dest == "sub":
            ev["sub"] = float(min(1.0, max(0.0, v.get("sub", 0.0) + depth * mid)))
        elif dest == "rate":
            # Multiplicative: rate is a tempo division, so a fixed offset
            # would mean something different at every tempo.
            ev["rate"] = float(max(0.05, v.get("rate", 1.0) * (1.0 + depth * 0.75 * mid)))
        elif dest == "waveform":
            # The "osc type" destination: step through the waveform list.
            # Deliberately quantised — the point is switching timbre, and a
            # smooth shape would just spend most of its time mid-step.
            u = (mid + 1.0) * 0.5
            ev["waveform"] = WAVEFORMS[min(len(WAVEFORMS) - 1,
                                           int(u * len(WAVEFORMS)))]
        return ev

    def _swell_wave(self, name: str, block_t: float) -> float:
        """This voice's raw swell oscillator, -1..1, at this block's start.

        Split out from `_swell_gain` so the level swell and the depth swell
        read the SAME phase — that is the whole point of the depth swell: a
        voice blooming louder also moves forward, rather than the two effects
        drifting against each other on independent oscillators."""
        period = self._swell_period.get(name) or float(self.spec.get("swell_period", 24.0))
        phase = self._swell_phase.get(name, 0.0)
        return math.sin(2 * math.pi * (block_t / period) + phase)

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
        w = self._swell_wave(name, block_t)
        return 1.0 - amount * (0.5 - 0.5 * w)                     # 1.0 at peak, 1-amount at trough

    def _swell_depth(self, name: str, block_t: float) -> float:
        """How far this voice is pushed backwards by the swell, 0..1, to be
        added to its authored depth.

        Zero at the peak of the cycle and `swell_depth_amount` at the trough,
        off the same wave as `_swell_gain` — so at the top of its swell a
        voice is both loudest and furthest forward, and at the bottom it is
        quietest and furthest back. That coupling is the effect; it is not
        two independent modulations that happen to share a name.

        Independent of `swell_amount`, so the room can breathe without the
        levels moving (or the other way round). Costs nothing when off, and
        nothing at all on a stereo rig, where `_pan_gains` ignores depth.
        """
        amount = float(self.spec.get("swell_depth_amount", 0.0))
        if amount <= 0.0:
            return 0.0
        w = self._swell_wave(name, block_t)
        return amount * (0.5 - 0.5 * w)

    # --- rendering ---------------------------------------------------------
    def render(self, frames: int) -> np.ndarray:
        n0 = self._clock
        idx = np.arange(n0, n0 + frames)
        t = idx / self.sr
        block_t = n0 / self.sr
        block_dt = frames / self.sr
        mix = np.zeros((frames, self.channels), np.float32)
        voice_peaks: dict[str, float] = {}

        # Filter-sweep phase, evaluated once for the whole block and shared by
        # the per-voice tone sweep below and the whole-mix EQ sweep after the
        # loop — they are two halves of one effect and must not drift apart.
        sweep_amount = float(self.spec.get("filter_sweep_amount", 0.0))
        sweep_period = max(1.0, float(self.spec.get("filter_sweep_period", 30.0)))
        sweep_swing = math.sin(2 * math.pi * block_t / sweep_period)

        for v in self.spec["voices"]:
            vt = v.get("type", "pad")
            name = v["name"]
            is_sustained = vt in self.ENVELOPED_TYPES

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

            # Per-voice LFO. Stepped destinations have to be resolved BEFORE
            # the voice renders (they pick a wavetable / frequencies / a note
            # schedule); the smooth ones are applied to the finished signal
            # further down.
            lfo = self._lfo(v, n0, frames)
            rv = v
            if lfo is not None and lfo[2] in self.LFO_DESTS_STEPPED:
                rv = self._lfo_apply_stepped(v, lfo[1], lfo[2], lfo[3])

            # Per-voice filter sweep: this voice's share of the scene's sweep,
            # as a swing of its own brightness. Stacks on top of a stepped
            # tone LFO rather than replacing it, and copies rather than
            # mutates for the same reason `_lfo_apply_stepped` does — the spec
            # the UI reads back (and Save writes out) must keep the AUTHORED
            # tone, not whatever the sweep happened to be doing at that
            # instant. Clamped on read: set_param bypasses _normalise.
            v_sweep = min(1.0, max(0.0, float(v.get("sweep", 0.0))))
            if v_sweep > 0.0:
                rv = dict(rv)
                rv["tone"] = float(min(1.0, max(0.0,
                    rv.get("tone", 0.4) + v_sweep * VOICE_SWEEP_TONE_RANGE * sweep_swing)))

            arp = rv.get("arp") or {}
            if arp.get("on") and vt in ("pad", "osc"):
                mono = self._render_arp(rv, n0, frames)
            elif vt == "pad" or vt == "sub":
                mono = self._render_pad(rv, t, n0, frames)
            elif vt == "osc":
                mono = self._render_osc(rv, t, n0, frames)
            elif vt == "pluck":
                mono = self._render_pluck(rv, n0, frames)
            elif vt == "bell":
                mono = self._render_bell(rv, n0, frames)
            elif vt == "harp":
                mono = self._render_harp(rv, n0, frames)
            elif vt == "noise":
                mono = self._render_noise(rv, frames)
            else:
                continue
            mono = mono * gain
            level = float(v.get("level", 0.4))
            if lfo is not None and lfo[2] == "level":
                # Tremolo, applied per-sample so it stays smooth at any rate.
                # Unipolar and downward-only: the authored level stays the
                # ceiling, so turning the LFO on never makes a voice louder
                # than it was mixed to be.
                _, _, _, depth = lfo
                mono = mono * (level * (1.0 - depth * (1.0 - (lfo[0] + 1.0) * 0.5)))
            else:
                mono = mono * level
            mono = mono * self._swell_gain(name, block_t)

            # Per-voice effects (applied after level, before pan/sum)
            distortion = float(v.get("distortion", 0.0))
            if lfo is not None and lfo[2] == "distortion":
                # LFO-modulated distortion: apply per-sample with varying amount
                _, _, _, depth = lfo
                dist_amt = np.clip(distortion + depth * lfo[0], 0.0, 1.0)
                g = 1.0 + dist_amt * 8.0
                mono = np.tanh(mono * g) / np.tanh(np.clip(g, 1.0, 9.0))
            elif distortion > 0:
                g = 1.0 + distortion * 8.0
                mono = np.tanh(mono * g) / np.tanh(g if g > 1 else 1)

            # Per-voice compressor (disabled for now — needs fixing)
            # comp = v.get("compress") or {}
            # if comp.get("on"):
            #     TODO: rewrite compressor with proper envelope follower + attack/release

            # Per-voice output level, as a modulation source. Measured here —
            # post level, post per-voice LFO/distortion, pre pan — so it is
            # what the voice actually contributes to the mix.
            #
            # This is also what makes an ARPEGGIATOR usable as a modulation
            # source without any special handling: each arp note is a spike in
            # its voice's level, so routing this at a visual param gives you
            # the arp's rhythm for free.
            if mono.size:
                voice_peaks[name] = float(np.max(np.abs(mono)))

            pan = float(v.get("pan", 0.0))
            if lfo is not None and lfo[2] == "pan":
                # Auto-pan around the authored position, per-sample. Clipped
                # at the extremes rather than wrapped, so a deep sweep parks
                # at hard left/right instead of jumping to the other side.
                pan = np.clip(pan + lfo[3] * lfo[0], -1.0, 1.0)
            # Front/back position. Clamped on READ, not just in _normalise:
            # set_param bypasses normalisation, so a live UI or MIDI write
            # lands here unclamped.
            depth = min(1.0, max(0.0, float(v.get("depth", 0.0))))
            depth = depth + self._swell_depth(name, block_t)
            if lfo is not None and lfo[2] == "depth":
                depth = depth + lfo[3] * lfo[0]
            # One clip covers the authored value, the swell and the LFO
            # together — the same "park at the wall rather than wrap" rule
            # the pan LFO follows, so a voice driven hard backwards sits in
            # the rears instead of reappearing at the front.
            depth = np.clip(depth, 0.0, 1.0)
            mix += mono[:, None] * _pan_gains(pan, depth, self.channels)

        eq = self.spec.get("eq")
        if sweep_amount > 0:
            # Whole-mix, single-phase — see FILTER_SWEEP_MAX_DB. Swings the
            # high band's dB gain around whatever it's statically set to,
            # so amount=0 is exactly the pre-existing behaviour and this
            # never needs its own on/off flag.
            swing = sweep_amount * FILTER_SWEEP_MAX_DB * sweep_swing
            eq = dict(eq or {})
            eq["high"] = max(-10.0, min(10.0, float(eq.get("high", 0.0)) + swing))
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
        self.voice_peaks = voice_peaks
        self._update_bands(mix)
        return mix

    #: Voice types that get an ADSR and so fade in/out on mute rather than
    #: being cut. `noise` belongs here despite not being a "note" voice: it is
    #: a continuous bed, so skipping its render on mute (which is what used to
    #: happen) is an instant hard cut, and on a wind/air texture that reads as
    #: a click. `pluck` stays out — each of its notes already has its own
    #: decay envelope, and gating the scheduler would fight that.
    ENVELOPED_TYPES = ("pad", "sub", "osc", "noise")

    # Band edges in Hz. Low is the drone/sub weight, mid the body of the pads
    # and plucks, high the air and bell transients.
    _BAND_EDGES = (250.0, 2000.0)

    def _update_bands(self, mix: np.ndarray) -> None:
        """Three-band energy of this block's OUTPUT, for the modulation matrix.

        This is what lets a scene's visuals react to the scene's OWN sound.
        The pre-existing `audio_level` source reads the microphone, so on a
        machine with no live input every audio->visual route sits at zero and
        looks broken — which is exactly what it is, for anyone not playing
        music into the mic.

        A block-wise rfft rather than a filter bank: this module's founding
        constraint rules out per-sample IIR recursion, and decimating to ~2048
        bins keeps this at a fraction of a percent of the callback budget
        while still resolving three bands comfortably.
        """
        n = mix.shape[0]
        if n < 64:
            return
        # Sum the channels this layout actually pans into, so a voice sent to
        # the rears still drives the visuals. Indexed via the layout rather
        # than summing the whole array because at 6 channels the untouched
        # centre/LFE slots are silent — harmless to add, but summing by layout
        # keeps this correct if either ever stops being silent. Identical to
        # the old `mix[:, 0] + mix[:, 1]` at 2 channels.
        fl, fr, rl, rr = SURROUND_LAYOUTS.get(self.channels, SURROUND_LAYOUTS[2])
        mono = mix[:, fl] + mix[:, fr]
        if rl is not None:
            mono = mono + mix[:, rl] + mix[:, rr]
        step = max(1, n // 2048)
        x = mono[::step]
        m = x.shape[0]
        if m < 32:
            return
        sr = self.sr / step
        power = np.abs(np.fft.rfft(x * np.hanning(m))) ** 2
        freqs = np.fft.rfftfreq(m, 1.0 / sr)
        lo_e, hi_e = self._BAND_EDGES
        # RMS per band via Parseval, then a gain into a usable 0..1 modulation
        # range. Smoothing is left to the matrix's Value sources, which
        # already slew and are where the rest of the app expects it.
        scale = 2.0 / max(m, 1)
        self.band_low = min(1.0, float(np.sqrt(power[freqs < lo_e].sum())) * scale)
        self.band_mid = min(1.0, float(np.sqrt(
            power[(freqs >= lo_e) & (freqs < hi_e)].sum())) * scale)
        self.band_high = min(1.0, float(np.sqrt(power[freqs >= hi_e].sum())) * scale)

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

    # A SECOND, tighter cap on top of MAX_ACTIVE_NOTES, scoped to one bell
    # voice's own contribution to `_render_bell_notes`. MAX_ACTIVE_NOTES was
    # calibrated for `pluck`'s cost model — one _osc() call per note, one
    # partial. Bell renders BELL_PARTIAL_RATIOS partials per note in one
    # batched call, so its per-note cost is that many times higher; the
    # shared 96-note cap alone lets one bell voice's worst case (decay long
    # enough, rate high enough — both independently reachable via
    # _normalise's clamps, not just the UI's narrower knob ranges) blow the
    # frame budget on its own. Measured directly at the default 8192-frame
    # blocksize: with 5 partials, 24 active notes for one bell voice averaged
    # ~60-80ms against a ~186ms budget — enough headroom for another bell
    # voice, other voice types, and master effects to coexist without
    # starving the render thread the way 3 uncapped pluck voices once did
    # (see MAX_ACTIVE_NOTES above). Enforced in `_render_bell_notes`, not at
    # schedule time — MAX_ACTIVE_NOTES stays the one shared budget across
    # every note-scheduling voice type; this only tightens bell's own slice
    # of it.
    MAX_ACTIVE_BELL_NOTES = 24

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

    def _render_bell(self, v, n0, frames):
        """Struck notes with a fixed inharmonic partial bank (bell/chime
        character) rather than `pluck`'s single fundamental. Scheduling
        reuses `_schedule_notes` unchanged — `wf="sine"` is passed as an
        unused placeholder, since the bell's timbre comes entirely from
        BELL_PARTIAL_RATIOS/AMPS, not from the note's own waveform. Rendering
        does NOT go through `_render_note_events` (see `_render_bell_notes`):
        that method does one `_osc()` call per note for one fundamental,
        and a bell needs a bank of partials per note — a different batching
        shape, not a bigger version of the same one."""
        tempo = float(self.spec.get("tempo", 60))
        rate = float(v.get("rate", 1.0))          # strikes per beat
        interval = int(self.sr * 60.0 / max(1e-3, tempo * rate))
        scale = v.get("scale") or [0, 3, 7, 10]
        root = v.get("note", 72)
        decay = float(v.get("decay", 1.2))
        tone = float(min(1.0, max(0.0, v.get("tone", 1.0))))

        def freq_fn(step):
            semi = scale[step % len(scale)] + 12 * ((step // len(scale)) % 2)
            return midi_to_hz(root + semi)

        self._schedule_notes(v["name"], n0, frames, interval, freq_fn, "sine", decay, tone)
        return self._render_bell_notes(v["name"], n0, frames, tone)

    def _render_bell_notes(self, voice_name, n0, frames, tone):
        """Batched partial-bank renderer for `bell`. Same technique as
        `_render_pad` — flatten (note x partial) into one 1D combo array,
        build a 2D phase matrix, one `_osc()` call, weighted sum — extended
        to handle notes with different start times via a per-note envelope
        repeated across its own partials. Partitions `self._active_notes`
        into this voice's notes and everyone else's exactly as
        `_render_note_events` does, so pluck/arp notes from other voices in
        the same shared pool are left untouched."""
        mine, other = [], []
        for note in self._active_notes:
            (mine if note["voice"] == voice_name else other).append(note)
        if not mine:
            return np.zeros(frames, np.float32)
        # Bell's own tighter cap on top of the shared MAX_ACTIVE_NOTES budget
        # — see MAX_ACTIVE_BELL_NOTES. Same "evict oldest" policy as
        # `_schedule_notes`; `mine` is in schedule order (oldest first), so
        # the newest MAX_ACTIVE_BELL_NOTES survive. Notes dropped here are
        # gone for good, not deferred — they don't reappear in `other` below.
        if len(mine) > self.MAX_ACTIVE_BELL_NOTES:
            mine = mine[-self.MAX_ACTIVE_BELL_NOTES:]

        idx = np.arange(n0, n0 + frames)
        starts = np.array([n["start"] for n in mine], dtype=np.int64)
        freqs = np.array([n["freq"] for n in mine], dtype=np.float64)
        decays = np.array([n["decay"] for n in mine], dtype=np.float64)

        age = (idx[None, :] - starts[:, None]) / self.sr           # (N, frames)
        env = np.where(age >= 0, np.exp(-age / np.maximum(decays, 0.05)[:, None]), 0.0)

        # tone brightens/dulls the fixed partial table, same spirit as pad's
        # tone-driven rolloff — it doesn't change WHICH partials ring, only
        # how loud the upper ones are relative to the fundamental.
        ratios = BELL_PARTIAL_RATIOS
        amps = BELL_PARTIAL_AMPS * (max(tone, 0.05) ** np.arange(len(BELL_PARTIAL_RATIOS)))
        n_partials = len(ratios)

        fk = (freqs[:, None] * ratios[None, :]).ravel()            # (N*P,)
        ak = np.tile(amps, len(freqs))                             # (N*P,)
        t = idx / self.sr
        ph = fk[:, None] * t[None, :]                               # (N*P, frames)
        osc_out = _osc(ph, "sine")                                  # one call

        env_rep = np.repeat(env, n_partials, axis=0)                # (N*P, frames)
        out = (ak[:, None] * env_rep * osc_out).sum(axis=0)
        out = (out / BELL_PARTIAL_AMP_SUM).astype(np.float32)

        # Prune: same eviction rule as _render_note_events (age at the end of
        # this block vs. the note's own un-floored decay*6), vectorised.
        alive_mask = age[:, -1] <= decays * 6
        surviving = [n for n, keep in zip(mine, alive_mask) if keep]
        self._active_notes = other + surviving
        return out * 0.6

    # Harp's own slice of the shared MAX_ACTIVE_NOTES budget, in the same
    # spirit as MAX_ACTIVE_BELL_NOTES but enforced at SCHEDULE time rather
    # than render time. Bell can leave it late because its notes are short and
    # its share of the pool drains on its own; a harp's notes are alive for
    # tens of seconds, so without a schedule-time cap it would monopolise the
    # shared pool and evict every other voice's notes through the oldest-first
    # slice in `_schedule_notes` — where "oldest" is exactly the wrong metric
    # for a voice whose oldest notes are still audible.
    #
    # Note that since `_render_harp_notes` was rewritten to render from a
    # deduplicated (frequency, tau) table, this is a POLYPHONY limit and no
    # longer really a cost limit: cost scales with the number of distinct
    # PITCHES ringing, which a scale bounds at ~10, not with the note count.
    # 24 notes of worst-case density measure 13ms a block.
    MAX_ACTIVE_HARP_NOTES = 24

    def _retire_excess(self, voice_name, cap, now):
        """Ramp this voice's oldest notes out once more than `cap` are ringing,
        rather than letting the shared MAX_ACTIVE_NOTES slice drop them mid-ring.

        Only RINGING notes count against the cap. Charging the retiring ones
        to it as well cascades: a retirement takes ~2 blocks to actually leave
        the pool, so it keeps being counted while fresh notes arrive, each
        block retires more to compensate, and the polyphony sawtooths (it fell
        from 24 to 9 and back every couple of seconds — clearly audible as the
        voice thinning out and swelling again). The cost of exempting them is
        a bounded overshoot of roughly two blocks' worth of scheduling, and
        those notes are old enough to land in the cheap tail bank anyway.
        """
        ringing = [n for n in self._active_notes
                   if n["voice"] == voice_name and n.get("retire") is None]
        for note in ringing[:max(0, len(ringing) - cap)]:
            note["retire"] = now

    def _schedule_harp(self, v, n0, frames):
        """Onsets for one harp voice.

        Deliberately not `_schedule_notes`: that lays notes down on a single
        fixed interval, and the harp's characteristic gesture is a ROLL — a
        fast sweep up the scale, then silence while the whole thing rings.
        `roll` notes fire per beat, `roll_spread` seconds apart, which makes
        the voice rhythmically sparse and polyphonically dense at the same
        time. That combination is the entire reason the long decay is worth
        paying for. `roll` of 1 degenerates to the even one-note-per-beat
        pattern `pluck` produces.
        """
        tempo = float(self.spec.get("tempo", 60))
        rate = float(v.get("rate", 0.5))              # rolls per beat
        interval = max(int(self.sr * 60.0 / max(1e-3, tempo * rate)),
                       int(self.sr * self.MIN_ONSET_INTERVAL_S))
        roll = max(1, min(12, int(v.get("roll", HARP_ROLL_DEFAULT))))
        spread = int(self.sr * max(self.MIN_ONSET_INTERVAL_S,
                                   float(v.get("roll_spread", 0.07))))
        if roll > 1:
            # A roll has to finish inside its own beat, or successive rolls
            # interleave and the ascending sweep stops reading as one gesture.
            spread = max(1, min(spread, interval // roll))

        scale = v.get("scale") or [0, 3, 7, 10]
        root = int(v.get("note", 60))
        decay = float(v.get("decay", 6.0))
        tone = float(min(1.0, max(0.0, v.get("tone", 0.6))))
        span = max(1, len(scale) * HARP_OCTAVES)

        end = n0 + frames
        # Look back far enough to catch rolls whose beat began in an earlier
        # block but whose later notes land in this one.
        g0 = max(0, (n0 - (roll - 1) * spread) // interval)
        for g in range(g0, end // interval + 1):
            for j in range(roll):
                onset = g * interval + j * spread
                if not (n0 <= onset < end):
                    continue
                # Each roll ascends from `j`, and successive rolls start one
                # degree higher, so the gesture stays recognisable while never
                # repeating the same sweep twice in a row.
                d = (g + j) % span
                self._active_notes.append(dict(
                    start=onset, wf="sine", decay=decay, voice=v["name"], tone=tone,
                    freq=midi_to_hz(root + scale[d % len(scale)] + 12 * (d // len(scale))),
                    retire=None))

        self._retire_excess(v["name"], self.MAX_ACTIVE_HARP_NOTES, end)
        if len(self._active_notes) > self.MAX_ACTIVE_NOTES:
            self._active_notes = self._active_notes[-self.MAX_ACTIVE_NOTES:]

    def _render_harp(self, v, n0, frames):
        self._schedule_harp(v, n0, frames)
        return self._render_harp_notes(v, n0, frames)

    def _render_harp_notes(self, v, n0, frames):
        """Harmonic partial bank with per-partial decay, rendered from a
        DEDUPLICATED (frequency, tau) table rather than one row per
        (note x partial).

        The naive shape — which is what `_render_bell_notes` does, correctly,
        for a voice whose notes last a second — costs one sin() and one exp()
        per note per partial. At this voice's steady state (24 notes ringing,
        5 partials) that is 120 rows x 8192 frames, measured at 66ms a block
        against every other scene in the library's 5-8ms. It made the audio
        callback overrun and drop out.

        Two redundancies collapse it, both specific to how this synth works:

        1. PHASE COMES FROM THE ABSOLUTE SAMPLE CLOCK, not per-note state
           (see the module docstring). So two notes at the same pitch have
           bit-identical oscillator rows — only their envelopes differ. A roll
           walks a scale of ~10 distinct pitches, so 24 notes need ~10 pitches
           worth of sines, not 24.

        2. THE ENVELOPE FACTORISES. `decay` and `damp` are voice params, so
           every note shares the same set of time constants, and
               exp(-(t - start)/tau) == exp(-t'/tau) * exp(start'/tau)
           with t' measured from the start of the block. The first term is one
           row per distinct tau (normally 5, one per partial); the second is a
           per-note SCALAR. Rebasing to block-local time is what keeps both
           factors well-conditioned — against the absolute clock the scalar
           overflows within seconds.

        What is left is a small table of rows, each a decaying sine, and a
        matrix of coefficients saying how much of each row every note wants.
        Summing is then one BLAS matmul. Notes only need their own row when
        their contribution is not a constant multiple of a shared one — i.e.
        when they start mid-block or are fading out — and those are handled as
        per-group masks over the same shared table.
        """
        name = v["name"]
        # Clamped at read time, not just in `_normalise`: `set_param` writes
        # live UI/MIDI values straight into the spec without going through it.
        damp = min(1.5, max(0.0, float(v.get("damp", HARP_DAMP_DEFAULT))))
        tone = min(1.0, max(0.0, float(v.get("tone", 0.6))))

        mine, other = [], []
        for note in self._active_notes:
            (mine if note["voice"] == name else other).append(note)
        if not mine:
            return np.zeros(frames, np.float32)
        # Hard safety net under the schedule-time cap, applied to the RINGING
        # notes only. Retiring notes are exempt and always rendered: they sort
        # oldest-first, so a plain `mine[-cap:]` slice would cut precisely the
        # notes that are mid-fade and make the whole retire ramp unobservable
        # (it did). They are self-limiting anyway — each is gone within
        # HARP_RETIRE_FADE_S.
        ringing = [n for n in mine if n.get("retire") is None]
        if len(ringing) > self.MAX_ACTIVE_HARP_NOTES:
            dropped = set(id(n) for n in ringing[:-self.MAX_ACTIVE_HARP_NOTES])
            mine = [n for n in mine if id(n) not in dropped]

        sr = self.sr
        last = n0 + frames - 1
        t_end = last / sr
        amps = HARP_PARTIAL_AMPS * (max(tone, 0.05) ** np.arange(HARP_PARTIALS))
        fade_len = sr * HARP_RETIRE_FADE_S

        rows: dict[tuple[float, float], int] = {}   # (freq, tau) -> row index
        groups: dict[tuple, dict[int, float]] = {}  # mask key -> {row: coeff}
        alive = []

        for note in mine:
            decay = max(0.05, float(note["decay"]))
            start = note["start"]
            retire = note.get("retire")
            age_end = t_end - start / sr

            # Still RENDERED in the block it dies in — pruning before the sum
            # would cut its final tail and put a step where a fade belongs.
            keep = age_end <= decay * HARP_NOTE_LIFETIME
            if retire is not None and (last - retire) / fade_len >= 1.0:
                keep = False
            if keep:
                alive.append(note)

            # Old notes drop to HARP_PARTIALS_TAIL — per-partial damping has
            # already silenced the rest (see HARP_TAIL_AFTER).
            n_partials = (HARP_PARTIALS_TAIL if age_end > HARP_TAIL_AFTER * decay
                          else HARP_PARTIALS)
            # A note needs its own mask only if it begins inside this block or
            # is fading. Everything else — the overwhelming majority — shares
            # the unmasked group and collapses into pure coefficients.
            key = (start if start > n0 else None, retire)
            g = groups.setdefault(key, {})
            startloc = (start - n0) / sr
            for ki in range(n_partials):
                tau = decay / HARP_PARTIAL_K[ki] ** damp
                row = rows.setdefault((note["freq"] * HARP_PARTIAL_K[ki], tau), len(rows))
                g[row] = g.get(row, 0.0) + amps[ki] * math.exp(startloc / tau)

        self._active_notes = other + alive
        if not rows:
            return np.zeros(frames, np.float32)

        # --- the table: one decaying sine per distinct (freq, tau) ----------
        n_rows = len(rows)
        freqs = np.empty(n_rows)
        taus = np.empty(n_rows)
        for (f, tau), i in rows.items():
            freqs[i] = f
            taus[i] = tau
        tloc = np.arange(frames) / sr                       # block-local
        tabs = np.arange(n0, n0 + frames) / sr              # absolute, for phase

        wave = np.sin((2.0 * np.pi) * freqs[:, None] * tabs[None, :])
        # exp() only for the distinct time constants — normally HARP_PARTIALS
        # of them for the whole voice, rather than one per row.
        utau, tinv = np.unique(taus, return_inverse=True)
        wave *= np.exp(-tloc[None, :] / utau[:, None])[tinv]

        # --- coefficients, one row per mask group, summed by BLAS -----------
        coeff = np.zeros((len(groups), n_rows))
        for gi, table in enumerate(groups.values()):
            for row, c in table.items():
                coeff[gi, row] = c
        mixed = coeff @ wave                                # (groups, frames)

        out = np.zeros(frames)
        for gi, (start, retire) in enumerate(groups.keys()):
            if start is None and retire is None:
                out += mixed[gi]
                continue
            m = np.ones(frames)
            if start is not None:
                m[:max(0, min(frames, int(start - n0)))] = 0.0
            if retire is not None:
                m *= np.clip(1.0 - (np.arange(n0, n0 + frames) - retire) / fade_len, 0.0, 1.0)
            out += mixed[gi] * m

        return (out / HARP_PARTIAL_AMP_SUM).astype(np.float32) * HARP_OUTPUT_GAIN

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

    def __init__(self, spec: dict | None = None, sr: int = SR, channels: int = 2):
        self.sr = sr
        self.channels = channels if channels in SURROUND_LAYOUTS else 2
        self.current = Soundscape(spec, sr=sr, channels=self.channels)
        self._pending_spec: dict | None = None
        self._pending_muted = False
        self._phase: str | None = None   # None | "out" | "in"
        self._fade_dur = 0.0             # duration of the CURRENT half (out or in)
        self._fade_pos = 0.0
        self._peak = 0.0
        self._clip = False

    def set_spec(self, spec: dict, fade: float = 0.0):
        # Simple crossfade: fade out old scene over `fade` seconds, then fade in new scene over `fade` seconds
        fade = max(0.0, float(fade or 0.0))
        if fade <= 0.0:
            self.current.set_spec(spec)
            self._phase = None
            return
        self._pending_spec = spec
        self._pending_muted = self.current.muted
        self._fade_dur = fade / 2.0  # Each phase (out and in) gets half the total crossfade time
        self._fade_pos = 0.0
        self._phase = "out"

    def set_param(self, path: str, value):
        self.current.set_param(path, value)   # live tweaks always target the incoming/current one

    def set_channels(self, channels: int):
        """Re-point at a different output width. Applied to the live
        soundscape and remembered, so the one built at the far side of a
        crossfade opens at the same width instead of reverting to stereo."""
        self.channels = channels if channels in SURROUND_LAYOUTS else 2
        self.current.set_channels(self.channels)

    @property
    def spec(self) -> dict:
        return self.current.spec

    @property
    def last_peak(self) -> float:
        return self._peak

    @property
    def last_clip(self) -> bool:
        return self._clip

    def bands(self) -> dict:
        """Level + three-band energy of the live soundscape, for the
        modulation matrix. Read straight off the currently-rendering
        Soundscape; during a crossfade that is whichever half is playing,
        which is the one you can actually hear."""
        c = self.current
        peaks = getattr(c, "voice_peaks", {})
        # Keyed off the SPEC's voice list, not off which voices happened to
        # produce output this block: a sparse pluck sitting between notes
        # contributes nothing, and keying off peaks alone made its modulation
        # source blink in and out of existence.
        names = [v["name"] for v in (c.spec.get("voices") or []) if "name" in v]
        return {"level": self._peak,
                "low": getattr(c, "band_low", 0.0),
                "mid": getattr(c, "band_mid", 0.0),
                "high": getattr(c, "band_high", 0.0),
                "voices": {n: float(peaks.get(n, 0.0)) for n in names}}

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
        # safety limiter on the faded output
        np.tanh(out, out=out)

        self._fade_pos += frames / self.sr
        if self._fade_pos >= self._fade_dur:
            if self._phase == "out":
                # Fade out complete: swap in the new soundscape and start fading it in
                self.current = Soundscape(self._pending_spec, sr=self.sr,
                                          channels=self.channels)
                self.current.set_muted(self._pending_muted)
                self._pending_spec = None
                self._phase = "in"
                self._fade_pos = 0.0
                # Return silence at the transition (avoids overlap artifacts)
                self._peak = 0.0
                self._clip = False
                return (block * 0.0).astype(np.float32)
            else:
                # Fade in complete: switch to normal rendering
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
    if field in ("arp", "chord", "scale", "env", "lfo", "compress"):
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


def _optional_clamp(d: dict, key: str, lo: float, hi: float, default: float):
    """Clamp `key` in place, but leave it ABSENT when it is absent and unused.

    For fields that only mean something on a rig most sessions don't have.
    Writing them unconditionally would churn every tracked scene file on the
    next save; dropping them when they sit at the default keeps a scene that
    never used one byte-identical, while a scene that does use one keeps it.
    """
    if key not in d:
        return
    v = _clamp(d.get(key), lo, hi, default)
    if v == default:
        d.pop(key, None)
    else:
        d[key] = v


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
    eq["low"] = _clamp(eq.get("low"), -10.0, 10.0, 0.0)
    eq["mid"] = _clamp(eq.get("mid"), -10.0, 10.0, 0.0)
    eq["high"] = _clamp(eq.get("high"), -10.0, 10.0, 0.0)
    s["eq"] = eq
    s["swell_amount"] = _clamp(s.get("swell_amount"), 0.0, 1.0, 0.0)
    s["swell_period"] = _clamp(s.get("swell_period"), 5.0, 120.0, 24.0)
    # Surround-only fields (`swell_depth_amount` here, `depth` per voice
    # below) are written back only when they are actually in use. `_normalise`
    # output is what lands in scenes/<name>.json, so defaulting them on every
    # spec would rewrite the entire tracked library the next time any scene
    # was saved, to record a number that does nothing on a stereo rig. Same
    # reasoning as the harp-only fields further down, and the same reason
    # `from_dict` reads every SceneSpec field with a default: absent means
    # "off", and stays absent.
    _optional_clamp(s, "swell_depth_amount", 0.0, 1.0, 0.0)
    s["filter_sweep_amount"] = _clamp(s.get("filter_sweep_amount"), 0.0, 1.0, 0.0)
    s["filter_sweep_period"] = _clamp(s.get("filter_sweep_period"), 5.0, 120.0, 30.0)
    voices = []
    for i, v in enumerate(s.get("voices", [])):
        v = dict(v)
        v.setdefault("name", f"voice{i+1}")
        v.setdefault("type", "pad")
        v["level"] = _clamp(v.get("level"), 0.0, 1.0, 0.4)
        v["pan"] = _clamp(v.get("pan"), -1.0, 1.0, 0.0)
        # Front/back position, 0 = front (exactly where stereo puts it),
        # 1 = rear. Unipolar rather than bipolar-centred: the no-op has to be
        # the default, and here the no-op is an END of the range, not its
        # middle. (`line_curve` is bipolar for the same underlying reason —
        # its no-op happens to sit in the middle.) Optional, so scenes that
        # never touch it stay byte-identical on save.
        _optional_clamp(v, "depth", 0.0, 1.0, 0.0)
        # This voice's share of the scene's filter sweep. Optional for the
        # same reason as `depth`: absent means off, and stays absent, so
        # scenes that never use it are untouched on save.
        _optional_clamp(v, "sweep", 0.0, 1.0, 0.0)
        v["mute"] = bool(v.get("mute", False))
        v["tone"] = _clamp(v.get("tone"), 0.0, 1.0, 0.4)
        v["detune"] = _clamp(v.get("detune"), 0.0, 0.05, 0.01)
        v["rate"] = _clamp(v.get("rate"), 0.05, 8.0, 0.5)
        # `harp` rings for far longer than any other note voice — that IS the
        # voice — so it gets its own ceiling rather than raising the shared
        # one, which would let a runaway pluck schedule notes that never age
        # out. See HARP_MAX_DECAY.
        if v["type"] == "harp":
            v["decay"] = _clamp(v.get("decay"), 0.05, HARP_MAX_DECAY, 6.0)
            # Harp-only fields, set only on harp voices: `_normalise`'s output
            # is what gets written back to scenes/<name>.json, and defaulting
            # these on every voice would churn the whole tracked library on
            # the next save for no benefit.
            v["damp"] = _clamp(v.get("damp"), 0.0, 1.5, HARP_DAMP_DEFAULT)
            v["roll"] = int(_clamp(v.get("roll"), 1, 12, HARP_ROLL_DEFAULT))
            v["roll_spread"] = _clamp(v.get("roll_spread"), 0.03, 0.4, 0.07)
        else:
            v["decay"] = _clamp(v.get("decay"), 0.05, 6.0, 1.2)
        v["unison"] = int(_clamp(v.get("unison"), 1, 7, 1))
        v["sub"] = _clamp(v.get("sub"), 0.0, 1.0, 0.0)
        v["distortion"] = _clamp(v.get("distortion"), 0.0, 1.0, 0.0)
        comp = v.get("compress")
        if isinstance(comp, dict):
            comp = dict(comp)
            comp["on"] = bool(comp.get("on", False))
            comp["threshold"] = _clamp(comp.get("threshold"), 0.1, 1.0, 0.6)
            comp["ratio"] = _clamp(comp.get("ratio"), 1.0, 16.0, 4.0)
            v["compress"] = comp
        else:
            v.pop("compress", None)
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
