# Changelog

All notable changes to PromptWaver are logged here. This project is **pre-1.0
and under active development** — expect breaking changes to scene JSON shape
and APIs between minor versions until a 1.0 release.

## [Unreleased]
- Helios DAC SDK build/install instructions (`libHeliosDacAPI.so` + udev rules)
- Project scaffolding for VSCode / GitHub (this changelog, `.vscode/`, `LICENSE`, `pyproject.toml`)

## [0.78.1]

### Audio dropouts on heavy scenes — the GIL, not the DSP

`hot lava` and other expensive 3D scenes dropped audio badly. The cause is not
DSP cost and not the prerender pipeline itself: **the producer is a plain
Python thread, so unlike the PortAudio callback it replaced it gets no realtime
scheduling priority.** On a scene whose frame cost fills the frame budget
(`hot lava` renders in ~20–23ms against 22ms at 45fps) the visual render thread
effectively never yields, and at CPython's default 5ms GIL handover the
producer's ~22ms of work stretched past the 186ms deadline.

Measured on `hot lava` under a real 45fps render loop:

| | starved callbacks | producer p95 | engine fps |
|---|---|---|---|
| before | 34.3% | 648ms | 40.0 |
| after | **0.0%** | 103ms | 38.8 |

- **`sys.setswitchinterval(0.0005)`** when a real audio stream starts. Handing
  the GIL over ten times more often costs ~3% of the frame rate on the worst
  scene and nothing measurable on lighter ones. Not set at import, so a
  `--no-audio` run keeps the interpreter default.
- **Do not raise `PRERENDER_BLOCKS` to fix this** — measured, it makes things
  worse: starvation went 18.8% at depth 1 to 21.1% at 2 and 49.1% at 4. A
  deeper queue only means the producer runs flat out for longer, competing
  harder for the GIL.

For reference, 0.77.0 was also dropping on this scene (23 PortAudio underruns
in 45s, callback averaging 73.7ms against the 186ms budget). What changed in
0.78.0 is that the dropouts stopped being *visible*:

### Diagnostics could not see the new failure mode

When the prerender queue is empty the callback writes silence and returns in
well under a millisecond — on time, with valid data — so PortAudio sets no
underflow flag and **every counter in the audio diagnostics reported a
perfectly healthy stream while the output was full of holes.** Verified: with
the fix reverted, `starved` reads 30.9% while `underruns` is 0 and average
callback duration is 0.46ms.

- **New `starved` / `starved_pct` counters** in `CallbackStats`, plus a
  `recent_starves` log, shown in Settings > Audio diagnostics and folded into
  the panel's health colour. Counted separately from `underruns`: the two have
  different causes and different fixes.

## [0.78.0]

### `harp` — a long-ringing string voice

The third note-based voice, after `pluck` and `bell`. Where a pluck ticks and a
bell strikes, a harp note *blooms*: harmonic partials that each decay at their
own rate, so the note darkens as it rings and keeps overlapping into itself.

- **Per-partial damping** (`damp`, 0–1.5) is what makes it a string rather than
  a longer pluck — the highs die faster than the fundamental. 0.7 is a harp,
  0.3 is glassier, 1.2+ is felted and almost woody.
- **`roll` / `roll_spread`** turn a beat into a gesture: `roll` notes fired
  `roll_spread` seconds apart as an ascending sweep that then rings on
  together. 5–8 at 0.05–0.09s is the characteristic strum; `roll` 1 plays
  single notes.
- **`decay` goes to 20s**, against every other voice's 6s ceiling. That long
  ring *is* the voice, so `HARP_MAX_DECAY` is a separate clamp rather than a
  relaxation of the shared one.
- Notes therefore pile up in a way no previous voice did, so the note budget
  needed a new shape. `MAX_ACTIVE_HARP_NOTES = 24` is enforced at **schedule**
  time by `_retire_excess` rather than at render time, so a still-ringing note
  is retired deliberately instead of being dropped mid-ring by the shared
  `MAX_ACTIVE_NOTES` slice — which was audible as the voice thinning out and
  swelling again.
- The director prompt warns to keep `rate` at 0.2–0.5 rolls per beat: a high
  rate plus a big roll is a continuous glissando that drowns the scene and
  defeats the long decay.

### Audio renders a block ahead of the callback

The PortAudio callback used to call `Soundscape.render()` directly, putting
full numpy synthesis on the realtime thread, behind the GIL, competing with the
45fps render loop and the 20Hz broadcaster. **That was the long-standing source
of audio dropouts, not DSP cost** — the callback measured a 75ms average and
368ms max against a 186ms budget while the actual DSP work was 5–15ms.

- A producer thread now renders `PRERENDER_BLOCKS` ahead into a queue and the
  **callback only copies** — no numpy, no allocation, no lock.
- Queue depth is deliberately 1, the smallest that measures clean, because it
  is pure output latency. `output_latency` includes it, so audio-driven visuals
  don't lead the sound.
- Muting flushes the queue: prerendered blocks would otherwise outlive the mute
  by the whole queue depth.

### Output ratio: flat scenes are no longer stretched

`output_ratio` describes the **surface**; the shape of the `[-1,1]` box the
renderers actually receive is a different number, and nothing distinguished
them. A 3D camera divides x by the viewport aspect, so its box really is 16:9 —
but `pattern2d` composes in a square and never sees the ratio, so every flat
scene was stretched horizontally in the browser and squeezed vertically on the
beam.

- **`Engine.content_aspect()`** derives the box's true shape from `Scene.is_3d`
  and is what `renderer.js` and `ilda.PathPlanner` now letterbox against.
- **New `output_fit` setting** (Settings > Output > *flat scenes*): `fit`
  (letterbox, default), `fill` (pan and scan), `stretch` (the old behaviour).
  MIDI-free rig setting, stored beside `output_ratio`.
- **Fixed: the in-page preview disagreed with the output window** on every
  non-1:1 ratio. `syncMonitorFilters` never set the aspect, so the shared
  renderer fell back to a square box inside a canvas already reshaped to the
  ratio and drew 3D scenes horizontally squashed. A 0.77.0 regression — the old
  Canvas2D preview scaled straight to the canvas and was accidentally correct.

### Unsupported-browser warning

Firefox and Safari both hand you a working WebGL2 context, so the existing
context check passed and the page then misbehaved in ways that didn't point at
the browser. A dismissible banner now names the cause up front, on both the
control page and the output window. Not a modal or an `alert()`: the output
window runs for hours on a projector with no keyboard near it.

### Generated scenes that rendered pure black

Two failure modes that produced a completely black screen with no error
anywhere — nothing raised, nothing logged, nothing to debug.

- **Nested ops.** The documented op shape is flat (`{"op":"line", ...}`) but
  models emit `{"line": {...}}` often enough to be worth tolerating. Both forms
  are unambiguous, so `_coerce_op` now accepts either; previously the op was
  skipped, every def came back empty, and the scene drew nothing. The prompt
  spells the flat form out as well.
- **Placements in the wrong units.** One real scene measured `at` over
  [-55..65] against a `[-1,1]` frame, so every motif landed off-screen. The
  composition is fine in that case and the error is uniform, so `pattern2d`
  rescales to fit and logs it once, rather than showing nothing. The threshold
  (3.0) is well clear of any deliberate composition.

### Generation progress

The determinate progress bars are gone, replaced everywhere by a flowing sine
wave. They were driven by `director_progress`, which is an estimate against an
expected duration rather than measured progress, so the bar regularly stalled
at 80% or jumped. The wave says "working" without claiming a position; the
elapsed-seconds readout is the honest number and is still shown.

### Scene library

`magic harp` and `mushrooms` added, `Rotocross` added, `crossword` and `falling
sand 5` removed, `jupiter` retuned.

## [0.77.0]

### WebGL2 renderer

The browser-side visuals are rendered with WebGL2 instead of Canvas2D. A dense
2D scene that ran at 5–13fps on a full-HD projector now draws in about 0.5ms a
frame. **Chrome (or another Chromium browser) with hardware acceleration is now
required** — there is no Canvas2D fallback.

- **GPU bloom** replaces `shadowBlur`, which was CPU-bound and had to be capped
  at a 16px radius to stay usable. A separable Gaussian costs the same at any
  radius, and the buffer is half-float, so overlapping strokes push past white
  the way real light does rather than clamping flat.
- **One renderer for both surfaces.** `web/static/renderer.js` is shared by the
  control page and the output window, replacing a hand-duplicated pair of paint
  functions that had to be kept in step by hand.
- **Kaleidoscope** is a single shader pass instead of one clipped full-canvas
  blit per wedge per frame. It now mirrors alternate wedges so neighbours meet
  along a shared edge; the old version flipped odd wedges about the canvas
  centre, which sampled an unrelated part of the image. Affects `cutlery
  drawer`, the only scene using it.
- The **in-page preview now applies kaleidoscope**, which it previously ignored,
  so it no longer disagrees with the projector.

### New monitor controls

- **Bloom shape** — `bloom_spread` and `bloom_intensity`, per-scene, under a
  collapsed *Bloom shape* section. Defaults match the previous fixed look.
- **Line curve** — bipolar, per-scene. 0 draws polylines exactly as authored;
  right resamples through a cardinal spline (smooth), left drops points
  (angular). MIDI-bindable and a modulation destination. Monitor-only: the DAC
  still receives the authored path.

### Broadcast rate

The output window was capped near 13fps by the server, not by drawing. Three
fixes take it to the intended 20Hz:

- The broadcaster slept a flat 50ms *after* its work, making the real period
  `work + 50ms`, so 20Hz was unreachable by construction. It now sleeps only the
  unused remainder of the budget.
- `preview()` rounded every coordinate in a Python loop; vectorised with numpy,
  the high-quality path went 13.15ms → 4.76ms. Output is byte-identical.
- The control-page payload was built even when only an output window was
  connected. Both payloads are now built lazily.

## [0.76.0]

### `bell` — a second percussive voice

`pluck` was the only note-based voice; the other four (`pad`/`sub`/`osc`/`noise`) are continuous
drones, so every soundscape's percussive accent came from the same place.

- **New `bell` voice type**: struck notes built from a fixed bank of **inharmonic** partials
  (`BELL_PARTIAL_RATIOS`), which is what makes it read as a bell/chime rather than a plucked
  string. Those ratios aren't integer multiples of the fundamental, so they can't come from the
  cached single-cycle wavetable every other waveform uses — it needs its own additive path.
- Reuses the existing note scheduler (`_schedule_notes`, shared `MAX_ACTIVE_NOTES` cap) unchanged,
  and stays **out** of `ENVELOPED_TYPES` for the same reason `pluck` does: each note already owns
  its decay envelope, so a top-level ADSR would fight it.
- Rendered with one batched `(notes × partials, frames)` call rather than one `_osc()` per note —
  the same lesson `_render_pad` already learned. Sine partials specifically, which sidesteps
  `_osc()`'s one-wavetable-per-call limit instead of fighting it.
- **`MAX_ACTIVE_BELL_NOTES = 24`**, a tighter cap layered on the shared 96-note budget, because a
  bell note costs several partials where a pluck note costs one. Both this and the drop from 8
  partials to 5 came from measurement, not taste: 8 partials blew the 186ms frame budget on their
  own (270ms avg). After: 62ms avg / 73ms max with two bell voices maxed out plus a pluck voice.

### Slow filter sweep

- **New whole-mix `filter sweep` + `sweep period`**: a slow sine added to the high band's EQ gain,
  swinging ±8dB around whatever `eq.high` is set to. Reuses `_apply_eq`'s existing per-block FFT
  untouched — only the dB figure it's handed varies over time — so `amount = 0` is byte-identical
  to the previous behaviour and it needs no on/off flag.
- Single global phase, unlike `swell`'s per-voice randomised phase: there's only one instance of
  this, so there's nothing to decorrelate it against.

### Effect previews

- **Swell and filter-sweep visualisers**, side by side in a collapsible *Effect previews* panel:
  the curve, a wall-clock-synced playhead, and a readout (depth/period, or dB swing/centre/period).
- The swell one is honestly labelled a *representative* curve, not a live per-voice readout — each
  voice runs on its own randomised phase **and** period (`base × 0.7–1.3`), by design, so there is
  no single phase that would be true of any voice.

### Generating a scene no longer freezes the interface

- **The director call is backgrounded** (`asyncio.create_task`) instead of awaited inline in the
  websocket handler. Awaiting it blocked that connection's whole message loop for the 1–3 minutes
  a generation takes — sliders, mute and scene switches all sat unprocessed — while the separate
  state broadcaster kept the canvas updating, which is exactly the "looks alive, nothing responds"
  gap.
- The modal now closes on submit and hands off to a **status strip** at the top of the page: live
  elapsed time, progress, a pulsing border for the first few seconds so the handoff isn't missed,
  and a **Cancel** button (arm-then-confirm) that genuinely interrupts the stream between chunks
  rather than just hiding the UI. On completion it becomes a **Play** button and stays until used.

### The director no longer accepts scenes it can't render

Claude emitted `"generator": "pattern3d"` — a name that doesn't exist — four times running on one
brief. Valid JSON, non-empty layers, so nothing caught it: it was billed, saved, and installed,
and only failed later in `Scene.__init__`, leaving a scene that could neither play nor reopen.

- **Generator names are now validated against the registry** before a generation is accepted, and
  a bad one routes through the same retry-once-then-fall-back path as a parse failure.
- **The retry now catches every exception**, not just `JSONDecodeError`/`ValueError`. A layer-level
  `camera` key raises `TypeError` from `Layer(**l)`, which previously escaped and so skipped *both*
  the retry and the log — the failure was invisible and unrecoverable.
- **Malformed responses are saved to `<cache_dir>/_failed/`.** Previously only the parser's error
  message survived, never the text that caused it, so there was nothing to diagnose from.
- **Both system prompts now state their generator name explicitly** in REQUIREMENTS. It previously
  appeared *only* inside the worked example — directly under a line reading "NEVER reuse the motifs
  from the example below". The 2D prompt also now says that a 3D-*sounding* subject still renders
  flat, which is what the failing brief ("sand falling between two glass panes") tripped on.

### Model choice narrowed to Haiku

- **Sonnet and Opus are commented out** (not removed) in the Generate modal. Sonnet-5 at high
  effort was measured overshooting 64,000 output tokens without closing its JSON, truncated, billed
  in full, taking up to 491s to fail. Opus costs more with no evidence it does better.

### Output windows report their real frame rate

- **New setting, Settings → Output monitors → "show frame rate on output windows"**: a corner
  readout of each window's *actual* paint rate, paint cost in ms, stroke count and canvas size.
- This is a different number from both the engine fps under the visualiser and the ~20Hz broadcast,
  either of which can look healthy while a full-screen canvas is too slow to keep up. Paint cost is
  shown next to the rate deliberately — it separates "the canvas is the bottleneck" (lower strokes
  or glow) from "frames aren't arriving" (server side), which need opposite fixes.

### Interface reorganisation

- Save controls are icon-only (🎵 sound / 🎥 camera / 💾 all / 💿 save-as) with a tick on save, and
  the Show prompts / Show title / Pin MIDI trio joins them as icons — two rows instead of five.
- Output VU meter moved to the top of the side column and is now a 4px horizontal bar.
- `hue` moved out of the stage column into the layer panel, next to the generator's own params.
- Soundscape globals now lay out 7 across; the Modulation panel's sections became individually
  collapsible sub-panels, with the ones that don't apply to the loaded scene hidden rather than
  shown doing nothing.

### Fixed

- Save buttons replaced their icon with a text label on click and never restored it, because the
  "revert" state was hardcoded from when they *were* text buttons.
- The output VU meter animated `height` after being changed to a horizontal bar, so it never moved.

## [0.75.1]

### UI refinements

- Increased left padding on accordion bodies in the control sidebar for better visual breathing room.

## [0.75.0]

### Shape speed — slow the objects without slowing the walk

- **New `shape speed` control** on the layer panel for 3D worlds (0–1). Scales node motion only —
  spin, bob, drift, pulse and time-aware primitives — and leaves camera orbit/walk speed alone.
  Generated scenes routinely author motion at `speed` 2.5–4.0, which thrashes at a camera pace
  that is otherwise right.
- Modulatable as `visual.shape_speed` like any other scalar param, so it can be driven from audio
  or an LFO.
- The clock is **accumulated, not scaled**, so changing the rate never teleports motion phase.

### Scenes are now as big as you ask for

- **`director/expand.py` grows a generated scene to the requested node count** by instancing the
  model's own `defs` along its own camera route. Every added node is a copy of an authored one;
  only its placement is recomputed, and that is copied too — decomposed against the route into
  lateral offset, height and forward nudge — so a floor stays on the floor and a hanging lamp
  stays hanging. A 1200-node world now costs one ~$0.08 call instead of a truncated 73k-token
  response.
- Seeded from the request, so a cache hit and a fresh generation produce the identical world, and
  applied on the cache-read path too so entries written before this still grow.
- Always reported, never hidden: the header flash shows "203 authored → 1200 nodes" and
  `generation_settings.expansion` records the split.

### Modulated controls are now visible as modulated

- **A slider with a modulation route pointing at it is marked amber with a `∿`**, and its tooltip
  names the source and depth. `ModMatrix.value` returns `base + Σ(source × depth)` — routes *add*
  to the slider rather than replacing it — so a modulated control is a floor, not the value.
  Without the mark it simply looks broken: `rabbithole` had camera speed at 0 and a camera that
  kept travelling, because `audio_level → camera.speed` was driving it at depth 0.7.
- `scenes/rabbithole.json`: route depth 0.7 → 0.18 and base speed 0 → 0.06, so the walk is a
  gentle drift that still breathes with the audio instead of peaking above a normal travel speed.

### Audio sync — modulation no longer runs ahead of the sound

The synth measures each block's energy as it *generates* the block, and that block is then played
one stream-latency later — so anything driven by the engine's own output reacted **before** you
heard it. Measured at blocksize 8192: 186ms of stream latency plus half a block, against ~80ms of
existing slew, for a net visual lead of ~0.2s.

- **New `audio sync` control** (Modulation → Depth & rate), on **auto** by default. Derives the
  hold-back from the live stream latency and blocksize rather than a guess, and recomputes it
  whenever the audio device or blocksize changes — including the fallback path, where the
  blocksize that opened may not be the one requested. Untick auto to trim by ear.
- `Value` gained an interpolating delay line. It runs on its own real-time clock, **not** the scene
  clock, so Freeze doesn't stall it — audio reactivity keeps working while motion is stopped, which
  is the point of Freeze. Interpolated rather than frame-snapped, so the correction doesn't
  reintroduce stepping. `delay=0` is bit-identical to the previous behaviour.
- **Only the synth path is corrected**, because it is the only one where we know the audio before
  it plays. `mic_level` measures sound already heard and can't be advanced, so it is never delayed;
  `audio_level` is compensated while it follows the synth and drops the correction the instant
  `audio_react` switches to the mic.
- Auto is 0 below ~4096 samples, where the existing smoothing already covers the lead. The UI
  distinguishes that from "audio is off" rather than showing an ambiguous zero.

### The side column, reordered

- **Scene leads the column.** It was at the bottom, below the modulation panel — so the two
  buttons that create a scene and every button that saves one were the last things in the longest
  column. Renamed from "Scene settings", with the four save buttons condensed to a
  **sound · camera · all** row plus Save as…
- **The header save trio (🎵/🎛/💾) is gone.** It was a pure duplicate — each button simply
  `.click()`ed its counterpart in the side column. Saving happens in one place now.
- **"Faders" → "Transitions"**, and it is no longer nested *inside* the group above it, which had
  been silently extending that group's heading over both the master fader and these durations.
  Nothing in the pane was a fader; they are three fade times.
- **Hue override promoted to the Master group.** It rewrites `Path.color` on every stroke, so it
  reaches the laser as well as the monitor — yet it was the most buried control in the app: three
  levels deep, last field of a collapsed pane.
- **Scene PPS moved to Settings › Output**, beside the `max PPS` ceiling it overrides, instead of
  sitting among three crossfade durations.
- "Global" section renamed **Master**, and Modulation's own first section renamed **Depth & rate** —
  the word appeared twice at two different scopes.

### Modulation is one panel now

- **Merged "2D Scene modulation" and "3D Scene modulation" into a single `Modulation` panel**
  with four sections: Global, Mappings, Shape scale (3D worlds only), Sources. The old titles
  implied a matched pair and weren't — the "2D" one was the universal matrix, where camera routes
  live on 3D scenes, so the naming sent you to the wrong pane. Supersedes 0.73.0 items 4 and 5.
- The Shape scale controls are now **hidden** on a 2D scene rather than captioned as inert.
- **Plain-English names in the mapping dropdowns**, with the underlying key on hover. Supplied by
  the engine so the browser carries no naming of its own. Two collisions fixed: `visual.glow` and
  `glow` both read as "glow" on 2D scenes and are now **pattern glow** / **screen glow**; `lfo_slow`
  was plain "LFO" sitting next to "lfo mid" and is now **LFO · slow**.
- Monitor destinations are grouped as **Monitor · screen only** — those filters never reach a laser.
- `audio ↔ visual` renamed **audio depth**, with a tooltip saying what it scales (all audio sources,
  not LFOs).

### Generation result moved to the modal

- **Removed the header cost flash.** The outcome now lands in the Generate modal, which stays open
  and switches to a **Generation complete** state: scene name, nodes placed vs authored, cost and
  token counts, elapsed time, and a **▶ Start scene** button that closes the modal and starts
  playback. A failed or fallback generation shows the reason and a Close button, so there is
  exactly one way out of the overlay.
- Audio regeneration reports its cost on its own modal's hint line for the same reason.

### Generation progress

- **The "Composing scene" overlay counts elapsed seconds** instead of claiming "10–45 seconds",
  which was wrong by about 3× — two measured Haiku runs at high effort took 112s and 114s. No
  per-size estimate is offered, because the timings were near-identical across very different
  node asks (the model writes ~200 nodes either way), so a formula would look precise and be
  fiction.

### Render loop

- **Node culling is now nearly free.** `_motion` was being evaluated for every node *before* the
  visibility test — 690 calls a frame to place ~40 nodes, ~20% of frame time, ~94% discarded. It
  now runs only for nodes that will be drawn, with the cull testing the resting position against a
  bounding sphere inflated to cover where motion could carry it. Verified **bit-identical** over 40
  frames and 29,745 points.
- `_motion` returns a shared identity rotation instead of allocating one per call, and `_emit`
  skips the identity matmul.
- Net: a 690-node scene went **43.9ms → 18.5ms**. The world-size ceiling roughly doubled, to
  ~3200 nodes at 45fps.
- **A scalar layer-param change no longer rebuilds the Scene.** `Scene.set_layer_param` applies it
  to the live scene; only non-schema keys still force a rebuild. Rebuilding discarded every
  generator's geometry cache on each pixel of a slider drag, and reset generator runtime state.

## [0.74.0]

### Scene size is a node count

- **Replaced the small/medium/large/massive dropdown with a logarithmic node slider**, 100–1200,
  in the Generate modal. Extent, def count, instances per def and camera waypoints all derive
  from the one number, so a bigger world can't disagree with itself about how big it is. Extent
  grows as sqrt(nodes) to hold density constant — a bigger world at the same spacing is mostly
  dark, which measured worst of all for a laser.
- **Live cost estimate under the slider**: node count, output tokens and USD on the selected
  model, updated as you drag and re-priced when the model changes. Served by
  `director.estimate()` rather than computed in the browser, so it is the same calculation the
  cost gate enforces.
- The legacy size strings still resolve. Scenes generated before this carry one in
  `generation_settings.size` and regenerate from it unchanged.

### Not paying for generations that get discarded

A response that overruns its token budget is truncated, thrown away, and billed in full. Measured
twice on Sonnet: 294s and 491s, a full budget each time, nothing usable returned.

- **Cost cap** (default $0.50) — estimated before the request is sent, and refused above the cap.
  The only guard that costs nothing to trip. Editable in the Generate modal; 0 disables.
- **Stream timeout** (default 240s) — aborts mid-stream and closes the connection, which stops
  generation. The browser's safety restore moved to 300s so the server-side stop fires first.
- **Cost is now recorded on failure.** `last_cost` was assigned after the truncation check, so the
  most expensive generations were the only ones reporting no cost at all. Aborted streams report
  a reconstructed figure flagged `estimated: true`.
- Token budget scales with the node count instead of a fixed 32,000 floor, capped at
  `MAX_OUTPUT_TOKENS` (64,000). `max_nodes_per_call()` reports the resulting ~757-node
  single-call ceiling and the UI warns above it — a warning, not a block, because models
  measurably undershoot the count they're asked for.

## [0.73.0]

### Output detail profiles, and a benchmark to size them with

A monitor and a laser want different densities from the same scene — the canvas
draws whatever it is handed, while the DAC spends most of its PPS budget on the
blanking jump between strokes. One render feeds both, so they now swap:
`camera.max_strokes` is the monitor value and an optional
`camera.laser_max_strokes` takes over while the beam is armed. Absent means one
density for both, which is every existing scene, unchanged.

- `camera` is a free-form dict in the spec, so this needs **no schema change**.
- The **max strokes** slider always edits the monitor value; editing the live
  figure would have made the control mean different things depending on whether
  the beam happened to be on. The panel shows what is actually being drawn, in
  amber when the laser profile is in force.
- Saving writes the monitor value — saving with the beam armed would otherwise
  persist the laser's reduced density as the scene's normal detail.
- Ceilings raised from 200 to 400 (slider and MIDI) so a denser monitor frame
  is reachable at all.

**New: `tools/bench_scene.py`** — the repo had no benchmark, so every perf
number in the docs was prose from a one-off measurement. It grows a real scene
in memory, sweeps `max_strokes`, and reports render time against the frame
budget using the same `LoopStats` accounting the live engine reports.

Building it turned up two measurement traps worth recording, both found by
measuring the same configuration twice and getting different answers:

- **A short sample under-reports.** An orbit/drift/path camera travels, so how
  much geometry is on screen depends where along its route it is. A one-second
  sample of `pottery` reports ~14ms; four seconds reports ~30ms, because the
  short window never reaches the dense part of the route.
- **The first run of a process is an outlier** — ~40% above the settled figure
  even after ten warmup frames. A full second of warmup removes it.
- **A single frame's stroke count means nothing** with a travelling camera. The
  tool reports the average across the sample; the last frame alone can read
  zero strokes on a scene costing 34ms.

Measured result: a drawn stroke costs ~0.12ms against ~0.014ms for a node, so
stroke count dominates and node count is nearly free — `World._render_budgeted`
doing its job. The ceiling is ~120–140 drawn strokes at 45fps; ~60% of a heavy
frame is in `Camera.project`/`_clip_and_project`, which is where to look if that
ever needs raising.

Also recorded, because it is the opposite of the intuitive answer: **engine fps
is not monitor fps.** The websocket broadcasts at ~20Hz and the canvas paints on
message arrival, so rendering faster than that only benefits the laser — raising
`--fps` toward a 60Hz refresh would tighten the frame budget for nothing.

### Generation cost

Every billed generation now reports what it cost, computed from the response's
own `usage` block at per-model rates. Cache tokens are priced at their
documented multipliers (writes 1.25x, reads 0.1x) even though this app's cache
is a local JSON file rather than prompt caching — so the figure stays right if
prompt caching is ever switched on.

- **In the header** while the scene lands, for ~20 seconds:
  `scene generated: $0.02 · 3.9k in / 4.1k out`, with the model and cache split
  on hover. Sub-cent results show 3dp — most Haiku scenes land there, and
  "$0.00" reads as free rather than cheap.
- **In the scene JSON**, under `generation_settings.cost`, so it survives
  restarts and shows up in **Show scene prompts** alongside the prompts that
  made the scene. Audio regeneration records `audio_cost` separately: folding
  it into the scene figure would misreport what composing the visuals cost.
- Three states are kept distinct, because they mean different things — a
  figure, "no API charge (cache or offline)", and "predates cost tracking".
  Collapsing the last two would report an unknown cost as free.
- Sonnet 5's introductory rate is honoured until it expires (2026-08-31) and
  then reverts automatically, rather than being hardcoded either way.

### Fixed

- **A `noise` voice now fades out on mute** instead of cutting. Muting skipped
  its render entirely — an instant hard cut, which on a wind/air bed reads as
  a click. It gets the same ADSR treatment as `pad`/`osc`/`sub` (verified:
  `jupiter`'s `space_whisper` now releases smoothly over its 5s release).
  `pluck` is deliberately excluded — each of its notes already carries a decay
  envelope, and gating the scheduler would fight it.
- **EQ narrowed to ±10dB** from ±24. The range lived in four places (DSP
  clamp, MIDI table, three UI knobs); all now agree.
- **`apply_audio_to_scene` now updates `spec.audio_prompt`** — the scene kept
  reporting the prompt it was originally generated with after a regenerate.
- **Save buttons in the header** — 🎵 sound / 🎛 midi / 💾 all, delegating to
  the existing panel buttons so both entry points behave identically. One
  button per slice, so saving the soundscape doesn't also overwrite camera
  settings you were part-way through adjusting.

### Not shipped

An **unsaved-changes indicator** (the sibling lightsaber project's pattern) was
built and reverted. The comparison worked — the three saveable slices read
clean when nothing was touched — but establishing the baseline across a scene
*load* did not: `library_name` updates the moment a load is enqueued, while the
crossfade keeps `scenes.current` on the outgoing scene, so early broadcasts
report the previous scene's data. Those are stable, wrongly signalling the load
has settled, and are then replaced — which reads as an edit. A
`scene_transition` guard plus a stability counter still left it firing after a
switch. Reinstating it wants a real "spec installed" signal from the engine
(a revision counter in `state()`), not more client-side heuristics. See
`future.md`.

## [0.72.0]

### Visuals react to the instrument's own sound

`audio_level` was wired to the **microphone**. On any machine not playing sound
into a mic — most of them — every audio→visual route in every generated scene
sat at zero and looked broken. An instrument that generates its own sound
should react to that sound.

- **`audio_level` now follows the engine's own output** by default, so every
  existing scene starts reacting with no edits and no mic. Measured on a
  library scene: 0.005 (mic, silent) → 0.20 tracking the soundscape.
- **The mic is still there, explicitly, as `mic_level`**, and a Settings
  toggle — *visuals react to* — switches which feed `audio_level` follows, for
  visualising an external source. Persisted in `settings.json`.
- **Three-band split of the output**: `synth_low` (<250Hz), `synth_mid`,
  `synth_high` (>2kHz). Computed block-wise via rfft where the mix is
  finalised — this module forbids per-sample IIR recursion, so a filter bank
  was out. Costs 1.7% of the audio callback budget.
- **Per-voice levels as sources** (`voice.<name>`), measured post-level,
  post-LFO, pre-pan. This is also what makes an **arpeggiator** usable as a
  modulation source with no special handling: each arp note is a spike in its
  voice's level, so routing it gives you the arp's rhythm for free. The source
  list tracks the loaded scene exactly: voices are keyed off the soundscape's
  own voice list (not off which ones happened to make a sound this block, or a
  sparse pluck between notes would blink in and out), and voices from other
  scenes are dropped — except any still referenced by a live route, which is
  what keeps a crossfade from breaking mid-fade.
- The audio↔visual slider scales all sound-driven sources together, not just
  the mic.

### The modulation matrix is editable

It was a list of depth sliders over whatever the director happened to emit —
you couldn't add a mapping, remove one, or change what drove what.

- Every mapping is now **[source] → [destination] [depth] [✕]**, with **Add
  mapping**. Sources are grouped into *engine* and *instruments*.
- **Destinations are derived from the layer schema**, the same registry that
  builds the layer panel — so a new generator param becomes routable the
  moment it exists, with no list to update. Camera destinations appear only on
  3D scenes.
- Each row carries a **live meter for its own source**, and a *modulation
  sources* readout lists every source's current value with flat ones greyed
  as idle. A route whose source never moves was previously indistinguishable
  from a broken route.
- Fixed: the depth slider's `max` was derived from its *current* depth, so
  dragging a route to zero collapsed its own range to 0–0.15 and left it
  stuck — a control that shrank when you turned it down.
- Fixed: a `<select>` asked for an option it doesn't have reports `""`, which
  was being stored as a route that could never fire. Now rejected server-side
  and reverted client-side.

### Freeze

A header button that eases **all** motion to a standstill over 2 seconds:
LFO phase, node motion and camera travel decelerate together and in
proportion, because the scene clock itself is ramped rather than each control
being stopped on its own schedule. The button shows the ramp counting down.

The matrix still receives real `dt` while `t` is frozen, so `audio_level`
keeps slewing — a stopped pattern still pulses with the sound, which is
rather the point of stopping it.

### Per-scene LFO rates

`SceneSpec.lfo` (`{"lfo_slow": 0.05, "lfo_mid": 0.2}`). LFO rate was global
engine state that no scene remembered: a scene routed from `lfo_slow` played
back at whatever rate the previous scene left behind, and the value reset on
restart. Scenes carrying no rate get the engine defaults restored, so one
scene's rate can no longer leak into the next, and the existing library is
unaffected. The state payload now carries the rates too — the slider never
used to follow a scene load.

### MIDI on 2D pattern params

`scale`/`rotate`/`spread`/`glow`/`max_strokes` are performance controls, so
they get learn icons. This needed more than the icon: `range_for()` returned
`None` for `layer<N>.<param>`, so a binding wouldn't have scaled at all. Added
`midi.DYNAMIC_RANGES`, refreshed per scene load from the registry's
`param_meta`, so a hardware knob and the on-screen slider cover the same range
by construction. Soft takeover can now read layer values, so a knob no longer
jumps the pattern on first touch.

### Fixed

- **`spread` did almost nothing.** It scaled node *placement*, but patterns
  are authored as centred nodes whose structure comes from repeat/symmetry —
  multiplying `[0,0]` by a number is a no-op. Scaling the repeat's own
  distance parameters was measured and was still nearly invisible (a 0.038
  band gap has nothing to give). It now displaces each piece from centre by
  its own centroid, at constant size, which is the actual difference from
  `scale`: mean radius across the slider now moves 0.28 → 0.63.
- **`scale` ceiling raised to 5** (spread to 3) for zooming into detail.
- **The Generate modal had no throbber.** `.spinner` was defined in the CSS
  and referenced nowhere; all you got was a 5px progress bar behind a modal
  dimmed to 50%, for the 10–45s a generation takes. Replaced with a proper
  overlay, and the same treatment applied to Regenerate audio.
- **Regenerate audio** now preselects the loaded scene, opens with an empty
  prompt, and on success closes and reloads the scene — the new patch is
  written to the scene *file* while the engine keeps playing the old one, so
  without the reload you never heard it. Only reloads when the regenerated
  scene is the loaded one.
- **Shape modulation is 3D-`world`-only and `scale`-only**; its hint claimed
  scale/rotate/position. Corrected, and a 2D scene now says plainly that the
  panel has no effect and points at the modulation matrix instead.
- Modulation panels retitled **3D Scene modulation** / **2D Scene
  modulation**, each collapsing when the other kind of scene loads — and only
  on an actual scene-kind change, so a panel opened deliberately doesn't slam
  shut on the next broadcast.

## [0.71.0]

### 2D pattern scenes (the headline of this release)

A scene is now explicitly **2D** or **3D**, derived from its generators rather
than stored — the same rule `Scene.is_3d` already followed, so the two can't
drift apart. A 2D scene has no camera at all: the pattern is composed directly
in the frame in normalized `[-1,1]` and never moves through space, which is
what makes it possible to design something edge-to-edge and have it land
exactly as authored. The scene library badges each entry, and the UI swaps the
camera panel for the layer panel accordingly.

- **New `pattern2d` generator** — flat symmetric line-art: mandalas,
  kaleidoscopes, neon lattices. Declarative `defs` + `nodes` like `world`,
  deliberately *not* another fixed-algorithm generator, because the
  declarative shape is the one that survived contact with the scene director
  (32 of 35 library scenes were `world`; two of the procedural three appeared
  in none at all).
- **`patterns2d.py` — the flat grammar**, in three separate layers:
  - ops: `line`, `polyline`, `circle`, `arc`, `rect`, `ngon`, `star`, `grid`
  - **space**, per motif: `cart` as authored, or `polar` where a point is
    `(radius, angle)` so a straight line becomes an arc or spiral
  - **combinators**, per node: `repeat` (offset / scale / radial / ring /
    grid) and `symmetry` (mirror x/y/xy, radial *n*)

  Keeping space local to a motif and combinators global to a node is what lets
  one pattern mix both idioms — Cartesian mirrored cross-arms alongside a
  concentric polar rosette. A single global "polar mode" flag could not
  express that.
- **Angles are in turns (0–1), not radians**, throughout the pattern grammar.
  Authored symmetry is nearly always a simple fraction of a circle, and `0.25`
  survives JSON and a language model's arithmetic far better than `1.5707963`.
- **Per-shape glow** — `Path.glow`, authored per node, carried to the canvas.
  Monitor-only: the laser's per-point intensity channel is on/off, so
  brightness there is carried by RGB. It rides in the payload only when
  non-zero, so every existing scene's frame is byte-identical to before.
  Applied identically in the preview and the output window.
- Pattern2D's top-level `scale`/`rotate`/`spread`/`glow` are flat scalars on
  purpose: `Scene._resolve` exposes every top-level param as `visual.<key>`,
  so all four are audio- and LFO-modulatable with no further wiring.
- A `max_strokes` ceiling bounds the combinatorics — a repeat crossed with a
  symmetry multiplies fast, and an unbounded pattern would blow the frame
  budget before anything else noticed.

### The director can author 2D scenes

- **A second system prompt, `_SYSTEM_2D`.** Separate from the 3D one rather
  than a branch inside it, because the two are contradictory: `_SYSTEM`
  requires "a full ENVIRONMENT to navigate inside, not a flat pattern", which
  is precisely what the 2D director must produce. They share
  `_SOUNDSCAPE_GUIDE`, extracted so the voice-ordering rules and LFO limits
  can't silently drift apart between them (verified byte-identical to the
  pre-extraction prompt, so 3D generation is unchanged).
- **Scene type picker** in the Generate modal, remembered across sessions.
  "Scene size" hides for 2D — object count is a 3D notion; a pattern's budget
  is its stroke count after repeat/symmetry expansion.
- `kind` is part of the generation cache key: one keyword has a legitimate 2D
  and 3D answer and they must not collide.
- The 2D prompt **requires** modulation routes — a static pattern is a poster,
  not an instrument — and is told to put spin on an LFO and brightness on
  `audio_level`, since rotation driven by audio jitters while brightness
  driven by audio is the whole point.
- Offline fallback gained a seeded 2D pattern, so the feature works with no
  API key.

### The in-page preview no longer truncates dense scenes

`Engine.preview()` emitted whole strokes until it passed `max_points` and then
stopped. Fine for 3D — `max_strokes` already holds those to ~90-130 short
strokes — but flat patterns are stroke-dense by nature, and a 263-stroke
pattern was arriving as the ~27 strokes that fit: a fragment of the
composition presented as though it were the whole thing, in the very window
you author against.

Now it drops **resolution, not strokes** — one uniform stride across the
frame, always keeping both endpoints so an open stroke can't shorten and a
closed one can't spring open. A coarser complete picture beats an exact
fraction of one. 3D payloads grow about 20%; every scene now previews whole.

### The generator registry is now self-describing

Two places were hardcoding what should have come from the registry, and
between them they had stranded most of the generator set:

- The director's prompt hardcoded `"generator":"world"`, so Claude could not
  select any other generator — the only `flow_field` scenes in the library came
  from the offline fallback.
- The web UI hardcoded three param keys (`layer0.speed/turbulence/hue`), which
  matched no generator's actual param list. `flow_field` was half-adjustable;
  `ripples` and `attractor` had nothing meaningful; `forest` and `ground_grid`
  had **no reachable controls at all**.

Generators now declare `description`, `param_meta` (explicit min/max/step) and
inherit `kind()` from `is_3d`; `catalog()` serves the lot.

- The UI builds its layer panel from that schema. Every generator is fully
  adjustable for the first time, multi-layer scenes get one section per layer,
  and `pattern2d` got its panel with no new UI code.
- `Generator.coerce()` casts incoming UI/MIDI values to the declared type —
  int params like `rings`/`segments`/`rails` previously arrived as floats and
  truncated, which made those sliders feel like they skipped.
- `layer<N>.<param>` addresses any layer; the old path was hardcoded to layer 0.
- `catalog()` and `Scene.layer_schemas()` are memoized — both ride the ~20Hz
  state broadcast, and the catalog is fixed once imports settle. Measured
  ~2.1 ms/s of pure rebuild before, ~0.006 ms/s after.

### Fixed

- Three generators each carried a byte-identical copy of the same hue ramp.
  Consolidated into `color.py` (verified identical across 1001 samples, so no
  saved scene shifts colour), which also adds the saturation- and
  value-preserving `hue_shift` the pattern colour ramps need.
- The canvas now writes `shadowBlur` only when the value actually changes —
  for a uniform-glow scene that is once per frame instead of once per stroke,
  fewer state changes than before rather than more.

## [0.30.0]

### Performance (the headline of this release)
- **Vectorized the 3D camera projection path** (`scene3d.py`) — was a
  per-point Python loop (visibility test, clip, project) for every stroke,
  every frame; profiled as the single largest render-loop cost for
  stroke-rich scenes. Rewrote the whole pipeline (visibility mask, run-
  finding, projection, and a new exact off-screen-stroke skip — scenes with
  more geometry than fits the camera's view at any moment were paying full
  clip cost for strokes that render nothing) as batched numpy plus plain
  Python for the genuinely small per-run work, where plain `min()`/`max()`
  benchmarked ~40% faster than numpy for arrays this size. Verified against
  the *unvectorized* version across 540 randomized trials (all camera
  modes × depth modes, varied geometry/near-far crossings/LOD) — 0
  mismatches — then measured: one representative scene went from **47ms to
  17ms per frame**; a stroke-dense scene from 26ms to ~17-24ms depending on
  its own `max_strokes` headroom.
- **`World` generator was rebuilding static geometry from scratch every
  frame** (`generators/world.py`) — every primitive node (planet, ring,
  ball, torus, crystal, starfield) was re-run through raw numpy/trig/RNG on
  every tick even though none of them (except the genuinely time-animated
  jellyfish) depend on time at all. Now cached per (primitive, params), like
  the def-based shapes already were. Forest trees had the same problem worse
  — full RNG-driven regeneration (trunk + branches) every frame just to
  apply a small sway offset; now the static layout is built once and only
  the sway translation runs per frame.
- **Audio DSP** (`audio/dsp.py`) pad/osc voice rendering was a Python
  triple-nested loop calling a tiny numpy op once per (chord note × detune
  layer × partial) — up to ~64 separate calls per voice per callback.
  Batched into one vectorized call per voice; verified numerically
  identical to the old loop across 200 randomized trials (max diff ~6e-7,
  pure float32 rounding).
- **Soundscape crossfades no longer double the audio DSP cost.** The old
  crossfade rendered the outgoing *and* incoming soundscape simultaneously
  for the whole fade — correct-sounding, but literally 2x the per-callback
  work, measured pushing a several-voice scene's callback over 300% of
  budget. Replaced with a sequential fade-out → swap → fade-in: only one
  soundscape is ever rendered at a time, and the swap lands exactly at the
  silent point between the two halves so there's no audible click. Verified
  the sequenced fade has no clipping and a clean crossover.
- Output with no laser attached was still doing the full DAC point-planning
  pass (arc-length resample + coordinate transform) every tick purely to
  feed the UI's point counter (~8ms on a mid-size scene). Now recomputed
  every 6th tick instead of every tick (~7Hz refresh on a text counter is
  plenty) — measured output cost drop from ~9ms to ~1.5ms average.
- The scene library listing (`SceneManager.names()`) did a fresh
  `os.listdir()` + sort on every ~20Hz state broadcast regardless of
  whether anything changed. Now cached, invalidated only on save/delete.
- **Fixed a real bug that could freeze the browser video preview whenever
  audio was struggling**, independent of the engine's own render loop:
  `synth.vu()` (the VU meter) was called from the websocket broadcaster
  thread and *blocked* waiting for the same lock the audio callback holds
  for its entire render — so a slow audio callback (which we'd just found
  several real causes of) froze every connected browser's preview for
  however long that render took. Made `vu()` non-blocking: it tries the
  lock and falls back to the last known reading on contention, since a
  meter reading one tick stale is imperceptible.
- Diagnostics themselves had overhead: `statistics.mean()` in the perf/audio
  summaries is ~100x slower than a plain `sum()/len()` for lists this size
  (benchmarked ~500us vs ~5us per call) for no precision benefit worth
  having — fixed, and it was on the same broadcaster thread already
  contending with the render/audio threads for the GIL.

### Diagnostics
- Added a render-loop performance monitor (`promptwaver/perf.py`) mirroring
  the existing audio `CallbackStats`: per-tick render/output timing, dropped-
  tick tracking, and — specifically — whether a drop happened *during a
  scene crossfade*, so "does it lag right when scenes fade" is something you
  can read off real numbers instead of guessing. New **Performance**
  accordion (sidebar) surfaces it; a lightweight always-on FPS counter (under
  the visualiser) works independent of the fuller instrumentation.
- Diagnostics (both the render-loop monitor above and the audio callback
  stats) are now **off by default** — a small but real cost (instrumentation
  timing calls, ~2.5% of a frame measured) most sessions don't need paying
  for. Toggle live in **Settings > Diagnostics**, or launch with `--diag`; no
  relaunch needed either way. When off, the Audio diagnostics/Performance
  blocks hide entirely rather than sitting open empty; the FPS counter keeps
  working regardless, since it's tracked separately and is effectively free.
- Audio diagnostics now also tags whether a slow/underrun callback happened
  during a soundscape crossfade, for the same "is it the fade" question on
  the audio side.

### New: monitor filters, disable scene plane, keystone, dual outputs
- Added **glow**, **trails**, and **mirror** (x/y, reflects one half over the
  centre line) as monitor-only canvas effects — screen/display only, never
  touch the vector data sent to the laser. These, plus **Disable scene
  plane**, are now **per-scene settings**: saved into the scene's own JSON
  via the existing "Save Camera settings" / "Save all scene settings"
  buttons, loaded back with whatever scene set them, off/0 by default for
  every scene that hasn't (including every scene that predates this).
- Added **Disable scene plane** (Camera controls): hides floor/ground/grid/
  plane-named geometry from a 3D scene, applied in `World.render3d` before
  the frame is even built — affects the laser and every display identically,
  not just a preview overlay. Matched by a small regex against the node's
  authored name (not its content), with a deliberate guard so "plane" doesn't
  also match "planet" (a very common primitive). Claude's scene-authoring
  prompt now asks it to name floor/backdrop shapes accordingly so future
  generations reliably work with this.
- Added a **second output window** (header: Output 1 / Output 2) — same live
  feed, independently flippable (whole-image reverse, distinct from the
  "mirror" effect above) and independently keystone-corrected, for driving
  two screens/projectors from one session.
- Added **live keystone correction** (Settings > Keystone): the laser's
  keystone (previously `--keystone-h/-v`, launch-only) is now adjustable
  while watching the beam, with the main visualiser mirroring it live so it
  can be tuned without the laser on. Output 1 and Output 2 each get their
  own independent keystone too (display-only, physical-screen concern, nothing
  sent anywhere). A **test pattern** toggle (border, diagonals, crosshair,
  inner box) overrides the live scene everywhere at once — laser, visualiser,
  both output windows — for calibrating against a known shape instead of
  whatever a scene happens to show. Verified the browser-side keystone
  formula is bit-for-bit identical to the DAC-side one for the same inputs.

### Safety / control
- Replaced the **LASER BLANK** button with an independent **Start/Stop
  Laser** toggle, off by default regardless of `--laser`: visuals, audio,
  and the browser preview all run normally while the physical beam stays
  dark until explicitly armed — useful while composing/previewing a scene
  before it's safe to send to the rig. NullOutput (no hardware) is
  unaffected either way, so the preview/point-counter stay accurate with
  nothing to protect.
- **Start** now fades audio in over 1 second instead of snapping straight to
  full level (a real pop/click before). **Stop** stays instant — the
  safety-critical direction must not lag behind the click.
- Fixed the audio blocksize/latency dropdown snapping back to the old value
  right after clicking Apply (or even before, on some browsers) — the
  broadcaster's next state update would overwrite the user's pick before the
  request had actually landed, since the activeElement-based guard used
  elsewhere doesn't reliably hold focus on a `<select>` after picking an
  option. Fixed with an explicit dirty/pending flag instead. Also fixed a
  related false negative: the server used to guess a fixed 300ms wait for a
  reconfigure to land, reporting "failed to (re)start" if a slower device
  reopen took longer than that even though it went on to succeed.

### UI
- The scene-transition indicator is now always visible (previously popped
  in/out of the layout on every scene switch, shoving the crossfade/audio-
  fade fields around) and sits directly above the crossfade field.
- The under-visualiser readout is now just the FPS counter.
- The vertical master fader is 35% shorter.

## [0.22.0]
- Added **per-voice "swell"** (`promptwaver/audio/dsp.py`) — a slow, continuous
  level modulation layered on top of each voice's own level, independent of
  the ADSR envelope (which only fires once on trigger/mute). Each voice gets
  its own random phase and period so voices swell in and out *independently*
  rather than in lockstep — the difference between "breathing" and
  "arranged". Off by default (`swell_amount=0`, byte-identical to before);
  new `swell`/`swell period` knobs in the Soundscape mixer for live control.
  Verified live on a real Claude-generated scene, real audio hardware: output
  peak measurably varied 3x (0.06-0.18) over a 30s window with swell active.
- Added **character sliders** (Generate modal): cold↔warm, calm↔energetic,
  static↔evolving, styled like the preference sliders on AI character
  generators. Warmth/energy are soft prompt hints appended to the Claude
  system prompt (same mechanism as effort/scene-size) — biasing voice-type
  mix, tone, attack time, and tempo; centered values (0.5) add no hint at
  all, so the default behaviour is unchanged. **Evolving is a guarantee, not
  a hint** — it sets `swell_amount` deterministically after the response
  comes back (`evolution * 0.6`), so orchestration happens regardless of
  whether Claude's own output would have included it. Verified with two real
  Claude generations at opposite extremes: warm/calm/evolving produced
  mostly pad/sub voices (one pluck accent), tone 0.3, 7-8s attacks, tempo 48,
  swell_amount exactly 0.54; cold/energetic/static produced two arpeggiated
  voices, tone 0.7, tempo 120, swell_amount exactly 0.0.
- Raised the ADSR attack ceiling from 10s to 15s so pad voices can genuinely
  take up to 15 seconds to build, matching the slower end of what the warmth
  slider now asks Claude for.
- Threaded warmth/energy/evolution through the "regenerate audio only" flow
  too, and included all three (plus scene size) in the generation cache key
  so different slider settings for the same keyword produce genuinely
  different, independently-cached results.

## [0.21.2]
- **Removed the "Envelope" section (Swell/Release buttons) — it never did
  anything.** Investigated rather than guessed: the matrix's `"env"` source
  was registered and triggered correctly, but no fallback scene, no shipped
  scene JSON, and no example in the Claude system prompt ever set
  `"source":"env"` on a modulation route, and there was no UI to author one —
  so its sampled value was computed and discarded every frame with zero
  observable effect, for every scene path. Removed the dead backend
  (`Engine.trigger()`/`release()`, the `env` matrix source, the `trigger`/
  `release` websocket messages) along with the UI. Confirmed **Modulation**
  (audio↔visual coupling, per-route depth) and **Scene**'s LFO rate *do* both
  have real, live effect (`audio_level` and `lfo_slow` are used throughout).
  Consolidated Scene's one remaining control (LFO rate — it drives the same
  `lfo_slow` source Modulation's routes act on) into the Modulation accordion
  rather than leaving it as its own near-empty section.
- Fixed Global-section sliders overflowing their column (crossfade/audio
  fade/hue-override values were getting pushed outside the sidecar): range
  and text inputs inside `.field` rows had no `min-width:0`, so flexbox
  respected their native intrinsic width instead of letting them shrink to
  fit a narrow container. Applied generally (not just to Global), so this
  can't recur in any other narrow column.

## [0.21.1]
- Global section: master fader and VU meter are now 25% shorter, with the
  Disable Audio/Visuals buttons moved to their right instead of below — more
  compact.
- Added a global **hue override** (Global section): a checkbox + hue slider
  that recolours every output stroke to one hue, keeping each point's own
  saturation/value so depth cueing and relative brightness still read.
  Pure-stdlib `colorsys` HSV round-trip on the final frame, independent of
  whatever the scene/generator itself chose. Verified visually on a real
  scene: turning it on repaints the whole wireframe a single consistent
  colour (tested red and blue), turning it off restores each object's
  original per-object colour exactly.
- Added a **scene transition indicator** (Global section, above the master
  fader): shows the outgoing/incoming scene names with a glowing gradient bar
  that fills proportionally to crossfade progress — so the crossfade slider's
  duration is now visible, not just felt. `SceneManager.transition_state()`
  exposes the in-flight crossfade's names + 0..1 progress; the indicator
  hides entirely when no transition is running. Verified live: the bar fills
  from 0 to 100% in step with a 6s crossfade and the indicator disappears the
  moment it completes.

## [0.21.0]
- Added a **Global** section (top of the sidecar column) with a vertical
  master fader and vertical VU meter, and independent **Disable Audio** /
  **Disable Visuals** buttons — each gracefully fades over 2s rather than
  cutting instantly, separate from Start/Stop (which stays instant, since
  that's the safety-critical gate). Required real DSP work, not just a UI
  toggle: `Soundscape.set_muted()` now ramps a gain multiplier over the fade
  window instead of snapping a boolean, and "Disable Visuals" dims every
  point's colour toward black each tick (the same per-point technique
  `SceneManager` already uses for a scene crossfade) while audio keeps
  playing normally. Verified live: the VU meter genuinely falls to 0 over the
  audio fade, and the visualiser genuinely dims to black and back over the
  visual fade, independently of each other. The Scene Transitions controls
  (crossfade, audio fade, scene PPS) moved here too, hint text dropped.
- Added a **Scene settings** section under Global: **Save Soundscape
  settings** / **Save Camera settings** / **Save all scene settings** (same
  underlying save, now with independent camera/soundscape flags so each
  button only touches its half of the saved file) and a **Save as…** button
  opening a small modal to save the current live config under a new name.
  Fixed a real, pre-existing bug found while building this: saving used
  `spec.name` (the scene's free-text internal name) as the library file key
  — for the 3 shipped scenes with a name/filename mismatch (see 0.19.0),
  this silently created a *new*, differently-named duplicate file instead of
  updating the one that's actually open, every time "Update scene from
  config" was clicked. Now saves under the tracked `library_name` instead,
  which also self-heals the mismatch (the file's internal name is corrected
  to match its filename on next save). Verified directly: loading
  `painters_studio` and saving no longer creates a `painters studio.json`
  duplicate.
- The bottom-row Scene column is now just the **Generate scene…** button —
  the inline save/update controls moved to the new Scene settings section.
- Header indicators reworked to "Connections — [●] Engine [●] Claude API":
  a new Engine dot tracks the websocket connection itself (previously only
  shown as text), alongside the existing Claude API dot.

## [0.20.0]
- Added a standalone **output window** (`promptwaver/web/static/output.html`,
  served at `/output`) — a chrome-less fullscreen canvas meant to be dragged
  onto a projector or second screen, opened/closed via a new header button.
  It's just another websocket client watching the same broadcast the control
  UI does, so it works identically with or without `--laser`: this is a
  software-only display path, useful for non-laser installations too (an
  ambient visual/data piece projected or shown on a second monitor, no
  hardware DAC required). Connects with `?hq=1`, which the server now honours
  per-connection — the output window gets a much less thinned preview
  (`max_points=6000, stroke_thin=300` vs. the control UI's `400/60`) since
  it's the actual thing being watched, not a status glance; built lazily so
  it costs nothing when no hq client is connected. Auto-reconnects if the
  websocket drops, and letterboxes the scene to whatever window size it's
  dragged/resized to rather than stretching. The laser DAC output path is
  completely unaffected — this is a purely additive second output, not a
  replacement.

## [0.19.0]
- Added a **scene size** option (small/medium/large, Generate modal) — tells
  Claude to spread objects and size the camera for a physically bigger world,
  independent of **effort** (which controls object *count/detail*, not
  distance). Defaults to small, matching prior scene sizing exactly (no hint
  is sent at all for "small" — it reproduces today's behaviour byte-for-byte).
- Generate scene modal now closes itself automatically once a generation
  completes.
- Fixed the ADSR envelope knobs (attack/decay/sustain/release) rendering
  cramped at a fixed 64px instead of full column width like every other
  slider in the mixer.
- Moved Camera/Motion out of the sidecar accordion and directly under the
  visualiser, and moved the crossfade, audio fade, and per-scene PPS controls
  there too (inline, not in an accordion) — grouping everything about *this
  scene's* transition/camera behaviour in one place next to what it affects.
- Trimmed the visualiser readout row to just scene name and point count —
  pps/output/audio/level/director were either redundant with the header's
  new connection dot or with Audio diagnostics.
- Highlighted the currently-loaded scene in the Library grid. Building this
  surfaced a real pre-existing content inconsistency: 3 of the 4 shipped
  example scenes have an internal `name` that doesn't match their filename
  (e.g. `forest_flythrough.json` is internally named "forest flythrough") —
  matching the grid against that free-text name would have silently failed
  to highlight them. Fixed properly rather than patching the content: the
  engine now tracks which library file was actually loaded as its own
  `library_name` state field, independent of the mutable scene name, and the
  UI matches against that instead.
- Bottom row is now 7fr/1fr (was 75%/25%) — Library gets most of the width,
  the Scene actions column is narrower.

## [0.18.0]
- **Fixed real overs during a soundscape crossfade** ("jerky/glitchy", peaking
  up to ~150% on a complex scene): the crossfade mixed two already
  master-scaled, tanh-limited full mixes with an equal-power (sin/cos) curve,
  whose weights sum to ~1.41 at the midpoint — fine for a single continuous
  signal, but for two independently-normalized full mixes with correlated
  peaks it can genuinely sum past 100%. Switched to a linear crossfade
  (weights always sum to exactly 1, so the blend of two bounded signals is
  provably bounded too) plus a safety-clamp on the combined signal, matching
  `Soundscape.render`'s own limiter. Verified on a deliberately complex
  6-voice scene: max peak during the fade dropped from the reported ~150%
  to 51% offline, and 32% across four live scene switches on the running
  server. Also checked whether rendering both scapes at once (double DSP
  cost) could itself cause glitching via a blown render budget — even at the
  smallest blocksize it stays under ~13% of budget, so that wasn't a factor.
  The VU meter also now reflects the true post-blend output during a fade
  (previously it only showed the incoming scape's own already-limited peak,
  which would never have shown the actual overs).
- **Layout**: pages now scroll naturally instead of clipping inside
  fixed-height per-panel scrollboxes — a long Library or several open
  sidecol accordions push the page down (confirmed the "Audio diagnostics"
  device/blocksize controls, previously unreachable, now scroll into view).
  The Soundscape globals row is now a fixed 5-column grid with full-width
  sliders instead of an unpredictable flex-wrap count. "Generate scene" moved
  out of the cramped bottom column into its own modal, opened via a
  **Generate scene…** button. Added a **Save Soundscape** button directly on
  the Soundscape panel (writes the live mix back to the loaded scene, same
  action as Update). The Claude connection indicator moved to the header as a
  red/green dot labelled "API connection"; a new **⚙ Settings** modal (button
  next to the title) now holds the API key/Connection controls and the
  Output (max/scene PPS) fields, out of the always-visible sidecar.

## [0.17.0]
- **Fixed a second, more common cause of "the soundscape doesn't change on a
  new scene"**: clicking a scene in the Library never reached `_install_spec`
  — it called a bespoke `scenes.set_scene(...)` directly, which only
  crossfades the *visuals*. The synth's soundscape, the modulation routes, and
  the PPS ceiling all silently stayed on whatever the previously-loaded scene
  had. `load_scene()` now routes through `_install_spec` like a fresh
  generation does, so all three actually update. Verified directly over the
  websocket: loading three different library scenes in a row now changes the
  reported soundscape every time (previously it wouldn't budge after the
  first scene of the session).
- Added a soundscape crossfade: switching scenes now equal-power fades the
  outgoing soundscape into the incoming one instead of hard-cutting, over a
  new **audio fade** slider (0-16s, Scene panel) independent of the visual
  crossfade duration. Implemented as `SoundscapeMixer` in `dsp.py` — renders
  both the outgoing and incoming `Soundscape` for the fade's duration and
  blends them, pure numpy, no per-sample loop. `fade=0` keeps the previous
  instant-swap behaviour.
- **Reworked the whole layout** to a compact, realtime-oriented arrangement
  tuned for a full-HD fullscreen browser: a fixed 720px top row (20% visualiser
  / 65% Soundscape / 15% Camera+Connection with Output, Audio diagnostics,
  Envelope, Modulation, and Scene collapsed into accordions), and a bottom row
  (75% Library, now a multi-column grid instead of one vertical list / 25%
  Generate scene, which now also carries the "save as" and "Update scene from
  config" controls).

## [0.16.0]
- **Fixed the real cause of "every scene gets the same soundscape"**: the
  `anthropic` package was never installed in the running environment, so the
  director silently fell back to the local keyword-based scene builder on
  *every* generation — and none of those fallback builders ever set a
  soundscape, so the identical hardcoded default got stamped onto every scene
  regardless of keyword. Confirmed directly: `scenes/generated/` (which only
  ever holds genuine Claude output) was completely empty, and the shipped
  library scenes carried byte-identical soundscapes. Also fixed a related
  diagnostic bug that made this hard to notice: the UI reported "no API key"
  for this failure even when a key was saved and the package was the actual
  problem — `SceneDirector` now tracks and surfaces the real reason.
- Added a 3-band EQ (low/mid/high, ±24dB) to the soundscape output stage — a
  per-block frequency-domain gain curve (rFFT → scale bins → irFFT), pure
  numpy like the rest of the DSP core, no new dependency. Live knobs in the
  Soundscape panel; saved into the scene JSON under `soundscape.eq`.
- Added a VU meter with a clipping indicator to the Soundscape panel,
  measuring the actual post-master, post-limiter output — moving the master
  fader (or muting) is directly visible on the meter.
- `requirements.txt` now documents actually-tested versions for the optional
  `sounddevice`/`anthropic` packages and clarifies `pyo` is no longer used
  (dropped for the pure-numpy synth after repeated GCC compile failures).
- Reorganized the main layout: Soundscape now sits beside the visualiser
  instead of below it; Library / Generate scene / Output form a row
  underneath; Output and Audio diagnostics are now collapsed accordions at
  the bottom of that row. Also fixed a layout bug hit while building this —
  the bottom row could get squeezed to near-zero visible height when the
  soundscape panel above it was tall (many voice cards), hiding the Library
  list entirely.
- Added a live Claude "connected" status dot to the Connection panel,
  reflecting the director's actual online state rather than only updating
  after clicking Test.

## [0.15.1]
- Fixed: an explicit blocksize request (e.g. clicking Apply) could silently
  fall back to a smaller size on failure *and persist it*, so the next
  session started from an already-degraded value — a one-way ratchet down
  with no way back up. Startup autodetect still falls back gracefully;
  explicit requests now try only the requested size and honestly report
  failure, restoring the last known-good config instead of substituting.
- Fixed: waveform/arpeggiator-mode `<select>` elements rendered absurdly
  tall — a global `flex:1` meant for horizontal control rows was also
  stretching selects inside column-flex containers.

## [0.15.0]
- **Found the real cause of audio glitching on complex scenes**: `PathPlanner`
  resampled every stroke with a pure per-point Python loop, holding the GIL
  and starving the realtime audio callback thread — confirmed by a user
  report that a *visual* setting (`max_strokes`) measurably affected *audio*
  glitching. Measured 431ms/frame (1941% over a 45fps budget) on a real
  scene. Rewrote fully vectorised (arc-length resampling via `np.interp`,
  one batched coordinate transform per frame instead of per stroke,
  zero-copy ctypes buffer). Result: ~20ms/frame, ~21x.

## [0.14.0]
- ADSR envelopes (attack/decay/sustain/release) for `pad`/`osc` voices,
  replacing a fixed 3s fade-in with no release — muting now fades out
  gracefully instead of cutting instantly. Reuses the existing `Envelope`
  state machine from the visual modulation matrix.
- Fixed a mixer UI bug (from 0.13.0): arpeggiator rate/decay knobs used a
  CSS selector that assumed a wrapper element one level higher than exists,
  so dragging them silently threw a JS error and never sent the change.

## [0.13.0]
- `osc` voice type: unison multi-oscillator with detune spread and an
  optional sub-octave layer, distinct from `pad`'s harmonic-partial warmth.
- Arpeggiator: any `pad`/`osc` voice can step through its chord (up / down /
  up-down / random) instead of sustaining it. Shares the same note-scheduling
  safety caps as `pluck` (see 0.12.0), so it can't reintroduce the same bug.
- Blocksize fallback ladder (first pass — refined in 0.15.1) and honest
  device/error reporting in the Audio Diagnostics panel.

## [0.12.0]
- **Fixed the original DSP-side glitch cause**: the `pluck` voice scheduler
  had no floor on onset spacing and no cap on active notes. A soundscape
  with a high tempo/rate could schedule notes faster than they decay,
  growing unboundedly (reproduced: 1248 active notes, 100ms+ renders against
  a ~93ms budget, within 30 seconds). Fixed with an onset-spacing floor, a
  hard cap on active notes, and centralised range-clamping of every
  soundscape parameter.

## [0.11.x]
- Master Start/Stop toggle and a Blank (immediate beam-off) button — nothing
  animates/plays/draws until Start is clicked; the scene clock freezes
  (not resets) while stopped.
- Fixed: the page was browser-cacheable, so an open tab could silently keep
  running a stale build across updates — `Cache-Control: no-store` added.
- Fixed a serious bug: `sd.default.device` could return a non-JSON-serialisable
  object, and the broadcaster had no per-tick error handling, so the first
  bad tick after a browser connected silently killed the state broadcast for
  the rest of the session (the UI would show "linked" but never update again).

## [0.10.x]
- Audio Diagnostics panel: realtime callback timing, hardware-reported xrun
  (underrun) counts, device enumeration, live device/blocksize/latency
  reconfiguration.
- Fixed a naked `while: pass` spin-wait in the Helios DAC output path
  (GIL-thrashing under load); 8192 made the default blocksize.

## [0.9.0]
- Vectorised the audio delay effect (was a per-sample Python loop in the
  realtime callback — a real glitch source) and tuned default blocksize/latency.
- Audio↔visual modulation mapping: a global "level effect" slider (scales
  all audio-driven visual coupling at once) plus per-route depth sliders.
- Regenerate just the soundscape for an existing scene without touching its visuals.

## [0.8.0]
- AI-generated ambient **soundscape** per scene (pure numpy + sounddevice —
  pyo would not compile on the target machine across two attempts, so the
  synth was built dependency-light by design) with a live mixer UI: master/
  tempo/distortion/delay, per-voice level/waveform/tone/pan/mute.

## [0.5.0]–[0.7.0]
- Model (Haiku/Sonnet/Opus) and effort (low/med/high) controls for scene
  generation; auto-save every generation to the library; "Update scene from
  config" to save live tweaks back into a scene; PPS (points/sec) as both a
  global hardware-ceiling setting (sent to Claude so scenes are authored
  within budget) and a per-scene override; fixed camera drift-mode jerkiness
  (was multiplying absolute time by live-modulated speed — now integrates a
  phase accumulator, matching how orbit mode already worked).

## [0.4.x]
- The core architectural shift: Claude **authors scene geometry directly** as
  a small shape grammar (line/polyline/circle/rect/box/arc/grid/lathe)
  rather than composing from a fixed bucket of primitives — genuinely
  unbounded scene vocabulary. Fixed the director silently falling back to
  local/cached content on truncated or malformed responses.

## [0.2.0]–[0.3.x]
- 3D scenes: a drifting/orbiting camera, near-plane + frame clipping,
  depth cueing (hue and/or hard cull, with a TTL-quantize option for
  on/off-only lasers), and a ready-made low-poly primitive kit (planet,
  ring, starfield, jellyfish, torus, crystal) for prompt-composed worlds.
- In-app Anthropic API key entry with a connection test.

## [0.1.0]
- Initial MVP: procedural flat-pattern generators (flow field, attractor,
  ripples), the modulation matrix (LFOs/envelopes/audio-reactive routing),
  Helios DAC + null output, the browser control surface, and a local
  keyword→scene fallback for offline use.
