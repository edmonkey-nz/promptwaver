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

## 1. The two rendering shapes

Every voice is one of these. Pick deliberately — this is the decision the rest
of the work follows from.

**Continuous** (`pad`, `sub`, `osc`, `noise`) — the voice always sounds; there
are no note events. Phase comes from the absolute sample clock, so tones stay
continuous across block boundaries with no stored per-oscillator phase. These
belong in `ENVELOPED_TYPES` so muting fades via ADSR instead of cutting.

**Note-scheduling** (`pluck`, `arp`, `bell`, `harp`) — onsets are appended to
the shared `_active_notes` pool and each note carries its own envelope. These
stay *out* of `ENVELOPED_TYPES`: each note already has a decay, and gating the
scheduler fights it.

Within note-scheduling there are two further sub-shapes, and this is where cost
lives:

| | per-note loop | batched matrix |
|---|---|---|
| used by | `pluck`, `arp` | `bell`, `harp` |
| method | `_render_note_events` | `_render_bell_notes` / `_render_harp_notes` |
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

### The 186ms block budget is not the budget

The audio block is 8192 frames at 44.1kHz, so the callback nominally has
**186ms**. Treating that as the target is the mistake that shipped a broken
voice: `harp` was measured at 75ms, judged "comfortably inside budget", and
dropped out audibly in the real app.

Two reasons the real ceiling is far lower:

- **Rendering competes for the GIL** with the 45fps visual render thread and
  the ~20Hz websocket broadcaster. Measured: a soundscape costing 14.5ms alone
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

**Calibrate against the library, not the budget.** Every saved scene's
soundscape renders in **5–8ms** a block. That is the number to sit near. A
voice at 5× the library baseline is a voice that will drop out on someone's
machine.

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

Five places, none of which check each other. Missing any one leaves the voice
inaudible, unselectable, or invisible.

| where | what |
|---|---|
| `dsp.py` `VOICE_TYPES` | add the name |
| `dsp.py` `Soundscape.render` | the `elif vt == "..."` dispatch branch |
| `dsp.py` `_normalise` | clamp every new param, with defaults |
| `claude_director.py` `_SOUNDSCAPE_GUIDE` | so the director can select it |
| `web/static/index.html` (`#voices` panel) | knobs for the new params |

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
