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

### e) Playing a scene's instruments — BUILT in 0.91

Each voice has a **map** button (beside its speaker-icon mute) that learns what
plays it: a computer key, a MIDI pad (CC press) or a MIDI keyboard channel (at
pitch). Saved per scene; played notes record into takes and replay at their
exact time. See docs/TECHNICAL.md "Playing a scene's instruments" for the
measured latency (a constant ~3 blocks: ~70 ms at 1024, ~35 ms at 512), the
struck/held split and the gain calibration.

Not built, natural next steps:

- **A QWERTY piano layout** (two rows as a keyboard, octave keys). Today a
  computer key is one trigger playing the voice as authored, not a pitch.
- **Sustain pedal** (CC 64) and **velocity curves** — velocity is linear now.
- **A "play only" switch per voice**: played notes add to the voice's own
  generative output; there is no way yet to silence the pattern but keep the
  played notes (mute silences both).
- **Automatic 512 while playing.** Deliberately not done: changing blocksize
  restarts the stream (an audible gap) and 512 starved 3 callbacks in 23 scene
  switches. It is a manual Settings › Audio choice for now.

### f) HOLD — bluetooth controllers from the laser-arcade sibling project

Poach the bluetooth controller code for driving 2D/3D camera and motion
settings. Not started.

### g) Surround output and per-voice `depth` — BUILT, needs a room test

Shipped as **quad**: a front pair and a rear pair, with each voice placed on a
front/back axis by its own `depth` alongside its existing `pan`. Driven by the
per-voice LFO and by the swell, as scoped. Verified on this machine end to end
except for the one thing that needs speakers — whether it *sounds* like a room.

What exists now:

- **`dsp.SURROUND_LAYOUTS`** maps a channel count to `(front L, front R, rear
  L, rear R)`. 2 = stereo, 4 = quad, 6 = the same quad image carried on a 5.1
  stream with **centre and LFE left silent**. 6 is there only because a 5.1
  sink may refuse a 4-channel stream; on this machine's
  `alsa_output.…hdmi-surround` (which enumerates `max_out: 6`) both 4 and 6
  open clean, measured.
- **`_pan_gains(pan, depth, channels)`** — one vectorised gain term per voice,
  broadcast against its mono buffer. Equal-power on both axes (`sum(g²) == 1`
  at every depth, verified). Returns `(channels,)` for scalar inputs and
  `(frames, channels)` when either axis is a per-sample LFO array.
- **Per-voice `depth`**, unipolar 0..1 — 0 is the front pair, i.e. exactly
  where stereo put the voice. Unipolar rather than bipolar-centred because the
  no-op has to be the default and here the no-op is an END of the range.
- **`swell_depth_amount`** rides the *same* wave as the level swell
  (`_swell_wave`, split out for this), so a voice at the top of its cycle is
  both loudest and furthest forward. Independent amount, defaults 0.
- **`depth` as an LFO destination**, in `LFO_DESTS_SMOOTH` next to `pan`.
  Listed unconditionally, including on a stereo rig: the LFO config is scene
  data and has to round-trip through a stereo session unchanged.
- **`audio_channels`** is a rig setting in `settings.json`, threaded through
  `Engine.configure_audio` → `SoundscapeSynth.reconfigure`, with a fallback to
  stereo that reports itself in `last_error` and persists what actually
  opened. UI: Settings > Audio > *speakers*, with each device's channel count
  now shown in the device list.
- **MIDI**: `voice#N.depth` on CC 70-77, `swell_depth_amount` learnable.

Two properties worth not breaking:

- **Stereo output is bit-identical to the pre-surround code** — verified
  against `git show HEAD` across all 39 scene soundscapes × 4 blocks (max abs
  sample difference 0.0, same for the band/VU readings). `depth` is ignored
  outright at 2 channels rather than folded down, and the gain vector is left
  at float64 so the arithmetic is the same operation it always was. The check
  has to exclude `noise` voices: `_render_noise` uses an unseeded
  `np.random.default_rng()`, so it is not reproducible run to run — that is
  pre-existing, and a trap for any future numerical comparison in this file.
- **No scene-library churn.** `depth`, `sweep` and `swell_depth_amount` go
  through `_optional_clamp`, which leaves a field absent when it is absent and
  unused, so `_normalise` adds no keys to any existing scene.

Cost, measured at blocksize 8192 (186ms budget), median over 40 blocks:
`Circuitz` 6.46 → 6.94 → 7.63ms and `hot lava` 9.51 → 10.24 → 10.82ms at 2/4/6
channels. Quad is **+7.5%**, 5.1 is **+15%**, and the worst case is still
under 6% of the block budget. Streams opened on the HDMI sink with zero
underruns and zero starved callbacks.

Still open:

- **The room test.** Nothing here proves the rears are the rears, that the
  channel ORDER matches what the receiver expects, or that a voice at depth 1
  actually sounds behind you. `SURROUND_LAYOUTS` is one dict if the order is
  wrong.
- **Centre and LFE stay silent at 6 channels.** A centre send is trivial but
  wants a reason (ambient music with no dialogue has nothing obvious for it);
  an LFE needs a crossover, and the cheap per-voice version is the per-sample
  recursion this file forbids. The affordable shape is one block-FFT lowpass
  over the finished mix, reusing `_apply_eq`'s machinery — worth doing if the
  sub sits silent on a real 5.1 receiver.
- **Stereo fold-down.** A surround-authored scene on a stereo rig currently
  ignores depth entirely. A distance cue wants attenuation *and* HF damping,
  and per-voice damping is the expensive half.
- **The director authors neither `depth` nor `sweep`.** `_SOUNDSCAPE_GUIDE` is
  untouched, deliberately: a field that does nothing on most output devices is
  noise in every generated scene. Revisit if surround becomes the normal rig.
- **`camera.depth` already exists** as an unrelated culling mode. No JSON
  collision — the new one is `soundscape.voices[].depth` — but the name is
  taken in the other half of the spec, which is worth knowing before adding a
  modulation destination called `depth`.

### h) Per-voice filter sweep — BUILT

A `sweep` knob on every voice, 0..1, default 0. The whole-mix `filter_sweep`
could not be scaled per instrument — it is one EQ curve over the summed mix,
so excluding a voice from it would mean giving that voice its own filter, and
a per-voice filter is either banned per-sample recursion or its own FFT per
voice per block. So the per-voice control sweeps the cheap brightness handle
each voice already has: its `tone`, which selects a bandlimited wavetable.

That costs nothing — `_wavetable` quantises tone to 1/100 and caches, so a
sweeping voice warms ~100 tables once — and it runs at the same block
granularity the whole-mix sweep already does. Both run off one `sweep_swing`
phase computed once per block, so a scene with both up moves as a single
gesture. Measured: one voice at `sweep=1` swings the mix's high-frequency
fraction 4x more than baseline, all voices considerably more.

Not a *filter* in the EQ sense — it is a brightness sweep. If a true
per-instrument filter is ever wanted, that is the block-FFT-per-voice cost
above, and it should be scoped explicitly rather than grown out of this.

### i) Voice knobs now follow what the renderers read — BUILT

Every voice type's panel is built from the parameters its renderer actually
uses in `dsp.py`, rather than from whatever got UI first. The drift had
stranded real parameters behind hand-edited JSON or a MIDI CC:

| voice | was missing |
|---|---|
| `bell` | `rate`, `decay`, `tone` — it had **no** type-specific knobs at all |
| `pluck` | `decay`, `tone` (it showed `rate`) |
| `osc` | `tone` |
| `pad`, `sub` | `detune` (and `tone` for `sub`, which renders through `_render_pad`) |

Verified each one changes the output: bell/pluck `rate` schedules more notes,
`decay` rings longer, `tone` brightens. **`tone` on a SINE does nothing** and
that is correct — `_wavetable` builds one partial for a sine, so there is
nothing above it to roll off (saw/square/triangle all move). The knob carries
a tooltip saying so on sine voices, because a correct knob that does nothing
looks like a broken one. `bell` is exempt: it ignores `waveform` and shapes
its own five-partial inharmonic bank.

**Deliberately not added: bell inharmonicity.** `BELL_PARTIAL_RATIOS` is a
fixed table, so `tone` only changes how loud the upper partials are, never
where they sit — the "clang vs chime" axis has no control. A `stretch`
parameter interpolating the ratios about the fundamental
(`1 + (ratios - 1) * k`, k default 1.0 so absent means unchanged) would be
nearly free: the ratios array is rebuilt per block anyway and is 5 elements
long. It was left out because it is a new DSP parameter and a new scene-format
field, where everything above was just exposing what already existed. Worth
doing if bells still sound one-note after playing with the knobs. Note the one
edge it would need thinking about: at high `stretch` with a high root note the
upper partials can pass Nyquist and alias — no scene in the library gets near
that today (highest bell root is 72, top partial 1570Hz) and the base ratios
have the same latent issue, but stretching widens it.

### j) Kiosk on a separate tablet — the mic is the blocker

Wanted: main computer runs everything and drives the output screen; an iPad on
the LAN browses only `/kiosk` as the visitor's control surface.

**Most of it already works, measured 2026-09-04 against `192.168.1.31:8097`:**
the server already binds `0.0.0.0`, a non-loopback client's `kiosk_press`
returns `ok: True`, and the armed loopback gate correctly refuses that same
client's operator commands ("operator controls are restricted to this
machine") — which is exactly the right split for this topology, by accident
rather than design.

**The blocker is the microphone.** `KioskSession.release` records from
`engine.analysis`, the `sd.InputStream` opened by whichever machine runs the
engine — so a visitor holding the iPad has their prompt captured by the mic in
whatever room the *computer* is in. Recording on the iPad instead is not a
small change: `getUserMedia` requires a secure context and `http://192.168.x.x`
is not one (only `localhost` is exempt), so iOS Safari refuses outright. Doing
it properly means HTTPS with a cert the iPad trusts (a self-signed cert needs a
profile installed and trusted on the device) *plus* a whole browser-recording
path — `MediaRecorder` upload, a binary/base64 transport (the ws handler is
`WSMsgType.TEXT` only), and server-side decode.

**The cheap answer is a long USB mic cable** to wherever the iPad lives. No code
changes, and the visitor's audio still never leaves the machine — which was the
whole point of choosing local Whisper over a cloud STT.

Two things to fix if this is ever built:

- **Bandwidth is ~11.3 Mbps** (measured: 73KB per state frame, ~20/s, because
  `/kiosk` connects with `?hq=1`). Fine on clean wifi, will stutter on a busy
  network. If the main computer already has the output screen the tablet does
  not need a high-fidelity render at all — a `?lite=1` mode that skips the
  canvas entirely would cut this to near nothing. That is the small,
  self-contained piece of this item and could be done on its own.
- **iOS housekeeping**: Add to Home Screen for fullscreen, Guided Access so
  visitors can't leave the page, and auto-lock off (or the Screen Wake Lock
  API, Safari 16.4+). `pointerdown`/`pointerup`/`pointercancel` all work on iOS
  Safari; WebGL2 needs iOS 15+.

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
- **CHANGELOG has no entries for 0.31–0.70.** Not worth reconstructing.
- **Surround / per-voice depth** is built — see **g)** above for what is done
  and what still needs a real 5.1 room.

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

### Reverb, chorus and an FM voice — BUILT in 0.92

All three became affordable once audio moved to 1024-frame blocks rendered
ahead of the callback (0.90). How each fits the "no per-sample recursion"
rule below, and what each costs, is in `docs/INSTRUMENTS.md` §8.

- **Reverb** took option 2 of the two this section used to weigh:
  uniform-partitioned **convolution** (`dsp.ConvReverb`) over a synthesised
  decaying-noise impulse, `size` 0.3–6s, `damp` darkening the tail as it
  decays. Option 1 (parallel long delay lines) was never needed: convolution
  has no recursion at all, so the block-length limit that made a delay-based
  room impossible doesn't apply. 1–2.4ms a block in stereo.
- **Chorus** is per voice, a three-tap ensemble swept a third of a cycle
  apart. The "short modulated delay vs blocksize" worry didn't apply either:
  the taps read history, never this block's output, so there is no feedback
  path to recurse. 0.14ms a voice.
- **`fm`** is a struck two-operator voice: e-piano, glass bells, mallets.

Then in 0.93:

- **`granular`**, a grain-cloud voice. The "what does it granulate?" problem
  this list used to raise was answered twice: `source: "scene"` granulates
  the OTHER voices' last few seconds (pitched up an octave by default, the
  shimmer halo), and `source: "tone"` makes grains of its own waveform at its
  scale's notes. 1.7ms a block at its worst case.
- **`index` and `density` LFO destinations**, for fm brightness and cloud
  thickness.
- **The audio-only Regenerate call got its own system prompt.** The note that
  stood here claimed it never saw `_SOUNDSCAPE_GUIDE`. That was wrong: it sent
  the whole 3D scene prompt, guide included. The real problem was the
  opposite. ~1.8k of its ~5.2k tokens were geometry rules that contradict an
  audio-only request. It now sends `_SYSTEM_AUDIO` (a framing line plus the
  guide).

Still open:

- **Feedback effects stay out**: true resonant filters, a phaser, a flanger
  with feedback and Karplus-Strong strings are all per-sample recursions.
  `scipy.signal.lfilter` would make them cheap, but scipy is not a
  dependency, and adding it grows every release binary.
- **Playing a granular voice.** It has no map button, because a cloud has no
  note. A natural design: while a key is held, a `tone` cloud draws its grain
  pitches from the held notes instead of its scale.

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
