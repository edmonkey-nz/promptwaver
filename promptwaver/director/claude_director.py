"""Claude as the *scene director*.

A keyword ("water flowing", "aurora over a still lake") becomes a SceneSpec.
This is the ONLY place PromptWaver touches the network, and it does so at most
once per new keyword: results are cached to disk, so a whole evening of
performance costs a handful of small calls. Between scene changes there is zero
API traffic — the local engine renders everything.

Cost control, by design:
  * one small structured call per *uncached* keyword
  * a low-cost model (Haiku-class) by default; override with PROMPTWAVER_MODEL
  * on-disk cache keyed by keyword (scenes/generated/)
  * graceful fallback to the local mapping if no key / no SDK / any error

Model IDs and pricing change — check the current low-cost model at
https://docs.claude.com/en/docs/about-claude/models before pinning one.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import time

from ..scenes import SceneSpec
from ..generators import available as available_generators
from ..shapes import available_ops
from ..primitives import available as available_primitives
from .. import settings
from .fallback import local_scene

_DEFAULT_MODEL = os.environ.get("PROMPTWAVER_MODEL", "claude-haiku-4-5")

_SYSTEM_3D_HEAD = """You are the scene director for PromptWaver, an ambient laser + synth
instrument that draws glowing wireframe VECTOR line-art (no fills, no shading —
just strokes on black). Given a keyword, return ONE JSON object describing a
calming, immersive 3D scene the viewer floats through. Output ONLY the JSON — no
prose, no markdown fences.

You AUTHOR the geometry yourself — do NOT rely on a fixed set of objects. In a
"defs" block, define each object in the scene as line-art built from these OPS
(each op is {"op": name, ...}):
  line     {"a":[x,y,z], "b":[x,y,z]}
  polyline {"pts":[[x,y,z],...], "closed":false}      // freeform escape hatch
  circle   {"r":1, "plane":"xy|xz|yz", "c":[x,y,z], "seg":18}
  rect     {"w":1, "h":1, "plane":"xy", "c":[x,y,z]}
  box      {"size":[w,h,d], "c":[x,y,z]}               // wireframe cuboid
  arc      {"r":1, "a0":0, "a1":3.14, "plane":"xy", "c":[x,y,z]}
  grid     {"w":4,"h":4,"nx":5,"ny":5,"plane":"xz","c":[x,y,z]}  // floors/walls
  lathe    {"profile":[[r,y],...], "seg":16, "meridians":4}  // revolve: jars, vases, lamps, planets

Building hints: furniture/architecture from box+rect+line; round/turned things
(jars, vases, lamps, bowls, planets) from lathe; organic/irregular things from
polyline. Def coordinates are LOCAL (centred on the object); the scene graph then
places each object.

Schema:
{
  "name": string,
  "layers": [{"generator":"world","params":{
      "defs": { "<object>": [ <ops...> ], ... },      // YOUR authored geometry
      "nodes": [
        {"shape":"<object>", "pos":[x,y,z], "scale":float, "color":[r,g,b],
         "motion":{"type":"spin|bob|drift|pulse|none","speed":float,"amp":float,"axis":"x|y|z"}}
      ]
  }}],
  "palette": ["#rrggbb", ...],
  "audio_patch": {"engine":"pad","waveform":"sine|triangle","voices":int,
                  "attack":float,"release":float,"base_note":int,"chord":[int,...]},
  "camera": {"mode":"orbit|drift|fly","target":[x,y,z],"orbit_radius":float,
             "far":float,"speed":float,"max_strokes":110,"depth":{"mode":"cull"}},
  "soundscape": {
     "tempo": 60, "master": 0.8, "distortion": 0.0,
     "delay": {"time":0.4, "feedback":0.35, "mix":0.3},
     "voices": [
       {"name":"drone","type":"pad","waveform":"sine|saw|square|triangle","note":36,
        "chord":[0,7,12],"level":0.5,"tone":0.4,"detune":0.01,"pan":0.0,
        "env":{"attack":3.0,"decay":1.2,"sustain":0.85,"release":2.5}},
       {"name":"lead","type":"osc","waveform":"saw","note":48,"chord":[0,7],
        "unison":3,"detune":0.015,"sub":0.2,"tone":0.55,"level":0.4,"pan":0.0,
        "lfo":{"on":true,"dest":"tone","shape":"sine","rate":0.05,"depth":0.5}},
       {"name":"bells","type":"pluck","waveform":"sine","note":72,
        "scale":[0,3,7,10],"level":0.3,"rate":0.5,"decay":1.4,"tone":0.7,"pan":0.2},
       {"name":"seq","type":"osc","waveform":"triangle","note":60,"chord":[0,4,7,11],
        "level":0.25,"arp":{"on":true,"mode":"up","rate":2.0,"decay":0.3},"pan":-0.2},
       {"name":"air","type":"noise","level":0.15,"tone":0.4,"pan":-0.1,
        "lfo":{"on":true,"dest":"pan","shape":"triangle","rate":0.03,"depth":0.6}}
     ]
  },
  "modulation": [{"source":"audio_level","dest":"camera.speed","depth":0.6}]
}
"""

# Shared by the 3D and 2D system prompts. Extracted rather than duplicated:
# it is the longest part of either prompt and the part most likely to be
# tuned, and two copies would drift apart silently — the symptom being that
# 2D scenes quietly stop getting the voice-ordering or LFO limits that 3D
# scenes get. Verified byte-identical to the pre-extraction prompt.
_SOUNDSCAPE_GUIDE = """
Soundscape guidance: ambient and calm. Voice types:
  "pad"   sustained drone chord, warmth from harmonic partials (params: chord, tone, detune)
  "osc"   unison multi-oscillator — thicker/simpler than pad, good for a lead or bass
          texture (params: chord, unison 1-7, detune, tone, sub 0-1 for an octave-down layer)
  "pluck" sparse notes stepping through a scale (params: scale, rate, decay, tone)
  "noise" wind/air texture (params: tone)

"tone" (0-1) is the brightness of EVERY voice type — it behaves like a filter
cutoff, so it is the main control over whether a soundscape sounds warm and
round or thin and buzzy. Use it deliberately:
  0.1-0.3   dark, mellow, felt more than heard — deep basses, soft pads
  0.4-0.6   warm but present — most sustained voices want to live here
  0.7-1.0   bright, edgy, cutting — leads and bell-like accents only
Default to the warm middle unless the scene really calls for edge. A whole
soundscape at 0.9 is the classic mistake: it sounds cheap and fatiguing.

For DEEP, HEAVY bass: an "osc" voice at note 24-36, tone 0.15-0.3, unison 2-3
with detune 0.005-0.01, and "sub": 0.4-0.8 for the octave below. Give it a
long attack (2-6s) so it swells rather than thuds.

Any voice can carry an LFO that modulates ONE of its own parameters:
"lfo": {"on":true, "dest":"level|pan|tone|detune|sub|waveform|rate",
        "shape":"sine|triangle|saw|square|random", "rate":<Hz>, "depth":0-1}
  dest "level"  tremolo — pulsing, breathing, heartbeat
       "pan"    auto-pan — movement across the stereo field
       "tone"   a slow filter sweep, the most useful one for ambient
       "detune" drifting chorus thickness
       "sub"    the weight underneath coming and going
       "waveform" steps between waveforms — abrupt, use sparingly
       "rate"   speeds a pluck/arp up and down
RATE: 0 to 0.5 Hz, and values outside that are clamped. Even 0.5 is brisk for
this instrument — the useful range is 0.02-0.15 Hz, one cycle every 7 to 50
seconds. Think "the room breathing", not "an effect pedal". Above about
0.2 Hz the tone/detune/sub/waveform/rate targets start to granulate, because
they are recalculated once per audio block; level and pan stay smooth
throughout.
USE LFOs SPARINGLY. Most scenes want ONE, and many want none — stillness is
what makes the one moving thing register. Hard limits:
  - never more than 2 voices in the whole soundscape, whatever the brief says
  - NEVER on the foundation/bass voice; the bottom has to stay planted
  - if you use 2, give them different targets AND unrelated rates, or they
    beat against each other and the result sounds mechanical
A brief asking for "evolving" or "never sitting still" is asking for slow
movement on one or two voices, not movement on all of them.
Any "pad" or "osc" voice can ARPEGGIATE instead of sustaining by adding
"arp": {"on":true, "mode":"up|down|updown|random", "rate":<notes/beat>, "decay":<seconds>}
— it then steps through that voice's "chord" one note at a time rather than
playing it as a block. Any "pad" or "osc" voice can also carry an optional
"env": {"attack","decay","sustain","release"} ADSR (seconds, sustain 0-1) —
attack/decay/sustain shape how it swells in and settles, release shapes how it
fades when muted (defaults are a slow ambient swell; shorten attack/release for
a more immediate feel). Use 2-4 voices; low tempo; gentle levels (they sum,
keep master headroom). Match key/mood to the scene.

VOICE ORDER MATTERS — always list "voices" in this fixed priority order:
  1. foundation: the lowest, most sustained voice (bass/sub/drone pad)
  2. body: mid-register pads and harmonic texture
  3. lead: whatever carries the melody or the most movement
  4. detail: plucks, sequences, arps
  5. air: noise/atmosphere beds
Omit any tier the scene doesn't need, but never reorder the ones you do use.
Hardware MIDI controllers bind knobs to voice POSITIONS, not names — the
names change with every scene, the positions must not — so a stable order is
what keeps one physical knob meaning "the low end" across every scene.
"""

_SYSTEM_3D_TAIL = """
Budget & feel: a laser draws only a few hundred strokes total, so keep the whole
scene to roughly 6-12 objects and each object simple (a chair is a few boxes and
lines, not a mesh). Recognizable silhouette beats detail. Spread objects across
positions about -8..8 so the camera can float among them. Gentle motion, long
attack/release, colours matched to the mood. r,g,b are 0..1.

REQUIREMENTS for every scene:
- Build a full ENVIRONMENT to navigate inside, not a flat pattern.
- Author geometry SPECIFIC to THIS keyword. Invent the objects that belong in it.
- Include a ground/floor or an enclosing boundary (walls, a shell) so it reads as
  a place, plus several distinct objects that identify the subject. Name that
  ground/floor/backdrop shape/node with "floor", "ground", "plane", or "grid"
  somewhere in its name (e.g. "floor", "cave_floor", "ocean_grid") — the viewer
  can toggle it off independently of the rest of the scene, and only matches on
  that naming, not on what the shape actually looks like.
- Place the camera inside/among the objects (orbit or drift), target near centre.
- NEVER reuse the objects from the example below — they are only to show format.

WORKED EXAMPLE (format only — for the keyword "a campfire at night"):
{"name":"a campfire at night",
 "layers":[{"generator":"world","params":{
   "defs":{
     "ground":[{"op":"grid","w":20,"h":20,"nx":8,"ny":8,"plane":"xz","c":[0,-1.5,0]}],
     "flame":[{"op":"polyline","pts":[[0,-1.5,0],[0.2,-0.6,0.1],[-0.1,0.1,-0.1],[0.05,0.8,0]]},
              {"op":"polyline","pts":[[0,-1.5,0],[-0.2,-0.5,0.1],[0.1,0.3,-0.1],[0,0.6,0.1]]}],
     "log":[{"op":"box","size":[1.4,0.3,0.3]}],
     "rock":[{"op":"polyline","pts":[[-0.4,0,0],[0,0.35,0.2],[0.4,0,0.1],[0.1,-0.2,-0.3],[-0.4,0,0]],"closed":true}],
     "tree":[{"op":"line","a":[0,-1.5,0],"b":[0,2.5,0]},{"op":"line","a":[0,1.4,0],"b":[-0.8,2.2,0]},{"op":"line","a":[0,1.7,0],"b":[0.9,2.6,0]}]
   },
   "nodes":[
     {"shape":"ground","pos":[0,0,0],"color":[0.2,0.3,0.4]},
     {"shape":"flame","pos":[0,0,0],"scale":1.2,"color":[1,0.6,0.2],"motion":{"type":"pulse","speed":3,"amp":0.3}},
     {"shape":"log","pos":[0,-1.3,0],"color":[0.8,0.5,0.3]},
     {"shape":"log","pos":[0.2,-1.3,0.3],"scale":1,"color":[0.8,0.5,0.3],"motion":{"type":"spin","speed":0,"axis":"y"}},
     {"shape":"rock","pos":[1.4,-1.2,0.5],"color":[0.6,0.6,0.7]},
     {"shape":"rock","pos":[-1.3,-1.2,-0.4],"color":[0.6,0.6,0.7]},
     {"shape":"tree","pos":[-5,0,-4],"scale":1.6,"color":[0.3,0.7,0.4]},
     {"shape":"tree","pos":[5,0,-5],"scale":1.8,"color":[0.3,0.7,0.4]}
   ]}}],
 "palette":["#0a0a14","#ff8830","#88b0d0"],
 "audio_patch":{"engine":"pad","waveform":"triangle","voices":4,"attack":3,"release":7,"base_note":43,"chord":[0,7,12,15]},
 "camera":{"mode":"orbit","target":[0,-0.5,0],"orbit_radius":7,"far":26,"speed":0.5,"max_strokes":120,"depth":{"mode":"cull"}},
 "modulation":[{"source":"audio_level","dest":"camera.speed","depth":0.6}]}

You MAY also drop in a ready-made primitive with {"primitive":name,"params":{..}}
instead of a def when one fits: %s. Prefer authoring defs for anything else.""" % (
    ", ".join(available_primitives()),)

_SYSTEM = _SYSTEM_3D_HEAD + _SOUNDSCAPE_GUIDE + _SYSTEM_3D_TAIL


# --- 2D pattern director -----------------------------------------------------
# A SEPARATE system prompt rather than a branch inside the 3D one, because the
# two give directly contradictory instructions: _SYSTEM requires "a full
# ENVIRONMENT to navigate inside, not a flat pattern", which is precisely what
# this one must produce. They share only the soundscape guidance.

_SYSTEM_2D_HEAD = """You are the pattern director for PromptWaver, an ambient laser +
synth instrument that draws glowing VECTOR line-art (no fills, no shading — just
strokes on black). Given a keyword, return ONE JSON object describing a FLAT 2D
pattern: a mandala, kaleidoscope, rosette, or neon lattice that fills the frame
and does not move through space. Output ONLY the JSON — no prose, no markdown fences.

There is NO CAMERA in a 2D scene. Do not emit a "camera" block. You compose
directly in normalized coordinates where x and y both run -1..1, (0,0) is the
centre, and anything beyond about 0.97 is clipped at the frame edge.

ANGLES ARE IN TURNS (0..1), never radians. A quarter turn is 0.25.

You author MOTIFS and then multiply them. This is the whole idea: never write
out fifty individual strokes. Write one arm, one chevron, one petal — then let
"repeat" and "symmetry" produce the rest. A good pattern is typically 3-6 nodes
of authored geometry that expand into 60-300 strokes.

"defs" maps a motif name to {"space": "cart"|"polar", "ops": [ ... ]}.

OPS (coordinates are LOCAL to the motif):
  line     {"a":[x,y], "b":[x,y]}
  polyline {"pts":[[x,y],...], "closed":false}
  circle   {"r":0.5, "c":[x,y], "seg":48}
  arc      {"r":0.5, "a0":0, "a1":0.25, "c":[x,y], "seg":32}   // a0/a1 in TURNS
  rect     {"w":0.4, "h":0.4, "c":[x,y]}
  ngon     {"n":6, "r":0.3, "c":[x,y], "rot":0}                // n=4 is a diamond
  star     {"n":5, "r1":0.4, "r2":0.18, "c":[x,y], "rot":0}
  grid     {"w":1.6, "h":1.6, "nx":5, "ny":5, "c":[x,y]}

SPACE — how that motif's own coordinates are read:
  "cart"   as authored. Straight lines stay straight.
  "polar"  each point is (radius, angle-in-turns). A straight line then becomes
           an ARC or a SPIRAL. Use it for petals, curved wedges, spiral arms —
           anything that should bend around the centre.

Each node then multiplies its motif:

"repeat" (one per node):
  {"kind":"offset","d":0.045,"n":4,"hue_step":0.06}   parallel copies — THE
        banded neon-tube look. d is the gap, n the number of lines in the band,
        hue_step rotates the colour a little per copy.
  {"kind":"scale","factor":1.5,"n":3,"hue_step":0.1}  concentric copies, each
        larger than the last — nested diamonds, rings, frames.
  {"kind":"radial","n":8,"hue_step":0.04}             n copies rotated about
        the centre — the mandala maker.
  {"kind":"ring","n":6,"r":0.55,"spin":true}          n copies placed around a
        circle of radius r.
  {"kind":"grid","nx":3,"ny":3,"step":[0.5,0.5]}      a lattice of copies.

"symmetry" (applied after repeat, folds the whole node):
  {"mirror":"x"}    left-right     {"mirror":"y"}   up-down
  {"mirror":"xy"}   both — 4-fold, the classic cross/star layout
  {"radial":8, "hue_step":0.03}   n-fold rotational symmetry

Node keys: "def", "at":[x,y] or "at_polar":[radius,turns], "scale", "rotate"
(turns), "color":[r,g,b] 0..1, "glow" 0..1, "closed", "repeat", "symmetry".

GLOW IS THE LOOK. These patterns read as neon tubing, and "glow" is what sells
it. Give most nodes 0.5-0.9, and push the focal shape to 1.0. A pattern with no
glow looks like a wireframe diagram.

Colour: give each node a distinct saturated "color", and use "hue_step" on its
repeat so a band of parallel lines runs through a small spectrum rather than
being flat. Deep blues/cyans/magentas/greens read best on black.
"""

_SYSTEM_2D_TAIL = """
Layer params (siblings of "defs"/"nodes", all plain numbers — these are the
LIVE-MODULATABLE controls, so always set them explicitly):
  "scale" 1.0     whole-pattern zoom
  "rotate" 0.0    whole-pattern spin, in turns
  "spread" 1.0    scales node PLACEMENT only, not node size
  "glow" 0.0      boost ADDED to every node's own glow
  "max_strokes" 420   hard ceiling; raise for dense patterns, lower for a laser

MODULATION IS REQUIRED. A static pattern is a poster, not an instrument. Always
return 3-4 routes so the pattern breathes and reacts.

SOURCES — all the audio ones read THIS SCENE'S OWN soundscape by default, so
the pattern reacts to the music it ships with:
  "audio_level"  overall loudness — the general-purpose one
  "synth_low"    low band (<250Hz) — the drone/sub weight, a slow heavy pulse
  "synth_mid"    mid band — pad and pluck body
  "synth_high"   high band (>2kHz) — bells and air, the twitchiest
  "synth_level"  same as audio_level, but pinned to the soundscape even if the
                 user switches the app over to a live microphone input
  "lfo_slow"     ~0.05Hz, "lfo_mid" ~0.2Hz — steady, independent of sound
Prefer the BANDS over plain audio_level where the destination suits one: they
are what make a pattern look played rather than merely pulsed.

DESTINATIONS:
  "visual.rotate"  slow continuous spin — the single most effective one
  "visual.scale"   breathing in and out
  "visual.glow"    brightness pumping
  "visual.spread"  the composition opening and closing

Example: [{"source":"lfo_slow","dest":"visual.rotate","depth":1.0},
          {"source":"audio_level","dest":"visual.glow","depth":0.6},
          {"source":"synth_low","dest":"visual.scale","depth":0.18},
          {"source":"synth_high","dest":"visual.spread","depth":0.25}]

Put the spin on an LFO and brightness/size/spread on the audio: rotation driven
by sound jitters, whereas brightness driven by sound is exactly right. Match
band to destination — low band to size (it thumps), high band to spread or glow
(it sparkles). DEPTHS MUST BE NON-ZERO, and 0.15-0.8 is the useful range; a
depth of 0 is a route that does nothing.

REQUIREMENTS for every 2D scene:
- Fill the frame. The composition should reach out to roughly 0.9 in at least
  one direction, and be centred on (0,0) unless the keyword says otherwise.
- Author motifs SPECIFIC to the keyword, then multiply them. Invent geometry
  that belongs to the subject.
- Use symmetry. 4-fold ("mirror":"xy") or 6/8/12-fold ("radial") is what makes
  these read as designed rather than scattered.
- Mix scales: a bold outer structure, a mid-layer, and a small dense centre.
- Set "glow" on nodes and return modulation routes. Both are required.
- NEVER reuse the motifs from the example below — it shows format only.

WORKED EXAMPLE (format only — for the keyword "neon temple"):
{"name":"neon temple",
 "layers":[{"generator":"pattern2d","params":{
   "defs":{
     "arm":    {"space":"cart","ops":[{"op":"line","a":[0.16,0.16],"b":[0.16,0.94]}]},
     "chevron":{"space":"cart","ops":[{"op":"polyline","pts":[[0.30,0.62],[0.52,0.84],[0.74,0.62]]}]},
     "core":   {"space":"cart","ops":[{"op":"ngon","n":4,"r":0.13}]},
     "petal":  {"space":"polar","ops":[{"op":"line","a":[0.22,0.0],"b":[0.60,0.06]}]}
   },
   "nodes":[
     {"def":"arm","color":[0.25,0.8,1.0],"glow":0.85,
      "repeat":{"kind":"offset","d":0.05,"n":4,"hue_step":0.05},
      "symmetry":{"mirror":"xy"}},
     {"def":"arm","rotate":0.25,"color":[0.35,0.6,1.0],"glow":0.85,
      "repeat":{"kind":"offset","d":0.05,"n":4,"hue_step":0.05},
      "symmetry":{"mirror":"xy"}},
     {"def":"chevron","color":[0.4,1.0,0.75],"glow":0.7,
      "repeat":{"kind":"offset","d":0.045,"n":3,"hue_step":0.07},
      "symmetry":{"mirror":"xy"}},
     {"def":"petal","color":[1.0,0.45,0.85],"glow":0.8,
      "repeat":{"kind":"offset","d":0.04,"n":2,"hue_step":0.05},
      "symmetry":{"radial":12,"hue_step":0.02}},
     {"def":"core","color":[0.8,0.35,1.0],"glow":1.0,"closed":true,
      "repeat":{"kind":"scale","factor":1.6,"n":3,"hue_step":0.09}}
   ],
   "scale":1.0,"rotate":0.0,"spread":1.0,"glow":0.0,"max_strokes":420
 }}],
 "palette":["#05060f","#33e0d0","#ff6fd8"],
 "modulation":[{"source":"lfo_slow","dest":"visual.rotate","depth":1.0},
               {"source":"synth_level","dest":"visual.glow","depth":0.6},
               {"source":"synth_low","dest":"visual.scale","depth":0.18},
               {"source":"synth_high","dest":"visual.spread","depth":0.25}]}
"""

_SYSTEM_2D = _SYSTEM_2D_HEAD + _SOUNDSCAPE_GUIDE + _SYSTEM_2D_TAIL

#: keyword -> the system prompt that authors that kind of scene.
SYSTEM_PROMPTS = {"3d": _SYSTEM, "2d": _SYSTEM_2D}


_SOUNDSCAPE_SCHEMA = """{
  "soundscape": {
    "tempo": 60, "master": 0.8, "distortion": 0.0,
    "delay": {"time":0.4, "feedback":0.35, "mix":0.3},
    "voices": [
      {"name":"drone","type":"pad","waveform":"sine|saw|square|triangle","note":36,
       "chord":[0,7,12],"level":0.5,"tone":0.4,"detune":0.01,"pan":0.0,
       "env":{"attack":3.0,"decay":1.2,"sustain":0.85,"release":2.5}},
      {"name":"lead","type":"osc","waveform":"saw","note":48,"chord":[0,7],
       "unison":3,"detune":0.015,"sub":0.2,"tone":0.55,"level":0.4,"pan":0.0,
        "lfo":{"on":true,"dest":"tone","shape":"sine","rate":0.05,"depth":0.5}},
      {"name":"bells","type":"pluck","waveform":"sine","note":72,
       "scale":[0,3,7,10],"level":0.3,"rate":0.5,"decay":1.4,"tone":0.7,"pan":0.2},
      {"name":"seq","type":"osc","waveform":"triangle","note":60,"chord":[0,4,7,11],
       "level":0.25,"arp":{"on":true,"mode":"up","rate":2.0,"decay":0.3},"pan":-0.2},
      {"name":"air","type":"noise","level":0.15,"tone":0.4,"pan":-0.1,
        "lfo":{"on":true,"dest":"pan","shape":"triangle","rate":0.03,"depth":0.6}}
    ]
  }
}

List "voices" in this fixed priority order: foundation (lowest/most sustained)
first, then body pads, then lead, then detail plucks/sequences, then noise/air.
Omit tiers the piece doesn't need, but never reorder the ones used — hardware
MIDI knobs bind to voice POSITIONS, and the names change with every scene."""

MODEL_PRESETS = {                       # friendly name -> API id (verify at docs)
    "haiku": "claude-haiku-4-5",
    "sonnet": "claude-sonnet-5",
    "opus": "claude-opus-4-8",
}

#: USD per MILLION tokens, (input, output), per model id. Used only to show
#: what a generation cost — nothing here affects a request.
MODEL_PRICES = {
    "claude-haiku-4-5": (1.00, 5.00),
    "claude-sonnet-5": (3.00, 15.00),
    "claude-opus-4-8": (5.00, 25.00),
    "claude-opus-5": (5.00, 25.00),
    "claude-fable-5": (10.00, 50.00),
}
#: Sonnet 5 is on introductory pricing until this date, after which it reverts
#: to the standard rate above. Dated rather than hardcoded either way, so the
#: figure stays right on both sides of the changeover without an edit.
_SONNET_INTRO = ("claude-sonnet-5", (2.00, 10.00), (2026, 8, 31))
_UNKNOWN_MODEL_PRICE = (5.00, 25.00)   # assume Opus-tier rather than under-report


def _price_for(model: str) -> tuple[float, float]:
    """(input, output) USD per million tokens for `model`."""
    name, intro_rate, until = _SONNET_INTRO
    if model == name:
        import datetime as _dt
        if _dt.date.today() <= _dt.date(*until):
            return intro_rate
    return MODEL_PRICES.get(model, _UNKNOWN_MODEL_PRICE)


def estimate_cost(model: str, usage) -> dict | None:
    """Cost of one API call, from the response's own usage block.

    Cache tokens are priced at their documented multipliers (writes 1.25x,
    reads 0.1x) even though this app's own cache is a local JSON file rather
    than prompt caching — if prompt caching is ever switched on, the figure
    stays right instead of silently drifting.
    """
    if usage is None:
        return None
    p_in, p_out = _price_for(model)
    tin = int(getattr(usage, "input_tokens", 0) or 0)
    tout = int(getattr(usage, "output_tokens", 0) or 0)
    twrite = int(getattr(usage, "cache_creation_input_tokens", 0) or 0)
    tread = int(getattr(usage, "cache_read_input_tokens", 0) or 0)
    usd = ((tin + twrite * 1.25 + tread * 0.1) * p_in + tout * p_out) / 1e6
    return {"model": model, "input_tokens": tin, "output_tokens": tout,
            "cache_read_tokens": tread, "cache_write_tokens": twrite,
            # True when the figures were reconstructed from an aborted stream
            # rather than read from the API's own usage block — the money was
            # still spent, but the token counts are inferred. See
            # `_EstimatedUsage`.
            "estimated": isinstance(usage, _EstimatedUsage),
            "usd": round(usd, 4)}


# --------------------------------------------------------------------------
# Estimating a generation BEFORE it is sent
#
# `estimate_cost` above reports what was actually billed, from the response's
# own usage block. Everything below is its counterpart for the decision you
# make first — how big a scene to ask for, and whether to send the request at
# all. Both read the same price table, so the "before" and "after" figures
# can't drift apart.
#
# This exists because of a measured failure: asked for a very large scene,
# Sonnet spent eight minutes and its entire 64,000-token budget without ever
# closing the JSON. The response was truncated, discarded, and billed in full.
# There is no way to get that money back after the fact, so the only place
# that failure can be stopped is before the request goes out.
# --------------------------------------------------------------------------

#: Hard ceiling on `max_tokens` for one generation. 64,000 is empirically
#: accepted by every model in MODEL_PRESETS — a Sonnet 5 run streamed all the
#: way to it — rather than a documented figure, so it is deliberately not
#: raised on a guess. The node slider can ask for more geometry than this can
#: carry; `estimate_generation` reports that as `fits: false` instead of
#: letting the request fail expensively.
MAX_OUTPUT_TOKENS = int(os.environ.get("PROMPTWAVER_MAX_TOKENS", "64000"))

#: Output tokens a generated scene costs, as `base + per_node * nodes`.
#: Calibrated against a measured run: Haiku 4.5 produced 197 nodes in 15,211
#: output tokens (the formula gives 14,926). The constant term is everything
#: that doesn't scale with node count — the `defs` library, soundscape, camera
#: and layer wrapper; the per-node term is one placement object of JSON.
#:
#: The per-node figure is what a model writes when left to its own devices,
#: including rotations and per-node colours it didn't have to spell out. A
#: prompt demanding minimal placements would land well under it, which would
#: raise the node ceiling — worth measuring, but not worth assuming here.
_TOKENS_BASE = 3500
_TOKENS_PER_NODE = 58

#: Slack between the estimate and the `max_tokens` actually requested. Models
#: overshoot their own node instruction (Haiku asked for 700-900 wrote 197;
#: Sonnet asked for the same wrote past 64,000 tokens), so a budget set to the
#: estimate exactly would truncate about as often as not. Unused headroom
#: costs nothing — output tokens are billed as generated, not as budgeted.
_BUDGET_HEADROOM = 1.35


def estimate_tokens(nodes: int) -> int:
    """Output tokens a `nodes`-node scene is expected to cost."""
    return int(_TOKENS_BASE + _TOKENS_PER_NODE * max(0, int(nodes)))


def token_budget(nodes: int, floor: int = 0) -> int:
    """`max_tokens` to request for a `nodes`-node scene, clamped to what the
    API will accept. `floor` keeps the effort tier's own budget when it is the
    larger of the two."""
    want = int(estimate_tokens(nodes) * _BUDGET_HEADROOM)
    return max(floor, min(MAX_OUTPUT_TOKENS, want))


def max_nodes_per_call() -> int:
    """Largest node count whose estimate still fits `MAX_OUTPUT_TOKENS` with
    headroom — the practical ceiling for a single generation. Above this a
    scene has to be reached some other way (several calls, or expanding
    placements locally from a small authored `defs` library) rather than by
    asking one call to write more JSON than it can emit."""
    room = MAX_OUTPUT_TOKENS / _BUDGET_HEADROOM - _TOKENS_BASE
    return max(1, int(room // _TOKENS_PER_NODE))


def estimate_input_tokens(kind: str = "3d") -> int:
    """The system prompt dominates input; ~4 chars a token is close enough for
    a figure whose job is to inform a slider."""
    return len(SYSTEM_PROMPTS.get(kind, _SYSTEM)) // 4 + 250


#: Refuse a generation whose *estimated* cost exceeds this, in USD. Sized
#: against the slider's own range so it bites where the risk is: at Haiku rates
#: the whole 100-1200 node span stays under it and never trips, Sonnet crosses
#: it near 1000 nodes, and Opus near 500. That ordering is the point — Sonnet
#: and Opus are the models that were measured spending a full token budget and
#: returning nothing usable. 0 disables.
DEFAULT_COST_CAP = 0.50

#: Abort a generation still streaming after this many seconds. The measured
#: runaway ran 491s before the API cut it off; nothing legitimate here takes
#: that long, and every second past the point of no return is billed. 0
#: disables. Keep the browser's own safety restore (index.html, `startGen`)
#: LONGER than this, so the server-side stop is the one that fires.
DEFAULT_GEN_TIMEOUT = 240.0


class _EstimatedUsage:
    """Stand-in for the API's usage block when a stream was aborted before its
    final message arrived. The tokens generated up to that point were billed,
    so reporting nothing would understate the cost of exactly the failure this
    is here to make visible. `output_tokens` is inferred from the text
    received at ~4 chars a token, and is flagged as an estimate by
    `last_cost["estimated"]` so the UI can say so.
    """

    def __init__(self, input_tokens: int, output_tokens: int):
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.cache_creation_input_tokens = 0
        self.cache_read_input_tokens = 0


def estimate_generation(model: str, nodes: int, kind: str = "3d") -> dict:
    """Predicted tokens and USD for one generation, before sending it."""
    p_in, p_out = _price_for(model)
    tin = estimate_input_tokens(kind)
    tout = estimate_tokens(nodes)
    return {"model": model, "nodes": int(nodes),
            "input_tokens": tin, "output_tokens": tout,
            "max_tokens": token_budget(nodes),
            "fits": tout * _BUDGET_HEADROOM <= MAX_OUTPUT_TOKENS,
            "max_nodes": max_nodes_per_call(),
            "usd": round((tin * p_in + tout * p_out) / 1e6, 4)}

# effort tier -> token budget + a richness directive appended to the prompt
EFFORT = {
    "low":  {"max_tokens": 4000,
             "hint": "Keep it simple and clean: about 5-6 objects."},
    "med":  {"max_tokens": 8000,
             "hint": "A full scene: about 8-10 distinct objects with supporting detail."},
    "high": {"max_tokens": 14000,
             "hint": "Rich and layered: about 11-14 distinct objects, careful spatial "
                     "composition, foreground and background depth, and subtle motion. "
                     "Add small secondary details that sell the place."},
}

# scene size -> spatial-scale directive. "small" is the historical default (no
# hint needed — it's what the system prompt's own baseline ranges already
# produce); medium/large ask for a physically bigger world to fly through,
# independent of effort (which controls object *count/detail*, not distance).
SCENE_SIZE = {
    "small": None,
    "medium": ("Scale: expansive, not intimate. Spread object placement over "
               "roughly -14..14 on each axis (wider than the usual -8..8), and "
               "size the camera accordingly: orbit/drift radius 10-16, far "
               "plane 30-45. The world should feel noticeably bigger to fly "
               "through, with more open space between features."),
    "large": ("Scale: vast. Spread object placement over roughly -22..22 on "
              "each axis, and size the camera accordingly: orbit/drift radius "
              "16-26, far plane 45-70. A sprawling environment with real "
              "travel distance between features — err on the side of more "
              "empty space and fewer, more spread-out landmarks rather than "
              "cramming more objects into the same small volume."),
    # Unlike the tiers above, this one raises object COUNT as well as extent,
    # and pairs the layout with a travelling camera. Those two go together:
    # a big world with few objects is the worst case for a laser (measured —
    # a drifting camera over route-shaped geometry got 1 stroke a frame and
    # 89% dark frames), and route-following geometry only reads from a camera
    # that follows the same route.
    "massive": (
        "Scale: MASSIVE — a place to travel through for minutes, not a tableau.\n"
        "This OVERRIDES the object-count guidance above: author 120-220 nodes, "
        "not 6-14.\n\n"
        "Compose it as a long CLOSED ROUTE through the environment, and place "
        "the geometry ALONG that route rather than scattered through a volume. "
        "Spread it over roughly -40..40 on each axis.\n\n"
        "Keep the node count affordable by REUSING geometry: author 6-10 named "
        "shapes in \"defs\" and instance them many times at varied positions, "
        "scales, rotations and colours. Author 8 shapes and place them 200 "
        "times — never 200 distinct shapes.\n\n"
        "Set the camera to travel that same route:\n"
        "  \"camera\": {\"mode\":\"path\",\n"
        "              \"waypoints\": [[x,y,z], ... 8-14 points forming a loop "
        "that comes back around to where it started, so it can be walked "
        "indefinitely. Do NOT repeat the first point at the end — the loop is "
        "closed automatically],\n"
        "              \"speed\":0.5, \"fov\":62, \"near\":0.4, \"far\":40,\n"
        "              \"max_strokes\":120, \"depth\":{\"mode\":\"cull\"}}\n\n"
        "Keep objects within about 3-8 units either side of the route, on both "
        "sides and overhead, so something is always in frame as the camera "
        "moves. A stretch of route with nothing beside it is a dark laser."),
}

# Token floor per size. `massive` asks for an order of magnitude more nodes
# than the other tiers, and a node is ~24 tokens of JSON — 200 of them plus a
# def library, soundscape and camera does not fit in the effort tier's normal
# budget, and overflowing it means a truncated response and a silent fall back
# to the local director. Unused headroom costs nothing but latency.
SIZE_MIN_TOKENS = {"massive": 32000}


def _size_hint(nodes: int) -> str:
    """The `massive` directive above, parameterised by an explicit node count.

    The tiers in SCENE_SIZE are four fixed points; this is the same shape of
    instruction as `massive` — a closed route with geometry along it and a
    camera that walks it — with the count, extent and camera scaled to a
    number the UI slider supplies. Everything derives from `nodes`, so there
    is one control rather than a count and an extent that can disagree.

    Extent grows as sqrt(nodes) so DENSITY stays roughly constant: a bigger
    world with the same geometry crammed into it would just be the same scene
    viewed from further away, and a bigger world with the spacing unchanged
    would be mostly empty — which measured worst of all for a laser (a
    drifting camera over sparse route geometry got 1 stroke a frame and 89%
    dark frames). The constant is set so ~170 nodes reproduces the +-40 the
    hand-written `massive` tier asked for.
    """
    nodes = max(1, int(nodes))
    extent = int(round(20 * math.sqrt(nodes / 40.0)))
    lo, hi = int(nodes * 0.9), int(nodes * 1.1)
    # More shapes for a bigger world (repetition shows up over minutes of
    # walking), but sub-linearly: the whole reason a big scene is affordable
    # at all is that placements are cheap and distinct shapes are not.
    ndefs = max(6, min(14, 6 + nodes // 130))
    each = max(4, round(nodes / ndefs))
    waypoints = max(8, min(24, 8 + nodes // 90))
    return (
        f"Scale: MASSIVE — a place to travel through for minutes, not a tableau.\n"
        f"This OVERRIDES the object-count guidance above: author {lo}-{hi} nodes, "
        f"not 6-14. That count is deliberate and it is the point of this scene — "
        f"do not stop early or trail off.\n\n"
        f"Compose it as a long CLOSED ROUTE through the environment, and place "
        f"the geometry ALONG that route rather than scattered through a volume. "
        f"Spread it over roughly -{extent}..{extent} on each axis.\n\n"
        f"Reach that node count by INSTANCING, never by authoring more shapes: "
        f"author {ndefs} named shapes in \"defs\" and place each of them about "
        f"{each} times at varied positions, scales, rotations and colours. "
        f"{ndefs} shapes placed {each} times each — never {nodes} distinct shapes.\n\n"
        f"Set the camera to travel that same route:\n"
        f"  \"camera\": {{\"mode\":\"path\",\n"
        f"              \"waypoints\": [[x,y,z], ... {waypoints} points forming a loop "
        f"that comes back around to where it started, so it can be walked "
        f"indefinitely. Do NOT repeat the first point at the end — the loop is "
        f"closed automatically],\n"
        f"              \"speed\":0.5, \"fov\":62, \"near\":0.4, \"far\":40,\n"
        f"              \"max_strokes\":120, \"depth\":{{\"mode\":\"cull\"}}}}\n\n"
        f"Keep objects within about 3-8 units either side of the route, on both "
        f"sides and overhead, so something is always in frame as the camera "
        f"moves. A stretch of route with nothing beside it is a dark laser.")


def _resolve_size(size) -> tuple[int | None, str | None]:
    """`size` -> (node target, prompt directive).

    Accepts either a node count (what the UI slider sends) or one of the
    legacy SCENE_SIZE keys. The strings have to keep working: every scene
    generated before the slider existed carries one in
    `generation_settings.size`, and regenerating from that panel replays it.
    A string size has no node target, so it keeps the old fixed token floor.
    """
    if isinstance(size, bool):
        return None, None
    if isinstance(size, (int, float)):
        n = int(size)
        return (n, _size_hint(n)) if n > 0 else (None, None)
    s = str(size).strip()
    if s.isdigit():
        n = int(s)
        return (n, _size_hint(n)) if n > 0 else (None, None)
    return None, SCENE_SIZE.get(s)

# "Character" sliders (Generate modal), 0..1 centered at 0.5. warmth/energy
# are soft prompt hints — like EFFORT/SCENE_SIZE, they bias choices Claude
# has to make itself (voice type, tempo), so there's no way to force them
# after the fact; a near-center value adds no hint at all, matching the
# pre-slider baseline exactly. "evolution" is different — see
# _apply_evolution below, which sets it deterministically after the response
# comes back, so it works regardless of whether Claude "listened".
def _character_hints(warmth: float | None, energy: float | None) -> str:
    lines = []
    if warmth is not None:
        if warmth >= 0.7:
            lines.append(
                "Tone: warm and mellow. Favour \"pad\"/\"sub\"/\"osc\" voices over "
                "\"pluck\" — at most one sparse pluck/bell accent, the rest "
                "sustained. Set \"tone\" 0.15-0.4 on EVERY voice (it works on all "
                "of them and is the difference between a round analogue warmth and "
                "a thin digital buzz), and a longer attack (4-10s) for a slow build "
                "rather than an immediately-present sound. Put real weight "
                "underneath: a low \"osc\" or \"sub\" at note 24-36 with \"sub\" "
                "0.4-0.8 mixed in.")
        elif warmth <= 0.3:
            lines.append(
                "Tone: bright and crisp. Pluck/arp textures and higher tone values "
                "(0.6-0.9) are welcome — present and percussive rather than soft. "
                "Keep at least the lowest voice below 0.5 so the bottom end still "
                "has body rather than buzzing.")
    if energy is not None:
        if energy >= 0.7:
            lines.append(
                "Energy: lively. Tempo 90-140, more movement (arpeggios, faster "
                "pluck rate), a busier texture with more simultaneous events.")
        elif energy <= 0.3:
            lines.append(
                "Energy: calm and spacious. Tempo 40-70, sparse — few simultaneous "
                "events, generous space between them.")
    return " ".join(lines)


def _apply_evolution(spec: SceneSpec, evolution: float | None) -> SceneSpec:
    """Deterministically sets swell_amount (the slow per-voice "orchestration"
    modulation in dsp.py) from the "evolution" character slider. Applied
    after the fact rather than as a prompt hint, so — unlike warmth/energy —
    it's a guarantee: every generation actually breathes over time by roughly
    the amount asked for, regardless of whether Claude's own response
    included (or would have honoured) any particular swell_amount."""
    if evolution is None or not spec.soundscape:
        return spec
    amount = max(0.0, min(1.0, float(evolution))) * 0.6   # capped so it's never overwhelming
    spec.soundscape["swell_amount"] = round(amount, 3)
    return spec


class SceneDirector:
    def __init__(self, cache_dir: str, model: str | None = None):
        self.cache_dir = cache_dir
        os.makedirs(cache_dir, exist_ok=True)
        # Two guards against an expensive runaway, both aimed at the same
        # measured failure: a model that keeps writing past its budget, is
        # truncated, and is billed in full for output that gets discarded.
        # `cost_cap` stops it before the request goes out (free); `timeout`
        # stops it mid-stream (refunds nothing already generated, but closing
        # the connection does stop the meter). 0 disables either.
        self.cost_cap = float(settings.get("cost_cap", DEFAULT_COST_CAP))
        self.timeout = float(settings.get("gen_timeout", DEFAULT_GEN_TIMEOUT))
        self.last_source = None       # "claude" | "cache" | "fallback"
        self.last_error = None
        self.last_progress = 0.0      # 0..1, approximate — see _from_claude
        self.generating = False
        # Cost of the last billed API call: None until one happens, and reset
        # to None on a cache hit or fallback so the UI can't show a stale
        # figure next to a generation that cost nothing.
        self.last_cost = None
        self._last_usage = None
        self._offline_reason = None   # why the client is unavailable, set by _make_client
        # model + effort persist across restarts via settings.json
        self.model_choice = model or settings.get("model", "haiku")
        self.model = MODEL_PRESETS.get(self.model_choice, self.model_choice)
        self.effort = settings.get("effort", "med")
        self.max_pps = int(settings.get("max_pps", 20000))
        settings.apply_env()          # pull a stored key into the env if present
        self._client = self._make_client()

    def set_max_pps(self, value: int):
        self.max_pps = max(1000, int(value))
        settings.set("max_pps", self.max_pps)

    def set_model(self, choice: str):
        self.model_choice = choice
        self.model = MODEL_PRESETS.get(choice, choice)
        settings.set("model", choice)

    def set_effort(self, effort: str):
        if effort in EFFORT:
            self.effort = effort
            settings.set("effort", effort)

    def _make_client(self):
        if not os.environ.get("ANTHROPIC_API_KEY"):
            self._offline_reason = "no API key set"
            return None
        try:
            import anthropic
        except ImportError:
            self._offline_reason = "anthropic package not installed (pip install anthropic)"
            return None
        try:
            client = anthropic.Anthropic()
        except Exception as e:
            self._offline_reason = f"client init failed: {_friendly_error(e)}"
            return None
        self._offline_reason = None
        return client

    def reload(self):
        """Rebuild the client, e.g. after the key changes."""
        settings.apply_env()
        self._client = self._make_client()

    def set_api_key(self, key: str):
        """Persist a key locally and re-init the client."""
        key = (key or "").strip()
        settings.set("anthropic_api_key", key)
        os.environ["ANTHROPIC_API_KEY"] = key
        self._client = self._make_client()

    def test(self) -> dict:
        """One tiny call to confirm the key + package work. Returns
        {"ok": bool, "detail": str} — safe to surface straight to the UI."""
        if not os.environ.get("ANTHROPIC_API_KEY"):
            return {"ok": False, "detail": "no API key set"}
        client = self._client
        if client is None:
            try:
                import anthropic
            except Exception:
                return {"ok": False, "detail": "anthropic package not installed (pip install anthropic)"}
            try:
                client = anthropic.Anthropic()
            except Exception as e:
                return {"ok": False, "detail": _friendly_error(e)}
        try:
            client.messages.create(
                model=self.model, max_tokens=1,
                messages=[{"role": "user", "content": "ping"}],
            )
            self._client = client
            return {"ok": True, "detail": f"connected \u00b7 model {self.model}"}
        except Exception as e:
            return {"ok": False, "detail": _friendly_error(e)}

    @property
    def online(self) -> bool:
        return self._client is not None

    def _cache_path(self, keyword: str) -> str:
        # "g2_" prefix invalidates any older cache that may hold fallback scenes
        h = hashlib.sha1(keyword.strip().lower().encode()).hexdigest()[:12]
        return os.path.join(self.cache_dir, f"g2_{h}.json")

    def generate(self, keyword: str, use_cache: bool = True, audio: str | None = None,
                 size: str | int = "small", warmth: float | None = None,
                 energy: float | None = None,
                 evolution: float | None = None, kind: str = "3d") -> SceneSpec:
        # `size` is either a node count (the UI slider) or a legacy tier name
        # carried by scenes generated before the slider existed. Anything else
        # falls back to the historical default rather than erroring.
        if not (isinstance(size, int) and not isinstance(size, bool)) and \
                not str(size).strip().isdigit() and size not in SCENE_SIZE:
            size = "small"
        kind = kind if kind in SYSTEM_PROMPTS else "3d"
        # `kind` is part of the cache key: the same keyword legitimately has a
        # 3D and a 2D answer, and they must not collide.
        cache = self._cache_path(keyword + "|" + (audio or "") + "|" + str(size) +
                                 f"|w{warmth}|e{energy}|v{evolution}|k{kind}")
        if use_cache and os.path.exists(cache):
            with open(cache) as f:
                self.last_source = "cache"
                self.last_error = None
                self.last_cost = None      # served from disk; nothing was billed
                self.last_progress = 1.0
                return _ensure_soundscape(SceneSpec.from_dict(json.load(f)))

        self.generating = True
        self.last_progress = 0.0
        self.last_cost = None
        try:
            if self.online:
                spec, ok = self._from_claude(keyword, audio, size, warmth, energy, kind)
                if ok:
                    self.last_source = "claude"
                    self.last_error = None
                    spec = _apply_evolution(_ensure_soundscape(spec), evolution)
                    with open(cache, "w") as f:      # only cache genuine Claude output
                        json.dump(spec.to_dict(), f, indent=2)
                    return spec
                # Claude failed — fall back but do NOT cache, so a retry/fix takes effect
                self.last_source = "fallback"
                return _apply_evolution(_ensure_soundscape(local_scene(keyword, kind)), evolution)

            self.last_source = "fallback"
            self.last_error = (self._offline_reason or "no API key") + " — using local fallback"
            return _apply_evolution(_ensure_soundscape(local_scene(keyword, kind)), evolution)
        finally:
            self.last_progress = 1.0
            self.generating = False

    def generate_audio(self, context: str, audio_prompt: str, use_cache: bool = True,
                       warmth: float | None = None, energy: float | None = None,
                       evolution: float | None = None) -> dict:
        """Generate (or fetch from cache) just a soundscape for an existing
        scene — cheaper and faster than a full scene regeneration."""
        cache = self._cache_path("audio|" + context + "|" + audio_prompt +
                                 f"|w{warmth}|e{energy}|v{evolution}")
        if use_cache and os.path.exists(cache):
            with open(cache) as f:
                self.last_source = "cache"
                self.last_error = None
                self.last_cost = None      # served from disk; nothing was billed
                self.last_progress = 1.0
                return json.load(f)

        self.generating = True
        self.last_progress = 0.0
        self.last_cost = None
        try:
            if not self.online:
                self.last_source = "fallback"
                self.last_error = (self._offline_reason or "no API key") + " — using local fallback"
                from ..audio import default_soundscape
                return default_soundscape()
            tier = EFFORT.get(self.effort, EFFORT["med"])
            character_hint = _character_hints(warmth, energy)
            character_line = f"\n\n{character_hint}" if character_hint else ""
            content = (f"The laser scene is: \"{context}\". Compose ONLY an ambient "
                      f"soundscape for it, matching this brief: \"{audio_prompt}\". "
                      f"Output ONLY a JSON object with this exact shape, no prose, no "
                      f"markdown fences:\n{_SOUNDSCAPE_SCHEMA}\n\n{tier['hint']}" + character_line)
            budget_chars = min(tier["max_tokens"], 3000) * 4
            text, stop_reason = self._stream_or_call(content, budget_chars)
            if stop_reason == "max_tokens":
                self.last_error = "response truncated"
                self.last_source = "fallback"
                from ..audio import default_soundscape
                return default_soundscape()
            data = json.loads(_extract_json(text))
            scape = data.get("soundscape", data)   # tolerate either shape
            if not scape.get("voices"):
                raise ValueError("no voices in response")
            self.last_cost = estimate_cost(self.model, self._last_usage)
            # Same ceiling the full-scene path gets via _ensure_soundscape —
            # this route returns a bare dict, so it has to be applied here too
            # (and BEFORE the cache write, or a trimmed scene would come back
            # untrimmed on the next load).
            _limit_lfos(scape)
            if evolution is not None:
                scape["swell_amount"] = round(max(0.0, min(1.0, float(evolution))) * 0.6, 3)
            self.last_source = "claude"
            self.last_error = None
            self.last_progress = 1.0
            with open(cache, "w") as f:
                json.dump(scape, f, indent=2)
            return scape
        except Exception as e:
            self.last_error = _friendly_error(e)
            self.last_source = "fallback"
            print(f"[promptwaver] audio-only generation failed: {self.last_error}")
            from ..audio import default_soundscape
            return default_soundscape()
        finally:
            self.last_progress = 1.0
            self.generating = False

    def _from_claude(self, keyword: str, audio: str | None = None, size: str | int = "small",
                     warmth: float | None = None, energy: float | None = None,
                     kind: str = "3d"):
        """Return (spec, ok). ok=False means fall back (reason in last_error)."""
        system = SYSTEM_PROMPTS.get(kind, _SYSTEM)
        tier = EFFORT.get(self.effort, EFFORT["med"])
        audio_line = ""
        if audio:
            audio_line = (f"\n\nAudio: also compose a matching ambient SOUNDSCAPE for "
                          f"this brief: \"{audio}\". Return it in the \"soundscape\" field.")
        else:
            audio_line = ("\n\nAudio: also compose a calm ambient soundscape that fits the "
                          "scene, in the \"soundscape\" field.")
        nodes, size_hint = _resolve_size(size)
        size_line = f"\n\n{size_hint}" if size_hint else ""
        character_hint = _character_hints(warmth, energy)
        character_line = f"\n\n{character_hint}" if character_hint else ""
        # The 3D hint counts OBJECTS; for a flat pattern the equivalent budget
        # is total strokes after repeat/symmetry expansion, which is a
        # different quantity, so the effort hint is dropped rather than
        # mistranslated. A 2D pattern is also display-first (per-shape glow
        # can't reach a laser at all), so it gets a stroke ceiling instead of
        # the PPS lecture.
        if kind == "2d":
            budget_line = (f"Budget: keep the expanded pattern under about "
                           f"{max(200, min(900, self.max_pps // 40))} strokes and set "
                           f"\"max_strokes\" to match. Remember a repeat crossed with a "
                           f"symmetry multiplies fast: 4 offsets x 12-fold radial is 48 "
                           f"strokes from ONE authored line.")
            effort_line = ""
        else:
            budget_line = (f"Hardware constraint: the laser draws at a maximum of "
                           f"{self.max_pps} points per second. Keep total stroke length "
                           f"and object count efficient for this budget — favour fewer, "
                           f"cleaner strokes over dense detail.")
            effort_line = f"Effort: {self.effort}. {tier['hint']}\n\n"
        content = (f"Design a scene for the keyword: {keyword}\n\n"
                   + effort_line + budget_line + size_line + character_line + audio_line)
        # Rough proxy for "percent complete": the API has no notion of overall
        # completion (it doesn't know the final length in advance), but a
        # streaming call reports tokens as they're generated, which we compare
        # against this effort tier's token budget to drive an approximate bar.
        # A node count budgets from the geometry actually asked for; a legacy
        # tier name has no count, so it keeps its old fixed floor.
        if nodes:
            max_tokens = token_budget(nodes, floor=tier["max_tokens"])
        else:
            max_tokens = max(tier["max_tokens"], SIZE_MIN_TOKENS.get(str(size), 0))
        budget_chars = max_tokens * 4

        # The cost gate, before anything is sent. This is the only point at
        # which an over-budget generation costs nothing to refuse — once the
        # request is out, a response that overruns and gets truncated is
        # billed in full for output that is then thrown away.
        if nodes and self.cost_cap:
            est = estimate_generation(self.model, nodes, kind)
            if est["usd"] > self.cost_cap:
                self.last_error = (
                    f"estimated ${est['usd']:.2f} for {nodes} nodes exceeds the "
                    f"${self.cost_cap:.2f} cap — lower the node count, switch to a "
                    f"cheaper model, or raise the cap in the Generate panel")
                print(f"[promptwaver] director: {self.last_error}")
                return None, False

        try:
            text, stop_reason = self._stream_or_call(content, budget_chars, system)
            # Record the cost BEFORE judging whether the response is usable.
            # Truncated and aborted responses are billed for everything they
            # did generate, and those are the expensive ones — an earlier
            # version returned above this line, so the only generations that
            # reported no cost were the ones that had cost the most.
            self.last_cost = estimate_cost(self.model, self._last_usage)
            if stop_reason == "timeout":
                self.last_error = (f"generation aborted after {self.timeout:.0f}s — the model "
                                   f"was still writing. Lower the node count or the effort")
                print(f"[promptwaver] director: {self.last_error}")
                return None, False
            if stop_reason == "max_tokens":
                self.last_error = ("response truncated — lower the node count, or raise "
                                   f"PROMPTWAVER_MAX_TOKENS (budget {max_tokens})")
                print(f"[promptwaver] director: {self.last_error}")
                return None, False
            data = json.loads(_extract_json(text))
            spec = SceneSpec.from_dict(data)
            if not spec.layers:
                raise ValueError("model returned no layers")
            self.last_progress = 1.0
            return spec, True
        except Exception as e:
            self.last_error = _friendly_error(e)
            print(f"[promptwaver] director generation failed: {self.last_error}")
            return None, False

    def _stream_or_call(self, content: str, budget_chars: int, system: str | None = None):
        """Stream the response (updating self.last_progress as text arrives) if
        the SDK supports it; otherwise fall back to a plain blocking call with no
        progress signal. Returns (text, stop_reason).

        `system` defaults to the 3D prompt, which is what the audio-only call
        has always used — its soundscape guidance is the part that call needs.
        """
        system = system or _SYSTEM
        # Usage rides back on an attribute rather than a third return value:
        # both call sites already unpack a 2-tuple, and only one of them
        # reports cost.
        self._last_usage = None
        tier_tokens = max(1, budget_chars // 4)
        stream_fn = getattr(self._client.messages, "stream", None)
        if stream_fn is None:
            msg = self._client.messages.create(
                model=self.model, max_tokens=tier_tokens,
                system=system, messages=[{"role": "user", "content": content}])
            text = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
            self._last_usage = getattr(msg, "usage", None)
            return text, getattr(msg, "stop_reason", None)

        acc_len = 0
        parts: list[str] = []
        t0 = time.monotonic()
        timed_out = False
        with stream_fn(model=self.model, max_tokens=tier_tokens,
                       system=system, messages=[{"role": "user", "content": content}]) as stream:
            for chunk in stream.text_stream:
                parts.append(chunk)
                acc_len += len(chunk)
                self.last_progress = min(0.95, acc_len / max(budget_chars, 1))
                if self.timeout and time.monotonic() - t0 > self.timeout:
                    # Leaving the `with` block closes the connection, which
                    # stops generation — tokens not yet produced are never
                    # billed. Waiting politely for a doomed response to finish
                    # is what made the measured runaway cost what it did.
                    timed_out = True
                    break
            if timed_out:
                # get_final_message() would block for the rest of the response
                # we just decided not to pay for, so the usage block is
                # reconstructed instead of read.
                self._last_usage = _EstimatedUsage(
                    input_tokens=len(system) // 4 + len(content) // 4,
                    output_tokens=acc_len // 4)
                print(f"[promptwaver] director: aborted stream after {self.timeout:.0f}s "
                      f"(~{acc_len // 4} output tokens generated)")
                return "".join(parts), "timeout"
            final = stream.get_final_message()
        text = "".join(b.text for b in final.content if getattr(b, "type", "") == "text")
        self._last_usage = getattr(final, "usage", None)
        return text, getattr(final, "stop_reason", None)

    def set_cost_cap(self, usd: float):
        self.cost_cap = max(0.0, float(usd))
        settings.set("cost_cap", self.cost_cap)

    def set_timeout(self, seconds: float):
        self.timeout = max(0.0, float(seconds))
        settings.set("gen_timeout", self.timeout)

    def estimate(self, nodes: int, kind: str = "3d") -> dict:
        """What a `nodes`-node generation would cost on the current model, plus
        whether it would be allowed. The UI reads this rather than carrying its
        own copy of the price table — one source of truth for a number the user
        is about to spend money on."""
        est = estimate_generation(self.model, nodes, kind)
        est["cost_cap"] = self.cost_cap
        est["over_cap"] = bool(self.cost_cap and est["usd"] > self.cost_cap)
        return est


#: Ceiling on how many voices in one soundscape may carry an LFO. The prompt
#: asks for at most two, and mostly gets it — but a brief that leans hard on
#: movement ("evolving", "never sits still") pushes the model past its own
#: instruction, and a soundscape where everything is breathing at once reads
#: as seasick rather than alive. Enforced here so the ceiling is a fact rather
#: than a request. Deliberately one higher than the prompt asks for: this is a
#: backstop against runaway, not a second opinion on musical judgement.
MAX_LFO_VOICES = 3


def _limit_lfos(scape: dict) -> dict:
    """Clear LFOs beyond what a soundscape can carry without turning to soup.

    Two rules, both about keeping something still to hear the movement
    against. The foundation voice never modulates — the bottom of a mix is
    what everything else is measured from, and a wandering bass makes the
    whole thing feel unmoored. Beyond that, later (less prominent) voices lose
    theirs first, so what survives is the movement you're most likely to
    notice.
    """
    voices = (scape or {}).get("voices") or []
    kept = 0
    for i, v in enumerate(voices):
        lfo = v.get("lfo")
        if not isinstance(lfo, dict) or not lfo.get("on"):
            continue
        if i == 0 or kept >= MAX_LFO_VOICES:
            lfo["on"] = False
            print(f"[promptwaver] director: cleared LFO on '{v.get('name')}' "
                  f"({'foundation voice' if i == 0 else f'over the {MAX_LFO_VOICES}-voice limit'})")
        else:
            kept += 1
    return scape


def _ensure_soundscape(spec: SceneSpec) -> SceneSpec:
    """Guarantee a scene has a soundscape so audio always works, even if a model
    omitted it or an old cached scene predates the field."""
    if not getattr(spec, "soundscape", None):
        from ..audio import default_soundscape
        spec.soundscape = default_soundscape()
    _limit_lfos(spec.soundscape)
    return spec


def _friendly_error(e: Exception) -> str:
    s = str(e)
    low = s.lower()
    if "401" in s or "authentication" in low or "invalid x-api-key" in low:
        return "authentication failed — check the key"
    if "model" in low and ("not_found" in low or "404" in s):
        return f"model not available — try a different --model"
    if "connection" in low or "network" in low or "timeout" in low:
        return "network error reaching the API"
    return s[:160]


def _extract_json(text: str) -> str:
    """Pull the JSON object out of a model reply, tolerating code fences or stray
    prose by taking the span from the first '{' to the last '}'."""
    t = text.strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[1] if "\n" in t else t
        t = t.rsplit("```", 1)[0]
    t = t.strip()
    i, j = t.find("{"), t.rfind("}")
    if i != -1 and j != -1 and j > i:
        return t[i:j + 1]
    return t
