# PromptWaver

![version](https://img.shields.io/badge/version-0.30.0-33e0d0)
![status](https://img.shields.io/badge/status-pre--release-orange)
![platform](https://img.shields.io/badge/platform-Ubuntu-informational)

> **Pre-release, active development.** Version stays 0.x until things settle;
> scene JSON shape and internal APIs may still change between minor versions.
> See [CHANGELOG.md](CHANGELOG.md) for what's landed.

A realtime **immersive audio/visual instrument** ambient scene and soundscape explorer. Procedural vector visuals are streamed to a laser over ILDA or to a second monitor/data projector, and a polyphonic synth with unlimited audio controls provides the sound.

Claude acts as an offline **scene director**: prompting one for the 3D scene ("water flowing", "aurora over a still lake") and one for audio (water dripping, flowing river, heavy bass rumblings) becomes a scene spec, which the local engine then
renders at full framerate with no further API calls and plays the audio. That keeps it cheap enough to run for hours — the network is touched only when a new scene is create. Scenes are saved locally as JSON.

Note: You'll need a paid Claude API account if you want to generate any scenes, scenes cost ~5-40 cents(NZD) each, depending on size, detail and model used. You can buy a $5 credit which should last a while (unless you use Sonnet/Opus and make big scenes.)

![Main UI](/promptwaver-snap.png)



## Install

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt          # numpy + aiohttp (required)
pip install pyo sounddevice anthropic    # optional: audio, mic reactivity, Claude
```

**pyo build fails with `portaudio.h: No such file or directory`**: pyo compiles a
C extension against PortAudio and needs the dev headers installed first:

```bash
sudo apt install -y portaudio19-dev libsndfile1-dev libportmidi-dev liblo-dev build-essential
pip install pyo
```

**pyo build fails with `incompatible-pointer-types` / `too many arguments to
function`**: this is a different, deeper problem — pyo 1.0.5's C source (2020-era)
declares old-style untyped function pointers (`void (*mode_func_ptr)();`), which
GCC 14 (shipped on current Ubuntu) now treats as a hard error instead of a
warning. This isn't your setup; it's pyo's source being incompatible with a
current compiler. Two ways through:

1. **Build with an older GCC** (works, keeps pyo):
   ```bash
   sudo apt install gcc-12 g++-12
   CC=gcc-12 CXX=g++-12 pip install --no-cache-dir pyo
   ```
2. **Skip pyo** — PromptWaver's audio layer (`promptwaver/audio/synth.py`) is a thin
   wrapper; a pure numpy + `sounddevice` synth backend needs no C compilation at
   all and sidesteps this class of problem entirely. Given pyo has now failed
   twice on your setup, this is the path I'd recommend, and it fits directly
   into the filters/oscillators work planned next — ask and I'll build it.

## Installing the Helios DAC library (`libHeliosDacAPI.so`)

`--laser` needs this shared library, and — per the SDK's own README — only
the Windows build is kept up to date; Linux/Mac users are expected to build
it themselves. There's no package to install; it's a small build:

```bash
sudo apt install -y libusb-1.0-0-dev build-essential git
git clone https://github.com/Grix/helios_dac.git
cd helios_dac/sdk/cpp
g++ -Wall -std=c++14 -fPIC -O2 -c HeliosDacAPI.cpp
g++ -Wall -std=c++14 -fPIC -O2 -c HeliosDac.cpp
g++ -shared -o libHeliosDacAPI.so HeliosDacAPI.o HeliosDac.o -lusb-1.0
```

Then either point PromptWaver at it directly:
```bash
HELIOS_LIB=/full/path/to/libHeliosDacAPI.so python run.py --web --laser
```
or install it system-wide so the default lookup finds it:
```bash
sudo cp libHeliosDacAPI.so /usr/local/lib/
sudo ldconfig
```

**Linux also needs udev permission to talk to the DAC without root** — without
this, expect a silent "no Helios DAC found" even with the library present and
the device plugged in:
```bash
sudo tee /etc/udev/rules.d/60-heliosdac.rules <<'EOF'
SUBSYSTEM=="usb", ATTR{idVendor}=="1209", ATTR{idProduct}=="e500", MODE="0666"
EOF
sudo udevadm control --reload-rules && sudo udevadm trigger
```
Unplug and replug the DAC after adding the rule. (1209:e500 is the common
Helios vendor/product ID — confirm yours matches via `lsusb` rather than
assuming.)

## Run

```bash
# browser preview, no hardware, offline (local) director
python run.py --web

# drive the Helios (tune to your rig — mirrors your laser-arcade flags)
python run.py --web --laser --pps 11000 --max-step 0.03 --invert-x

# use Claude as the scene director
ANTHROPIC_API_KEY=sk-... python run.py --web
```

Open <http://localhost:8080>, type a keyword, hit **Generate**. Dial in a look,
name it, **Save**. Click a saved scene to crossfade to it.

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

The matrix is the spine: sources are sampled once per tick, routes add scaled
source values onto namespaced destinations (`visual.*`, `audio.*`). Because
destinations span both domains, an audio-level source can open visual
turbulence while an LFO opens the synth filter — one instrument, not two apps.

Module map:

- `promptwaver/geometry.py` — `Path` (normalized polyline + colour), the unit everything speaks.
- `promptwaver/modulation.py` — sources (LFO, ADSR `Envelope`, `Value`) + `ModMatrix` routing.
- `promptwaver/generators/` — `flow_field`, `attractor`, `ripples`; `@register` to add more.
- `promptwaver/scenes.py` — `SceneSpec`, live `Scene`, `SceneManager` (library + crossfade).
- `promptwaver/director/` — `SceneDirector` (Claude + cache) and the local `fallback`.
- `promptwaver/audio/` — `PadSynth` (pyo) and `AudioAnalysis` (sounddevice).
- `promptwaver/output/` — `PathPlanner` + `HeliosOutput` / `NullOutput`.
- `promptwaver/scene3d.py` — `Camera` + projection (near-clip, frame-clip, depth cueing) for 3D scenes.
- `promptwaver/primitives.py` — ready-made low-poly primitive kit (planet, ring, jellyfish…).
- `promptwaver/shapes.py` — the shape-grammar interpreter that expands Claude-authored geometry `defs`.
- `promptwaver/settings.py` — local settings store (API key), gitignored.
- `promptwaver/engine.py` — the realtime loop and thread-safe control surface.
- `promptwaver/web/` — `aiohttp` server + single-page control UI.

## 3D immersive scenes

A scene is 3D when any of its layers uses a 3D generator (`ground_grid`,
`forest`). The world stays as 3D polylines; a slow-drifting `Camera` projects
them to the same 2D `Path`s everything downstream speaks, so the laser output
and browser preview are identical. Fly-through speed is a modulation
destination (`camera.speed`), so audio or an LFO can steer your drift.

Because a laser draws only a few hundred strokes per frame, the camera's `far`
plane culls distant geometry (and *is* the fog), with per-object LOD dropping
detail strokes in the distance. Configure via the scene's `camera` block:

```json
"camera": {
  "fov": 62, "near": 0.4, "far": 14.0, "speed": 0.6, "max_strokes": 90,
  "depth": {"mode": "hue", "near_color": [0.3,0.95,0.5], "far_color": [0.08,0.15,0.5],
            "ttl_quantize": false}
}
```

### Depth on an on/off (no-brightness) laser

Units that can't fade brightness show depth with **colour**, not intensity.
`camera.depth.mode`:

- `"hue"` — lerp `near_color` → `far_color` by distance. Set `"ttl_quantize": true`
  to snap channels to clean 0/1 primaries for a TTL RGB unit.
- `"cull"` — hard-drop anything past `far` (pure depth culling, no colour change).
- `"both"` — hue *and* cull.

Try a keyword like *"float through a forest"* to build one.

### Render cost scales with what's drawable, not with world size

`World.render3d` used to transform the entire world every frame and hand the
lot to the camera, which depth-sorted it, kept `max_strokes`, and discarded the
rest. So cost tracked how big the world *was*, not how much of it could be
drawn — on a scene 10× the size of the heaviest shipped one, ~92% of the
transform work was thrown away, and the frame budget was missed outright.

A node's bounding sphere is one cached radius and one position, so visibility
and distance can be decided per *node*, before any per-stroke work. Nodes are
rejected against the far plane and the view cone, sorted by their sphere's
nearest point, and transformed near-to-far until roughly 3× `max_strokes` has
been produced — the camera's own depth sort was going to drop the rest anyway.
Sorting on the sphere's nearest point rather than its centre is what keeps a
large node like a floor plane sorting early, on the strength of the part of it
that's close.

Measured on `ants.json` (35 nodes, `max_strokes` 130), scaling the node count,
p95 render time against a 22.2ms budget at 45fps — output identical in every
case:

| world size | before | after | |
|---|---|---|---|
| 1× (as shipped) | 11.0ms | 11.0ms | break-even |
| 3× (105 nodes) | 17.8ms | 15.4ms | 1.16× |
| 10× (350 nodes) | 37.9ms | 21.3ms | 1.78× |
| 20× (700 nodes) | 60.0ms | 24.1ms | 2.49× |

Fly mode is excluded: it wraps geometry in Z against `field_depth` and tracks
the camera as a travelled distance rather than a position, so the distance
maths wouldn't hold. Every director-composed scene uses orbit/drift.

Note the bound derives from `max_strokes`, so it self-tunes for a dual-output
rig — raising detail for a data projector automatically buys more geometry
through the transform, and dropping it for the laser stops paying for it. The
*detail* axis is a separate bottleneck though: at `max_strokes` 600 the cost is
dominated by per-stroke camera projection, not the world transform.

### Disable scene plane (v0.30.0)

A checkbox under **max strokes** (Camera controls) that hides a scene's
floor/ground/backdrop geometry — applied in the `World` generator before the
frame is even built, so the laser and every display are affected identically,
not just a preview overlay. It matches on the node's authored *name*, not its
appearance: anything with `floor`, `ground`, `plane`, or `grid` in its name
(e.g. `floor`, `cave_floor`, `ocean_grid`) — with a deliberate guard so
`plane` doesn't also catch `planet` (a common primitive). Claude's
scene-authoring prompt now asks it to name floor/backdrop shapes this way, so
new generations reliably support the toggle; older or oddly-named scenes may
not have anything that matches.

## Claude authors the geometry (shape grammar)

The director isn't limited to a fixed bucket of objects. For a prompt like
*"inside a painter's studio"* Claude **authors the geometry itself** and stores
it in the scene JSON: a `defs` block defines each object as line-art built from a
small, open-ended **shape grammar**, and nodes reference those defs. The app is a
general interpreter (`shapes.py`), not a noun-list — so the vocabulary of scenes
is unbounded and nothing is baked into the local tool.

Shape ops: `line`, `polyline` (raw escape hatch), `circle`, `rect`, `box`, `arc`,
`grid`, `lathe` (revolve a profile — jars, vases, lamps, planets). Add an op =
add a function in `shapes.py`.

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

Nodes may reference an authored `shape` **or** a ready-made `primitive` — mix
freely. Built geometry from defs is cached per object (defs are static; motion is
applied via the node transform). This costs more tokens per generation than the
old fixed-primitive approach, but every scene is genuinely composed to the
prompt. Try *"inside a painter's studio"* (a full defs example ships in
`scenes/painters_studio.json`).

## PPS (points per second) control

Two settings govern draw rate, in the **Output** group:

- **max PPS** — the hardware ceiling. Persists in `settings.json` and is sent to
  Claude with every generation, so scenes are authored (object count, stroke
  density) within your rig's real budget rather than guessed.
- **scene PPS** — an optional per-scene override, blank by default. Set it and
  that scene always plays at that rate regardless of the global ceiling (clamped
  to never exceed it); clear it and the scene falls back to max PPS. Saved into
  the scene JSON as `"pps"`, so it round-trips through the library and
  **Update scene from config**.

## Monitor filters — glow / trails / mirror (v0.30.0)

A **Monitor filters** group under Camera controls adds three canvas-only
display effects — they never touch the vector data sent to the laser, only
how it's drawn on screen:

- **glow** — a soft blur halo around each stroke.
- **trails** — instead of clearing to black each frame, fades the previous
  frame slightly, so motion leaves a persistence trail.
- **mirror x/y** — reflects one half of the frame over the centre line onto
  the other (kaleidoscope-style).

All three are **per-scene settings**: they save into the scene's own JSON via
the existing **Save Camera settings** / **Save all scene settings** buttons,
and load back with whichever scene set them — off/0 by default for every
scene that hasn't (which is every scene made before this existed). They apply
identically in the small in-page preview and any open Output Window.

## Keystone correction & dual output windows (v0.30.0)

**Output 1** and **Output 2** (header buttons) each open a chrome-less window
on the same live feed — for driving two screens/projectors from one session.
Each has its own independent **flip** (a plain whole-image reverse — distinct
from the "mirror" effect above, which reflects rather than reverses) and its
own independent **keystone**, both configured per-window in **Settings >
Output monitors**; purely display-side, nothing sent anywhere.

The laser's own keystone (previously `--keystone-h`/`--keystone-v`,
launch-only) is now live-adjustable in **Settings > Keystone** — tune it
while the laser's running, no restart needed. The small in-page visualiser
mirrors the laser's keystone so it can be dialled in without turning the beam
on. A **test pattern** toggle there (border, diagonals, crosshair, inner box)
overrides the live scene *everywhere at once* — laser, visualiser, both
Output windows — so calibration is against a known shape rather than
whatever the current scene happens to show.

## Prompt-composed 3D worlds (scene graph)

The richest scenes are **composed**, not procedural: Claude (or the local
fallback) emits a *scene graph* — a list of nodes that place low-poly
**primitives** in 3D space, each with a transform, colour, and motion. PromptWaver
instantiates the geometry and the camera floats through it. Claude never emits
raw meshes; it arranges a vetted kit, so every object stays clean and
budget-safe. Try *"floating in space"* or *"swimming in a coral reef with
jellyfish"*.

Primitive kit (`promptwaver/primitives.py`): `planet`, `ring`, `ball`,
`starfield`, `jellyfish`, `torus`, `crystal`. Add one = add a `@register`
function; it's instantly available to the director.

```json
"layers": [{"generator": "world", "params": {"nodes": [
  {"primitive": "planet", "pos": [0,0,0], "scale": 3.0, "color": [0.35,0.6,1.0],
   "params": {"lat": 3, "lon": 5}, "motion": {"type": "spin", "speed": 0.15}},
  {"primitive": "starfield", "pos": [0,0,0], "color": [0.7,0.8,1.0],
   "params": {"count": 36, "spread": 9.0}}
]}}]
```

Camera `mode`: `"orbit"` circles a scene, `"drift"` wanders inside it, `"fly"`
moves forward through an endless field (forest/grid). Node `motion`: `spin`,
`bob`, `drift`, `pulse`. Composed worlds keep each object's own colour, so they
default to `depth.mode: "cull"`.

Tuning note: stroke *count* drives DAC load more than point count — each stroke
adds blanked travel. A dense `starfield` is the usual budget hog; trim its
`count` or the camera's `max_strokes` if you see flicker.

## Soundscape (AI audio + mixer)

Every scene carries a **soundscape** — an ambient synth patch generated alongside
the visuals. Give the "audio prompt" box a brief ("slow deep drones, distant
whale calls") and Claude composes it into the scene's `soundscape`; leave it
blank and it composes something that fits the scene.

The synth is **pure numpy + sounddevice** (no C build — deliberately, after pyo
wouldn't compile). It needs the PortAudio *runtime*:
`sudo apt install libportaudio2`. Without it, PromptWaver runs silently and
everything else is unaffected.

Voices: `pad` (sustained drone chord), `pluck` (sparse scale notes), `noise`
(air/wind texture), `sub` (low drone). Global effects: tempo, master, soft
distortion (waveshaping), and a stereo delay (time/feedback/mix). The DSP core
(`promptwaver/audio/dsp.py`) is separate from audio I/O so it can be rendered and
tested offline. The delay is fully vectorised (no per-sample Python loop) and
the output stream runs at a larger blocksize with high-latency buffering —
earlier builds glitched because a per-sample delay loop plus a small buffer left
the realtime audio callback fighting the GIL against the 45fps visual thread and
losing; the render is now ~1.6ms of work per ~46ms callback, comfortable
headroom.

The **Soundscape mixer** sits beside the preview: master/tempo/distortion,
delay, and a 3-band EQ (low/mid/high, ±24dB) up top, then a strip per voice
with level, waveform, tone/rate, pan, and mute. The EQ is a per-block
frequency-domain gain curve (`promptwaver/audio/dsp.py:_apply_eq`) — pure numpy,
no new dependency — applied to the whole mix before the delay/distortion
stage. A VU meter under the global knobs shows the actual post-master,
post-limiter output level, with a clipping LED that lights (and holds
briefly) if a block's peak nears digital full-scale. Every control updates
the running synth live, and mirrors into the scene — hit **Update scene from
config** to save your mix back into the scene JSON (`"soundscape"`), so it
travels with the scene through the library.

**Regenerate just the audio**: tick "regenerate audio only, for an existing
scene" under Generate, pick a scene from the dropdown, write an audio prompt,
and hit **Apply to scene** — Claude composes a new soundscape for that scene's
existing visuals (a smaller, cheaper call than a full regeneration) and saves it
back to the library. If that scene happens to be the one currently loaded, you
hear the change immediately.

## Audio ↔ visual mapping

Scenes are generated with **modulation routes** — e.g. `audio_level →
camera.speed` — that make the visuals react to the soundscape. Two levels of
control live in the **Modulation** group:

- **audio ↔ visual** — a single slider (0–2×) that scales *every* audio-driven
  route at once: the "level effect." Turn it to 0 to decouple sound from
  visuals entirely, or past 1 to intensify the reaction, without touching any
  individual mapping.
- **Per-route sliders** — one per relationship in the current scene (e.g.
  `audio_level→speed`), each independently adjustable. This is the granular
  mapping: how much *this specific* relationship contributes.

Both are live and both save via **Update scene from config** (`audio_link` and
each route's `depth` in the scene JSON).

## MIDI control

Hardware knobs drive the same parameter keys the web UI does. Needs
`pip install mido python-rtmidi`; without them the MIDI panel in Settings just
shows as unavailable and nothing else changes.

```bash
python run.py --list-midi          # show input ports
python run.py --web --midi MPK     # match a port by substring
```

Without `--midi` it picks the saved port, else the first non-loopback device
(`Midi Through` is never auto-selected — it swallows everything silently and
looks exactly like a dead controller). Change it live in **Settings → MIDI**.

A **MIDI in** indicator sits in the header next to Engine and Claude API: red
with no controller, green when a port is open, and it flashes cyan on every
incoming message — so you can confirm the link is live, and see which knob is
which, without watching a parameter move.

**Learn any control.** Every slider has a small `midi` tag at the end of its
label. Click it, move a knob, done. Shift-click a bound one to unmap it. The
tag shows the bound CC (`cc20`), pulses while waiting, and is barely visible
when unmapped.

**Sliders follow the hardware.** Every on-screen control tracks the engine
live, so turning a knob moves the matching slider and its readout — the two
never disagree about what the patch actually is. A control you're dragging is
left alone (and for ~400ms after you let go), so the two input paths don't
fight over it.

**Voice knobs bind to a position, not a name.** This is the part worth
understanding. Camera and master-audio params (`camera.speed`, `master`,
`eq.low`) are fixed strings that mean the same thing in every scene. A scene's
*instruments* are not — they're addressed by name (`voice.deep_bass.level`) and
the names are invented per scene by the director, so a binding to one would die
on the next generate.

So instrument bindings are stored against an **ordinal** — `voice#0.level`,
`voice#1.pan` — resolved to whatever name currently occupies that slot at the
moment the CC arrives. The director is asked to emit voices in a fixed priority
order (foundation → body → lead → detail → air), so CC 20 is "the low end" on
every scene you ever generate and muscle memory survives a scene change.

Two levels of storage, deliberately not one:

| where | form | scope |
|---|---|---|
| `settings.json` | slot-based (`voice#0.level: 20`) | the controller — constant across every scene |
| scene JSON | name-based `midi_overrides` | wins while that scene is loaded |

**Pin MIDI map** (Global section) freezes the currently-resolved slot bindings
into the loaded scene as name-based overrides — for a scene dialled in ahead of
a set, where a knob should stay on *that* voice wherever it lands in the
ordering. Nothing needs pressing after a normal generate; slots already track
the ordering on their own. The button flips to **Unpin** once a scene has pins.

**Encoder modes** (Settings → MIDI, applies to the control you last learned):

- `catch` *(default)* — soft takeover: a knob is ignored until it sweeps across
  the current value. This is the default because loading a scene replaces every
  level at once, so without it the first knob you touch snaps the whole mix.
  Re-arms automatically on every scene load.
- `absolute` — 0–127 straight onto the range.
- `relative` — deltas from endless encoders (auto-detects both common signed
  encodings).

Default layout: CC 1–16 are the globals (master, camera speed, distortion,
delay mix, EQ, swell, draw depth, max strokes, audio↔visual, crossfade, glow,
trail, orbit distance, tempo); then banks of 8 for voice slots — CC 20–27
levels, 30–37 pans, 40–47 tone, 50–57 attack, 60–67 release. One row of knobs
per field across all voices, which is how you actually mix live. Learning a CC
takes it from whatever held it, and unmapping hands it back.

## Blocksize kept resetting to a smaller value (fixed v0.15.1)

Root cause: the fallback ladder I added to auto-detect a working blocksize was
also firing on *explicit* Apply-button requests. If your backend rejected
8192 even transiently, it would silently step down to whatever smaller size
opened, then **persist that as the new default**. Next session started from
the already-degraded value — a one-way ratchet downward with no way back up
except by luck. That's what "keeps resetting to something smaller" actually
was, compounding a little further each time it happened.

Fixed by splitting the two cases: **startup autodetect** (finding something
that works on a fresh launch) still uses the ladder. An **explicit request**
(clicking Apply) now tries only the size you asked for — if it fails, you get
an honest error ("requested 8192 failed to open: `<real reason>`; restored
previous working blocksize 8192") and PromptWaver falls back to *restoring*
whatever was last known to genuinely work, not silently substituting and
remembering a smaller one. Verified directly: a request that fails no longer
lands on an untracked smaller size, and a config that was working before a
transient failure is correctly restored rather than degraded further.

Also fixed: the waveform and arpeggiator-mode dropdowns in the Soundscape
mixer were rendering absurdly tall. Cause: a global CSS rule gave every
`<select>` `flex:1`, correct for the horizontal control rows it was written
for, but those two dropdowns sit in *column*-flex containers (a voice card, the
arp fields) — there, `flex:1` stretches an element to fill remaining
*vertical* space instead. Scoped the rule to just the rows it was meant for.

## The real audio glitch cause (v0.15.0)

Your `max_strokes` experiment (dropping it to 20 measurably reduced glitching)
was the key clue: that's a *visual* setting, and it shouldn't affect audio at
all if the two were properly independent. It pointed at CPU/GIL contention
between the render loop and the audio callback thread, not the audio engine
itself — and that's exactly what it was.

`PathPlanner.plan()` — which turns a rendered scene into laser points, and
which `NullOutput` (what's running whenever you're testing without `--laser`)
still ran in full just to produce a UI point counter — resampled every stroke
with a **pure Python loop, one point at a time**, calling numpy on individual
scalars and constructing a ctypes struct per point. Measured on your actual
uploaded scene (18 nodes, ~89 strokes/frame): **431ms per frame against a
22ms budget at 45fps — 1941% over.** The render loop wasn't hitting anywhere
near 45fps when a scene this rich was loaded; it was hanging for nearly half a
second on every tick, continuously holding the GIL, which is what was
starving the audio thread. This had nothing to do with the audio DSP, which
was already fast — it explains why nothing I fixed in `dsp.py` fully resolved
this, because the actual bottleneck was upstream of it.

Rewritten to be fully vectorised: arc-length resampling per stroke via
`np.interp` instead of a nested Python loop, the whole frame's coordinates
batched into **one** DAC transform instead of one per stroke (the first
vectorised pass was still ~40 tiny numpy calls per stroke — numpy's per-call
overhead dominates at that size, so batching across the whole frame mattered
as much as vectorising within a stroke), and the result handed to ctypes as a
zero-copy buffer view instead of built one struct at a time.
`NullOutput` now uses a count-only path that skips the ctypes step entirely,
since there's no hardware to send it to.

Measured on your real scene: **431ms → ~20ms per tick — about 21x**, moving
from wildly over budget to mostly within it. Verified for correctness (exact
coordinate/color match against the old method), not just speed. This should
resolve the great majority of what you were seeing; `max_strokes` remains a
valid dial for any remaining headroom, and scene generation itself
(`scene.render()`, the 3D world + camera projection) is now roughly tied with
output planning as the next-largest cost if you still see occasional spikes —
a candidate for a future pass if needed.

## ADSR envelopes

Any `pad` or `osc` voice now has a real ADSR (attack/decay/sustain/release),
exposed as four knobs in the mixer under that voice. This replaced a fixed
3-second linear fade-in with no release at all — muting a pad/osc voice used
to cut it instantly; it now genuinely fades out over its `release` time, and
re-triggers a clean attack on unmute rather than clicking back in. Verified
directly: attack ramps up from near-silent, sustain holds flat, muting fades
out gradually (not an instant cut) down to true silence, and unmuting
re-attacks cleanly. Reuses the same ADSR state machine already used for visual
modulation (`Envelope` in `modulation.py`) rather than a second implementation.
Note the envelope updates once per audio callback block (not per-sample), so
very fast attack/release times (well under a callback's duration, which is
~0.05-0.2s depending on your blocksize) will sound block-stepped rather than
perfectly smooth — irrelevant for the slow ambient swells this is meant for,
but worth knowing if you push attack/release very short.

While building this I also caught and fixed a real bug in the mixer's own
JavaScript: several knobs (the arpeggiator's rate/decay, and now the new ADSR
knobs) used a CSS selector that assumed a wrapper element one level higher
than actually exists, meaning dragging them would have silently thrown a
JavaScript error every time — never actually sending the change. Caught this
one myself by actually exercising the mixer in a simulated browser (not just
checking that the HTML contained the right markup) before shipping.

## Tone — the brightness control on every voice

`tone` (0–1) behaves like a filter cutoff and is the main thing separating a
warm, round soundscape from a thin, buzzy one. It applies to **every** voice
type.

It didn't used to. `tone` was read only by `pad` and `noise`; on `osc` and
`pluck` — exactly the voices a director reaches for when asked for bass and
leads — writing it did nothing at all. Those voices used naive oscillators
(`2*(phase%1)-1`), which have two problems: no brightness control, and
infinite harmonics at a finite sample rate, so everything above Nyquist folds
back as inharmonic tones. That folding is a sampling artefact, not a musical
choice, and no amount of prompting removes it.

A resonant lowpass is ruled out by this module's founding constraint — no
per-sample IIR recursion, because pure numpy can't vectorise one (see
[dsp.py](promptwaver/audio/dsp.py)'s docstring). The same warmth is available
additively instead: build one cycle from a truncated harmonic series with a
rolloff, and the result is bandlimited *and* has a brightness knob.

Summing those partials every block would be far too expensive (32 partials
across a 3-note chord measured ~11% of the audio callback budget, and unison
multiplies it), so the cycle is built once into a small cached wavetable and
read back by phase. Measured on `chocolate factory`:

| | |
|---|---|
| aliasing on a high saw | 2.79% → **0.00%** inharmonic |
| audio callback cost | 8.3ms of a 372ms budget (**2.2%**) |
| worst case (unison 7, 4-note chord) | 8.5ms (2.3%) |
| wavetable cache | 19 tables |

The rolloff is shaped like a cutoff sweeping through the harmonic series
rather than a per-partial decay. `pad`'s original `tone**(k-1)` curve gets
away with it because it caps at 8 partials, but across 64 it collapses almost
immediately — everything from 0.15 to 0.6 measured identically dark, so five
sixths of the control did nothing.

Ranges the director is told to use: **0.1–0.3** dark and mellow, **0.4–0.6**
warm but present, **0.7–1.0** bright and cutting. The **cold ↔ warm** slider
in the Generate panel drives this directly — measured across a generated pair,
mean tone 0.26 (warm) versus 0.65 (cold), a 340× difference in the 800Hz–3kHz
band.

For deep, heavy bass the director is given a recipe: an `osc` voice at note
24–36, `tone` 0.15–0.3, `unison` 2–3 with light detune, and `sub` 0.4–0.8 for
the octave below, with a long attack so it swells rather than thuds.

Sine voices are bit-identical to before — there is nothing to bandlimit.

## Oscillators and the arpeggiator

Two new soundscape building blocks, in the mixer per-voice:

- **`osc` voice type** — a classic unison multi-oscillator, distinct from
  `pad` (which builds warmth from harmonic partials): stacks detuned copies of
  the same waveform for thickness (`unison` 1-7, `detune` spread), with an
  optional one-octave-down `sub` layer mixed in. Good for a lead or bass
  texture rather than a drone.
- **Arpeggiator** — any `pad` or `osc` voice can arpeggiate its `chord`
  instead of sustaining it: tick "arpeggiate chord" on that voice and set a
  pattern (up / down / up-down / random), rate, and note decay. It steps
  through the chord one note at a time rather than playing it as a block.
  Random mode is a properly-distributed deterministic hash (an earlier
  multiplicative version degenerated to the same order as "up" for small
  chord lengths — fixed).

The arpeggiator schedules notes through the exact same machinery — and the
exact same safety caps — as the `pluck` voice type, so it can't reintroduce
the unbounded-note-growth bug above: verified directly with a deliberately
aggressive multi-voice arp setup (high tempo, high rate, `random` mode) held
flat at the same note cap with no budget overrun.

## Master Start/Stop and the Laser toggle

**Start/Stop fades the sound, both directions.** Audio ramps over
**start/stop fade** (Global, default 1.5s, 0–8s) rather than cutting — a hard
mute clicked and popped on every stop. Audio is not the safety-critical part
of a Stop, the *beam* is, so the two are deliberately separate: `output.blank()`
still lands on the very same tick, unfaded, while the sound rides down. The
ramp completes after the engine goes inactive because the synth renders on its
own callback thread; the render loop's inactive branch only stops drawing.

This is distinct from **audio fade** next to it, which is the scene-to-scene
soundscape crossfade. The **Blank** action stays instant in both — it's the
panic button, not a toggle.

**Fixed in v0.13.0 — "picking a higher blocksize resets back to a lower one":**
`reconfigure()` used to silently revert to whatever was running *before* your
change if the requested blocksize failed to open — with no visibility into
why, and worse, the engine's own config record kept showing the *requested*
value even after the silent revert, so the UI's dropdown and the real running
state could quietly disagree. Also, `online` was a fixed class attribute
(always true the moment the synth object existed) rather than reflecting
whether a stream was actually running — so a synth stuck in a broken state
could still report itself healthy.

Now: `reconfigure()` steps *down* through a size ladder (32768 → … → 512)
until one actually opens, so you get the **largest size your backend
supports** rather than snapping all the way back to the old value; `online`
only goes true after a real successful stream start; the engine always reads
back the synth's *actual* resulting config after any change rather than
trusting the request; and clicking Apply now gets an immediate, explicit
result — "applied ✓", "⚠ 16384 not supported here — running at 8192 instead",
or the real error — shown right under the button, plus in the diagnostics
panel itself. Some backends (PulseAudio/PipeWire virtual devices — likely
`pipewire`/`default`/`Default Sink` in your device list) cap how large a
buffer they'll accept independent of anything PromptWaver does; the ladder finds
the largest size that specific backend actually honours.

**Still landing on 4096 after v0.13.0's ladder fix?** At that point it's very
likely a genuine ceiling on your audio backend, not a PromptWaver bug — the
ladder was verified working correctly in isolation (finds and reports the
largest size that actually opens). Your device list (`pipewire`, `default`,
`Default Sink`) points at PipeWire/PulseAudio virtual devices, which commonly
cap the buffer/quantum size in their own config regardless of what any
application requests. If you want to raise that ceiling, it's a system-level
change, not an app setting:
```bash
mkdir -p ~/.config/pipewire/pipewire.conf.d
cat > ~/.config/pipewire/pipewire.conf.d/99-quantum.conf <<'EOF'
context.properties = { default.clock.max-quantum = 32768 }
EOF
systemctl --user restart pipewire pipewire-pulse
```
That said — the actual glitch cause (unbounded note growth, fixed in v0.12.0)
no longer depends on a large blocksize to stay stable; 4096 should now hold up
fine even on complicated soundscapes. Worth confirming before spending time on
the PipeWire config.

**Fixed in v0.11.2 — a real bug, not a cache issue:** device enumeration for
the Audio diagnostics panel read `sd.default.device`, which on some
sounddevice versions returns an object that supports indexing but isn't a
`list`/`tuple` instance. The old type check missed it, so that non-serializable
object ended up in every `state` broadcast. Worse, the broadcaster had no
per-tick error handling, so the very first failed serialization — the moment
a browser connected — silently killed the broadcast loop **for the rest of the
session**: the WebSocket itself stayed "linked" (that part genuinely works),
but no further state ever arrived, so the library, soundscape mixer, and
version number all stayed empty no matter what you did in the UI. The error
only surfaced in the terminal when the process exited. Both are fixed: device
extraction is now robust to whatever type sounddevice returns, and the
broadcaster now catches and logs a bad tick instead of dying — so a future
edge case degrades to one skipped update, not a silently dead session.

If the UI ever looks like it's missing something you know shipped (the
Soundscape mixer, a library entry, a control) — hard-refresh the browser tab
first. The whole UI is one HTML file, and a tab left open across an update, or
even just a normal browser cache, can silently keep serving an old copy; the
server now sends `Cache-Control: no-store` on every load specifically to
prevent this, but an already-open tab won't pick that up until you reload it.
The version shown top-left (`v0.11.1 · ...`) is the quickest way to confirm
you're on the current build.

Nothing draws, plays, or animates until you click **Start** (a single toggle,
top-right of the header — it reads **▶ Start** when idle and **■ Stop** when
running). On load, PromptWaver sits idle: the laser is sent an explicit blanked
(zero-intensity) frame every tick, audio is muted at the DSP level, and the
scene clock is frozen — not stopped from zero, *frozen*, so Stop then Start
again resumes exactly where it left off rather than jumping the animation
forward by however long it was paused. **Start** fades audio in over 1 second
rather than snapping to full level (a real pop/click otherwise); **Stop**
stays instant, since that's the safety-critical direction and must not lag
behind the click.

**Start/Stop Laser** (v0.30.0, header, next to Start/Stop) is a separate,
independent gate for the physical beam only — **off by default**, regardless
of whether `--laser` was passed at launch. Visuals, audio, and the browser
preview all run normally with it off; only the real DAC keeps getting an
explicit blanked frame until you arm it. The point is being able to compose
and preview a scene safely before it's actually sent to the rig, without also
having to stop/restart playback to do it. Turning it off blanks the beam
immediately, same "real zero-intensity frame" guarantee the old **Blank**
button gave (which this replaces) — not just skipping a write, which would
leave the last frame looping on the DAC's own buffer. With no laser hardware
attached (`NullOutput`), this toggle has no effect either way — the preview
and point-counter stay accurate regardless.

**Fixed in v0.12.0 — the real "glitches on complicated soundscapes" cause:**
render duration climbing to 500-1000%+ of budget (rather than occasional
scheduling jitter) meant the render itself was doing unbounded work, not
missing its deadline. Root cause: the `pluck` voice scheduler had no floor on
onset spacing and no cap on simultaneously-active notes. A soundscape with a
high tempo/rate (very plausible from a rich or energetic AI-generated brief)
could schedule notes faster than they decay, growing without bound — every
note costs a full block-length pass per callback, so render time climbs with
the pile-up. Reproduced directly: three moderate pluck voices reached 1248
active notes and 100ms+ renders (against a ~93ms budget) within 30 seconds.
Fixed with three layers: a floor on onset spacing, a hard cap on active notes
(oldest evicted first — inaudible in an ambient context), and centralised
range-clamping of every soundscape parameter (tempo, rate, decay, chord/scale
length, etc.) so any future pathological value degrades gracefully instead of
compounding. Verified bounded and within budget for a 45-second run that
previously blew up, and confirmed deliberately extreme/malformed values (empty
scale, tempo=99999, 500-note chords) can no longer even reach that state.
Blocksize options up to 32768 are also available now for extra headroom on
top of this fix.

## Performance diagnostics (v0.30.0)

A render-loop counterpart to the Audio diagnostics below: per-tick render/
output timing, dropped-tick tracking, and whether a drop happened *during a
scene crossfade* — so "does it lag right when scenes fade" is something you
can read off real numbers instead of guessing. Lives in a **Performance**
accordion (sidebar); a lightweight FPS counter under the visualiser works all
the time, independent of everything else here.

**Off by default** — the timing instrumentation itself has a small real cost
(measured ~2.5% of a frame). Turn it on live in **Settings > Diagnostics**,
or launch with `--diag`; no relaunch needed either way, and the FPS counter
keeps working regardless of this setting. When off, the Audio
diagnostics/Performance panels hide entirely rather than sitting open empty.

## Audio diagnostics (glitch troubleshooting)

**Resolved (v0.10.1):** if you were seeing glitches that cleared up only at
blocksize ≥ 8192 regardless of latency mode, that pointed at GIL scheduling
contention rather than DSP cost (the diagnostics panel consistently showed
comfortable render headroom even at small blocksizes — the problem was the
audio callback occasionally not getting *scheduled* in time, not being slow
once it ran). Two fixes: the Helios DAC output had a naked `while: pass` spin
loop waiting for the DAC to report ready, which reacquires the GIL at very high
frequency and can starve other threads — it now sleeps briefly between polls
(with a timeout guard so a wedged DAC can't hang the render thread). And 8192
is now the shipped default blocksize, since it's the setting proven stable.
Smaller blocksizes remain selectable if you want lower audio latency and your
system tolerates it.

If audio is glitching, the **Audio diagnostics** panel is built to tell you
*where* the problem is instead of guessing. It's driven by ground truth from
PortAudio itself (via sounddevice's callback `status` flags), not estimates:

- **underruns** — hardware-reported xruns. **0 underruns but audible
  glitching** points to something *below* PromptWaver: OS audio scheduling,
  another app holding the device, or a sandboxed/virtual audio layer (e.g.
  PulseAudio routed through a snap/flatpak) adding jitter no amount of Python
  optimisation fixes.
- **max render / budget %** — how close our DSP came to missing its deadline.
  Consistently high here (with underruns) means the render genuinely needs a
  bigger blocksize or a lighter soundscape.
- **max interval** vs the expected callback interval — large gaps mean the
  callback itself was delayed before starting, independent of our render time;
  also points below PromptWaver.

Controls: pick an output **device** (rescan to refresh the list), a
**blocksize** (bigger = more buffer headroom, more latency before you hear a
change — try 4096 or 8192 if underruns show up at 2048), and **latency**
(`high` trades latency for stability; try it first). **Apply** reconfigures the
live stream — no restart needed — and rolls back automatically if the new
settings fail to open.

**Copy diagnostics** puts a compact JSON snapshot (config, live stats, device
list, last error) on the clipboard — paste that back for analysis rather than
transcribing numbers by hand.

## Connecting the API key (in-app)

Open the app and use the **Connection** panel: paste your key, **Save key**
(persists to a gitignored `settings.json` and exports it to the process), then
**Test connection** for a pass/fail using one tiny call. The `director` readout
flips to your model id when it's live. This is a convenience for local use — for
a public release, move the key to the OS keyring or an env-only flow.

A status dot next to **Connection** reflects the director's actual live state
(green "connected" / grey "offline") on every state broadcast — not just
right after you click Test — so a key that stops working mid-session (package
missing, network down, key revoked) is visible without re-testing.

## Adding a scene generator

Drop a file in `promptwaver/generators/`, subclass `Generator`, `@register` it,
and import it in `generators/__init__.py`:

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

## Scene spec format

```json
{
  "name": "water_flowing",
  "layers": [{"generator": "flow_field", "params": {"turbulence": 0.3, "speed": 0.18}}],
  "palette": ["#0a3d62", "#3c9dd0", "#c8f0ff"],
  "audio_patch": {"engine": "pad", "waveform": "triangle", "voices": 4,
                  "attack": 3.0, "release": 6.0, "base_note": 48, "chord": [0, 7, 12, 16]},
  "modulation": [
    {"source": "lfo_slow",    "dest": "visual.speed",      "depth": 0.05},
    {"source": "lfo_slow",    "dest": "audio.cutoff",      "depth": 400.0},
    {"source": "audio_level", "dest": "visual.turbulence", "depth": 0.4}
  ],
  "midi_overrides": {"voice.shimmer_lead.pan": 47}
}
```

`midi_overrides` is optional and empty for most scenes — see
[MIDI control](#midi-control). It only appears once **Pin MIDI map** has been
used on that scene.

## Cost control

The director makes at most **one structured call per new keyword** using a
low-cost model (Haiku-class) by default, and caches **successful Claude results**
to `scenes/generated/` (fallback scenes are never cached, so fixing your key
takes effect immediately). Between scene changes there is zero API traffic.

**Model & effort** are set in the UI (Generate panel) and persist in
`settings.json`. Model chooses the brain — Haiku (fast/cheap), Sonnet (better),
Opus (best); effort chooses how hard it works — low / medium / high scales the
token budget (4k / 8k / 14k) and asks for a simpler or richer scene (≈5 vs ≈13
objects). Bigger model + higher effort = better environments, more tokens. The
preset model IDs (`MODEL_PRESETS` in `director/claude_director.py`) change over
time — verify at <https://docs.claude.com/en/docs/about-claude/models>.

Every generation is **added to the library** automatically. Tweak the live
camera/config, then **Update scene from config** writes those settings back into
the loaded scene. Override the response ceiling with `PROMPTWAVER_MAX_TOKENS`.

**Naming**: give a scene an explicit name in the Generate panel and it's used as
the library title; leave it blank and Claude's own name (or the keyword) is used.

**Prompt detail**: a semi-detailed prompt beats a bare keyword. "swimming with
jellyfish" leaves count, scale, and colour to the model's default guess;
"exploring underwater with dozens of large jellyfish, long pink trailing
tentacles, shafts of light from above" gives Claude concrete things to place and
colour, so the composition is closer to what you pictured. The keyword field is
multiline for exactly this — write a sentence or two, not just a noun.

**Progress bar**: the Claude API has no notion of overall completion — it
doesn't know the final response length in advance, so there's no true
percentage. The bar is a proxy: a streaming call reports output as it's
generated, compared against the effort tier's token budget. It climbs steadily
during generation and lands on 100% at completion; treat it as "working, this
far into the budget" rather than an exact ETA.

The UI's director line reports the source of the last scene: *composed by Claude*,
*from cache*, or *local fallback* (with the reason).

## Roadmap

- **More shape ops**: revolve-with-caps, sweep-along-path, mirror/array helpers,
  bezier — widen what Claude can author cheaply.
- **Node-list readout in the UI**: show (and hand-tweak) what Claude placed.
- **Waterfall / cave / canyon** environments; richer camera paths (banking, look-at).
- **Shape-tween crossfade**: resample outgoing + incoming scenes to a common
  point budget and interpolate positions (currently dims/overlays instead).
- **BLE pads**: note-triggered scene recall (reuse your ESP32 HID pads) — the CC
  half of this now exists, see [MIDI control](#midi-control).
- **Output detail profiles**: laser and data projector want very different
  `camera.far` / `max_strokes`; currently changed by hand on every switch.
- **Key storage**: move `settings.json` key to OS keyring before public release.

## Output ratio

**Settings → Output → output ratio** (`1:1`, `4:3`, `16:10`, `16:9`, `21:9`)
sets the shape of the surface you're projecting onto. It's a rig setting like
keystone — stored in `settings.json`, not in any scene — so it survives scene
loads rather than being reverted by each one.

Widening it **reveals more at the sides** rather than cropping: `fov` stays the
*vertical* field of view, and the horizontal angle grows to fill the extra
width. Geometry keeps its proportions — a circle stays a circle.

It reaches all three outputs so they agree:

- **preview canvas** is reshaped to the ratio, keeping its pixel count roughly
  constant so a wide viewport doesn't quietly cost more to draw
- **output windows** letterbox the viewport into the window. Set the ratio to
  match your screen and the bars disappear; leaving it at `1:1` on a widescreen
  pillarboxes, which is correct rather than a bug
- **the laser** letterboxes into the galvos' square scan field, because they
  scan a square regardless. `1:1` is a laser's native shape — a wider ratio
  trades vertical scan range for the wider image

One thing this fixed on the way: `Camera.aspect` existed but was hardcoded to
1.0, and `_clip_and_project` *multiplied* by it. That's backwards — it would
have shown less world on a wider screen. Harmless while it was always 1.0
(both conventions agree there), which is how it sat unnoticed.

## About panel and display preferences

The **?** button beside the gear opens an About panel that renders
[`about.md`](about.md) from the project root. It's read per request rather than
cached at startup, so editing that file shows up on the next open with no
restart — it's a document, not a hot path. The renderer is a small markdown
subset (headings, lists, rules, inline code / bold / italic / links) that
escapes the source *before* adding any markup, since the result goes through
`innerHTML`.

**Settings → Display → hide slider values** clears the numeric readouts beside
every slider and knob, for a calmer surface on stage. On by default. It uses
`visibility` rather than `display`, so nothing reflows as it toggles, and each
value reappears while you hover or drag its control — hiding them outright
makes fine adjustment guesswork. The preference lives in `localStorage` rather
than `settings.json`, because it's a per-browser cosmetic choice and not
something that changes what the hardware does.

## Development

Opens straight into VSCode: `.vscode/settings.json` points the Python
interpreter at `.venv`, and `.vscode/launch.json` has F5-ready configs
("PromptWaver: web (no hardware)" is the fast one for UI/scene work — no audio
device or laser required). `.vscode/extensions.json` recommends Pylance and
Ruff; `pyproject.toml` holds Ruff/Black config (line length 100).

See [CHANGELOG.md](CHANGELOG.md) for version history. This repo isn't on
GitHub yet — when it is, this section is the place to add contribution/PR
notes.

MIT — see [LICENSE](LICENSE).
