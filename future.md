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
