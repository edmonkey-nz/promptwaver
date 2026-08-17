# Backlog

## Done in 0.73.0

1) ~~when useign the 'regenerate audio' make it choose the current scene in the dropdown when first opened, and make sure the prompt field is empty.~~
2) ~~fix the status of the 'regenerating audio' throbber like the 'generating scene'~~
3) ~~when audio has been regenerated, close the modal and refrsh the scene so the new audio plays~~
4) ~~change the 'shape modulation' collasped pane title to '3D Scene modulation' and make it collasped if an 2d scene is open.~~ — **superseded in 0.75.0**: the two panes were merged into one *Modulation* panel. The 2D/3D titles implied a matched pair, but the "2D" one was the universal matrix (camera routes live there, on 3D scenes) and the "3D" one a narrow shape-scale extra, so the naming sent you to the wrong pane.
5) ~~change the 'modulation' pane title to '2D Scene modulation' and make it collasped if an 3d scene is open.~~ — **superseded in 0.75.0**, see above.
6) ~~update the new 2d paramter silders to allow midi mapping.~~

a) ~~check noise - doesnt fade out on muting instreument (jupiter - space whsiper)~~ — `noise` now gets the same ADSR as pad/osc/sub, so muting releases over its own release time instead of cutting. `pluck` deliberately left out: each of its notes already carries a decay envelope.
b) ~~main eq's are too strong, limit to +/- 10~~ — narrowed in all four places that carried the range (DSP clamp, MIDI table, three UI knobs).
c) ~~display the API cost of the request after generating~~ — computed from the response's own `usage` block at per-model rates. Cache hits and offline fallbacks say "no API charge" rather than showing a stale figure.
d) ~~new save buttons at top of screen with icons~~ — 🎵 sound / 🎛 midi / 💾 all, delegating to the existing panel buttons. **The 'not yet saved' indicator was attempted and reverted — see below.**

## Open

### The 'not yet saved' indicator (the other half of d)

Ported the lightsaber pattern: signature-compare the saveable slice of live
state against a "last known clean" snapshot. The comparison itself works —
with nothing touched, all three slices (soundscape, camera, spec) read clean
and stay clean while idle.

Establishing the baseline across a scene **load** is what defeated it.
`library_name` updates the moment a load is enqueued, but the crossfade keeps
`scenes.current` on the *outgoing* scene, so the next several broadcasts still
report the previous scene's soundscape and layers. Those are stable — wrongly
signalling that the load has settled — and are then replaced, which reads as
an edit. Waiting on `scene_transition` plus a five-tick stability counter
still left it firing after a switch.

**Reinstating this wants a real signal from the engine rather than more
client-side heuristics** — e.g. a monotonically increasing `spec_rev` in
`state()`, bumped by `_install_spec`, so the client can baseline on the first
broadcast whose revision matches the load it asked for.

### e) 25-note MIDI keyboard playing a scene's own instrument

Play one of the loaded scene's voices from a MIDI controller (and optionally
the QWERTY keyboard), instead of only turning its knobs.

**The blocking constraint is latency, and it should be settled before any UI
work.** The synth renders whole blocks inside the audio callback, and
`audio_blocksize` defaults to **8192 frames ≈ 186ms** at 44.1kHz. A note
cannot sound before the start of the next block, so at the default every
keypress is up to a fifth of a second late — unplayable. 1024 frames ≈ 23ms is
fine; 2048 ≈ 46ms is borderline. Blocksize is already a setting, so step one
is simply: does this machine run glitch-free at 1024 with a busy scene? If
not, the feature isn't worth building.

Given that, the pieces:

1. **Note events into the engine.** `midi.py` handles CC only today — add
   note-on/note-off. Notes must NOT go through `_enqueue`: that queue is
   drained by the render thread at `fps`, adding another ~22ms and jitter on
   top. They want a lock-free queue read directly by the audio callback.

2. **A playable voice in the DSP.** `Soundscape` renders from a static spec;
   the only per-note machinery is `pluck`'s scheduler. **Reuse it.** The
   arpeggiator already schedules through that same path specifically so it
   can't reintroduce the unbounded-note-growth bug fixed in 0.12.0 — a played
   note should take the same route and inherit the same note cap.

3. **Voice selection.** Which of the scene's voices the keyboard drives. The
   voice keeps its own waveform/tone/env, so a played note *is* that
   instrument. Per-scene, so it belongs in `SceneSpec` (`played_voice`, read
   with a default like every other field, keeping the round-trip
   backward-compatible).

4. **QWERTY input.** Browser `keydown`/`keyup` → websocket → the same queue.
   Needs auto-repeat suppression and a focus guard so typing in the prompt box
   doesn't play notes. Latency is worse over the websocket — treat it as a
   convenience for auditioning, not for performance.

5. **UI.** A 25-key widget under the Soundscape pane, plus the voice picker.
   Highlight held notes; show the octave offset.

Worth flagging that this changes what the app *is* — from a generative
instrument you tune into one you also play. Velocity curves, sustain pedal and
sequencing are all natural follow-ons; none are in scope above.

### f) HOLD — bluetooth controllers from the laser-arcade sibling project

Poach the bluetooth controller code for driving 2D/3D camera and motion
settings. Not started.

### Smaller open items

- **The 2D director prompt still lets polar motifs dominate.** Generated
  patterns read spidery rather than the crisp straight-line geometry of the
  reference images. Prompt tuning, not a code problem.
- **`max_strokes` is authored too low.** Claude picked 50 for `jupiter`, which
  expands to 133 — so 62% of the pattern never draws. The budget line in the
  2D prompt needs work.
- **Per-voice `waveform` / `arp` as modulation sources.** Deliberately not
  added: waveform is a discrete choice, so mapping it to a continuous param
  produces meaningless jumps. Arp rhythm is already covered by `voice.<name>`
  level. An honest version of this is a step sequencer, which is its own
  feature.
- **The 2D/3D panel split assumes the matrix is 2D-only.** It isn't — it also
  drives `camera.speed` and the other camera destinations on 3D scenes, and is
  now collapsed by default there. Revisit if that gets annoying.
- **`apply_audio_to_scene` doesn't update `spec.audio_prompt`**, so a scene's
  recorded prompt still shows the original after a regenerate.
- **The three procedural generators are still not AI-selectable.** Each
  director prompt names exactly one generator, so `flow_field`, `attractor`
  and `ripples` can be driven by hand but never chosen by Claude.
- **CHANGELOG has no entries for 0.31–0.70.** Not worth reconstructing.
- **5.1 surround audio with depth/front-back panning.** Standard term: "depth" (front/back axis). Would need per-voice depth parameter routed through modulation matrix, and graceful fallback for stereo headphones (either ignored or optional HRTF simulation). Only viable on HDMI surround setups, so conditional UI. Worth revisiting when surround hardware support is part of the scope.

## Audio enhancements

`pluck` is the only percussive/note-based voice (the other four —
`pad`/`sub`/`osc`/`noise` — are all continuous drones), so it's doing double
duty and reads as repetitive across a set. The two directions below were
scoped against the actual DSP architecture (`promptwaver/audio/dsp.py`)
rather than as a generic synth wishlist, because this codebase's performance
rules are stricter than usual: everything renders inside the realtime audio
callback in pure numpy, so a naive implementation is a glitch, not just a
slow frame.

### Bell / mallet voice

The obvious next percussive voice, and the reason it's not a small addition:
a bell's character comes from **inharmonic** partials (ratios like
2.756×, 5.404× the fundamental for a real bell mode) rather than the integer
harmonic series every existing waveform is built from. `_wavetable()` (dsp.py
~130) bakes one periodic single-cycle table per (waveform, tone, partial
count) by summing *integer-multiple* sine partials — that's what makes it
cacheable and lookup-able via `_osc()`'s phase-indexed interpolation.
Inharmonic partials aren't periodic at the fundamental, so they can't be
baked into that same single-cycle table. A bell needs its own additive
render path, not a fifth entry in `WAVEFORMS`.

**The real trap is performance, and it's one this codebase already hit
once.** `_render_pad`'s docstring (dsp.py ~659) documents switching from a
Python loop calling `_osc` once per partial to one batched
`(partials × frames)` vectorised call, because the old version was "up to
len(chord)×2×n_partials separate numpy calls per audio callback" and each
call's fixed dispatch overhead — inside the GIL-held realtime callback — was
stealing time from the render thread. `pluck` and a bell voice would go
through `_render_note_events` (dsp.py ~764), which *still* calls `_osc` once
per active note in a Python loop (up to `MAX_ACTIVE_NOTES` = 96). That's
already the pre-fix pad pattern at the note level; adding several inharmonic
partials *per note*, each its own `_osc` call, multiplies it by
`n_partials` and reintroduces exactly the bug the pad rewrite fixed, just one
level up.

**So:** batch across active notes *and* partials into one vectorised array
op per render block — same lesson as the pad/osc rewrites, applied to the
note scheduler instead of the chord/unison stack. Concretely, treat a bell
strike as a short-lived batched additive voice (fixed inharmonic ratio
table × per-note amplitude envelope, one array op over all currently-ringing
strikes) rather than routing it through the existing per-note
`_render_note_events` loop unmodified. Numerically verify against a naive
reference the same way the pad rewrite did (docstring claims max abs diff
~6e-7) before landing it.

### Reverb

Delay already exists (`Delay` class, dsp.py ~225) but a room reverb is not
"another Delay instance" — worth writing down why before someone tries that.
`Delay.process()` is block-granular specifically because delay time is
clamped to `>= one block` (dsp.py ~240-241), which guarantees read/write
ranges never overlap within a block and lets the whole thing be vectorised
with zero per-sample Python recursion. That's free for an echo effect, which
wants delay times of tens to thousands of ms anyway. A convincing *room*
reverb wants early reflections/comb filters in the 5–80ms range — shorter
than one block at the default blocksize (8192 frames ≈ 186ms) entirely, and
still coarse at the lowest practical setting (1024 ≈ 23ms). The Delay
pattern can't express that.

Two ways forward that respect the constraint instead of fighting it:

1. **Lean into it — a long, washy ambient reverb** built from several
   parallel long delay lines (each independently legal under the same `>=
   one block` rule), detuned/modulated slightly against each other. Less
   "small room," more "cathedral" — which arguably suits this app's ambient
   character better than a tight room sim would anyway.
2. **Block-granular convolution.** `_apply_eq()` (dsp.py ~190) already does
   one rFFT/irFFT per block for the 3-band EQ — that's precedent and
   existing infrastructure for frequency-domain block processing. A fixed
   impulse response applied via FFT overlap-add is the standard efficient
   convolution reverb approach and, done right, needs no per-sample loop
   either. More work than (1), but it's the "real" reverb if that's wanted
   later.

Chorus/phaser carry a milder version of the same constraint (short modulated
delay), worth checking against blocksize before promising either.

### The general rule for anything added here

Every existing voice type and effect in this file follows the same
discipline: pure numpy, block-granular (no per-sample Python loop or
recursion), and where there's per-note or per-partial structure, batch it
into one vectorised array op rather than one numpy call per note/partial.
`_render_pad`'s rewrite and `Delay`'s block-time clamp are both that same
rule applied in different places. Any new voice or effect should be
scoped against it explicitly before implementation, not discovered by a
frame-drop report afterward — that's how the pad/osc batching bug and the
pluck note cap (`MAX_ACTIVE_NOTES`, added after 3 pluck voices hit 1248
active notes and 100ms+ renders against a ~93ms budget) both got found the
first time.
