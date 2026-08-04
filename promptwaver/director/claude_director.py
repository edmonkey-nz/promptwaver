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
import os

from ..scenes import SceneSpec
from ..generators import available as available_generators
from ..shapes import available_ops
from ..primitives import available as available_primitives
from .. import settings
from .fallback import local_scene

_DEFAULT_MODEL = os.environ.get("PROMPTWAVER_MODEL", "claude-haiku-4-5")

_SYSTEM = """You are the scene director for PromptWaver, an ambient laser + synth
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
        self.max_tokens = int(os.environ.get("PROMPTWAVER_MAX_TOKENS", "8000"))
        self.last_source = None       # "claude" | "cache" | "fallback"
        self.last_error = None
        self.last_progress = 0.0      # 0..1, approximate — see _from_claude
        self.generating = False
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
                 size: str = "small", warmth: float | None = None, energy: float | None = None,
                 evolution: float | None = None) -> SceneSpec:
        size = size if size in SCENE_SIZE else "small"
        cache = self._cache_path(keyword + "|" + (audio or "") + "|" + size +
                                 f"|w{warmth}|e{energy}|v{evolution}")
        if use_cache and os.path.exists(cache):
            with open(cache) as f:
                self.last_source = "cache"
                self.last_error = None
                self.last_progress = 1.0
                return _ensure_soundscape(SceneSpec.from_dict(json.load(f)))

        self.generating = True
        self.last_progress = 0.0
        try:
            if self.online:
                spec, ok = self._from_claude(keyword, audio, size, warmth, energy)
                if ok:
                    self.last_source = "claude"
                    self.last_error = None
                    spec = _apply_evolution(_ensure_soundscape(spec), evolution)
                    with open(cache, "w") as f:      # only cache genuine Claude output
                        json.dump(spec.to_dict(), f, indent=2)
                    return spec
                # Claude failed — fall back but do NOT cache, so a retry/fix takes effect
                self.last_source = "fallback"
                return _apply_evolution(_ensure_soundscape(local_scene(keyword)), evolution)

            self.last_source = "fallback"
            self.last_error = (self._offline_reason or "no API key") + " — using local fallback"
            return _apply_evolution(_ensure_soundscape(local_scene(keyword)), evolution)
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
                self.last_progress = 1.0
                return json.load(f)

        self.generating = True
        self.last_progress = 0.0
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

    def _from_claude(self, keyword: str, audio: str | None = None, size: str = "small",
                     warmth: float | None = None, energy: float | None = None):
        """Return (spec, ok). ok=False means fall back (reason in last_error)."""
        tier = EFFORT.get(self.effort, EFFORT["med"])
        audio_line = ""
        if audio:
            audio_line = (f"\n\nAudio: also compose a matching ambient SOUNDSCAPE for "
                          f"this brief: \"{audio}\". Return it in the \"soundscape\" field.")
        else:
            audio_line = ("\n\nAudio: also compose a calm ambient soundscape that fits the "
                          "scene, in the \"soundscape\" field.")
        size_hint = SCENE_SIZE.get(size)
        size_line = f"\n\n{size_hint}" if size_hint else ""
        character_hint = _character_hints(warmth, energy)
        character_line = f"\n\n{character_hint}" if character_hint else ""
        content = (f"Design a scene for the keyword: {keyword}\n\n"
                   f"Effort: {self.effort}. {tier['hint']}\n\n"
                   f"Hardware constraint: the laser draws at a maximum of "
                   f"{self.max_pps} points per second. Keep total stroke length "
                   f"and object count efficient for this budget — favour fewer, "
                   f"cleaner strokes over dense detail." + size_line + character_line + audio_line)
        # Rough proxy for "percent complete": the API has no notion of overall
        # completion (it doesn't know the final length in advance), but a
        # streaming call reports tokens as they're generated, which we compare
        # against this effort tier's token budget to drive an approximate bar.
        max_tokens = max(tier["max_tokens"], SIZE_MIN_TOKENS.get(size, 0))
        budget_chars = max_tokens * 4
        try:
            text, stop_reason = self._stream_or_call(content, budget_chars)
            if stop_reason == "max_tokens":
                self.last_error = ("response truncated — try a lower effort or raise "
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

    def _stream_or_call(self, content: str, budget_chars: int):
        """Stream the response (updating self.last_progress as text arrives) if
        the SDK supports it; otherwise fall back to a plain blocking call with no
        progress signal. Returns (text, stop_reason)."""
        tier_tokens = max(1, budget_chars // 4)
        stream_fn = getattr(self._client.messages, "stream", None)
        if stream_fn is None:
            msg = self._client.messages.create(
                model=self.model, max_tokens=tier_tokens,
                system=_SYSTEM, messages=[{"role": "user", "content": content}])
            text = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
            return text, getattr(msg, "stop_reason", None)

        acc_len = 0
        with stream_fn(model=self.model, max_tokens=tier_tokens,
                       system=_SYSTEM, messages=[{"role": "user", "content": content}]) as stream:
            for chunk in stream.text_stream:
                acc_len += len(chunk)
                self.last_progress = min(0.95, acc_len / max(budget_chars, 1))
            final = stream.get_final_message()
        text = "".join(b.text for b in final.content if getattr(b, "type", "") == "text")
        return text, getattr(final, "stop_reason", None)


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
