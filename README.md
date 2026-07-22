# LaserFlow

![version](https://img.shields.io/badge/version-0.15.2-33e0d0)
![status](https://img.shields.io/badge/status-pre--release-orange)
![platform](https://img.shields.io/badge/platform-Ubuntu-informational)

> **Pre-release, active development.** Version stays 0.x until things settle;
> scene JSON shape and internal APIs may still change between minor versions.
> See [CHANGELOG.md](CHANGELOG.md) for what's landed.

A realtime **immersive audio/visual instrument** for the Helios Laser DAC — an
ambient scene sculptor in the spirit of *Fluid / Depth* (PS1, 1996): you steer
a living system rather than play to win. Procedural vector visuals are streamed
to a laser over ILDA, a polyphonic pad synth breathes underneath, and a shared
**modulation matrix** couples the two so light and sound move together.

Claude acts as an offline **scene director**: a keyword ("water flowing",
"aurora over a still lake") becomes a scene spec, which the local engine then
renders at full framerate with no further API calls. That keeps it cheap enough
to run for hours — the network is touched at most once per new keyword, and
results are cached.

Sibling project to *Laser! Laser Laser!* — same stack (vanilla Python + numpy,
`aiohttp` browser control surface, a thin ctypes Helios wrapper), same
`{type, key, value}` websocket protocol, keyed to **scenes** instead of patterns.

## ⚠️ Laser safety first

- Wear eyewear rated for **every** wavelength your unit emits.
- Bench-test with laser current at **minimum** for first light.
- Put a hardware **e-stop / key switch** on the laser supply — do not rely on
  ILDA interlock pins or software blanking.
- A blanking failure leaves a **stationary hot spot**, not a scanning pattern.
- Keystone/invert are applied to the DAC output only; the browser preview is a
  reference and is never distorted.

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
2. **Skip pyo** — LaserFlow's audio layer (`laserflow/audio/synth.py`) is a thin
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

Then either point LaserFlow at it directly:
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

- `laserflow/geometry.py` — `Path` (normalized polyline + colour), the unit everything speaks.
- `laserflow/modulation.py` — sources (LFO, ADSR `Envelope`, `Value`) + `ModMatrix` routing.
- `laserflow/generators/` — `flow_field`, `attractor`, `ripples`; `@register` to add more.
- `laserflow/scenes.py` — `SceneSpec`, live `Scene`, `SceneManager` (library + crossfade).
- `laserflow/director/` — `SceneDirector` (Claude + cache) and the local `fallback`.
- `laserflow/audio/` — `PadSynth` (pyo) and `AudioAnalysis` (sounddevice).
- `laserflow/output/` — `PathPlanner` + `HeliosOutput` / `NullOutput`.
- `laserflow/scene3d.py` — `Camera` + projection (near-clip, frame-clip, depth cueing) for 3D scenes.
- `laserflow/primitives.py` — ready-made low-poly primitive kit (planet, ring, jellyfish…).
- `laserflow/shapes.py` — the shape-grammar interpreter that expands Claude-authored geometry `defs`.
- `laserflow/settings.py` — local settings store (API key), gitignored.
- `laserflow/engine.py` — the realtime loop and thread-safe control surface.
- `laserflow/web/` — `aiohttp` server + single-page control UI.

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

## Prompt-composed 3D worlds (scene graph)

The richest scenes are **composed**, not procedural: Claude (or the local
fallback) emits a *scene graph* — a list of nodes that place low-poly
**primitives** in 3D space, each with a transform, colour, and motion. LaserFlow
instantiates the geometry and the camera floats through it. Claude never emits
raw meshes; it arranges a vetted kit, so every object stays clean and
budget-safe. Try *"floating in space"* or *"swimming in a coral reef with
jellyfish"*.

Primitive kit (`laserflow/primitives.py`): `planet`, `ring`, `ball`,
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
`sudo apt install libportaudio2`. Without it, LaserFlow runs silently and
everything else is unaffected.

Voices: `pad` (sustained drone chord), `pluck` (sparse scale notes), `noise`
(air/wind texture), `sub` (low drone). Global effects: tempo, master, soft
distortion (waveshaping), and a stereo delay (time/feedback/mix). The DSP core
(`laserflow/audio/dsp.py`) is separate from audio I/O so it can be rendered and
tested offline. The delay is fully vectorised (no per-sample Python loop) and
the output stream runs at a larger blocksize with high-latency buffering —
earlier builds glitched because a per-sample delay loop plus a small buffer left
the realtime audio callback fighting the GIL against the 45fps visual thread and
losing; the render is now ~1.6ms of work per ~46ms callback, comfortable
headroom.

The **Soundscape mixer** sits under the preview: master/tempo/distortion and
delay knobs up top, then a strip per voice with level, waveform, tone/rate, pan,
and mute. Every control updates the running synth live, and mirrors into the
scene — hit **Update scene from config** to save your mix back into the scene
JSON (`"soundscape"`), so it travels with the scene through the library.

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
previous working blocksize 8192") and LaserFlow falls back to *restoring*
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

## Master Start/Stop and Blank

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
buffer they'll accept independent of anything LaserFlow does; the ladder finds
the largest size that specific backend actually honours.

**Still landing on 4096 after v0.13.0's ladder fix?** At that point it's very
likely a genuine ceiling on your audio backend, not a LaserFlow bug — the
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
running). On load, LaserFlow sits idle: the laser is sent an explicit blanked
(zero-intensity) frame every tick, audio is muted at the DSP level, and the
scene clock is frozen — not stopped from zero, *frozen*, so Stop then Start
again resumes exactly where it left off rather than jumping the animation
forward by however long it was paused.

**Blank** is a separate, always-available safety action: it stops playback
(same as Stop) and sends the DAC a genuine zero-intensity frame on that same
tick — a real "beam off" command, not just skipping a write (which would leave
the last frame looping on the device's own buffer). Muting for audio is
non-destructive — it doesn't touch the scene's master/level settings, so
nothing needs re-tuning after Start.

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
  glitching** points to something *below* LaserFlow: OS audio scheduling,
  another app holding the device, or a sandboxed/virtual audio layer (e.g.
  PulseAudio routed through a snap/flatpak) adding jitter no amount of Python
  optimisation fixes.
- **max render / budget %** — how close our DSP came to missing its deadline.
  Consistently high here (with underruns) means the render genuinely needs a
  bigger blocksize or a lighter soundscape.
- **max interval** vs the expected callback interval — large gaps mean the
  callback itself was delayed before starting, independent of our render time;
  also points below LaserFlow.

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

## Adding a scene generator

Drop a file in `laserflow/generators/`, subclass `Generator`, `@register` it,
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
  ]
}
```

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
the loaded scene. Override the response ceiling with `LASERFLOW_MAX_TOKENS`.

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
- **MIDI + BLE pads**: route CC to matrix destinations (reuse your ESP32 HID pads).
- **Key storage**: move `settings.json` key to OS keyring before public release.

## Development

Opens straight into VSCode: `.vscode/settings.json` points the Python
interpreter at `.venv`, and `.vscode/launch.json` has F5-ready configs
("LaserFlow: web (no hardware)" is the fast one for UI/scene work — no audio
device or laser required). `.vscode/extensions.json` recommends Pylance and
Ruff; `pyproject.toml` holds Ruff/Black config (line length 100).

See [CHANGELOG.md](CHANGELOG.md) for version history. This repo isn't on
GitHub yet — when it is, this section is the place to add contribution/PR
notes.

MIT — see [LICENSE](LICENSE).
