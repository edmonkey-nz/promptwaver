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

**There is no test suite** — no pytest, no test files, no CI lint or test gate. `.github/workflows/build.yml` only builds PyInstaller binaries on `v*` tags. Verify changes by running the app and looking at the preview; don't claim tests pass.

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

### The registry is the source of truth for generator metadata

Generators declare `description` and `param_meta` (explicit ranges); `kind()` derives from `is_3d`; `catalog()` serves the lot. The UI builds its layer panel from that schema and addresses params as `layer<N>.<param>`. **Read the catalog rather than adding a third place that knows generator names** — hardcoding is exactly what stranded the generators above. `schema()` deliberately exposes only int/float/bool defaults, so `world`'s `defs`/`nodes` correctly yield no sliders.

### The modulation matrix, and its implicit destination surface

`modulation.py` is the spine: sources are sampled once per tick and routed to namespaced destination keys spanning both visual and audio domains.

The non-obvious part is in `Scene._resolve`: **every top-level generator param is automatically a modulation destination** as `visual.<key>`, because resolve pushes each key through `matrix.value(f"visual.{key}", ...)`. Adding a flat scalar param to a generator makes it live-modulatable with no other wiring. Conversely, burying a number inside a nested dict hides it from the matrix — keep modulatable values as top-level scalars.

**The audio sources read the engine's own output, not a microphone.** `audio_level` follows the synth by default (the mic is `mic_level`, selectable via the `audio_react` setting); `synth_low/mid/high` are a three-band rfft split computed in `dsp.Soundscape._update_bands`; `voice.<name>` is per-voice output level, registered dynamically as soundscapes load. Wiring `audio_level` to the mic is what made every generated scene's audio routes look broken on any machine without live input — don't reintroduce that default.

Routes are editable at runtime (add/remove/repoint) and the destination list is **derived from `Scene.layer_schemas()`**, so a new generator param becomes routable with no list to update. `Engine.mod_destinations()` is the one place that assembles it.

`shape_modulation` is a separate, narrower mechanism: `world`-only, `scale`-only, and it reads *synth voice params/LFOs* rather than the matrix. Its UI says so explicitly on 2D scenes rather than presenting controls that do nothing.

### The scene clock is an accumulator, not wall time

`Engine._scene_t` advances by `dt * motion_rate`, and `t` is what every time-driven thing reads. That's what makes **Freeze** work: ramping one number decelerates LFO phase, node motion and camera travel together. Note the deliberate asymmetry — `matrix.update()` gets **real** `dt` while `t` is frozen, so audio-driven sources keep slewing and a frozen pattern still reacts to sound. Don't "fix" that by passing the scaled dt to both.

### Threading: one render thread, one queue

`Engine` runs a single daemon render thread at `fps`. Every mutation from the web layer or MIDI goes through `_enqueue(fn)`; the loop drains the queue at the top of each tick. **Never mutate engine or scene state directly from a web handler** — that's the invariant the whole control surface depends on.

The websocket broadcasts state and a thinned preview frame at ~20Hz on the same process, so anything expensive in `state()` competes with rendering for the GIL. `SceneManager.library()` (which reads each scene once to derive its kind), `generators.catalog()`, and `Scene.layer_schemas()` are all cached for this reason. Anything else added to `state()` should be too — assume it runs 20 times a second forever.

### Loose keys on the params dict

Cross-cutting render inputs are passed as underscore-prefixed keys on the resolved params dict (`p["_camera"]`, `p["_disable_plane"]`) rather than by widening method signatures. Generators that don't look for them ignore them. Follow this convention rather than changing the generator interface.

### Laser vs display are deliberately different

Monitor filters — glow, trails, mirror, kaleidoscope, keystone — are **canvas-only** and never touch the vector data sent to the DAC. They're implemented in the browser (`web/static/index.html`) and stored per-scene in `spec.camera`.

`Path.glow` (per-stroke, authored by `pattern2d`) is monitor-only for the same reason: the DAC's per-point intensity channel is written as a constant 255, so brightness on a laser is carried by RGB, not blur. Note the renderer is **duplicated** in `web/static/output.html` — a change to how strokes are drawn needs making in both, or the projector and the preview will disagree.

For laser output, `output/ilda.py` resamples each stroke to `max_step` spacing and inserts blanking and dwell points **between every stroke** (~8 fixed points per stroke transition). So the PPS budget is dominated by *stroke count*, not geometry complexity: at 28000 PPS and 45fps you have roughly 620 points per frame, and a full-width stroke alone costs ~67. Chaining strokes into continuous polylines is the highest-leverage optimisation for dense flat content. `shadowBlur` in the canvas renderer is also expensive per state change — there's a comment recording a previous multi-pass glow attempt that made the preview unusable.

### Scene JSON round-trip

`SceneSpec` is a dataclass serialised straight to `scenes/<name>.json`. `from_dict` reads every field with `.get(key, default)`, so adding a field is backward-compatible with the existing library — old scenes load with the default. Keep that property.

Anything that behaves like a scene setting belongs in the spec, and anything left as engine state silently leaks between scenes. `spec.lfo` exists because LFO rate was global: a scene routed from `lfo_slow` played back at whatever the previous scene left behind. Note the pattern in `Engine._apply_lfo` — it **restores defaults** for absent keys rather than only applying present ones, which is the half that actually stops the leak.

**Regenerating audio writes to the scene file**, while the running engine keeps playing the old soundscape — the UI reloads the scene afterwards so the change is audible. Worth knowing before you test that path against a scene you care about.

The director (`director/claude_director.py`) makes one cached Claude call per scene and returns JSON; `director/fallback.py` is a deterministic keyword→scene mapping used when no API key is set, so the app stays fully usable offline (including a seeded 2D pattern via `_pattern_2d`). Each system prompt carries the entire schema for its scene kind plus the shared soundscape guidance — changes to the scene format usually need a matching edit there, **in both prompts**, or Claude will keep emitting the old shape.

### Known drift

`CHANGELOG.md` jumps from `0.30.0` to `0.71.0` — the intervening releases were never logged. Don't try to reconstruct them.

`scenes/painters_studio.json` is a single `flow_field` layer despite its name and despite the docs using "inside a painter's studio" as the shape-grammar example (now noted in `TECHNICAL.md`). Treat saved scenes, not the docs, as ground truth for what the format currently produces.
