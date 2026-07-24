# Changelog

All notable changes to PromptWaver are logged here. This project is **pre-1.0
and under active development** — expect breaking changes to scene JSON shape
and APIs between minor versions until a 1.0 release.

## [Unreleased]
- Helios DAC SDK build/install instructions (`libHeliosDacAPI.so` + udev rules)
- Project scaffolding for VSCode / GitHub (this changelog, `.vscode/`, `LICENSE`, `pyproject.toml`)

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
