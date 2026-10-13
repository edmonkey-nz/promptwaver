# Adding an Instrument

How a synth voice type gets built in `promptwaver/audio/dsp.py`, and the
constraints that decide its shape. Written after adding `harp`; the reasoning
generalises, the numbers are measured on this codebase.

Read `TECHNICAL.md` for the surrounding architecture. This file is only about
voices.

## 0. First decide it's a voice type at all

Most sound ideas are a *param* on an existing voice, not a new type. A new type
is justified when the thing you want changes the **rendering shape** — how
samples get produced — rather than the numbers fed into an existing one.

- Brighter, darker, thicker, detuned, slower, panned → a param. `tone`,
  `detune`, `unison`, `rate`, `decay` already exist and are already
  modulation destinations.
- A different partial structure, a different envelope topology, a different
  note-scheduling gesture → a type.

`bell` earned its place because a struck bar needs a bank of inharmonic
partials per note, and `_render_note_events` renders one fundamental per note —
"a different batching shape, not a bigger version of the same one". `harp`
earned its place because a plucked string needs a decay *per partial*, which
the same method has nowhere to put.

Adding a param costs one line in `_normalise` and one in the UI. Adding a type
costs everything below. Be sure.

**Worked example — `character` and `drift` (2026-09).** A brief for "brighter,
sweeter instruments" sounds like several new voice types and is none. Both
shipped as params:

- `character` swaps `bell`'s partial table for one of six voicings (celesta,
  glockenspiel, music box, gamelan, tine). The rendering shape is untouched —
  §2's "once you are at `(N*P, frames)` resolution, per-partial behaviour is
  free" is exactly this case. Measured spectral centroid 1547 Hz (bell) to
  2556 Hz (gamelan), a 1.0–1.65x spread, at **no cost change**: 5.01ms vs
  4.82ms a block at default density, against a 5–8ms library baseline. One
  param turned one instrument into six.
- `drift` detunes each note by a slow bounded random walk. Note the constraint
  that decided its shape: `_osc` derives phase from the absolute sample clock
  (`ph = fk * t`), so retuning a *ringing* note jumps its phase and clicks.
  Applying it at **schedule** time instead is free, click-free, and keeps every
  note in the deduplicated `(frequency, tau)` table.

**Worked example 2 — `resonance` and multiple LFOs (2026-09).** A brief for
"less drone, more evolving synth" also turned out to need no new voice type.

- `resonance` adds a peak at the cutoff the rolloff in `_wavetable` already
  defines. A real resonant filter is a per-sample recursion, which this module
  forbids — but that rolloff is already per-harmonic gain, so resonance is one
  more term on the same vector, and tables are cached by
  `(waveform, tone, n, res)` so it costs nothing per block (1.41ms → 1.31ms,
  i.e. noise). **Measure the peak, not the centroid**: resonance *narrows*
  centroid swing (3.87x → 2.70x under a filter sweep) because it keeps high
  energy present at the dark end. The right measure is an interior local
  maximum that tracks the cutoff — cutoff h14 → peak h12, h20 → h17, h29 →
  h25, always slightly below, as a real filter behaves against a falling
  source. Note the gain has to fight the source's own slope: a saw falls as
  1/k, so +12dB read as a tilt and +28dB was needed to sing.
- Multiple LFOs per voice: `lfos` is a LIST beside the original single `lfo`,
  and `_lfos` returns them **keyed by destination**. Keying by destination is
  what kept the change small — every one of the six consumption sites still
  gets the same `lfo[0..3]` tuple and none of their maths changed. Cost 1.16ms
  → 3.40ms at the cap of four, still inside the library band.

Two lessons worth carrying: the cheap lever is usually the partial table or the
onset, not a new renderer; and if you state a bound in a constant, test it —
`MAX_DRIFT_CENTS` was initially only *typically* true (the walk's steady state
reached ±1.003, i.e. 10.53 cents against a stated 10.5) until the walk position
was clamped.

**Worked example 3 — the arp that wasn't its voice (2026-09).** "With the arp
on, none of the other instrument settings do anything" was literally true, and
measured: tone, resonance, detune, unison and sub all produced a 0.000 spectral
difference. Two causes, both invisible from any one file:

- `_render_arp` defaulted `waveform` to `"sine"` while `_render_osc` and
  `_render_pad` default to `"saw"`. A voice that never set a waveform was a saw
  stack with the arp off and pure sine blips with it on — and a sine has no
  harmonics for tone or resonance to shape. **When one voice has two render
  paths, their defaults must agree.**
- The arp rendered through pluck's `_render_note_events`, one bare oscillator
  per note, so unison/detune/sub were never read.

`_render_arp_notes` now renders each note as the voice (waveform, tone,
resonance, unison stack, detune, sub — read live, so knobs move ringing notes)
in the batched shape: oscillator rows dedupe on pitch (an arp walks a chord),
and envelopes factorise into one shared row per decay plus a per-note scalar,
with only mid-block onsets getting their own row. Numbers: `wing_shimmer` went
from 1.4ms (sine blips) to ~10ms a block, i.e. what the same voice costs with
its arp off — that is the price of sounding like it. Factorising the envelopes
took the dense no-drift worst case (notes piling up to the 96-note pool) from
21.8ms to 12.6ms; `ARP_MAX_ROWS = 42` bounds the drift worst case, where every
note is its own pitch, at ~16ms. `ARP_OUTPUT_GAIN = 0.8` recentres the 23 arp
voices in the library at a median -0.2dB (range -2.6..+4.4) against the old
sound; no full mix peaks higher (0.773 vs 0.768). Pluck output is bit-identical.

**Worked example 4 — `fm`, a type that had to be one (2026-10).** FM fails
the "is it a param?" test cleanly: its sound comes from one oscillator's
output being fed into another's *phase*, `sin(2pi fc t + I(t) sin(2pi r fc t))`,
and no existing renderer has a phase input to feed. Three decisions shaped it:

- **Phase from the note's onset, not the absolute clock.** Every other voice
  derives phase from the sample clock so rows dedupe across notes (§3). FM
  can't use that: the attack of a struck FM sound IS the starting relationship
  between carrier and modulator, and a clock-derived phase gives every strike
  a different transient. So each note is its own row, cost scales with note
  count rather than distinct pitches, and `MAX_ACTIVE_FM_NOTES = 24` is a real
  cost cap: four transcendentals a note-sample, 1.2ms a block at the cap.
- **`ratio`/`index`/`index_decay` are snapshotted onto the note at its
  strike**, like `drift`, through `_schedule_notes(extra=)`. Retuning `ratio`
  under a ringing note would jump its modulator phase; a knob shapes the next
  note instead.
- **The index is capped per note so the sidebands stay under Nyquist**
  (Carson: most energy within `(I+1) * fm` of the carrier). Measured: a note
  at MIDI 100 with ratio 8 and index 10 puts 0.0000 of its energy below the
  carrier, where aliasing would land.

At `index` 0 it is a pure sine at exactly a sine `pluck`'s level (peak ratio
1.000), which is why `FM_OUTPUT_GAIN` is pluck's 0.6 rather than a calibrated
constant. Measured spectral centroid on a strike with `index_decay` 0.3:
1147Hz at the attack, 446Hz 1.5s in. That fall IS the voice.

A testing trap found on the way: a render is only reproducible with **three**
seeds — `np.random.seed`, `random.seed` (swell phase and period), and the
Soundscape's own `self._rng` (drift). Without the second, the committed code
did not match itself and a regression check reported a false difference.

## 1. The two rendering shapes

Every voice is one of these. Pick deliberately — this is the decision the rest
of the work follows from.

**Continuous** (`pad`, `sub`, `osc`, `noise`, `granular`) — the voice always sounds; there
are no note events. Phase comes from the absolute sample clock, so tones stay
continuous across block boundaries with no stored per-oscillator phase. These
belong in `ENVELOPED_TYPES` so muting fades via ADSR instead of cutting.

**Note-scheduling** (`pluck`, `arp`, `bell`, `harp`, `fm`) — onsets are appended to
the shared `_active_notes` pool and each note carries its own envelope. These
stay *out* of `ENVELOPED_TYPES`: each note already has a decay, and gating the
scheduler fights it.

Within note-scheduling there are two further sub-shapes, and this is where cost
lives:

| | per-note loop | batched matrix |
|---|---|---|
| used by | `pluck` | `bell`, `harp`, `arp`, `fm` |
| method | `_render_note_events` | `_render_bell_notes` / `_render_harp_notes` / `_render_arp_notes` / `_render_fm_notes` |
| cost | one `_osc()` call **per note** | one `_osc()` call **total** |
| shape | 1 fundamental per note | flatten (note × partial) into one 2D matrix |

The per-note loop is a Python-level loop over numpy calls. It is fine for a
voice whose notes are gone in a second and only a handful overlap. It is not
fine for anything else — three uncapped pluck voices once starved the render
thread this way.

**If your voice sustains, you need the batched shape.** Long sustain means many
concurrent notes by definition; that is not an optimisation you add later.

## 2. The batched shape, concretely

The idiom, from `_render_bell_notes` and `_render_harp_notes`:

1. Partition `_active_notes` into this voice's notes and everyone else's. The
   pool is shared, so leaving other voices' notes untouched is mandatory.
2. Enforce this voice's own note cap.
3. Build `(N,)` arrays of `start`, `freq`, `decay`.
4. `age = (idx[None,:] - starts[:,None]) / sr` → `(N, frames)`.
5. Flatten `(note × partial)` into `(N*P,)` frequency and amplitude vectors.
6. One `_osc()` call over the `(N*P, frames)` phase matrix.
7. Weighted sum down to `(frames,)`, prune dead notes, return.

The useful property: **once you are at `(N*P, frames)` resolution, per-partial
behaviour is free.** `bell` repeats one per-note envelope across each note's
partials; `harp` instead gives every *row* its own time constant
(`decay / k**damp`) and gets frequency-dependent damping for the same array,
the same single `_osc()` call, and no extra passes. If you want a partial to
behave differently from its siblings, this is where it costs nothing.

## 3. The cost model

### The block budget is not the budget

**Since 0.90 the default block is 1024 frames (23.2ms at 44.1kHz)**, down from
8192 (186ms); a saved `audio_blocksize` still wins. The numbers in this section
that say 8192 were measured at the old size — read them as ratios, and see
"Calibrate against the library" below for the 1024 figures.

At 8192 the callback nominally had **186ms**. Treating that as the target is
the mistake that shipped a broken voice: `harp` was measured at 75ms, judged
"comfortably inside budget", and dropped out audibly in the real app.

Two reasons the real ceiling is far lower:

- **Rendering competes for the GIL** with the render thread and the ~20Hz
  websocket broadcaster. (Before 0.90 that render thread projected every frame
  at 45fps; it now only ticks, ~0.6ms, which is what made 1024 viable.) Measured: a soundscape costing 14.5ms alone
  took 28ms with a single competing CPU-bound thread, and spiked to 43ms.
  Whatever you measure in isolation, assume roughly double under load.
- **You have no idea what scene it runs under.** Cost is per-*voice*, but the
  budget is per-*mix*, and a heavy visual scene is competing too.

Note this used to be far worse. `Soundscape.render()` was called **inside the
PortAudio callback**, so the realtime thread had to win the GIL before it could
produce a sample — the callback reported a 75ms average and a 368ms max against
a 186ms budget, and was being *delivered* late (200ms average interval against
186 expected). `synth.py` now renders a block ahead on a normal thread and the
callback only copies, which absorbs that jitter entirely (max callback 6ms
under six competing threads). The budget above is what the *producer* has, and
it gets a full block period to use it — but the buffer is one block deep, so a
voice that regularly exceeds a block period will still drop out.

**Calibrate against the library, not the budget.** Measured 2026-10-13 on all
26 library soundscapes, 8 s of audio each, first quarter skipped:

| block | library mean per block | library p95 per block | worst single block | share of the block |
|---|---|---|---|---|
| 1024 (23.2ms) | 0.7–2.4ms | 0.9–4.8ms | 7.0ms (`LionsMane`) | p95 4–21% |
| 8192 (186ms) | 2.2–18.1ms | — | 27.7ms (`Mandala`) | ~1–15% |

**Small blocks cost proportionally more.** Going 8192 → 1024 divides the
frames by 8 but the cost only by ~5–7: every block pays a fixed numpy dispatch
overhead per voice and per row (§2 — the win is fewer calls, not cheaper ones).
So a voice's share of the budget is ~1.3–1.6× larger at 1024 than the 8192
figures below suggest, and the "120 rows" ceiling in the table was set at 8192
and **has not been re-measured at 1024** — leave margin under it. A voice
at 5× the library baseline is a voice that will drop out on someone's machine.

### What actually costs

Rows × frames, where a row is one `(note, partial)` evaluated across the block.
At 120 rows × 8192 frames, measured:

| op | cost |
|---|---|
| `np.sin` (oscillator) | 28ms |
| `np.exp` (envelope) | 16ms |
| multiply + sum | 2.6ms |
| `np.where` gate | 2.3ms |
| `np.repeat` | 1.3ms |

**The transcendentals are everything; the bookkeeping is free.** So the only
optimisation that matters is evaluating fewer `sin`/`exp` rows — never
micro-optimising the array shuffling around them.

### Two redundancies worth exploiting before cutting features

Both are properties of how this synth is built, and between them they took
`harp` from 66ms to 17ms with **no loss of polyphony or partials**:

**Notes at the same pitch share an oscillator row exactly.** Phase is derived
from the absolute sample clock rather than per-oscillator state (a founding
choice of this module — it is what makes tones continuous across blocks). So
two notes of the same pitch have bit-identical `sin` rows and differ only in
their envelopes. A voice walking a scale has ~10 distinct pitches however many
notes are ringing, so **dedupe rows on `(frequency, tau)` and accumulate a
coefficient per note** instead of giving each note its own row.

**The envelope factorises out of the note.** `decay` and `damp` are voice
params, so every note shares one small set of time constants, and

```
exp(-(t - start)/tau)  ==  exp(-t'/tau) * exp(start'/tau)
```

with `t'` measured from the start of the block. The first factor is one row
per distinct `tau` (normally one per partial); the second is a per-note
**scalar**. Rebasing to block-local time is not cosmetic — against the
absolute sample clock `exp(start/tau)` overflows within seconds.

What survives is a small table of decaying sines plus a coefficient matrix, so
the summation becomes one BLAS matmul. Notes need their own row only when
their contribution is not a constant multiple of a shared one — starting
mid-block, or fading out — and those are handled as per-group *masks* over the
same shared table, not as separate rows.

The consequence for capacity planning: once deduplicated, **cost scales with
distinct pitches, not note count.** The note cap stops being a cost lever and
becomes purely a polyphony choice.

### Caveats that still apply

Design for the case where **every live note is fresh**. Optimisations that
depend on notes being old (`HARP_TAIL_AFTER` renders faded notes with 2
partials instead of 5) reduce *typical* cost, not worst-case, and must not be
load-bearing.

Partial count is still a linear multiplier and the lowest-risk lever if you
need a quick cut — `bell` went from 8 partials to 5 for exactly that reason.

## 4. Note lifecycle — and the assumptions long sustain breaks

Three mechanisms guard the shared pool. All three encode "notes are short", and
a sustaining voice invalidates all three.

**`MAX_ACTIVE_NOTES` (96, shared).** `_schedule_notes` trims oldest-first when
the pool overflows, on the argument that the oldest note is the quietest. For a
sustaining voice *oldest is not quietest* — its oldest notes are still audible —
so it will both suffer from and cause bad evictions, starving every other
voice. **A sustaining voice needs its own cap enforced at schedule time**, not
just at render time like `MAX_ACTIVE_BELL_NOTES`, so it never reaches the
shared trim at all.

**Lifetime `decay * 6`.** exp(-6) is about -52dB: a sane margin at a 1s decay,
absurd at 12s, where it holds notes for 72 seconds. `harp` uses `* 4` (-35dB,
still inaudible under a mix) and gets a third of its note budget back.

**Hard eviction.** Dropping an evicted note outright is inaudible when it has
already decayed to nothing. Measured on `harp` at a dense setting, the note
being retired was 1.6s old and still at **-1dB of its onset amplitude** —
cutting that is a step, not a fade. A sustaining voice needs a release ramp
(`HARP_RETIRE_FADE_S`); the difference is only -19dB relative to the signal, but
it is the difference between a click and no click.

Two traps found while building that ramp, both of which made it silently
useless:

- **Don't let the render-time cap slice cut retiring notes.** They sort
  oldest-first, so a plain `mine[-cap:]` removes precisely the notes that are
  mid-fade, and the ramp becomes unobservable. Exempt them; they are
  self-limiting.
- **Don't count retiring notes against the cap.** A retirement takes ~2 blocks
  to leave the pool, so charging it to the cap makes each block retire more to
  compensate. Polyphony sawtoothed from 24 down to 9 and back every couple of
  seconds — clearly audible as the voice thinning out and swelling again.

## 5. Output gain

`pluck`/`bell` end with a flat `* 0.6`. That works because only a handful of
their notes ever overlap.

A sustaining voice is louder in proportion to its polyphony, and needs its own
trim. `harp` measured **2.0–2.5 peak against pluck's 0.5** at the same `level`
before calibration, which slams the master `tanh` and turns the ring into
distortion. `HARP_OUTPUT_GAIN` is `0.6/sqrt(12)`, calibrated so a dozen
simultaneously-ringing notes land in pluck's range.

Deliberately a **constant, not a divide by the live note count**: dividing
would duck the whole voice by ~3dB every time a chord or roll fires, which is
audible pumping on exactly the gesture the voice exists to play. The cost is
that sparse settings come out quiet and want their `level` raised — so pick
defaults that produce the voice's *characteristic* density (`harp` defaults to
`roll: 6`, not `1`, because a bare single-note harp renders ~7× quieter than a
pluck and reads as broken).

## 6. The registration points

Seven places, none of which check each other. Missing any one leaves the voice
inaudible, unselectable, invisible, or unplayable from a key or keyboard.

| where | what |
|---|---|
| `dsp.py` `VOICE_TYPES` | add the name |
| `dsp.py` `Soundscape.render` | the `elif vt == "..."` dispatch branch |
| `dsp.py` `_normalise` | clamp every new param, with defaults |
| `dsp.py` `_coerce` | **any non-float param** — it `float()`s everything it doesn't know |
| `claude_director.py` `_SOUNDSCAPE_GUIDE` | so the director can select it |
| `web/static/index.html` (`#voices` panel) | knobs for the new params |
| `dsp.py` `Soundscape.play` + `trigger_pitches` | how a PLAYED note sounds: struck (joins `_active_notes`, decays) or held (`_held`, gated by the voice's `env`) — and what one trigger plays |

`_coerce` is the one that bites, because it only fails on a LIVE edit — the
voice renders fine from a saved scene and then throws `could not convert string
to float` the moment someone touches the control. It was missing from this
table until `character` (a string param, like `waveform`) hit it.

`_SOUNDSCAPE_GUIDE` is shared by the 3D and 2D system prompts, so it is one
edit, not two. Without it the voice exists but no generated scene will ever
use it — the same way `ripples` and `attractor` sat unreachable for so long.

Two things about `_normalise` worth knowing:

- Its output is what gets written to `scenes/<name>.json`. Defaulting a new
  field on **every** voice churns the whole tracked scene library on the next
  save. Gate voice-specific fields on `v["type"]`.
- **`set_param` bypasses it.** Live UI and MIDI writes go straight into the
  spec unclamped, so anything that would divide by zero or explode must *also*
  be clamped where it is read.

## 7. What to check before calling it done

There is no test suite, so this is the substitute. Render offline against
`Soundscape` directly — no audio device needed — and confirm:

- **Finite and bounded** over a few hundred blocks, including degenerate params
  (decay 0, empty scale, rate at both clamps, damp at both clamps).
- **Cost** — mean and max ms per block at the voice's worst-case density, not
  its default, and compared against **other scenes in the library** (5–8ms),
  not against the 186ms block budget. See §3: the budget is not the ceiling.
- **Polyphony is stable**, not sawtoothing. Print the live note count per block
  and look at the trajectory, not just the final value.
- **Headroom** — `voice_peaks` against `pluck`'s at the same `level`.
- **The full mix doesn't clip** — `last_peak` with the voice in a real scene.
- **The characteristic claim is real.** Whatever the voice is *for*, measure it.
  For `harp` that was spectral centroid falling as a note rings (411 → 342 Hz
  over 6.5s) — the actual definition of a plucked string, and something you
  cannot confirm by looking at the waveform.
- **No regression** — every other voice type still renders finite and non-silent.
- **A JSON round-trip is stable** and non-harp voices gain no new keys.

Then run it in the app (`verify-in-app` skill) and confirm the panel renders.
Note that a machine with no audio output device will report `callbacks: 0` and
`device: None` in `audio_diag` — the VU still moves because the engine renders
for the modulation sources, so you can verify *signal* but not *sound*.

## 8. Effects — the same rules, applied after the voice

An effect is a voice-shaped problem with the oscillator removed: it must be
pure numpy, block-granular, and never a per-sample recursion. What that
rules in and out is the useful part.

**What the rule allows.** Anything whose output depends on past *input* but
not on its own past *output*. Convolution (reverb) and a modulated
multi-tap delay reading history (chorus) are both in. A delay *with
feedback* is only in because `Delay` clamps its time to at least one block,
so the feedback read never touches samples written in the same block. True
resonant filters, a phaser, a flanger with feedback and Karplus-Strong are
all out: each needs `y[n-1]` to compute `y[n]`. `scipy.signal.lfilter`
would make them cheap, and scipy is deliberately not a dependency.

**Where effects sit in `Soundscape.render`:**

```
per voice:  render -> played notes -> envelope gain -> level/tremolo -> swell
            -> distortion -> CHORUS -> voice meter -> REVERB SEND -> pan
whole mix:  sum -> + REVERB RETURN -> EQ / filter sweep -> delay
            -> master distortion -> master/mute -> tanh limiter
```

Chorus is after distortion so the copies are of the voice as it finally
sounds. The reverb send is pre-pan (the room is the whole space, not a place
beside the voice) and the return goes in before the EQ and delay, so it is
shaped and echoed with everything else.

**Absent means off, and off costs nothing.** `reverb` (soundscape),
`reverb`/`chorus`/`chorus_rate` (per voice) are all optional: absent keys
skip the code path entirely, and the whole library renders bit-identical
to before they existed (26 soundscapes, 60 blocks, seeded). `_normalise`
keeps them absent on save (`_optional_clamp`), so no scene file churns.

**`ConvReverb`** is uniform-partitioned convolution (overlap-save, FFT of
twice the block) over a synthesised impulse: seeded decaying noise, -60dB at
`size`, with high frequencies dying faster by `damp`. Measured at 1024
frames, a ~23ms block (the slower of two runs: the CPU clock moves these by
up to 2x):

| size | stereo | quad |
|---|---|---|
| 1s | 0.42ms | 0.70ms |
| 2.5s | 0.85ms | 1.56ms |
| 4s | 1.32ms | 3.02ms |
| 6s (max) | 1.92ms | 4.12ms |

Three things about it that are easy to undo:

- **The impulse is built incrementally.** A full build is 40-70ms, two to
  three whole blocks, so a size/damp drag would starve the audio. It builds
  `BUILD_PER_BLOCK` partition-channels per block while the old response keeps
  playing, then crossfades over one block: at most 6.2ms a block in stereo
  and 8.8ms in quad, and a 6s tail is ready in 8 blocks (0.2s) in stereo or
  16 (0.4s) in quad. Measured with size dragged every 100ms on `hot lava`:
  8.7ms a block on average, zero starved callbacks.
- **The input history is sized for the longest tail**, so a size change
  keeps what is ringing instead of resetting it.
- **Each output channel gets its own noise**, so the tail is decorrelated
  (L/R correlation 0.01) and wraps the listener. On 5.1 it spreads over the
  four corners and never the centre or LFE.

A steady pure tone can come out slightly *quieter* with reverb (`hot lava`
-0.4dB at mix 0.3): a static impulse has a fixed, random gain and phase at
each frequency, so a drone partly cancels against its own tail. That is what
a real room does to a sine; it is not a bug.

**Chorus** is three taps at 9/13/17ms swept by one LFO a third of a cycle
apart, read from the voice's last ~21ms by linear interpolation. 0.14ms a
voice. Its LFO phase **accumulates** so a live rate change bends the sweep
rather than jumping all three copies' pitch at once. The history is emptied
when a block was skipped (a muted voice), or unmuting would replay its old
tail. Output is divided by `sqrt(1 + 3w^2)` so loudness holds within ~1dB
across the amount range.

Measured together on `hot lava` (the heaviest library scene): reverb at 6s,
chorus on every voice and an added `fm` voice took the block from 2.9ms to
6.2ms, with zero starved callbacks.

### `granular` — a voice that is half an effect (0.93)

A grain cloud: short Hann-windowed grains, Poisson onsets at `density` per
second, every live grain one row of a `(G, frames)` matrix (the batched
shape of §2), capped at `MAX_GRAINS = 64`. Worst case (80/s of 0.5s grains,
57 alive) measured 1.7ms a block. Two sources:

- **`scene`** reads the OTHER voices' recent output from a 6s history ring.
  Each voice's finished mono (post level, distortion and chorus, pre-pan)
  is summed into a bus after it renders, and the bus is written to the ring
  after the voice loop. Two rules keep this out of feedback territory, and
  both are load-bearing:
  - **Granular voices never feed the bus.** A cloud with nothing else
    playing is exactly silent, and two clouds can't build on each other.
  - **A grain never reads the block being rendered.** That block isn't in
    the ring yet. The read starts at least `(ratio - 1) * length + frames`
    back, because an upward-pitched grain's read head gains on the present
    as it plays. Measured at +24 semitones with 0.5s grains: the cloud's
    first sample came 4,398 samples after its source started, never before.
  Reads before the ring's continuous history began (or past its length)
  return silence, not stale audio from before a gap.
- **`tone`** grains are the voice's own waveform at random degrees of its
  `scale` over two octaves. 97% of the 200-900Hz energy measured within
  ±12Hz of a scale degree, which is a 0.2s Hann grain's own main-lobe width.

**Each voice's grains come from its own generator, seeded from its NAME.**
So a scene's cloud is the same every time it plays, and adding a cloud never
shifts another voice's random stream. Loudness divides by
`sqrt(density * grain)` (the average overlap), then a calibration gain. At
level 0.4 a scene cloud measures 0.38-0.40 of its (saw) source, flat across
density 16-80 and grain 0.15-0.5s; only a very sparse cloud of a pure sine
reads higher (0.47), from grain-on-grain interference.

A granular voice is in `ENVELOPED_TYPES` (mute fades it, like `noise`) but
plays no notes: `play` ignores it and `trigger_pitches` is empty, so the
panel gives it no map button.

