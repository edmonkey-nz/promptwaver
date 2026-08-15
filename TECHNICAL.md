# PromptWaver Technical Documentation

## Architecture

```
keyword ─▶ Scene director (Claude, async, ~1 call/scene, cached)
                 │  scene spec (JSON)
                 ▼
        Scene manager  ── load · save · crossfade
                 │
        ┌────────┴───────── Modulation matrix ─────────┐
        │        (LFOs · envelopes · audio level · MIDI)│
        ▼                                               ▼
   Synth engine (pyo)                            Visual engine
   voices · ADSR · filter ◀── shared params ──▶  generators · colour
        │                                               │
        ▼                                               ▼
   Audio output                              PathPlanner ─▶ Helios DAC
```

The modulation matrix is the spine: sources (LFO, ADSR, audio level, MIDI CC) are sampled once per tick and routed to destinations (both visual and audio). Because destinations span both domains, an audio-level source can open visual turbulence while an LFO opens the synth filter — one instrument, not two apps.

### Module map

- `promptwaver/geometry.py` — `Path` (normalized polyline + colour + glow), the unit everything speaks
- `promptwaver/modulation.py` — sources (LFO, ADSR `Envelope`, `Value`) + `ModMatrix` routing
- `promptwaver/generators/` — `flow_field`, `attractor`, `ripples`, `pattern2d`; `@register` to add more
- `promptwaver/patterns2d.py` — flat pattern grammar (ops · cart/polar space · repeat/symmetry)
- `promptwaver/color.py` — shared hue ramp + saturation-preserving `hue_shift`
- `promptwaver/scenes.py` — `SceneSpec`, live `Scene`, `SceneManager` (library + crossfade)
- `promptwaver/director/` — `SceneDirector` (Claude + cache) and local `fallback`
- `promptwaver/audio/` — `PadSynth` (pyo) and `AudioAnalysis` (sounddevice)
- `promptwaver/output/` — `PathPlanner` + `HeliosOutput` / `NullOutput`
- `promptwaver/scene3d.py` — `Camera` + projection (near-clip, frame-clip, depth cueing) for 3D scenes
- `promptwaver/primitives.py` — ready-made low-poly primitives (planet, ring, jellyfish, etc.)
- `promptwaver/shapes.py` — shape-grammar interpreter that expands Claude-authored geometry `defs`
- `promptwaver/settings.py` — local settings store (API key), gitignored
- `promptwaver/engine.py` — realtime loop and thread-safe control surface
- `promptwaver/web/` — aiohttp server + single-page control UI

## Scene kind: 2D or 3D

Every scene is one or the other, and the distinction is **derived from its generators, never stored** — the same rule `Scene.is_3d` follows, so the two cannot drift apart. `SceneManager.library()` reports `{"name", "kind"}` per scene for the library badge, built on the same cache as the name list (rebuilt on save/delete, never per 20Hz poll).

- **3D** — any layer uses a 3D generator (`world`, `ground_grid`, `forest`). A `Camera` exists and the camera panel is shown.
- **2D** — no 3D generator. **There is no camera at all**; the frame is composed directly in normalized `[-1,1]`. The UI shows the layer panel instead.

This is why 2D is not a fourth camera mode alongside orbit/drift/fly/path: those are properties of a `Camera`, and a flat scene doesn't have one.

## 2D pattern scenes (`pattern2d`)

The flat sibling of `world` — both are declarative interpreters over authored `defs` + `nodes` rather than fixed algorithms with knobs. Because there is no camera, what you author is exactly what fills the output, edge to edge.

```json
"layers": [{"generator": "pattern2d", "params": {
  "defs": {
    "arm":     {"space":"cart",  "ops":[{"op":"line","a":[0.17,0.17],"b":[0.17,0.97]}]},
    "diamond": {"space":"polar", "ops":[{"op":"ngon","n":4,"r":0.12}]}
  },
  "nodes": [
    {"def":"arm", "color":[0.3,0.8,1.0], "glow":0.85,
     "repeat":{"kind":"offset","d":0.05,"n":4,"hue_step":0.05},
     "symmetry":{"mirror":"xy"}},
    {"def":"arm", "rotate":0.25, "color":[0.45,0.55,1.0], "glow":0.85,
     "repeat":{"kind":"offset","d":0.05,"n":4,"hue_step":0.05},
     "symmetry":{"mirror":"xy"}},
    {"def":"diamond", "color":[0.75,0.35,1.0], "glow":1.0,
     "repeat":{"kind":"scale","factor":1.55,"n":3,"hue_step":0.09}}
  ]}}]
```

Three separate layers (`patterns2d.py`):

- **Ops** — `line`, `polyline`, `circle`, `arc`, `rect`, `ngon`, `star`, `grid`
- **Space**, per motif — `cart` reads coordinates as authored; `polar` reads a point as `(radius, angle)`, so a straight line becomes an arc or spiral. Segments are subdivided *before* conversion, or the curve would render as its chord
- **Combinators**, per node — `repeat`: `offset` (parallel bands), `scale` (concentric), `radial`, `ring`, `grid`; `symmetry`: `mirror` x/y/xy and `radial` *n*

Space is local to a motif and combinators are global to a node **on purpose**: that split is what lets one pattern mix idioms — Cartesian mirrored cross-arms alongside a concentric polar rosette. A single global "polar mode" flag could not express it.

**Angles are in turns (0–1), not radians**, everywhere in this grammar. Authored symmetry is nearly always a simple fraction of a circle, and `0.25` survives JSON and a language model's arithmetic far better than `1.5707963`.

Node keys: `def`, `at` `[x,y]` or `at_polar` `[r, turns]`, `scale`, `rotate` (turns), `color`, `glow`, `repeat`, `symmetry`.

The top-level `scale`, `rotate`, `spread` and `glow` params are flat scalars deliberately — `Scene._resolve` pushes every top-level param through the matrix as `visual.<key>`, so all four are audio/LFO-modulatable with no further wiring, while anything nested inside `defs`/`nodes` is not. All four carry MIDI learn icons and are bindable to hardware knobs.

Two of them behave in a specific way worth knowing:

- **`glow` is added** to each node's own glow rather than acting as a floor. A floor can never exceed the brightest authored shape, so routing audio at it would do nothing on exactly the scenes that bother to author glow.
- **`spread` displaces each piece from centre by its own centroid, at constant size.** That is the whole difference from `scale`, which zooms size *and* position. It runs after repeat/symmetry, because the copies are what have distinct positions to spread. An earlier version scaled node placement only — a no-op on the usual case of centred nodes whose structure comes entirely from repeat and symmetry, since that just multiplies `[0,0]` by a number.

`max_strokes` bounds the combinatorics — a repeat crossed with a symmetry multiplies fast (40 offsets × 32-fold radial is 1280 copies), and an unbounded pattern would blow the frame budget before anything else noticed.

### Generating one

The **scene type** picker in the Generate modal (3D world / 2D pattern) selects which system prompt authors the scene. It's an explicit choice rather than something inferred from the prompt text, because the two prompts are contradictory by design: the 3D one requires "a full ENVIRONMENT to navigate inside, **not a flat pattern**". The choice is remembered across sessions, and the "scene size" slider hides for 2D along with its cost readout — node count is a 3D notion, whereas a pattern's budget is its stroke count after expansion, and a cost estimate for a number that isn't being sent is worse than none. See [Asking for a big world](#asking-for-a-big-world-the-node-slider) for what that slider does.

The 2D prompt requires modulation routes on every scene, because a static pattern is a poster rather than an instrument. It's told to put spin on an LFO and brightness/size on `audio_level` — rotation driven by audio jitters, while brightness driven by audio is the entire point.

Offline (no API key), `director/fallback.py` produces a seeded pattern: the keyword hash picks symmetry order, palette and centre motif, while the skeleton — banded 4-fold cross, n-fold petal ring, nested centre — stays fixed, because that structure is what reads as *designed*.

### Per-shape glow

`Path.glow` (0–1) is authored per node and carried to the canvas alongside the geometry, distinct from the global glow slider that applies to the whole frame.

It is **monitor-only**. The laser's per-point intensity channel is on/off (`output/ilda.py` writes a constant 255), so brightness there is carried by RGB, not blur — consistent with the existing rule that display filters never touch vector data. The value rides in the preview payload only when non-zero, so every scene that doesn't use it produces a byte-identical frame to before, and both the in-page preview and the output window apply it identically.

## 3D immersive scenes

A scene is 3D when any layer uses a 3D generator (`ground_grid`, `forest`). The world stays as 3D polylines; a slow-drifting `Camera` projects them to the same 2D `Path`s everything downstream speaks, so laser output and browser preview are identical. Fly-through speed is a modulation destination (`camera.speed`), so audio or an LFO can steer your drift.

Because a laser draws only a few hundred strokes per frame, the camera's `far` plane culls distant geometry (and *is* the fog), with per-object LOD dropping detail strokes in the distance. Configure via the scene's `camera` block:

```json
"camera": {
  "fov": 62, "near": 0.4, "far": 14.0, "speed": 0.6, "max_strokes": 90,
  "depth": {"mode": "hue", "near_color": [0.3,0.95,0.5], "far_color": [0.08,0.15,0.5],
            "ttl_quantize": false}
}
```

### Depth on an on/off (no-brightness) laser

Units that can't fade brightness show depth with **colour**, not intensity. `camera.depth.mode`:

- `"hue"` — lerp `near_color` → `far_color` by distance. Set `"ttl_quantize": true` to snap channels to clean 0/1 primaries for TTL RGB
- `"cull"` — hard-drop anything past `far` (pure depth culling, no colour change)
- `"both"` — hue *and* cull

### Render cost scales with what's drawable, not with world size

`World.render3d` used to transform the entire world every frame and hand it all to the camera, which kept `max_strokes` and discarded the rest. So cost tracked how big the world *was*, not how much of it could be drawn — ~92% of the transform work was thrown away on a 10× scene.

A node's bounding sphere (one cached radius, one position) lets visibility and distance be decided per *node*, before per-stroke work. Nodes are rejected against the far plane and view cone, sorted by their sphere's nearest point, and transformed near-to-far until roughly 3× `max_strokes` has been produced.

Measured on `ants.json` (35 nodes, `max_strokes` 130), scaling node count against a 22.2ms budget at 45fps:

| world size | before | after | |
|---|---|---|---|
| 1× (35 nodes) | 11.0ms | 11.0ms | break-even |
| 3× (105 nodes) | 17.8ms | 15.4ms | 1.16× |
| 10× (350 nodes) | 37.9ms | 21.3ms | 1.78× |
| 20× (700 nodes) | 60.0ms | 24.1ms | 2.49× |

## Claude authors the geometry (shape grammar)

The director isn't limited to a fixed bucket of objects. For a prompt like *"inside a painter's studio"* Claude **authors the geometry itself** and stores it in the scene JSON: a `defs` block defines each object as line-art built from a small, open-ended **shape grammar**, and nodes reference those defs. The app is a general interpreter (`shapes.py`), not a noun-list.

Shape ops: `line`, `polyline` (raw escape hatch), `circle`, `rect`, `box`, `arc`, `grid`, `lathe` (revolve a profile — jars, vases, lamps, planets).

```json
"layers": [{"generator": "world", "params": {
  "defs": {
    "easel": [{"op":"line","a":[0,2.4,0],"b":[-0.9,-2,0.7]},
              {"op":"line","a":[0,2.4,0],"b":[0.9,-2,0.7]}],
    "jar":   [{"op":"lathe","profile":[[0,0],[0.4,0.25],[0.3,0.7]],"meridians":4}]
  },
  "nodes": [
    {"shape":"easel","pos":[0,0.5,-1],"scale":1.2,"color":[0.95,0.8,0.5]},
    {"shape":"jar","pos":[4.5,0.2,-2],"scale":1.2,"color":[0.7,1.0,0.9]}
  ]
}}]
```

Built geometry from defs is cached per object (defs are static; motion is applied via the node transform). Generate *"inside a painter's studio"* to see a full example — note that the shipped `scenes/painters_studio.json` is **not** one: it is a single `flow_field` layer from an older build, despite the name.

## The generator registry is self-describing

`generators/base.py` is the single source of truth for what generators exist, what kind each is, and what knobs each has. Each declares `description` and `param_meta` (explicit `(min, max[, step])`); `kind()` derives from `is_3d`; `schema()` and `catalog()` serve it.

Read the catalog rather than adding another place that knows generator names. Two places used to hardcode instead, and between them they stranded most of the generator set: the director's prompt hardcoded `"generator":"world"` so Claude could never select another, and the UI hardcoded three param keys (`layer0.speed/turbulence/hue`) that matched no generator's actual param list — leaving `forest` and `ground_grid` with no reachable controls at all.

- `schema()` exposes only int/float/bool defaults as params, so a generator whose spec is authored *data* rather than knobs (`world`, with its `defs`/`nodes`) correctly reports none and gets no slider panel.
- Ranges omitted from `param_meta` are inferred from the default's type and magnitude, so a new generator gets a usable panel immediately — but inference can't know that `step_len` wants a finer step than `turbulence`, so declaring it is what makes a control feel right.
- `Generator.coerce()` casts incoming UI/MIDI values to the declared type; without it an int param arrives as a float and its truncation makes the slider feel like it skips.
- The UI addresses any layer as `layer<N>.<param>` and renders one section per layer.
- `catalog()` and `Scene.layer_schemas()` are memoized — both ride the ~20Hz state broadcast, and the catalog is fixed once imports settle.

## Output detail profiles — monitor vs laser

A monitor and a laser want very different densities from the same scene. The canvas draws whatever it is handed; the DAC spends most of its PPS budget on the blanking jump between strokes, so a frame that looks rich on screen flickers on the beam.

One render feeds both outputs, so they cannot differ *simultaneously*. Instead the scene carries two settings and the engine swaps between them when the beam is armed:

- `camera.max_strokes` — the monitor value, and what the **max strokes** slider edits
- `camera.laser_max_strokes` — optional; used only while the laser is on. Absent (or 0) means "one density for both", which is how every scene authored before this behaved

`Camera.apply_profile(laser_on)` sets the live `max_strokes` and is called on the transitions that can change which output is live — arming the laser, loading a scene, editing either setting — never per tick, so it can't fight a slider mid-drag. `state()` reports `max_strokes` (the monitor setting), `max_strokes_live` (what is actually rendering) and `laser_max_strokes`; the camera panel shows the live figure, in amber when the laser profile is in force.

Saving writes the *monitor* value to `max_strokes` — saving while the beam is armed would otherwise persist the laser's reduced density as the scene's normal detail.

### How dense can a monitor scene actually be?

Measured with `tools/bench_scene.py` on `pottery` (161 nodes) against a 22.2ms budget at 45fps:

| nodes | max_strokes | drawn | avg ms | peak ms | verdict |
|---|---|---|---|---|---|
| 161 (1×) | 120 | 120 | 14.6 | 15.5 | ok |
| 161 (1×) | 300 | 175 | 21.6 | 23.7 | tight |
| 483 (3×) | 120 | 120 | 21.2 | 26.4 | tight |
| 805 (5×) | 120 | 120 | 25.2 | 33.9 | over |
| 805 (5×) | 600 | 483 | 71.3 | 85.5 | over |

**A drawn stroke costs ~0.12ms; a node costs ~0.013ms.** Stroke count dominates — that asymmetry is `World._render_budgeted`'s node-level culling doing its job. The practical ceiling is roughly **120–140 drawn strokes at 45fps**, and the slider's ceiling of 400 exists so a sparse scene with a near `far` plane can use it, not because a dense one can.

### Big explorable worlds

Node count is cheap but **not free**, and the cost is paid whether or not a node is visible: every node gets a bounding-sphere and view-cone test every frame. Two consequences worth knowing before authoring a large world:

- **Camera speed is irrelevant to cost.** Measured on an 805-node world at 100 strokes: 19.6ms standing still, 20.7ms at speed 2.0. A slow ambient walk is not cheaper than a fast one — you simply see less of the world per unit time.
- **Geometry beyond the far plane still costs.** A world spread wide enough that most of it is culled still pays the per-node test: at 2415 nodes the culling alone came to 33.6ms while drawing *zero* strokes.

At 100 drawn strokes on a 30fps budget (33.3ms), measured: 161 nodes → 13.2ms, 805 → 21.3ms, 1610 → 26.9ms, 2415 → 33.6ms (over). So **~1600 nodes is the practical world-size ceiling**, and 800 is comfortable.

Since the browser only updates at ~20Hz anyway (below), running the engine at `--fps 30` for a monitor-only big scene costs the display nothing and buys ~50% more frame budget.

Profiling puts ~60% of a heavy frame in `Camera.project`, nearly all of it in `_clip_and_project` — that is where to look if this ceiling ever needs raising.

**Engine fps is not monitor fps.** The websocket broadcasts at ~20Hz (`web/server.py`) and the canvas paints on message arrival, with no `requestAnimationFrame`. Rendering above ~20fps only benefits the laser; raising `--fps` toward a 60Hz monitor's refresh would tighten the frame budget without the monitor ever seeing the extra frames.

### Asking for a big world: the node slider

The renderer's ceiling (~1600 nodes) is well above what one API call will *write*, so the binding constraint on a big scene is authoring, not rendering. The **scene size** slider in the Generate modal is a node target, 100–1200, logarithmic — a linear slider would spend most of its travel in the range you can least afford to land in by accident.

The slider replaced a four-tier `small`/`medium`/`large`/`massive` dropdown. Those strings still resolve (`_resolve_size`), because every scene generated before the slider carries one in `generation_settings.size` and the Generate panel regenerates from it; a string size keeps the old fixed token floor, an integer budgets from the count.

Everything else derives from the number. `_size_hint(nodes)` is the old `massive` directive parameterised: extent grows as **sqrt(nodes)** so density stays roughly constant (a bigger world with the same geometry is just the same scene seen from further away; a bigger world with unchanged spacing is mostly dark, which measured worst of all), and def count, instances-per-def and waypoint count scale with it.

**Token budget follows the ask.** `estimate_tokens(nodes) = 3500 + 58·nodes`, calibrated against a measured run (Haiku 4.5: 197 nodes in 15,211 output tokens; the formula says 14,926). `token_budget` applies 1.35× headroom and clamps to `MAX_OUTPUT_TOKENS` (64,000 — empirically accepted, not a documented figure). That puts the single-call ceiling at **~757 nodes**; `max_nodes_per_call()` computes it and the UI warns above it.

#### What models actually do when asked for a big scene

Measured, asking for 700–900 nodes at `effort=high`:

| model | asked for | result | out tokens | cost | time |
|---|---|---|---|---|---|
| haiku-4-5 | 120–220 (old `massive`) | 108 nodes | — | — | — |
| haiku-4-5 | 700–900 | 197 nodes, 12 defs | 15,211 | $0.08 | 114s |
| haiku-4-5 | 621–759 (slider at 690) | 201 nodes, 13 defs | 14,726 | $0.08 | 112s |
| sonnet-5 (32k budget) | 700–900 | truncated, discarded | budget exhausted | billed, unusable | 294s |
| sonnet-5 (64k budget) | 700–900 | truncated, discarded | budget exhausted | billed, unusable | 491s |

Three things follow, and all three shaped the design:

- **Haiku settles near 200 nodes whatever it is asked for** — 108, 197, 201 across asks of 220, 900 and 690. The slider above ~250 buys extent and instancing discipline from Haiku, not more nodes. Reaching 800 needs a different mechanism (several calls, or expanding placements locally from a small authored `defs` library), not a bigger ask.
- **The cost estimate is therefore a ceiling, not a prediction.** The 690-node ask estimated $0.22 and billed $0.08. Over-stating is the right direction for a figure that gates spending, so the UI labels it "up to" rather than recalibrating downward — a model that *does* comply must not blow past a cap that assumed it wouldn't.
- **Sonnet overshoots instead, and the failure is expensive and silent.** It wrote past 64,000 output tokens without closing the JSON; the response was truncated, thrown away, and billed in full.

The 201-node scene above renders at **28.7ms avg / 30.8ms peak** at its authored `max_strokes: 120` — inside a 30fps budget, over a 45fps one. Consistent with the stroke-dominance measurements above: it is the 120 strokes costing that, not the 201 nodes.

#### Guards against paying for nothing

Three, in the order they can save money:

1. **`cost_cap`** (default $0.50, `set_cost_cap`) — `_from_claude` estimates before sending and refuses over the cap. This is the only guard that costs nothing to trip. Sized against the slider's range: Haiku never crosses it, Sonnet crosses near 1000 nodes, Opus near 500.
2. **`timeout`** (default 240s, `set_timeout`) — `_stream_or_call` breaks out of the stream loop and lets the `with` block close the connection, which stops generation. Tokens already produced are still billed; the rest never happen. The browser's own safety restore in `startGen` is deliberately *longer* (300s) so the server-side stop is the one that fires.
3. **Cost is recorded on failure.** `self.last_cost = estimate_cost(...)` now runs *before* the truncation and timeout checks. It previously ran after, so the only generations reporting no cost were the ones that had cost the most. On an aborted stream the usage block is reconstructed by `_EstimatedUsage` and flagged as `estimated: true`, since the API's own figures never arrive.

## PPS (points per second) control

Two settings govern draw rate, in the **Output** group:

- **max PPS** — the hardware ceiling. Persists in `settings.json` and is sent to Claude with every generation, so scenes are authored within your rig's real budget
- **scene PPS** — an optional per-scene override, blank by default. Saved into the scene JSON as `"pps"`, so it round-trips through the library

## Monitor filters — glow / trails / kaleidoscope

**Canvas-only display effects** — they never touch the vector data sent to the laser, only how it's drawn on screen:

- **glow** — a soft blur halo around each stroke
- **trails** — instead of clearing to black each frame, fades the previous frame slightly, so motion leaves a persistence trail
- **mirror x/y** — reflects one half of the frame over the centre line onto the other (kaleidoscope-style)
- **kaleidoscope segments** — wedge-based radial symmetry with alternating mirrors (3-12 segments)

All three are **per-scene settings**: they save into the scene's JSON via **Save Camera settings** / **Save all scene settings**, and load back with whichever scene set them. They apply identically in the preview and any open Output Window.

## Keystone correction & dual output windows

**Output 1** and **Output 2** (header buttons) each open a chrome-less window on the same live feed — for driving two screens/projectors from one session. Each has its own independent **flip** (a plain whole-image reverse — distinct from mirror) and its own independent **keystone**. Both are configured per-window in **Settings > Output monitors**; purely display-side, nothing sent anywhere.

The laser's own keystone (previously launch-only) is now live-adjustable in **Settings > Keystone** — tune while the laser's running, no restart needed. The in-page visualiser mirrors the laser's keystone so it can be dialled in without turning the beam on. A **test pattern** toggle (border, diagonals, crosshair, inner box) overrides the live scene *everywhere at once*.

## Prompt-composed 3D worlds (scene graph)

The richest scenes are **composed**, not procedural: Claude emits a *scene graph* — a list of nodes that place low-poly **primitives** in 3D space, each with a transform, colour, and motion. The camera floats through it. Try *"floating in space"* or *"swimming in a coral reef with jellyfish"*.

Primitive kit: `planet`, `ring`, `ball`, `starfield`, `jellyfish`, `torus`, `crystal`.

```json
"layers": [{"generator": "world", "params": {"nodes": [
  {"primitive": "planet", "pos": [0,0,0], "scale": 3.0, "color": [0.35,0.6,1.0],
   "params": {"lat": 3, "lon": 5}, "motion": {"type": "spin", "speed": 0.15}},
  {"primitive": "starfield", "pos": [0,0,0], "color": [0.7,0.8,1.0],
   "params": {"count": 36, "spread": 9.0}}
]}}]
```

Camera `mode`: `"orbit"` circles a scene, `"drift"` wanders inside it, `"fly"` moves forward through an endless field. Node `motion`: `spin`, `bob`, `drift`, `pulse`.

## Disable scene plane (v0.30.0)

A checkbox under **max strokes** (Camera controls) that hides a scene's floor/ground/backdrop geometry — applied in the `World` generator before the frame is built, so laser and every display are affected identically. It matches on the node's authored *name*: anything with `floor`, `ground`, `plane`, or `grid` in its name (e.g. `floor`, `cave_floor`, `ocean_grid`) — with a guard so `plane` doesn't catch `planet`.

## Soundscape (AI audio + mixer)

Every scene carries a **soundscape** — an ambient synth patch generated alongside the visuals. Give the "audio prompt" box a brief ("slow deep drones, distant whale calls") and Claude composes it into the scene's `soundscape`; leave it blank and it composes something that fits the scene.

The synth is **pure numpy + sounddevice** (no C build). Voices: `pad` (sustained drone chord), `pluck` (sparse scale notes), `noise` (air/wind texture), `sub` (low drone). Global effects: tempo, master, soft distortion (waveshaping), and a stereo delay (time/feedback/mix).

The **Soundscape mixer** sits beside the preview: master/tempo/distortion, delay, and a 3-band EQ (low/mid/high, ±24dB) up top, then a strip per voice with level, waveform, tone/rate, pan, and mute. A VU meter under the global knobs shows the actual post-master, post-limiter output level, with a clipping LED that lights if a block's peak nears digital full-scale.

**Regenerate just the audio**: tick "regenerate audio only, for an existing scene" under Generate, pick a scene, write an audio prompt, and hit **Apply to scene** — Claude composes a new soundscape for that scene's existing visuals (smaller, cheaper call) and saves it back to the library.

### Tone — the brightness control on every voice

`tone` (0–1) behaves like a filter cutoff. It applies to **every** voice type. It used to not: `tone` was read only by `pad` and `noise`; on `osc` and `pluck`, writing it did nothing. Those voices used naive oscillators, which have no brightness control and infinite harmonics at a finite sample rate (inharmonic aliases).

A resonant lowpass is ruled out by this module's founding constraint — no per-sample IIR recursion. Warmth is available additively instead: build one cycle from a truncated harmonic series with a rolloff, and the result is bandlimited *and* has a brightness knob. The cycle is built once into a small cached wavetable and read back by phase.

Ranges: **0.1–0.3** dark and mellow, **0.4–0.6** warm but present, **0.7–1.0** bright and cutting. The **cold ↔ warm** slider in the Generate panel drives this directly.

### Per-voice LFO

Every voice can carry an LFO that modulates **one** of its own parameters — separate from the global modulation matrix. This one lives in the soundscape, travels with the scene, and only touches the voice it belongs to.

```json
"lfo": {"on": true, "dest": "tone", "shape": "sine", "rate": 0.06, "depth": 0.5}
```

| target | what it does | |
|---|---|---|
| `level` | tremolo — pulsing, breathing | **smooth** |
| `pan` | auto-pan across the stereo field | **smooth** |
| `tone` | slow filter sweep — the most useful one | stepped |
| `detune` | drifting chorus thickness | stepped |
| `sub` | the weight underneath coming and going | stepped |
| `waveform` | steps between waveforms | stepped |
| `rate` | speeds a pluck/arp up and down | stepped |

Shapes: `sine`, `triangle`, `saw`, `square`, `random` (sample & hold, hashed so a scene sounds the same on every playback).

**Rate is 0–0.5 Hz** (clamped in DSP, UI, and MIDI table). The useful range is **0.02–0.15 Hz** (one cycle every 7–50 seconds). Rate 0 freezes the LFO at its phase offset. `level` and `pan` are applied as per-sample arrays and stay smooth across the whole range. The rest select a wavetable *before* the block renders, so they step between blocks and granulate near the top of the dial.

Phase comes from the absolute sample clock, so modulation is identical regardless of block size (measured: 0.99+ correlation between 2048 and 16384 blocks).

A `level` LFO is unipolar and downward-only — the authored level stays the ceiling. Cost with an LFO on every voice: 3.6% of the audio callback budget.

**The director uses them sparingly** — both via prompt constraint and a hard ceiling in code (`_limit_lfos`): never on the foundation voice, at most three total, later voices losing theirs first.

### ADSR envelopes

Any `pad` or `osc` voice now has a real ADSR (attack/decay/sustain/release), exposed as four knobs in the mixer under that voice. This replaced a fixed 3-second linear fade-in with no release. Muting a pad/osc voice now fades out gradually over its `release` time; unmuting re-triggers a clean attack.

### Oscillators and the arpeggiator

- **`osc` voice type** — a classic unison multi-oscillator, distinct from `pad`: stacks detuned copies of the same waveform for thickness (`unison` 1–7, `detune` spread), with an optional one-octave-down `sub` layer mixed in. Good for a lead or bass texture
- **Arpeggiator** — any `pad` or `osc` voice can arpeggiate its `chord` instead of sustaining it: tick "arpeggiate chord" on that voice and set a pattern (up / down / up-down / random), rate, and note decay. It steps through the chord one note at a time

The arpeggiator schedules notes through the exact same machinery as the `pluck` voice type, so it can't reintroduce unbounded-note-growth bugs: verified with deliberately aggressive multi-voice arp setups (high tempo, high rate, `random` mode) held flat at the same note cap with no budget overrun.

## Audio ↔ visual mapping

Scenes carry **modulation routes** — e.g. `audio_level → camera.speed` — that make the visuals react to the soundscape. Everything here is per-scene and saves via **Update scene from config**.

### Sources

The audio sources read **the instrument's own output**, not a microphone. That default matters: `audio_level` used to be wired to the mic, so on any machine not playing sound into one, every audio→visual route in every generated scene sat at zero and looked broken.

| source | what it follows |
|---|---|
| `audio_level` | whichever feed **Settings → visuals react to** selects (default: engine output) |
| `synth_level` | the soundscape, always — unaffected by that setting |
| `synth_low` / `synth_mid` / `synth_high` | three-band split of the output (<250Hz / mid / >2kHz) |
| `mic_level` | microphone / line input |
| `voice.<name>` | one per voice in the loaded soundscape |
| `lfo_slow` / `lfo_mid` | free-running, rate stored per scene |

Bands are computed block-wise by rfft where the mix is finalised (`dsp.Soundscape._update_bands`) — this module forbids per-sample IIR recursion, so a filter bank was ruled out; decimating to ~2048 bins keeps it at ~1.7% of the audio callback budget.

`voice.<name>` is measured post-level, post-LFO, pre-pan, so it's what the voice actually contributes. It also makes an **arpeggiator** usable as a modulation source with no special handling: each arp note is a spike in that voice's level, so routing it gives you the arp's rhythm.

The voice source list tracks the loaded scene exactly. Two details make that work: names come from the soundscape's own voice list rather than from which voices happened to produce output this block (a sparse pluck between notes contributes nothing, and would otherwise blink in and out of the picker), and voices belonging to other scenes are dropped — except any still referenced by a live route, since during a crossfade the outgoing soundscape stops reporting a moment before its scene is replaced.

### The matrix editor

Each mapping is **source → destination** with its own depth, and mappings can be added, repointed and deleted live. **Destinations are derived from the layer schema** — the same registry that builds the layer panel — so a new generator param becomes routable the moment it exists, with no list to maintain. Camera destinations appear only on 3D scenes; monitor effects (`glow`, `trail`, `kaleidoscope_segments`) always.

Every row shows a live meter for its own source, and a *modulation sources* readout lists all current values with flat ones greyed as idle — a route whose source never moves is otherwise indistinguishable from a broken route.

**audio ↔ visual** is a single slider (0–2×) scaling *every* sound-driven source at once, on top of each route's own depth.

### Freeze

Eases all motion to a standstill over `motion_ramp` seconds (2 by default). It ramps the **scene clock** rather than stopping controls individually, so LFO phase, node motion and camera travel decelerate together and in proportion — everything time-driven is a function of `t`.

The matrix still receives real `dt` while `t` is frozen, so `audio_level` keeps slewing and a frozen pattern still pulses with the sound. Distinct from **Stop**, which pauses the engine outright and blanks the output.

## MIDI control

Hardware knobs drive the same parameter keys the web UI does. Needs `pip install mido python-rtmidi`.

```bash
python run.py --list-midi          # show input ports
python run.py --midi MPK           # match a port by substring
```

Without `--midi` it picks the saved port, else the first non-loopback device. Change it live in **Settings → MIDI**.

A **MIDI in** indicator sits in the header: red (no controller), green (port open), flashing cyan on every incoming message.

### Learn any control

Every slider has a small `midi` tag at the end of its label. Click it, move a knob, done. Shift-click a bound one to unmap it. The tag shows the bound CC (`cc20`), pulses while waiting, and is barely visible when unmapped.

### Generator params take their range from the registry

`layer<N>.<param>` bindings (a 2D pattern's `scale`/`rotate`/`spread`/`glow`, a forest's `trees`) can't have a fixed range table: which params exist, and over what bounds, is a property of whichever generator the loaded scene uses. `midi.DYNAMIC_RANGES` is refreshed on every scene load from the registry's own `param_meta`, so a hardware knob and the on-screen slider cover the same range by construction. Soft takeover reads the current value out of the layer schema, so a knob doesn't jump the pattern when first touched.

### Sliders follow the hardware

Every on-screen control tracks the engine live, so turning a knob moves the matching slider and its readout — the two never disagree about what the patch actually is. A control you're dragging is left alone (and for ~400ms after) so the two input paths don't fight.

### Voice knobs bind to a position, not a name

Camera and master-audio params (`camera.speed`, `master`, `eq.low`) are fixed strings. A scene's *instruments* are addressed by name (`voice.deep_bass.level`) and the names are invented per scene by the director, so a binding to one would die on the next generate.

So instrument bindings are stored against an **ordinal** — `voice#0.level`, `voice#1.pan` — resolved to whatever name currently occupies that slot at the moment the CC arrives. The director emits voices in a fixed priority order (foundation → body → lead → detail → air), so CC 20 is "the low end" on every scene you ever generate and muscle memory survives a scene change.

Two levels of storage:

| where | form | scope |
|---|---|---|
| `settings.json` | slot-based (`voice#0.level: 20`) | the controller — constant across every scene |
| scene JSON | name-based `midi_overrides` | wins while that scene is loaded |

**Pin MIDI map** (Global section) freezes the currently-resolved slot bindings into the loaded scene as name-based overrides — for a scene dialled in ahead of a set. Nothing needs pressing after a normal generate; slots already track the ordering on their own. The button flips to **Unpin** once a scene has pins.

**Encoder modes** (Settings → MIDI, applies to the control you last learned):

- `catch` *(default)* — soft takeover: a knob is ignored until it sweeps across the current value. This is the default because loading a scene replaces every level at once
- `absolute` — 0–127 straight onto the range
- `relative` — deltas from endless encoders (auto-detects both common signed encodings)

**Default layout**: CC 1–16 are the globals (master, camera speed, distortion, delay mix, EQ, swell, draw depth, max strokes, audio↔visual, crossfade, glow, trail, orbit distance, tempo); then banks of 8 for voice slots — CC 20–27 levels, 30–37 pans, 40–47 tone, 50–57 attack, 60–67 release. Learning a CC takes it from whatever held it, and unmapping hands it back.

## Scene spec format

```json
{
  "name": "water_flowing",
  "layers": [{"generator": "flow_field", "params": {"turbulence": 0.3, "speed": 0.18}}],
  "camera": {"fov": 62, "far": 14.0, "max_strokes": 90},
  "soundscape": {
    "master": 0.8, "tempo": 60,
    "voices": [
      {"name": "base", "type": "pad", "level": 0.4, "chord": [0, 7, 12, 16]},
      {"name": "lead", "type": "osc", "level": 0.3, "note": 64, "pan": 0.2}
    ]
  },
  "modulation": [
    {"source": "lfo_slow", "dest": "visual.speed", "depth": 0.05},
    {"source": "synth_low", "dest": "visual.turbulence", "depth": 0.4}
  ],
  "lfo": {"lfo_slow": 0.05, "lfo_mid": 0.2},
  "midi_overrides": {"voice.lead.pan": 47}
}
```

`midi_overrides` is optional and empty for most scenes — only appears once **Pin MIDI map** has been used.

`lfo` holds this scene's LFO rates. Optional: an absent or partial block falls back to the engine defaults, which are also **restored** on load, so a rate set by one scene can't leak into the next. (It was global engine state before, meaning a scene routed from `lfo_slow` played back at whatever the previous scene happened to leave behind, and the value reset on restart.)

Every field is read with `.get(key, default)`, so adding one stays backward-compatible with the existing library — keep that property.

## Cost control

The director makes at most **one structured call per new keyword** using Haiku-class by default, and caches **successful Claude results** to `scenes/generated/` (fallback scenes are never cached, so fixing your key takes effect immediately).

**Model & effort** are set in the UI (Generate panel) and persist in `settings.json`. Model chooses the brain — Haiku (fast/cheap), Sonnet (better), Opus (best); effort chooses how hard it works — low / medium / high scales the token budget (4k / 8k / 14k) and asks for a simpler or richer scene. Bigger model + higher effort = better environments, more tokens. Verify model IDs at <https://docs.claude.com/en/docs/about-claude/models>.

Every generation is **added to the library** automatically. Tweak the live camera/config, then **Update scene from config** writes those settings back into the loaded scene. Override the response ceiling with `PROMPTWAVER_MAX_TOKENS`.

**Naming**: give a scene an explicit name in the Generate panel and it's used as the library title; leave it blank and Claude's own name (or the keyword) is used.

**Prompt detail**: a semi-detailed prompt beats a bare keyword. "swimming with jellyfish" leaves count, scale, and colour to the model's guess; "exploring underwater with dozens of large jellyfish, long pink trailing tentacles, shafts of light from above" gives Claude concrete things to place and colour.

**Progress bar**: the Claude API has no notion of overall completion — it doesn't know the final response length in advance. The bar is a proxy: output is compared against the effort tier's token budget. Treat it as "working, this far into the budget" rather than an exact ETA.

The UI's director line reports the source of the last scene: *composed by Claude*, *from cache*, or *local fallback* (with the reason).

## Blocksize and audio configuration

**Blocksize** governs the audio callback buffer size. Bigger = more buffer headroom, more latency before you hear a change; smaller = lower latency but more CPU work per callback.

Sizes up to 32768 are available. If picking a higher blocksize resets back to a lower one, your audio backend is imposing a ceiling — this is common on PipeWire/PulseAudio virtual devices. To raise that ceiling:

```bash
mkdir -p ~/.config/pipewire/pipewire.conf.d
cat > ~/.config/pipewire/pipewire.conf.d/99-quantum.conf <<'EOF'
context.properties = { default.clock.max-quantum = 32768 }
EOF
systemctl --user restart pipewire pipewire-pulse
```

**Latency mode**: `high` trades latency for stability; try it first if underruns show up.

**Audio diagnostics** tells you *where* the glitch is:

- **underruns** — hardware-reported xruns. **0 underruns but audible glitching** points to something below PromptWaver: OS audio scheduling, another app holding the device, or sandboxed/virtual audio layers
- **max render / budget %** — how close the DSP came to missing its deadline
- **max interval** vs expected callback interval — large gaps mean the callback was delayed before starting, independent of our render time

## Performance diagnostics (v0.30.0)

A render-loop counterpart to audio diagnostics: per-tick render/output timing, dropped-tick tracking, and whether a drop happened *during a scene crossfade*. Lives in a **Performance** accordion (sidebar); a lightweight FPS counter under the visualiser works all the time.

**Off by default** — the timing instrumentation has a small real cost (measured ~2.5% of a frame). Turn it on live in **Settings > Diagnostics**, or launch with `--diag`; no relaunch needed.

## Troubleshooting real bugs

### Unbounded note growth in plucks/arps (v0.12.0 fixed)

The `pluck` voice scheduler had no floor on onset spacing and no cap on simultaneously-active notes. A soundscape with high tempo/rate could schedule notes faster than they decay, growing without bound — every note costs a full block-length pass, so render time climbs. Fixed with three layers: a floor on onset spacing, a hard cap on active notes (oldest evicted first — inaudible in ambient context), and centralised range-clamping so any future pathological value degrades gracefully.

### Per-sample delay loops starving the audio thread (v0.15.0 fixed)

`PathPlanner.plan()` resampled every stroke with a **pure Python loop, one point at a time**, calling numpy on individual scalars and constructing a ctypes struct per point. Measured on a rich scene: **431ms per frame against a 22ms budget — 1941% over.** This starved the audio thread by continuously holding the GIL.

Rewritten fully vectorised: arc-length resampling per stroke via `np.interp` instead of a nested Python loop, the whole frame's coordinates batched into **one** DAC transform instead of one per stroke, and the result handed to ctypes as a zero-copy buffer view. Measured: **431ms → ~20ms per tick — about 21x**, moving from wildly over budget to mostly within it.

### Device enumeration breaking broadcast loop (v0.11.2 fixed)

Device enumeration for the Audio diagnostics panel read a non-list object from sounddevice that wasn't type-checked, ended up in every `state` broadcast. The broadcaster had no per-tick error handling, so the first failed serialization silently killed the broadcast loop for the rest of the session: the WebSocket stayed "linked" but no further state arrived.

Both fixed: device extraction is now robust, and the broadcaster catches and logs bad ticks instead of dying.

### Cache headers preventing hot-reload (v0.10.1 fixed)

The whole UI lives in one HTML file and changes across sessions. Without `Cache-Control: no-store`, a browser tab left open could silently keep serving an old cached copy — looking exactly like "features went missing" even though the server was fully up to date. Now the server sends `Cache-Control: no-store` on every load.

## Display preferences

**Settings → Display → hide slider values** clears the numeric readouts beside every slider and knob, for a calmer stage look. On by default. It uses `visibility` rather than `display`, so nothing reflows, and each value reappears on hover/drag. The preference lives in `localStorage` (per-browser cosmetic choice, not something that changes what the hardware does).

## Adding a scene generator

Drop a file in `promptwaver/generators/`, subclass `Generator`, `@register` it, and import it in `generators/__init__.py`:

```python
from ..geometry import Path, Frame
from .base import Generator, register

@register("lissajous")
class Lissajous(Generator):
    defaults = dict(a=3, b=2, speed=0.1, hue=0.5, points=300)

    def render(self, t, p) -> Frame:
        import numpy as np
        th = np.linspace(0, 2*np.pi, int(p["points"]))
        x = np.sin(p["a"]*th + t*p["speed"]); y = np.sin(p["b"]*th)
        pts = np.stack([x, y], axis=1) * 0.9
        return [Path(pts, (1, 1, 1))]
```

It's now available to the director and the `layer0.*` UI sliders.

## About panel

The **?** button beside the gear opens an About panel that renders [`about.md`](about.md) from the project root. It's read per request rather than cached at startup, so editing that file shows up on the next open with no restart. The renderer is a small markdown subset (headings, lists, rules, inline code / bold / italic / links) that escapes the source *before* adding any markup.

## Roadmap

- **More shape ops**: revolve-with-caps, sweep-along-path, mirror/array helpers, bezier
- **Node-list readout in the UI**: show (and hand-tweak) what Claude placed
- **Waterfall / cave / canyon** environments; richer camera paths (banking, look-at)
- **Shape-tween crossfade**: resample outgoing + incoming scenes to a common point budget and interpolate positions (currently dims/overlays instead)
- **BLE pads**: note-triggered scene recall (reuse your ESP32 HID pads) — the CC half of this now exists
- **Key storage**: move `settings.json` key to OS keyring before public release
