# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

The venv is at `.venv` and is not activated automatically — invoke it explicitly:

```bash
.venv/bin/python run.py                                  # browser UI on :8080
.venv/bin/python run.py --web-port 8097 --no-audio       # usual dev invocation
.venv/bin/python run.py --laser --pps 11000 --invert-x   # with Helios hardware
.venv/bin/python run.py --list-midi                      # enumerate MIDI ports
```

**There is no `--web` flag**, despite README, CI release notes, and older transcripts using one. The web UI is on by default; `--headless` disables it. Passing `--web` makes argparse exit with an error.

Lint/format (config in `pyproject.toml`, line length 100, `E501` deliberately ignored):

```bash
.venv/bin/ruff check .
.venv/bin/black .
```

**There is no test suite** — no pytest, no test files, no CI lint or test gate. `.github/workflows/build.yml` only builds PyInstaller binaries on `v*` tags. Verify changes by running the app and looking at the preview; don't claim tests pass. **The `verify-in-app` skill has the working browser-automation recipe and this repo's traps** — read it before hand-rolling one.

Two things that bite regardless of how you verify:

- **The user is usually running their own instance on :8080.** Use a different port and scope any `pkill` to it — a bare `pkill -f run.py` kills their session. **A second port is NOT an isolated instance**: both processes share `settings.json` and the whole `scenes/` tree, so a test that saves a setting overwrites theirs, and the kiosk settings page's "Delete all" wipes `scenes/kiosk/` for both. Read a value before assuming a surprising one came from your own test.
- **`body.hide-values` collapses any `.val` element to zero width.** That's correct for slider readouts, and wrong for anything else — text you want read must not carry that class, or it renders blank with no error.

Local settings including the Anthropic API key live in `settings.json` at the repo root (gitignored, untracked). `scenes/*.json` **is** tracked — saving a scene from the UI dirties the working tree.

**Regenerating audio overwrites the target scene's file.** `apply_audio_to_scene` loads the spec, replaces `soundscape`, and saves — so testing that path against a real scene destroys its audio. Copy the file first, or use a throwaway scene.

## Bumping the version

The version is duplicated in **three** places that must move together. Nothing checks them, so a partial bump ships a UI reporting one version and a README claiming another:

| file | what to change |
|---|---|
| `promptwaver/__init__.py` | `__version__ = "x.y.z"` — the source of truth |
| `README.md` (line 3) | the shields.io badge URL, which embeds the number twice-over as `version-x.y.z-33e0d0` |
| `CHANGELOG.md` | a new `## [x.y.z]` section above the previous one |

Everything else derives and needs no edit: `engine.state()` reads `__version__` and the browser header renders it, `pyproject.toml` declares `dynamic = ["version"]` with no literal, and `.github/workflows/build.yml` takes the release name from `github.ref_name` (the git tag). Releases build from a `v*` tag, so a real release also needs `git tag vx.y.z`.

Verify with `grep -rn "x\.y\.z" --include="*.py" --include="*.md" . | grep -v .venv` and check the old number survives only in CHANGELOG history.

## Releasing, and how to tell whether it worked

**You cannot push.** The agent environment has no GitHub credentials — HTTPS has no helper and the SSH keys in the agent are rejected. Commit and tag locally, then hand Eddie `git push origin master && git push origin vx.y.z`. Don't burn a turn discovering this again.

**A tag push runs the workflow file from the TAGGED COMMIT, not from master.** So a fix to `.github/workflows/build.yml` does nothing for tags that already exist — it needs a new tag containing it. This is why the CI fixes landed as 0.78.3 and 0.78.4 rather than by re-running anything.

**"The build passed" is not "the release published."** The workflow has three build jobs and a separate `release` job, and the builds succeeded on *every single tag from v0.70.0 to v0.78.2* while the release job failed each time — so the repo had working binaries in workflow artifacts and no published release at all for months. A `workflow_dispatch` run on master is not evidence either: it skips the release job entirely (`if: startsWith(github.ref, 'refs/tags/')`), so it goes green no matter how broken releasing is.

Check the release, not the run:

```bash
curl -s "https://api.github.com/repos/edmonkey-nz/promptwaver/releases?per_page=3" \
  | python3 -c 'import sys,json; [print(r["tag_name"], len(r["assets"]), "assets") for r in json.load(sys.stdin)]'
```

Three assets, named `promptwaver-{linux-x86_64,macos-arm64,windows-x86_64.exe}`, is a good release. Two assets means the historic name collision is back (see the workflow comment); zero releases means the permission problem is back. `/actions/runs/<id>/jobs` names the failing job and step without needing auth — run *logs* need a token and 403 without one.

## Architecture

Read `TECHNICAL.md` for the subsystem-level detail. What follows is the load-bearing structure that isn't obvious from any single file.

### Everything is a `Path`

`geometry.Path` — an `(N,2)` polyline in normalized `[-1,1]`, one RGB colour, and an optional per-stroke `glow` — is the universal unit. A frame is `list[Path]`. Generators produce them, the laser planner and the browser canvas both consume them, so preview and hardware output are the same data by construction. `Path3D` is the world-space variant; a `Camera` projects it down to `Path`.

This is a **vector** instrument: strokes only, no fills, no pixels.

### Generators, and the derivation that decides everything

Two families behind one registry (`generators/base.py`, `@register` / `create`):

- `Generator.render(t, p) -> Frame` — 2D, drawn directly in normalized space
- `Generator3D.render3d(t, p) -> list[Path3D]` — world space, projected by the scene camera

`Scene.is_3d` is **derived** from whether any layer's generator is 3D, and that derivation decides whether a `Camera` is constructed at all. Flat scenes have `camera = None` and skip projection entirely. The library's 2D/3D badge derives from the same rule via `SceneManager.library()`. Don't add a parallel stored "kind" field — `is_3d` is the single source of truth.

Two generators carry most of the weight, and both are **declarative interpreters** over authored `defs` + `nodes` rather than fixed algorithms with knobs — that shape is the one that survived contact with the scene director:

- `world` (3D) — `defs` are shape-grammar geometry expanded by `shapes.py`; nodes place them. 32 of 35 pre-existing library scenes use it and nothing else.
- `pattern2d` (2D) — flat symmetric line-art via `patterns2d.py`. No camera, composed directly in the frame.

`flow_field`, `attractor`, and `ripples` are the original procedural generators. They were long stranded — the director could not select them and the UI exposed only three param keys — which is why `ripples` and `attractor` appear in zero saved scenes. The registry is now self-describing and the UI builds panels from it, so they are fully adjustable by hand again. They are still not *AI-selectable*, though: each director prompt names exactly one generator (`_SYSTEM` → `world`, `_SYSTEM_2D` → `pattern2d`), so a generated scene is always one of those two.

### Two director prompts, chosen explicitly

`_SYSTEM` (3D) and `_SYSTEM_2D` (flat patterns) are **separate prompts**, not a branch inside one, because they give directly contradictory instructions — the 3D one requires "a full ENVIRONMENT to navigate inside, not a flat pattern", which is exactly what the 2D one must produce. They share `_SOUNDSCAPE_GUIDE`, extracted so the voice-ordering and LFO limits can't drift between them.

`SYSTEM_PROMPTS` maps `"2d"`/`"3d"` to the right one. `kind` threads from the UI toggle through `generate_scene` → `generate` → `_from_claude`, and **is part of the cache key** — the same keyword has a legitimate 2D and 3D answer and they must not collide. It's recorded in `generation_settings` as what was *asked for*; the scene's actual kind still derives from its generators.

### `size` is a node count, but the old strings must keep working

The Generate modal's size control is a log slider over 100–1200 **nodes**, and `size` is now an int. The `small`/`medium`/`large`/`massive` strings are still accepted by `_resolve_size` and must stay that way: every scene generated before the slider carries one in `generation_settings.size`, and the panel regenerates from it. An int budgets `max_tokens` from `estimate_tokens(nodes)`; a string keeps the old fixed `SIZE_MIN_TOKENS` floor.

**A generation that overruns its token budget is billed in full and then discarded** — measured, twice, on Sonnet at ~$0.50 a time. Three guards exist and only the first is free: `cost_cap` refuses before sending, `timeout` aborts the stream (closing the connection stops generation), and `last_cost` is now assigned *before* the truncation/timeout checks so the most expensive failures stop being the ones that report no cost. Don't move that assignment back below them.

Cost estimates are served by `director.estimate()` over the websocket rather than computed in the browser, so the figure shown next to the slider and the figure the cost gate enforces are the same calculation. Don't add a price table to the JS.

### Big scenes are grown locally, not asked for

One call reliably writes ~200 nodes; the renderer handles ~3200. `director/expand.py` closes the gap by instancing the model's own `defs` along its own camera route — every added node is a copy of an authored one, repositioned by decomposing against the route into (lateral, height, forward) so a floor stays on the floor. Seeded from the request, so a cache hit and a fresh generation agree, and applied on **both** the cache-read and fresh-generation paths. Always reported as `{authored, total}` — never present a grown world as though the model wrote all of it.

### Two render-loop invariants that are easy to undo

**`_motion` must stay after the cull in `World._render_budgeted`.** Evaluating it per-node before the visibility test was ~20% of frame time on a big world and ~94% of that was discarded. Culling deliberately tests the node's *resting* position with the bounding sphere inflated by `amp` to cover where motion could carry it; that bound is conservative, and the reordering was verified bit-identical over 40 frames.

**A scalar layer-param change must not rebuild the Scene.** `Scene.set_layer_param` writes to the live params dict that `render` already resolves from. A rebuild throws away geometry caches and resets generator runtime state — which is what `world`'s accumulated shape clock (`_shape_time`) depends on. Rebuild only for keys absent from `schema()`.

### The registry is the source of truth for generator metadata

Generators declare `description` and `param_meta` (explicit ranges); `kind()` derives from `is_3d`; `catalog()` serves the lot. The UI builds its layer panel from that schema and addresses params as `layer<N>.<param>`. **Read the catalog rather than adding a third place that knows generator names** — hardcoding is exactly what stranded the generators above. `schema()` deliberately exposes only int/float/bool defaults, so `world`'s `defs`/`nodes` correctly yield no sliders.

### The modulation matrix, and its implicit destination surface

`modulation.py` is the spine: sources are sampled once per tick and routed to namespaced destination keys spanning both visual and audio domains.

The non-obvious part is in `Scene._resolve`: **every top-level generator param is automatically a modulation destination** as `visual.<key>`, because resolve pushes each key through `matrix.value(f"visual.{key}", ...)`. Adding a flat scalar param to a generator makes it live-modulatable with no other wiring. Conversely, burying a number inside a nested dict hides it from the matrix — keep modulatable values as top-level scalars.

**The audio sources read the engine's own output, not a microphone.** `audio_level` follows the synth by default (the mic is `mic_level`, selectable via the `audio_react` setting); `synth_low/mid/high` are a three-band rfft split computed in `dsp.Soundscape._update_bands`; `voice.<name>` is per-voice output level, registered dynamically as soundscapes load. Wiring `audio_level` to the mic is what made every generated scene's audio routes look broken on any machine without live input — don't reintroduce that default.

Routes are editable at runtime (add/remove/repoint) and the destination list is **derived from `Scene.layer_schemas()`**, so a new generator param becomes routable with no list to update. `Engine.mod_destinations()` is the one place that assembles it.

`shape_modulation` is a separate, narrower mechanism: `world`-only, `scale`-only, and it reads *synth voice params/LFOs* rather than the matrix. Its UI says so explicitly on 2D scenes rather than presenting controls that do nothing.

### Audio renders a block AHEAD of the callback, not inside it

`synth.py`'s PortAudio callback used to call `Soundscape.render()` directly, which put full numpy synthesis on the realtime thread, behind the GIL, competing with the 45fps render loop and the 20Hz broadcaster. That — not DSP cost — was the long-standing source of audio dropouts: the callback measured a 75ms average and 368ms max against a 186ms budget while the actual DSP work was 5–15ms.

A producer thread now renders `PRERENDER_BLOCKS` ahead into a queue and **the callback only copies** — no numpy, no allocation, no lock. Keep it that way; anything added to `_callback` runs on the realtime thread. The queue depth is deliberately the smallest that measures clean (1), because it is pure output latency, and `output_latency` must keep including it or audio-driven visuals will lead the sound.

The trade that move makes: the producer is a **plain Python thread**, so it lost the realtime scheduling priority the PortAudio callback had. On a scene whose frame cost fills the frame budget (`hot lava`: ~20–23ms against 22ms at 45fps) the render thread never yields and the producer starves — 34% of callbacks on that scene, at CPython's default 5ms GIL handover. `GIL_SWITCH_INTERVAL = 0.0005`, set in `SoundscapeSynth.start()`, is what fixes it, at ~3% of the frame rate on the worst scene. **Raising `PRERENDER_BLOCKS` is the wrong instinct and is measurably worse** (18.8% → 21.1% → 49.1% starved at depths 1/2/4): a deeper queue just means the producer runs flat out for longer, competing harder for the GIL. Depth helps jitter; this is an average-throughput problem.

**A starved callback is invisible to PortAudio.** It writes silence and returns in under a millisecond — on time, with data — so no underflow flag is set and `underruns`, `avg_duration_ms` and the budget percentages all stay green while the output gaps. That is how a badly dropping build shipped. `CallbackStats.starved`/`starved_pct` is the only counter that moves; keep it in any health judgement about the audio stream.

### Synth voices have a formula — read `INSTRUMENTS.md` before adding one

**`INSTRUMENTS.md` is the recipe for adding a voice type to `audio/dsp.py`**: the two rendering shapes and when each applies, the measured cost model (~0.6ms per note×partial row against a 186ms block budget, 120 rows the safe ceiling for one voice), the note-lifecycle guards and the assumptions a sustaining voice breaks, output-gain calibration, and the **five registration points** — `VOICE_TYPES`, the `render` dispatch, `_normalise`, `_SOUNDSCAPE_GUIDE`, and the `#voices` panel — none of which check each other, so missing one leaves the voice inaudible, unselectable, or invisible.

Two things from it that bite outside the audio module: `_normalise`'s output is what gets written to `scenes/<name>.json`, so defaulting a new field on every voice churns the whole tracked library on the next save — gate voice-specific fields on `v["type"]`. And **`set_param` bypasses `_normalise`**, so live UI/MIDI writes land in the spec unclamped and anything dangerous must also be clamped where it's read.

### The scene clock is an accumulator, not wall time

`Engine._scene_t` advances by `dt * motion_rate`, and `t` is what every time-driven thing reads. That's what makes **Freeze** work: ramping one number decelerates LFO phase, node motion and camera travel together. Note the deliberate asymmetry — `matrix.update()` gets **real** `dt` while `t` is frozen, so audio-driven sources keep slewing and a frozen pattern still reacts to sound. Don't "fix" that by passing the scaled dt to both.

### Threading: one render thread, one queue

`Engine` runs a single daemon render thread at `fps`. Every mutation from the web layer or MIDI goes through `_enqueue(fn)`; the loop drains the queue at the top of each tick. **Never mutate engine or scene state directly from a web handler** — that's the invariant the whole control surface depends on.

The websocket broadcasts state and a thinned preview frame at ~20Hz on the same process, so anything expensive in `state()` competes with rendering for the GIL. `SceneManager.library()` (which reads each scene once to derive its kind), `generators.catalog()`, and `Scene.layer_schemas()` are all cached for this reason. Anything else added to `state()` should be too — assume it runs 20 times a second forever.

### Loose keys on the params dict

Cross-cutting render inputs are passed as underscore-prefixed keys on the resolved params dict (`p["_camera"]`, `p["_disable_plane"]`) rather than by widening method signatures. Generators that don't look for them ignore them. Follow this convention rather than changing the generator interface.

### Laser vs display are deliberately different

Monitor filters — bloom, trails, mirror, kaleidoscope, keystone, line curve, line width — are **browser-only** and never touch the vector data sent to the DAC. They're implemented in `web/static/renderer.js` and stored per-scene in `spec.camera`.

**`line_width` only widens** (1..8, a multiplier on each surface's own base stroke) because the renderer's AA feather is a fixed half pixel: a stroke thinner than that never reaches full alpha at its centre, and every line goes semi-transparent with brighter dots at the joints. It is a multiplier rather than a pixel count so the two browser surfaces keep their deliberate difference — see `renderer.js _getLineWidth`.

**`line_curve` is bipolar** (-1..1) where every other filter is unipolar: 0 means "draw the polyline exactly as authored", positive resamples it through a spline, negative drops points. It is centred rather than based at 0 specifically so the default leaves saved geometry untouched — don't "normalise" it to 0..1.

`Path.glow` (per-stroke, authored by `pattern2d`) is monitor-only for the same reason: the DAC's per-point intensity channel is written as a constant 255, so brightness on a laser is carried by RGB, not blur.

**Both surfaces share one renderer.** `web/static/renderer.js` is loaded by `index.html` and `output.html` alike and owns all drawing; each page only supplies a `filters` object and a canvas. This replaced a hand-duplicated pair of paint functions, so a change to how strokes are drawn is now made once. What legitimately differs is what each page puts in `filters`: the in-page preview has no flip (it isn't a physical screen) and a fixed hairline line width, while the output window scales width with the viewport and carries per-monitor flip/keystone from localStorage.

### Output ratio and content aspect are two different numbers

`output_ratio` (Settings > Output) is the shape of the **surface** — a rig setting, stored in `settings.json`, reapplied on every scene load. `Engine.content_aspect()` is the shape of the **`[-1,1]` box the renderers receive**, and they are not the same:

- **3D**: the camera divides x by the viewport aspect (`scene3d._clip_and_project`), so its normalized box already *is* the viewport's shape. Content aspect = output aspect.
- **2D**: `pattern2d` composes in a square and never sees the ratio. Content aspect = 1.0, whatever the rig is.

Derived from `Scene.is_3d`, same as everything else that asks that question — don't store it. Every consumer (`renderer.js`'s letterbox, `ilda.PathPlanner`) takes the **content** aspect; assuming the box always filled the viewport is what stretched every flat pattern on a non-1:1 rig and squeezed it vertically on the beam.

`output_fit` (`fit` / `fill` / `stretch`) resolves the remaining mismatch — letterbox, pan-and-scan, or distort. **It is resolved in two different places, and that's the point:**

- **3D — at the camera** (`Camera._focal`). A wide ratio widens the horizontal field of view because `fov` is the vertical angle, so 16:9 shows ~78% more world sideways than 1:1. A `world` scene's geometry has bounded lateral extent, so that extra view lands on **empty space** — measured on `Circuitz`, lit pixels covered the full width at 1:1 but only 746/800 at 16:9. `fill` scales the focal length by `aspect`, which cancels the `/aspect` in `_clip_and_project` and restores the square framing, cropping vertically instead. Circuitz then covers 800/800.
- **2D — at the renderer's letterbox**, because a flat pattern has no camera to re-frame; `fit` there means real black bars.

So "the sides are empty on a 3D scene at 16:9" is **not** a letterboxing bug — an exact-16:9 output window computes zero bars. Don't go looking in `renderer.js` for it. Equally, `fill` cannot invent content: a scene that already fails to fill at 1:1 (`alien algebra`, 11.7% dead at 1:1) still has that gap afterwards.

Both browser surfaces read `content_aspect` + `output_fit` straight off engine state. **`index.html`'s `syncMonitorFilters` must keep setting them**: it originally didn't, and the preview silently fell back to a square box inside a canvas already reshaped to the ratio, so 3D scenes rendered horizontally squashed there and correct in the output window.

For laser output, `output/ilda.py` resamples each stroke to `max_step` spacing and inserts blanking and dwell points **between every stroke** (~8 fixed points per stroke transition). So the PPS budget is dominated by *stroke count*, not geometry complexity: at 28000 PPS and 45fps you have roughly 620 points per frame, and a full-width stroke alone costs ~67. Chaining strokes into continuous polylines is the highest-leverage optimisation for dense flat content.

The browser side is no longer the bottleneck it was: rendering is WebGL2 and a full-HD frame with bloom costs ~0.5ms, against a 50ms budget. **Assume the monitor's frame rate is limited by the ~20Hz broadcast, not by drawing** — if it reads low, profile `engine.preview()`/`state()` and the broadcaster before touching the renderer.

### Kiosk mode is a runtime toggle, and diverges from `generate_scene` on purpose

`promptwaver/kiosk.py` is the public-installation surface: one button on
`/kiosk`, a visitor speaks, faster-whisper transcribes **locally** (voice audio
never leaves the box; only the text goes to the API), and their world crossfades
in. It is a **toggle**, not a launch mode — `KioskSession` is constructed always
and costs nothing while off; `enable()` is what loads the model, arms the mic and
installs the attract scene. `--kiosk`/`--no-kiosk` only override the saved
`kiosk_enabled` at startup.

Four things here will look like bugs if you "fix" them:

- **`KioskSession._generate` must NOT stop the engine before installing.**
  `Engine.generate_scene` deliberately calls `_set_active_now(False)` first, so
  the dev UI's "Start scene" button sits on a scene that hasn't started — and
  `_install_spec` then sees `active == False` and uses a **zero** crossfade. The
  kiosk needs the opposite: staying active is the only reason the attract scene
  dissolves into the visitor's world instead of snapping. That is the whole
  reason kiosk has its own generate path rather than a flag on the existing one.
- **`enable()` sets `director.effort` in memory and `disable()` restores it.**
  `director.set_effort` persists to the shared `settings.json`; using it here
  would silently rewrite the operator's own config on the same checkout.
- **`AudioAnalysis._callback` is a PortAudio realtime callback.** Recording
  writes into a buffer preallocated by `arm()` — no append, no resize, no
  allocation. A list of blocks would put per-callback allocation on the realtime
  thread *and* make it unbounded. Overrunning the buffer just stops recording
  (that's the hold cap); it deliberately does not wrap.
- **The loopback gate can lock you out.** While armed, `server._handle` refuses
  every non-kiosk command from a non-loopback socket, because the websocket has
  no auth of any kind and an installation is usually on a venue network.
  `set_kiosk` is subject to that same gate, so *disarming must be done from the
  kiosk machine itself*.

Three more that were found by measuring, not by reading:

- **Whisper invents confident sentences out of silence.** 2.5s of an empty room
  transcribed as *"My turn. I'll tell you that. . . ."*, which passed a
  non-empty check and bought a real scene generation. In a public space that
  fires on every stray touch. Three guards now stand in front of it, in
  `kiosk.py`: a peak-level gate before the model runs at all (the cheap one
  that catches this case), faster-whisper's own `no_speech_prob` /
  `avg_logprob` per segment, and a word/letter floor on the cleaned text. Don't
  remove the level gate as redundant — it's the one that costs nothing.
- **The speech model is fetched over plain HTTPS, deliberately.**
  huggingface_hub's `hf_xet` backend was measured stalling *indefinitely at 0
  bytes* on the weights file while curl pulled the same URL at 16MB/s. Worse,
  this machine's IPv6 route advertises but doesn't carry traffic, and Python's
  HTTP stack takes the first address and blocks where curl races both families
  — so `_ensure_model` probes IPv6 once and hides AAAA records for the
  download if it's dead. A hang inside `enable()` looks exactly like a freeze.
- **The kiosk page coalesces frames; `output.html` does not.** Painting
  synchronously inside `ws.onmessage` means a backlog is replayed rather than
  skipped, and the overlay ends up behind the engine — measured at ~2s between
  the press and "Listening". The kiosk page updates the overlay immediately and
  defers drawing to a `requestAnimationFrame` that only ever draws the newest
  state (~0.5s after the fix). Worth copying to `output.html` if it ever reads
  laggy there.

**The kiosk deliberately does not read the director cache** (`use_cache=False`
in `_generate` — the only place in the app that opts out). The prompts likely to
collide are the short common ones, which are also the ones most likely to have
produced a weak scene; a poor result would then stick to that phrase for every
future visitor, silently and permanently. It still *writes* the entry, which is
harmless. The Generate panel keeps its cache, where re-running a prompt while
iterating saves real money.

**The camera PATH lives in the size hint, not the system prompt.** `"mode":"path"`
and the waypoint list are requested only by `_size_hint(nodes)` /
`SCENE_SIZE["massive"]` — and `_resolve_size("small")` returns `(None, None)`,
i.e. **no size directive at all**. So a string `size` silently produces
orbit/drift-only scenes while any int always asks for a closed route. That is
why the slider-driven Generate panel always gets a path and the kiosk does not.

**The size control and the route are separable, via `want_path`.** The kiosk
wants a node count (to scale the world) but NOT the camera path (which measured
58.4s / $0.040 against 27.2s / $0.018 on the same prompt). `_size_hint(nodes,
path=False)` is that variant, threaded through `_resolve_size` and
`generate(..., want_path=)` and folded into the cache key. It changes the
COMPOSITION instruction too, not just the camera block — asking for geometry
"along a route" and then not walking it leaves a corridor an orbiting camera
only sees the outside of — and explicitly forbids `waypoints`.

Note `Scene.camera_modes()` is derived from the SETTLED scene, so reading it
during a crossfade reports the OUTGOING scene's modes — that looks exactly like
the waypoints having been dropped. Check the saved file, or wait out the fade.

**Kiosk scene settings are two mechanisms behind one panel** (`/kiosk-settings`,
operator-only, stored as `kiosk_gen`). `DEFAULT_GEN` in `kiosk.py` is the closed
set — `set_gen` drops anything else, because these values go straight into
prompt text and scene params.

- **Prompt-side** — `interpretation`, `exclude_figures`, plus warmth/energy/
  evolution. `_style()` turns them into a directive appended LAST in the
  director's prompt, via `generate(..., style=)`. **It is part of the cache
  key**, for the same reason `kind` is: "a forest" asked for literally and asked
  for abstractly are two different scenes and must not collide. Only the ends of
  the interpretation slider say anything; the middle is silence, so neutral
  costs no tokens and biases nothing.
- **Post-generation** — `shape_speed` (the `world` generator's own param),
  `glow` + `glow_random`, `trail_chance`. `_apply_look()` writes these onto the
  returned spec, so they apply to **cache hits too** and re-roll per visitor.
  That's deliberate: two people saying the same words get the same world, and it
  should not look identical.

`prompt_suffix` is free operator text appended after both directives, capped at
`MAX_SUFFIX` (400) because it rides in every single request. `effort` and
`nodes` are the two richness dials; `enable()` and `_generate` both set
`director.effort` from the setting, and `disable()` restores whatever it was.

`kiosk-settings.html` mirrors `_style()`'s thresholds in JS to show the operator
the sentence a slider position actually produces — if you change the wording or
the 0.35/0.65 thresholds in `kiosk.py`, change `directionText()` too.

**`spec.layers` is `list[Layer|dict]`, and the director path always gives you
`Layer`.** `SceneSpec.from_dict` converts layer dicts into the dataclass
(`scenes.py:66`), so anything from the director or off disk holds `Layer`
objects while a hand-built spec may hold dicts. Calling `.get()` on a layer
therefore works in a test fixture and raises `AttributeError: 'Layer' object has
no attribute 'get'` on every real generation — which is exactly how it shipped
broken once. **Build test specs with `SceneSpec.from_dict`, never the
constructor**, or you are testing a shape the app never produces.

Related, same method, same day: a generated `world` layer's params contain only
`defs` and `nodes` — **`shape_speed` is a class DEFAULT, not something the model
writes**. Any "only overwrite the key if it's already there" guard silently does
nothing on real scenes. `_apply_look` asks the registry which generators declare
the param instead of testing for its presence or hardcoding `"world"`.

**The archive panel is answered on demand, never in `state()`.** Listing
`scenes/kiosk/` reads every file to pull each scene's `image_prompt`, so it
rides its own `kiosk_scenes` websocket command rather than the 20Hz broadcast.
A file that won't parse still lists (under its filename) so it can be deleted,
and deletion goes through `SceneManager.delete`, whose `path_for()` strips names
to alnum/space/_/- — a crafted name can't escape the archive directory.

**The visitor loop closes itself.** After a world starts playing the screen is
left clean for `PLAY_HINT_AFTER` (25s) — that is the visitor's moment — then a
small bottom pill fades in so the *next* person can see the installation is
theirs to use, and it triggers a new session immediately rather than making
them wait. With nobody pressing, `tick()` returns to the attract loop after
`PLAY_TIMEOUT` (300s). `press()` accepts `IDLE`, `PLAYING` and `ERROR`, so the
pill is a real trigger, not decoration; every other phase is refused, and that
refusal is the only thing serialising visitors — `SceneDirector` has no
concurrency guard of its own.

Generations are archived to `scenes/kiosk/` (gitignored), never the tracked
`scenes/` library, so an unattended run doesn't churn the working tree. The page
(`web/static/kiosk.html`) is modelled on `output.html`, not `index.html`: it
shares `renderer.js`, holds no phase of its own, and renders entirely from
`state.kiosk` in the 20Hz broadcast — which is what lets a browser refreshed
mid-generation land back in the right place.

### Scene JSON round-trip

`SceneSpec` is a dataclass serialised straight to `scenes/<name>.json`. `from_dict` reads every field with `.get(key, default)`, so adding a field is backward-compatible with the existing library — old scenes load with the default. Keep that property.

Anything that behaves like a scene setting belongs in the spec, and anything left as engine state silently leaks between scenes. `spec.lfo` exists because LFO rate was global: a scene routed from `lfo_slow` played back at whatever the previous scene left behind. Note the pattern in `Engine._apply_lfo` — it **restores defaults** for absent keys rather than only applying present ones, which is the half that actually stops the leak.

**Regenerating audio writes to the scene file**, while the running engine keeps playing the old soundscape — the UI reloads the scene afterwards so the change is audible. Worth knowing before you test that path against a scene you care about.

The director (`director/claude_director.py`) makes one cached Claude call per scene and returns JSON; `director/fallback.py` is a deterministic keyword→scene mapping used when no API key is set, so the app stays fully usable offline (including a seeded 2D pattern via `_pattern_2d`). Each system prompt carries the entire schema for its scene kind plus the shared soundscape guidance — changes to the scene format usually need a matching edit there, **in both prompts**, or Claude will keep emitting the old shape.

### Known drift

`CHANGELOG.md` jumps from `0.30.0` to `0.71.0` — the intervening releases were never logged. Don't try to reconstruct them.

`scenes/painters_studio.json` is a single `flow_field` layer despite its name and despite the docs using "inside a painter's studio" as the shape-grammar example (now noted in `TECHNICAL.md`). Treat saved scenes, not the docs, as ground truth for what the format currently produces.
