# Changelog

All notable changes to LaserFlow are logged here. This project is **pre-1.0
and under active development** — expect breaking changes to scene JSON shape
and APIs between minor versions until a 1.0 release.

## [Unreleased]
- Helios DAC SDK build/install instructions (`libHeliosDacAPI.so` + udev rules)
- Project scaffolding for VSCode / GitHub (this changelog, `.vscode/`, `LICENSE`, `pyproject.toml`)

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
