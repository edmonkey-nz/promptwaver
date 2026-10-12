# GPU renderer build log

Branch `gpu-renderer`. Plan: ~/.claude/plans/shimmying-whistling-wand.md

## Phase 0 — baseline (master @ 5daf8a7, 2026-10-13)

Method: instance on :8098, scenes loaded while paused, soundscape master
zeroed in memory before Start (full DSP runs, silent). No settings writes —
blocksize sweep via `synth.reconfigure()` in-process (`Engine.configure_audio`
persists to settings.json, so it was NOT used). settings.json diffed
before/after: unchanged.

### Projector feed (hq websocket, audio on, blocksize 8192)

| scene | feed msgs/s | kB/msg | engine fps (idle 24) | Python render ms (avg/max) |
|---|---|---|---|---|
| hot lava | 19.6 | 53.3 | 23.7 | 9.3 / 50.3 |
| eniac | 19.4 | 111.0 | 23.2 | 17.8 / 72.1 |
| Mandala | 19.7 | 74.8 | 23.3 | 8.4 / 65.8 |
| crop circles | 19.7 | 51.8 | 23.7 | 7.9 / 56.2 |

`scene_spec` alone: 5–13 ms per broadcast tick to build + serialise
(eniac 13.1 ms, 84 kB) — 100–260 ms/s of GIL at 20Hz.

### Audio vs blocksize (in-process, 20 s per cell, + emulated output client)

| blocksize | latency ms | hot lava starved/underruns | eniac starved/underruns |
|---|---|---|---|
| 8192 | 372 | 0/108, 0 | 0/107, 0 |
| 4096 | 186 | 0/216, 0 | 0/215, 0 |
| 2048 | 93 | 0/431, 0 | 1/430 (0.23%), 0 |
| 1024 | 46 | 3/859 (0.35%), 2 | 29/849 (3.42%), 8 |

### Gesture -> projector feed (set glow -> first hq message carrying it)

eniac, no audio: median 52.7 ms, p90 91.8, max 93.2 (40 samples). Excludes
browser paint (~1 ms) and display.

### Browser GPU availability (headless Chrome via Playwright)

- default: SwiftShader (software)
- `--enable-gpu --use-angle=gl`: AMD Radeon (renoir iGPU)
- same + PRIME env (`__NV_PRIME_RENDER_OFFLOAD=1 __GLX_VENDOR_LIBRARY_NAME=nvidia`): **GTX 1650**

A plain launch lands on the iGPU — relevant to the packaged build.

## Phase 0 — slim scene_spec (commit fd31e93)

state()'s `scene_spec` is now def names + prompts + generation settings.

| scene | kB/msg before → after | Python render ms avg (max) before → after |
|---|---|---|
| hot lava | 53.3 → 21.0 | 9.3 (50.3) → 8.3 (14.1) |
| eniac | 111.0 → 27.1 | 17.8 (72.1) → 12.4 (18.5) |
| Mandala | 74.8 → 70.4 | 8.4 (65.8) → 8.3 (79.6) |
| crop circles | 51.8 → 39.9 | 7.9 (56.2) → 7.7 (16.9) |

UI check (Playwright): shape-mod def names (9), prompts panel text and
generation settings all read correctly from the slim view; no page errors.

## Phase 1 — laser + legacy generators removed (7ff9429, e26e0cc)

- All 25 library scenes bit-identical to master over 40 frames (272,746 arrays).
- Every offline-director keyword group builds and renders (forest now a `world`
  path scene; flow/cloud/smoke/rain keywords -> seeded pattern2d).
- Startup keyword "water flowing" was flow_field; it is now a seeded pattern2d
  (the launch screen before any scene is loaded looks different — unavoidable).
- Director cache key is request params only (not prompt text), so the prompt
  rewording orphaned NO cached scenes.
- index.html laser sweep: Sonnet subagent, reviewed diff, page loads clean.

## Phase 2 — tick/draw split + snapshots (13fba67)

- CPU render still bit-identical (single scenes AND crossfades).
- GPU client: geometry binary once (eniac 339 kB, packed in 15 ms), then a
  ~530-byte JSON frame per tick; late joiner gets geometry first.

## DESIGN DEVIATION (decided during Phase 2/3)

Plan said: project in the vertex shader (GLSL twin of `_motion` + camera).
Built instead: **JS scene evaluator** (`scene-eval.js`) — geometry resident in
the browser, motion + max_strokes culling + projection + clipping in JS each
rAF frame, feeding the existing WebGL2 stroke renderer.

Why: (1) 6 of 25 library scenes use `line_curve`, which re-splines/decimates
the PROJECTED polyline and needs neighbouring points — awkward per-segment in
a vertex shader; (2) `max_strokes` culling (Decision 1) already has to
transform every candidate point in JS to get z-ranges, so a GLSL version would
duplicate that work; (3) one twin (JS) instead of two (JS + GLSL), and it can
be tested exactly in node against Python's own frames, no browser needed.
Same outcome the plan was for: Python does no geometry, display runs at rAF
rate, geometry uploaded once. Moving the transform into the shader stays
available as a later optimisation if a scene ever needs it.

## Phase 3a — JS scene evaluator twin test

`scene-eval.js` run in node against Python's own frames (Scene.tick + draw),
same snapshots, same packed geometry. Compared as a stroke MULTISET (draw order
is invisible under additive blending; near-tied distances legitimately sort
differently between float32/float64 — the first ordered run "failed" 11 cases
on exactly that, verified to be swapped pairs, not different strokes):

- 25/25 library scenes, 20 frames each: PASS, same stroke set, max point err
  <= 3.0e-6, colour <= 3e-8.
- Variants all PASS: disable-plane (eniac, train station), hue override +
  visual fade, depth-cue hue/both + ttl_quantize, pattern2d with modulated
  scale/rotate/spread/glow/max_strokes, shape_modulation scales, fly camera
  (hot lava, rocks; max err 1.9e-5), whole-world eniac (max_strokes 5000,
  2962 strokes over 6 frames), two crossfades through SceneManager, test pattern.

## Phase 3b — browser GPU path live (e922c47)

Output page, headless Chrome on the real GPU (PRIME env + --use-angle=gl):

| GPU | canvas | eniac | Mandala | hot lava | crop circles |
|---|---|---|---|---|---|
| GTX 1650 | 1920x1080 | 60.8 fps, 0.41 ms | 61.4, 2.19 ms | 59.7, 0.37 ms | 59.5, 0.64 ms |
| GTX 1650 | 3840x2160 | 59.9, 0.37 ms | 60.0, 2.05 ms | — | 60.6, 0.61 ms |
| AMD iGPU | 1920x1080 | 59.9, 0.43 ms | 59.7, 2.62 ms | — | — |
| AMD iGPU | 3840x2160 | **42.4**, 0.42 ms | **38.5**, 2.36 ms | — | 61.3, 0.73 ms |

(ms = JS evaluate + draw submit per frame.) Interpolation delay ~18 ms with
one output; ~30 ms with preview + 2 outputs + kiosk + a CPU client open.
iGPU at 4K is fill-bound: 5 full-screen float passes run every frame
(review 3.7) — the lever if the show machine ever lands on an iGPU.

All pages (index preview, output, output?renderer=cpu, kiosk) render with
no page errors; GPU vs CPU-reference output lit-pixel counts 405,065 vs
406,422 (CPU preview thins strokes).

Decision 1 narrowed: max-strokes SLIDER ceiling 400 -> 1000 only. MIDI CC 10
and the modulation range stay 20-400 so existing knob mappings and range
routes behave exactly as before (a 20-5000 linear knob would change them).

## Phase 4 — in-browser verification (4305798)

**Parity** (GPU output vs `?renderer=cpu&full=1` unthinned CPU reference,
frozen, 960x540, GTX 1650): lit-pixel IoU 0.927-0.999 on 23/25 scenes
(3D thin hairlines lose a few % to half-pixel AA differences). `coffee`
0.64 and `honeycomb` 0.87 are mic-driven (mic_level -> visual.scale/spread):
the GPU output compared with ITSELF 0.8 s apart scores the same (0.62 /
0.82) while `rose` scores 1.000 — the room's sound moves them, not the
renderer.

**Features** — 24/24 PASS: test pattern on/off, hue override, disable/
enable visuals fade, freeze (shape clock + camera hold), disable plane
(train station), max strokes 120->600 (strokes 120->493), each monitor
filter (glow, trail, kaleidoscope, mirror, line width, line curve +/-,
bloom intensity), layer param edit -> uniforms without geometry rebuild,
LFO -> visual.rotate route, 2D->3D crossfade draws both then settles,
resize, WebGL context loss + restore, record/replay a take (deleted after).

**Bug found and fixed:** WebGL context restore drew nothing — extensions
(EXT_color_buffer_float) and GL state were never re-initialised, so every
RGBA16F target was incomplete. Pre-existing (my audit had marked context
loss "Matches" from reading the handler; forcing it showed otherwise).

Not re-tested in-browser: fit/fill/stretch and output ratio — changing them
writes settings.json; the letterbox code they drive is unchanged.

## Phase 4b — latency (audio + visual)

**Audio vs blocksize AFTER** (in-process, silent, emulated GPU output client;
engine tick now ~0.57 ms vs 8-18 ms render before):

| blocksize | to-ear latency | hot lava | eniac | LionsMane | producer duty |
|---|---|---|---|---|---|
| 8192 | 372 ms | 0 starved | 0 | 0 | 6-8% |
| 4096 | 186 ms | 0 | 0 | 0 | 6-7% |
| 2048 | 93 ms | 0 | 0 | 0 | 7-8% |
| 1024 | 46 ms | 0 / 2584 (60 s) | 0 / 2583 | 0 / 2584 | 10-12% |
| 512 | 23 ms | 0 / 5168 (60 s) | 0 / 5168 | 0 / 5168 | 16-19% |

BEFORE (Phase 0): 1024 starved 0.35% (hot lava) / 3.42% + 8 underruns
(eniac); 2048 starved on eniac.

**Scene-switch stress** (23 scene loads with 2 s crossfades + geometry
packing over 90 s, silent via the mute gate): 512 -> 3 starved + 3
underruns; 1024 -> 0; 2048 -> 0. One prerendered block at 512 is only
11.6 ms of slack, and packing eniac's geometry is ~15 ms on the render
thread.

**Chosen default: 1024** (synth.py, engine fallback, UI dropdown). Saved
`audio_blocksize` still wins: Eddie's settings.json has 8192 — set
Settings > Audio > blocksize 1024 to get it. settings.json verified
unchanged after every sweep.

**Gesture -> screen** (set glow from an output page; GPU path, GTX 1650):

| | before | after |
|---|---|---|
| gesture -> reaches the output (median / p90) | 52.7 / 91.8 ms | 12.7 / 16.8 ms |
| gesture -> first drawn frame showing it | (feed + paint) | 20.4 / 29.5 ms |
| gesture -> fully settled on screen | — | 37.5 / 47.5 ms |

**Audio-sync compensation** with an output window open: mod_delay_auto 384 ->
350 ms at blocksize 8192 (subtracts the window's 17.3 ms delay + one 16.7 ms
refresh). At 1024 the audio lead itself is 46 + 12 = ~58 ms, so the hold-back
becomes ~58 - 80 slew - 34 -> clamped 0: visuals react essentially live.

## Phase 5 — docs, release prep, executable (6ffa955, 23dbd70)

- Docs: CLAUDE.md (GPU output path section; laser/legacy sections rewritten),
  TECHNICAL.md / README / PACKAGING / about.md / launch.json / build.yml notes
  (Sonnet subagent, diff reviewed). verify-in-app skill: real-GPU recipe.
- Version 0.90.0 in __init__.py, README badge, CHANGELOG. Not tagged.
- **Local PyInstaller one-file build** (same flags as build.yml): builds, 165 MB;
  run on :8099 it serves scene-eval.js / frame-clock.js / gpu-view.js /
  renderer.js and draws 60.0 fps on eniac and Mandala on the GTX 1650, no page
  errors.
- End state: settings.json byte-identical to session start; recordings/ empty
  as it was; no scene files touched; comparison worktree removed; branch
  gpu-renderer, 8 commits on master, 45 files, +2742/-1762.
