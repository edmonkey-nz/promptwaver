# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

The venv is at `.venv` and is not activated automatically — invoke it explicitly:

```bash
.venv/bin/python run.py                                  # browser UI on :8080
.venv/bin/python run.py --web-port 8097 --no-audio       # usual dev invocation
.venv/bin/python run.py --list-midi                      # enumerate MIDI ports
```

**There is no `--web` flag**, despite README, CI release notes, and older transcripts using one. The web UI is on by default; `--headless` disables it. Passing `--web` makes argparse exit with an error.

Lint/format (config in `pyproject.toml`, line length 100, `E501` deliberately ignored):

```bash
.venv/bin/ruff check .
.venv/bin/black .
```

**There is no test suite** — no pytest, no CI lint or test gate. `.github/workflows/build.yml` only builds PyInstaller binaries on `v*` tags. Verify changes by running the app and looking at the preview; don't claim tests pass. **The `verify-in-app` skill has the working browser-automation recipe and this repo's traps** — read it before hand-rolling one.

The one exception is **`tools/gpu_twin/`**, the twin test for the browser's scene evaluator (see "The GPU output path" below). Run it after ANY change to `web/static/scene-eval.js` or to the Python it mirrors — it compares the two numerically on every library scene, no browser needed:

```bash
.venv/bin/python tools/gpu_twin/twin_gen.py /tmp/pw_twin 20
node tools/gpu_twin/twin_test.js /tmp/pw_twin promptwaver/web/static/scene-eval.js   # ALL PASS
```

Two things that bite regardless of how you verify:

- **The user is usually running their own instance on :8080.** Use a different port and scope any `pkill` to it — a bare `pkill -f run.py` kills their session. Write it `pkill -f "[r]un.py --web-port 8098"`, and **never in the same command as the restart**: `pkill -f` matches its own shell's command line, so a pattern that appears anywhere in that line (the restart included) kills the shell running it, exit 144. **A second port is NOT an isolated instance**: both processes share `settings.json` and the whole `scenes/` tree, so a test that saves a setting overwrites theirs, and the kiosk settings page's "Delete all" wipes `scenes/kiosk/` for both. Read a value before assuming a surprising one came from your own test.
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

## The packaged build has TWO roots, and using the wrong one is silent data loss

> **`PACKAGING.md` is the portable version of this section** — the whole
> "ship a Python server app as a double-clickable binary" recipe, written to be
> copied to the other server-shaped tools rather than re-derived. Read that for
> the pattern; what follows is what it means *here*.


`promptwaver/paths.py` is the only thing that knows the difference:

- `bundle_dir()` — `sys._MEIPASS`, PyInstaller's unpack directory. Read-only,
  and **deleted when the process exits**. The web UI, `about.md`/`welcome.md`
  and the starter scene library ship here.
- `data_dir()` — beside the executable, the only place that survives a restart.
  `settings.json`, the scene library and the director cache go here, falling
  back to `~/.promptwaver` when the app sits somewhere unwritable.

Both resolve to the repo root from a checkout, so none of this is visible in
development — which is exactly why it shipped broken twice over:

1. **The build bundled no data at all.** No `--add-data`, so `_STATIC` did not
   exist and aiohttp's `add_static` raised `ValueError: does not exist` inside
   `make_app()`. The binary died before printing anything and the console
   closed too fast to read. Verified with a local one-file build: with the data
   bundled all four routes serve and 52 scenes seed beside the exe on first run.
2. **Writable paths pointed into the bundle**, so every saved scene and the API
   key were discarded on exit.

**`--add-data` uses `:` on Linux/macOS and `;` on Windows**, which is why
`build.yml` has two build steps rather than one. Relative source paths resolve
against `--specpath` (default: the working directory), so they work in CI but
need absolute paths if you pass an explicit `--specpath`.

**A packaged build shows a desktop window; a checkout does not.** `run.py`
defaults `--gui` to `sys.frozen`, because a checkout already has a terminal and
a double-clicked binary has nothing at all — no window, no way to quit short of
Task Manager or `pkill`, and a console that closes before an error is readable.
`promptwaver/gui.py` is four labels and two buttons in **tkinter**, chosen
because it is stdlib on all three platforms and needs no PyInstaller hook; only
Linux splits it out, hence `python3-tk` in the workflow's apt step.

**The window owns the main thread and the server does not.** Tk requires the
main thread and macOS enforces it, so the GUI path uses
`web.ServerHandle`/`serve_in_thread` (AppRunner + TCPSite on a private loop)
instead of `web.run_app`, which is blocking and installs signal handlers.
Both paths build the same app object, so windowed and headless cannot drift.
The status line polls cheap attributes from Tk's own `after()` timer rather
than calling `engine.state()` — that assembles the whole broadcast payload and
is already being built 20x a second.

Every failure degrades rather than stops: no tkinter → console with a message;
tkinter but no display → keeps serving headless. A missing window must never be
why the instrument won't start.

**Resolve the port BEFORE building the engine.** aiohttp only discovers a busy
port at `site.start()`, by which point the render thread, synth and MIDI port
are running — so a port clash arrived as a traceback after a successful-looking
startup. `--web-port` now defaults to `None` so `_resolve_port` can tell "the
user asked for this port" (busy is an error, with a readable message) from
"nobody said" (busy scans on to the next free one). The first packaged build
anyone ran hit exactly this: it was up and serving, the second launch failed,
and the only evidence was `[Errno 98]` under a fourteen-frame traceback.

**The released binaries deliberately omit speech recognition.** CI installs
no `faster-whisper`, so kiosk mode's transcription is unavailable in a
downloaded build — `KioskSession.enable()` refuses and says so, with a message
that changes by audience (`paths.frozen()`): from a checkout it names the pip
install, from a binary it says to run from source, because pip cannot reach
inside a frozen app. The reason is size: faster-whisper drags in `av`,
`onnxruntime` and `ctranslate2`, which took a local test bundle past 500MB
against the ~160MB release, for a feature most people never arm. Everything
else in kiosk mode works in a packaged build — only the microphone-to-text step
is missing. Revisit by adding `pip install faster-whisper` to the workflow's
install step and accepting the download size.

**`error.txt` is the field diagnostic.** `run.py` writes a breadcrumb per
startup step beside the executable, the full traceback on any unhandled
exception, and holds the console open on a frozen build. Written per-line and
flushed, because a segfault in a native audio/MIDI library leaves no Python
traceback at all and the last breadcrumb is then the only evidence. The handler
is installed **above the promptwaver imports** — an import failure is one of
the things being diagnosed.

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

`geometry.Path` — an `(N,2)` polyline in normalized `[-1,1]`, one RGB colour, and an optional per-stroke `glow` — is the universal unit. A frame is `list[Path]`. `Path3D` is the world-space variant; a `Camera` projects it down to `Path`.

The Python frame is now the **reference**, not what the outputs draw: the GPU outputs evaluate the same frame in the browser (`scene-eval.js`) from geometry sent once plus a per-tick snapshot. Python still draws it for the CPU reference renderer (`?renderer=cpu`), for the director's trial render, and as the ground truth `tools/gpu_twin` checks the browser against. There is no laser output any more (removed in 0.90).

This is a **vector** instrument: strokes only, no fills, no pixels.

### Generators, and the derivation that decides everything

Two families behind one registry (`generators/base.py`, `@register` / `create`), and since 0.90 exactly one generator in each:

- `Generator.render(t, p) -> Frame` — 2D, drawn directly in normalized space
- `Generator3D.render3d(t, p) -> list[Path3D]` — world space, projected by the scene camera

`Scene.is_3d` is **derived** from whether any layer's generator is 3D, and that derivation decides whether a `Camera` is constructed at all. Flat scenes have `camera = None` and skip projection entirely. The library's 2D/3D badge derives from the same rule via `SceneManager.library()`. Don't add a parallel stored "kind" field — `is_3d` is the single source of truth.

Two generators carry most of the weight, and both are **declarative interpreters** over authored `defs` + `nodes` rather than fixed algorithms with knobs — that shape is the one that survived contact with the scene director:

- `world` (3D) — `defs` are shape-grammar geometry expanded by `shapes.py`; nodes place them. 32 of 35 pre-existing library scenes use it and nothing else.
- `pattern2d` (2D) — flat symmetric line-art via `patterns2d.py`. No camera, composed directly in the frame.

The procedural generators (`flow_field`, `attractor`, `ripples`, `forest`, `ground_grid`) were **removed in 0.90**: no saved scene used them, and their per-frame procedural geometry could not be uploaded once. A scene that names one still loads — `Scene.__init__` puts a `MissingGenerator` in that layer's SLOT (layer params are addressed by index, so dropping it would re-point every later layer's controls) and logs it. The offline director (`director/fallback.py`) builds its forest from `world` defs and sends the old flow/cloud/smoke/rain keywords to seeded `pattern2d`. `jellyfish` is now a static primitive. Each director prompt names exactly one generator (`_SYSTEM` → `world`, `_SYSTEM_2D` → `pattern2d`).

**Both remaining generators have a GPU half**: `gpu_pack(params)` describes the static geometry once, `gpu_tick(t, p)` gives the per-tick uniforms and is where a generator advances its own clocks (`World._shape_time`). A new generator needs both, plus its evaluator in `scene-eval.js` — see "The GPU output path".

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

These describe the PYTHON renderer — since 0.90 the CPU reference and the twin
test's ground truth, not what the outputs draw. `scene-eval.js` mirrors the
same culling, budget and projection in JS, so a change to any of it is made in
both places and checked with `tools/gpu_twin`.

**`_motion` must stay after the cull in `World._render_budgeted`.** Evaluating it per-node before the visibility test was ~20% of frame time on a big world and ~94% of that was discarded. Culling deliberately tests the node's *resting* position with the bounding sphere inflated by `amp` to cover where motion could carry it; that bound is conservative, and the reordering was verified bit-identical over 40 frames.

**The projection loop rejects laterally as well as by depth.** `Camera`'s two
projection loops sort strokes near-first and stop at `max_strokes`, but that
counts strokes *emitted* — a stroke that clips to nothing costs a full clip
pipeline and advances nothing. Measured on `bikes` (330 nodes, max_strokes
120): 277 `_clip_and_project` calls a frame of which **157 (57%) returned
empty**, all of them in front of the camera and inside `far` but off to one
side, where the depth-only early-out could not see them. Adding a lateral
frustum reject took that to 139 calls / 19 empty, and whole scenes with it —
`cutlery drawer` 21.5→11.1ms, `alien algebra` 17.1→10.0ms, `brain` 9.8→5.7ms.
Verified **bit-identical over 40 frames across 7 scenes**, the same bar the
`_motion` reordering was held to.

It is only applied when `z.min() > near`: a perspective projection maps lines
to lines, so if every vertex is beyond one edge of the `[-1,1]` box so is
everything between them — but a stroke crossing the near plane has no single
meaningful projection to test.

**The look-at projection is BATCHED; the fly branch is not.** Everything before
the clip pipeline — the transform, the centroid distances, the z range, the
lateral bounds — is done once over the concatenated frame with `reduceat`,
and each stroke is then a view into that array. Same lesson as the audio
module's batched note renderers (INSTRUMENTS.md §2): the win is making FEWER
numpy calls, not cheaper ones. A stroke is ~24 points, so dispatch cost
dominated the arithmetic — 2.302ms a frame for the transform alone against
0.050ms batched.

**It is NOT bit-identical, and the reason is worth knowing.** `.sum(axis=0)`
uses pairwise summation, `np.add.reduceat` sequential, so per-stroke centroids
shift by ~1e-5 — enough to reorder near-ties, and the loop CUTS at
`max_strokes`, so a reorder can change which stroke wins the last slot.
Measured over 40 frames on 7 scenes: the SET of strokes drawn is identical on
six, and `Topo map` differs by 0.9 strokes a frame (2%); what changes
everywhere is draw ORDER, which is invisible under additive blending. The
transform itself is exact to 1e-15. Don't "fix" the ordering by restoring a
per-stroke `.sum(axis=0)` — that loop is most of what was removed.

**Stroke count is what costs the PYTHON renderer, not node count.** From the
same measurements: `cutlery drawer` draws 10 strokes from 690 nodes at 11ms,
while `bikes` draws 120 from 330 nodes at 30ms. That was why `max_strokes` was
the lever when a scene overran. On the GPU path it is not a cost lever at all
(a whole 2,962-stroke eniac is a few ms of JS) — `max_strokes` is now purely
each scene's authored DENSITY, nearest strokes first, and it is kept because
it is part of how every 3D scene looks. The slider reaches 1000; MIDI (CC 10)
and modulation keep 20-400 so existing mappings behave as they did.

**A scalar layer-param change must not rebuild the Scene.** `Scene.set_layer_param` writes to the live params dict that `render` already resolves from. A rebuild throws away geometry caches and resets generator runtime state — which is what `world`'s accumulated shape clock (`_shape_time`) depends on. Rebuild only for keys absent from `schema()`.

### The registry is the source of truth for generator metadata

Generators declare `description` and `param_meta` (explicit ranges); `kind()` derives from `is_3d`; `catalog()` serves the lot. The UI builds its layer panel from that schema and addresses params as `layer<N>.<param>`. **Read the catalog rather than adding a third place that knows generator names** — hardcoding is exactly what stranded the generators above. `schema()` deliberately exposes only int/float/bool defaults, so `world`'s `defs`/`nodes` correctly yield no sliders.

### The modulation matrix, and its implicit destination surface

`modulation.py` is the spine: sources are sampled once per tick and routed to namespaced destination keys spanning both visual and audio domains.

The non-obvious part is in `Scene._resolve`: **every top-level generator param is automatically a modulation destination** as `visual.<key>`, because resolve pushes each key through `matrix.value(f"visual.{key}", ...)`. Adding a flat scalar param to a generator makes it live-modulatable with no other wiring. Conversely, burying a number inside a nested dict hides it from the matrix — keep modulatable values as top-level scalars.

**The audio sources read the engine's own output, not a microphone.** `audio_level` follows the synth by default (the mic is `mic_level`, selectable via the `audio_react` setting); `synth_low/mid/high` are a three-band rfft split computed in `dsp.Soundscape._update_bands`; `voice.<name>` is per-voice output level, registered dynamically as soundscapes load. Wiring `audio_level` to the mic is what made every generated scene's audio routes look broken on any machine without live input — don't reintroduce that default.

Routes are editable at runtime (add/remove/repoint) and the destination list is **derived from `Scene.layer_schemas()`**, so a new generator param becomes routable with no list to update. `Engine.mod_destinations()` is the one place that assembles it.

**A route is `add` (default) or `range`.** `add` is the original `slider + source × depth`. `range` OWNS the destination: `lo + clamp01((source + bias) × source_scale / in_max) × (hi − lo)`, ignoring the slider; `lo > hi` inverts, `in_max` is the source value that counts as full (audio sits ~0–0.4), and audio depth/link parks it at `lo` when turned down. Range routes on one destination are averaged, then add routes stack on top. `mode`/`lo`/`hi`/`in_max` are written to the route dict only once a route has been switched to range, so untouched scenes save byte-for-byte as before; `Engine._add_route_from_spec` is the single place a saved dict becomes a live route. Range mode needs the destination's min/max: generator params get it from the registry, and camera/monitor destinations from `Engine.DEST_RANGES`, which **mirrors the sliders in index.html** — change both together.

**`ModMatrix.value` must return `base` untouched when no route targets it.** `Scene._resolve` pushes EVERY layer param through it, including `defs` (a dict) and `nodes` (a list). A version that always computed `base + 0.0` raised TypeError on the first frame and killed the render thread — which looked like UI edits silently not applying, because that thread is also what drains the action queue. Add-only routes are verified bit-exact against the original loop.

**Any route can `glide`** (seconds, 0–10): a per-route first-order lag on its source, updated in `ModMatrix.update` on the REAL dt (so a frozen scene still glides toward audio), with the time constant at `glide/3` so the number reads as time-to-~95%. It is written to the route dict only while non-zero. Routes are rebuilt from the spec on every edit, so `clear_routes` stashes each smoothed value by `(index, source, dest)` and `add_route` picks it back up — without that, touching any handle on the page snapped every gliding route back to its raw source. Both LFOs now have rate sliders (`lfo_slow.rate`, `lfo_mid.rate`); the engine side of `lfo_mid` already existed and only lacked a control.

`shape_modulation` is a separate, narrower mechanism: `world`-only, `scale`-only, and it reads *synth voice params/LFOs* rather than the matrix. Its UI says so explicitly on 2D scenes rather than presenting controls that do nothing.

### Audio renders a block AHEAD of the callback, not inside it

`synth.py`'s PortAudio callback used to call `Soundscape.render()` directly, which put full numpy synthesis on the realtime thread, behind the GIL, competing with the 45fps render loop and the 20Hz broadcaster. That — not DSP cost — was the long-standing source of audio dropouts: the callback measured a 75ms average and 368ms max against a 186ms budget while the actual DSP work was 5–15ms.

A producer thread now renders `PRERENDER_BLOCKS` ahead into a queue and **the callback only copies** — no numpy, no allocation, no lock. Keep it that way; anything added to `_callback` runs on the realtime thread. The queue depth is deliberately the smallest that measures clean (1), because it is pure output latency, and `output_latency` must keep including it or audio-driven visuals will lead the sound.

The trade that move makes: the producer is a **plain Python thread**, so it lost the realtime scheduling priority the PortAudio callback had. On a scene whose frame cost fills the frame budget (`hot lava`: ~20–23ms against 22ms at 45fps) the render thread never yields and the producer starves — 34% of callbacks on that scene, at CPython's default 5ms GIL handover. `GIL_SWITCH_INTERVAL = 0.0005`, set in `SoundscapeSynth.start()`, is what fixes it, at ~3% of the frame rate on the worst scene. **Raising `PRERENDER_BLOCKS` is the wrong instinct and is measurably worse** (18.8% → 21.1% → 49.1% starved at depths 1/2/4): a deeper queue just means the producer runs flat out for longer, competing harder for the GIL. Depth helps jitter; this is an average-throughput problem.

**Since 0.90 the render thread no longer saturates**, so the default blocksize is 1024 (~46 ms to the ear) instead of 8192 (~372 ms). Measured: 1024 and 512 run with zero starved callbacks in steady play; under scene switching (crossfade + geometry packing, ~15 ms on the render thread for eniac) 512 starved 3 callbacks in 23 switches and 1024 none. Three places carry the default — `SoundscapeSynth.__init__`, the engine's `audio_blocksize` fallback, and index.html's preselected option. **Measure blocksize changes with `Engine.synth.reconfigure()` in-process, never `Engine.configure_audio()` or the websocket `audio_config`: those persist `audio_blocksize` to the shared settings.json.** Silence a measurement with `disable_audio(fade=0)` — the mute gate multiplies the finished mix by 0, so the DSP load is unchanged.

**A starved callback is invisible to PortAudio.** It writes silence and returns in under a millisecond — on time, with data — so no underflow flag is set and `underruns`, `avg_duration_ms` and the budget percentages all stay green while the output gaps. That is how a badly dropping build shipped. `CallbackStats.starved`/`starved_pct` is the only counter that moves; keep it in any health judgement about the audio stream.

### Synth voices have a formula — read `INSTRUMENTS.md` before adding one

**`INSTRUMENTS.md` is the recipe for adding a voice type to `audio/dsp.py`**: the two rendering shapes and when each applies, the measured cost model (~0.6ms per note×partial row against a 186ms block budget, 120 rows the safe ceiling for one voice), the note-lifecycle guards and the assumptions a sustaining voice breaks, output-gain calibration, and the **five registration points** — `VOICE_TYPES`, the `render` dispatch, `_normalise`, `_SOUNDSCAPE_GUIDE`, and the `#voices` panel — none of which check each other, so missing one leaves the voice inaudible, unselectable, or invisible.

Two things from it that bite outside the audio module: `_normalise`'s output is what gets written to `scenes/<name>.json`, so defaulting a new field on every voice churns the whole tracked library on the next save — gate voice-specific fields on `v["type"]`. And **`set_param` bypasses `_normalise`**, so live UI/MIDI writes land in the spec unclamped and anything dangerous must also be clamped where it's read.

### The scene clock is an accumulator, not wall time

`Engine._scene_t` advances by `dt * motion_rate`, and `t` is what every time-driven thing reads. That's what makes **Freeze** work: ramping one number decelerates LFO phase, node motion and camera travel together. Note the deliberate asymmetry — `matrix.update()` gets **real** `dt` while `t` is frozen, so audio-driven sources keep slewing and a frozen pattern still reacts to sound. Don't "fix" that by passing the scaled dt to both.

### The render loop ticks; it does not draw

`Engine._loop` runs at a fixed `fps` (60). A tick drains the queue, advances
the clock, the matrix and `SceneManager.tick()` (camera, shape clocks, params,
crossfade), and publishes a snapshot — ~0.6 ms on eniac, measured. It draws a
CPU frame ONLY while a CPU-reference client is connected (`cpu_clients`, kept
by the server), and then at most `idle_fps` (24, a persisted rig setting) times
a second, because that path is shown over the ~20Hz preview broadcast and
drawing it every tick would spend 8-13 ms of GIL per tick on frames nobody can
see. Don't make the tick draw unconditionally again: that render-thread GIL
time is exactly what used to starve the audio producer.

`Scene.tick()` / `Scene.draw()` are split so that running both in one tick
advances the camera and the shape clocks ONCE: `World.gpu_tick` advances
`_shape_time`, and `render3d` reads the result from `p["_shape_t"]` rather than
advancing it again. `render()` (tick + draw) is kept for the director's trial
render and `tools/bench_scene.py`, and is bit-identical to the pre-split
render over 40 frames on every scene and through crossfades. `SceneManager.tick`
captures `live` — the `[(scene, gain)]` pair — BEFORE a completing crossfade
swaps `current`, so the frame where a fade ends still holds both scenes.

### Threading: one render thread, one queue

`Engine` runs a single daemon render thread at `fps`. Every mutation from the web layer or MIDI goes through `_enqueue(fn)`; the loop drains the queue at the top of each tick. **Never mutate engine or scene state directly from a web handler** — that's the invariant the whole control surface depends on.

The websocket broadcasts state at ~20Hz (plus a thinned preview frame, to CPU-reference clients only) on the same process, so anything expensive in `state()` competes with the render thread and the audio producer for the GIL. **`state()["scene_spec"]` is deliberately a slim view** (`_scene_spec_view`: def names, prompts, generation settings — what index.html reads): it used to be the whole `spec.to_dict()`, measured at 5-13 ms a tick (eniac 13.1 ms, 84 kB) for data no projector window read. If the page needs another spec field, add it to the view. `SceneManager.library()` (which reads each scene once to derive its kind), `generators.catalog()`, and `Scene.layer_schemas()` are all cached for this reason. Anything else added to `state()` should be too — assume it runs 20 times a second forever.

### The GPU output path

What every browser surface draws, since 0.90. **Python sends geometry once and
a small snapshot per tick; the browser evaluates and draws every frame.**

- **Snapshot** (`Engine._snapshot`, ~0.5 kB JSON, every tick): per live scene
  its `gpu_id`, crossfade gain, `Camera.gpu_state()` (pose, focal, near/far,
  max_strokes, depth cue) and each layer's `gpu_tick()` uniforms; plus the
  visual-fade gain, hue override, test-pattern flag, disable-plane flag and the
  modulated monitor filters. Serialised once on the render thread and handed to
  the server through `engine.snapshots` (a deque) + `engine.on_snapshot`, which
  only does `loop.call_soon_threadsafe(event.set)` — it runs on the render
  thread, so it must never do more than schedule.
- **Geometry** (`gpu_pack.py`, binary, once per Scene object): a uint32 header
  length, a JSON header, then 4-byte-aligned arrays the browser views in place.
  A new Scene object (load, crossfade, rebuild after a non-schema edit) is a new
  `gpu_id` and new geometry; scalar edits are uniforms only. The server sends a
  client each id the first time a snapshot it is about to receive names it, so
  geometry always arrives before the first frame that needs it.
- **Clients opt in with `?gpu=1`** on the websocket. Everything else is a CPU
  reference client (engine.preview frames, as before). `?renderer=cpu` on a page
  selects that; `output?renderer=cpu&full=1` is unthinned, for pixel compares.
- **`scene-eval.js` is a TWIN of the Python renderer** — `_motion`,
  `World._render_budgeted`/`_render_all`, `Camera.project`/`_project_lookat`,
  `_clip_and_project`, `_clip_frame`, `DepthCue.color_for`, `Pattern2D.render`
  (via its packed form), `_clip_2d`, the crossfade and post-processing in
  `SceneManager.draw`/`Engine._loop`. Its header lists the mapping. It emulates
  float32 where Python computes in float32, including the look-at centroid
  sums (np.add.reduceat accumulates in float32; a float64 sum reorders
  near-ties, and the budget CUTS on that order). Strokes still legitimately come
  out in a slightly different ORDER on near-tied distances — invisible under
  additive blending, which is why `tools/gpu_twin` compares stroke SETS.
  Measured: same strokes on all 25 library scenes + 15 variants, max point
  error 2e-5. **Run the twin test after touching either side.**
- **`frame-clock.js`** estimates the engine-clock offset as the windowed
  minimum of (arrival − te) and interpolates snapshots at `now − offset −
  delay`, `delay` = one tick + p95 jitter (~18 ms with one output, ~30 ms with
  four windows open). Continuous values lerp; discrete ones (scenes present,
  flags, integer budgets, kaleidoscope segments) come from the later snapshot.
- **`gpu-view.js`** draws on rAF, reschedules FIRST, try/catches each frame,
  reports `client_stats` (fps, draw ms, delay, size, GPU name from
  `WEBGL_debug_renderer_info`) every 2 s. The server accepts `client_stats`
  before the kiosk loopback gate; `state()["outputs"]` carries them to the
  Performance panel.
- **Audio sync includes the visual latency.** `mod_delay_auto` subtracts
  `visual_latency()` (worst output/kiosk window's delay + one refresh), re-applied
  every ~2 s on the render thread. Before, the 20Hz broadcast's own lag was never
  compensated.
- **Geometry pruning in the page waits 10 s** after arrival: geometry lands just
  before the first snapshot naming it and the server sends each id once, so
  pruning in that gap would lose it for good.
- **WebGL context restore re-runs `_initGLState()`** (EXT_color_buffer_float,
  blend state). Before 0.90 it rebuilt only programs and buffers, so a restored
  context drew nothing — found by forcing a loss with `WEBGL_lose_context`.

**Why JS evaluation and not a vertex shader:** `line_curve` (6 of 25 library
scenes) re-splines/decimates the PROJECTED polyline and needs neighbouring
points, and the `max_strokes` budget already needs every candidate point
transformed to get its z-range — a GLSL version would have duplicated that
work and added a second twin. Moving the transform into the shader remains
open if a scene ever needs it; nothing measured so far does.

### Loose keys on the params dict

Cross-cutting render inputs are passed as underscore-prefixed keys on the resolved params dict (`p["_camera"]`, `p["_disable_plane"]`) rather than by widening method signatures. Generators that don't look for them ignore them. Follow this convention rather than changing the generator interface.

### Monitor filters live in the renderer

Monitor filters — bloom, trails, mirror, kaleidoscope, keystone, line curve, line width — are applied by `web/static/renderer.js` over the finished frame, never authored into scene geometry, and stored per-scene in `spec.camera`. The modulatable ones (glow, trail, kaleidoscope, line curve, line width) reach the GPU outputs in the 60Hz snapshot (`snap.f`); the slow ones (mirror, bloom, fit, content aspect) come off the 20Hz state.

**`line_width` only widens** (1..8, a multiplier on each surface's own base stroke) because the renderer's AA feather is a fixed half pixel: a stroke thinner than that never reaches full alpha at its centre, and every line goes semi-transparent with brighter dots at the joints. It is a multiplier rather than a pixel count so the two browser surfaces keep their deliberate difference — see `renderer.js _getLineWidth`.

**`line_curve` is bipolar** (-1..1) where every other filter is unipolar: 0 means "draw the polyline exactly as authored", positive resamples it through a spline, negative drops points. It is centred rather than based at 0 specifically so the default leaves saved geometry untouched — don't "normalise" it to 0..1.

`Path.glow` (per-stroke, authored by `pattern2d`) feeds the renderer's glow target through a second colour attachment in the same geometry pass (MRT), so per-stroke glow costs no extra draw.

**Every surface shares one renderer.** `web/static/renderer.js` is loaded by `index.html`, `output.html` and `kiosk.html` alike and owns all drawing; each page only supplies a `filters` object and a canvas (and, on the GPU path, a `PWGpuView`). This replaced a hand-duplicated pair of paint functions, so a change to how strokes are drawn is now made once. What legitimately differs is what each page puts in `filters`: the in-page preview has no flip (it isn't a physical screen) and a fixed hairline line width, while the output window scales width with the viewport and carries per-monitor flip/keystone from localStorage.

### Output ratio and content aspect are two different numbers

`output_ratio` (Settings > Output) is the shape of the **surface** — a rig setting, stored in `settings.json`, reapplied on every scene load. `Engine.content_aspect()` is the shape of the **`[-1,1]` box the renderers receive**, and they are not the same:

- **3D**: the camera divides x by the viewport aspect (`scene3d._clip_and_project`), so its normalized box already *is* the viewport's shape. Content aspect = output aspect.
- **2D**: `pattern2d` composes in a square and never sees the ratio. Content aspect = 1.0, whatever the rig is.

Derived from `Scene.is_3d`, same as everything else that asks that question — don't store it. The renderer's letterbox takes the **content** aspect; assuming the box always filled the viewport is what stretched every flat pattern on a non-1:1 rig. (On the GPU path the 3D aspect reaches `scene-eval.js` as `cam.aspect` in the snapshot, the same number the Python camera divides by.)

`output_fit` (`fit` / `fill` / `stretch`) resolves the remaining mismatch — letterbox, pan-and-scan, or distort. **It is resolved in two different places, and that's the point:**

- **3D — at the camera** (`Camera._focal`). A wide ratio widens the horizontal field of view because `fov` is the vertical angle, so 16:9 shows ~78% more world sideways than 1:1. A `world` scene's geometry has bounded lateral extent, so that extra view lands on **empty space** — measured on `Circuitz`, lit pixels covered the full width at 1:1 but only 746/800 at 16:9. `fill` scales the focal length by `aspect`, which cancels the `/aspect` in `_clip_and_project` and restores the square framing, cropping vertically instead. Circuitz then covers 800/800.
- **2D — at the renderer's letterbox**, because a flat pattern has no camera to re-frame; `fit` there means real black bars.

So "the sides are empty on a 3D scene at 16:9" is **not** a letterboxing bug — an exact-16:9 output window computes zero bars. Don't go looking in `renderer.js` for it. Equally, `fill` cannot invent content: a scene that already fails to fill at 1:1 (`alien algebra`, 11.7% dead at 1:1) still has that gap afterwards.

Both browser surfaces read `content_aspect` + `output_fit` straight off engine state. **`index.html`'s `syncMonitorFilters` must keep setting them**: it originally didn't, and the preview silently fell back to a square box inside a canvas already reshaped to the ratio, so 3D scenes rendered horizontally squashed there and correct in the output window.

**The output's frame rate is the display's refresh**, not the broadcast: the GPU path draws on requestAnimationFrame. Measured on this laptop: 60 fps at 1080p and 4K on the GTX 1650, 0.4-2.2 ms of JS per frame; the AMD iGPU holds 60 at 1080p but only 38-42 at 4K, fill-bound on the five full-screen float post passes that run every frame. If an output reads slow, look at the Performance panel's GPU-outputs lines first — on a hybrid laptop the browser often gets the integrated GPU.

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
- **Never paint inside `ws.onmessage`.** Doing so replays a backlog instead
  of skipping it, and the kiosk overlay ended up behind the engine — measured
  at ~2s between the press and "Listening". Every page now draws on
  requestAnimationFrame (`PWGpuView`); the kiosk updates its overlay straight
  from the 20Hz state. The CPU reference path (`?renderer=cpu`) still paints in
  the handler on output.html, which is fine for a reference and not for a show.

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
orbit/drift-only scenes while any int always asks for a closed route. (3D only — a
2D request never receives a size directive; see "A 2D request must never carry
a size" below.) That is
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

**The interpretation slider is shared with the Generate modal.** Its directive
text and 0.35/0.65 thresholds live once, in `director.interpretation_directive`;
the kiosk's `_style()` and `Engine.generate_scene(interpretation=)` both call
it. Two pages mirror the thresholds in JS for display only —
`kiosk-settings.html directionText()` and `index.html interpDirection()` —
change all three together. The **literal** end has separate 2D wording ("flat
line art"), because the 3D wording pulled flat-pattern requests into writing 3D
scenes; the kiosk only asks for 3D, so none of its cache keys moved. **Rewording the directive orphans cached scenes:**
the text rides in `generate(style=)`, which is part of the cache key. The
neutral middle returns `""`, so an untouched slider leaves the prompt and the
key exactly as they were before the modal had it.

**A generated response is validated by rendering it, and a failed attempt is
retried with a correction.** `_validate_spec` rejects unknown generators, a
scene whose derived kind differs from the one asked for, and anything that
raises on a one-frame trial render — a 2D layer with 3-number points passes
every name check and only breaks there. The retry appends `_retry_note` (what
was rejected, plus kind-specific rules) rather than resending the identical
request, because the most common failure is systematic: Haiku writing a 3D
"pattern3d" scene for a 2D request on both attempts.

**A 2D request must never carry a size.** `_size_hint` is a 3D directive —
"MASSIVE, a place to travel through", a camera route with `[x,y,z]` waypoints,
"-50..50 on each axis", "225-275 nodes, this OVERRIDES the guidance above" —
and it was appended after the 2D system prompt on every request, because the
Generate modal hid the size slider for 2D but still sent its value. That one
leak explains every 2D failure logged since the slider landed (2026-08-15):
3D `pattern3d` scenes, `camera` keys inside layers, nodes placed in tens, and
blown stroke budgets. `SceneDirector.generate` now forces `size="small"` (no
directive) for `kind == "2d"` so no caller can reintroduce it, the modal omits
`size` for 2D, and `generation_settings.size` is recorded as `None`. 2D gets a
`MIN_TOKENS_2D` output ceiling instead, since the bare "low" tier (4000) is
under real 2D responses (~3900-6300). This changed the 2D cache key, which is
intended: every 2D entry cached before it was generated under the 3D directive.

**2D strokes are clipped at the frame edge, not clamped onto it.**
`Scene.render` ends in `clamp_frame`, which clips each COORDINATE into [-1,1].
The camera has already clipped 3D, so that's a no-op there — but a 2D pattern
reaching past the frame had its outside points pushed onto the border and drawn
as lines along it (263,040 such segments in 40 frames of `napkins`; also
LionsMane, jupiter, crop circles, skilsaw). `_clip_2d` cuts those strokes
properly; strokes wholly inside are returned untouched, and 3D output is
identical. It picks a clipper by length because their costs cross at ~60
points: `scene3d._clip_frame` (Python, ~2.5us/point) below `_CLIP_NUMPY_FROM`,
the vectorised `_clip_polyline` (~140us flat) above — a single-clipper version
took `crop circles` from 9 to 40ms a frame.

**The 2D prompt's stroke numbers must agree with each other.** It used to say
"60-300 strokes" in one place, "under ~`max_pps // 40`" (≈680 on this rig) in
the per-request budget line, and `"max_strokes":420` in the example — and the
model filled the biggest (463 drawn, against library patterns that draw 32-222).
It now says 80-200 everywhere, 250 at most, with `max_strokes` as ~1.2x the
expanded count. Don't cap `max_strokes` after generation instead: `pattern2d`
stops at the budget node by node, so a cap drops whole later nodes (often the
centre) rather than thinning the pattern evenly.

**2D voices need their names asked for explicitly.** The voice schema with
`"name"` lives in the 3D prompt's head, so 2D scenes came back unnamed and
`_normalise` labelled them `voice1..N` (also why the log said `cleared LFO on
'None'`). The 2D prompt now carries its own short soundscape format block.

**2D placements in the wrong units are fixed in the saved file.**
`_bake_placement_units` applies `pattern2d._placement_scale`'s rescue to the
spec before validation — measured identical output (<1.2e-7) — because models
place nodes in tens despite the prompt, and a rescue applied only at draw time
left the file wrong and warning on every load.

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

### Generated scenes are re-paced after the fact, not just asked nicely

`director._apply_pacing` runs on every fresh generation, next to
`_apply_evolution` and for the same reason: the system prompt asks for these
ranges, but asking is not a guarantee and every scene has to land inside them.
It clamps camera `speed` into 0.03–0.15, caps `far` at 28, and stretches the
soundscape's `swell_period` by 1.3x. The prompt's own example values were
lowered to match (speed 0.5 → 0.08, far 40 → 26), so the clamp rarely has to
bite and the variation Claude authors between scenes survives.

Two deliberate exceptions: **`speed: 0` is left alone** — a stationary camera
is a real authored choice (two library scenes use it) and flooring it would
silently start every still scene moving — and `far` is floored at 1 as well as
capped, because a negative far otherwise passes straight through.

It runs before the cache write, so cache entries written from now on are paced
and are not re-paced on read. Entries cached earlier keep their old pacing;
regenerating the prompt is what refreshes them.

### Scene JSON round-trip

`SceneSpec` is a dataclass serialised straight to `scenes/<name>.json`. `from_dict` reads every field with `.get(key, default)`, so adding a field is backward-compatible with the existing library — old scenes load with the default. Keep that property.

Anything that behaves like a scene setting belongs in the spec, and anything left as engine state silently leaks between scenes. `spec.lfo` exists because LFO rate was global: a scene routed from `lfo_slow` played back at whatever the previous scene left behind. Note the pattern in `Engine._apply_lfo` — it **restores defaults** for absent keys rather than only applying present ones, which is the half that actually stops the leak.

**Regenerating audio writes to the scene file**, while the running engine keeps playing the old soundscape — the UI reloads the scene afterwards so the change is audible. Worth knowing before you test that path against a scene you care about.

The director (`director/claude_director.py`) makes one cached Claude call per scene and returns JSON; `director/fallback.py` is a deterministic keyword→scene mapping used when no API key is set, so the app stays fully usable offline (including a seeded 2D pattern via `_pattern_2d`). Each system prompt carries the entire schema for its scene kind plus the shared soundscape guidance — changes to the scene format usually need a matching edit there, **in both prompts**, or Claude will keep emitting the old shape.

### Known drift

`CHANGELOG.md` jumps from `0.30.0` to `0.71.0` — the intervening releases were never logged. Don't try to reconstruct them.

`scenes/painters_studio.json` is a single `flow_field` layer despite its name and despite the docs using "inside a painter's studio" as the shape-grammar example (now noted in `TECHNICAL.md`). Treat saved scenes, not the docs, as ground truth for what the format currently produces.
