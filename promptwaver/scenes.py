"""Scenes: the unit the director produces and the UI saves/loads.

A SceneSpec is plain JSON-serialisable data: one or more visual layers (a
generator name + its params), a colour palette, an audio patch, and a set of
modulation routes. A live Scene builds generators from the spec and renders
them; the SceneManager owns the current scene, the on-disk library, and the
crossfade between scenes.
"""

from __future__ import annotations

import itertools
import json
import math
import os
from dataclasses import dataclass, field, asdict

import numpy as np

from .geometry import Frame, clamp_frame
from . import generators as gen


# --- spec --------------------------------------------------------------------

@dataclass
class Layer:
    generator: str
    params: dict = field(default_factory=dict)


@dataclass
class SceneSpec:
    name: str = "untitled"
    layers: list = field(default_factory=list)          # list[Layer|dict]
    palette: list = field(default_factory=list)          # list of "#rrggbb"
    audio_patch: dict = field(default_factory=dict)      # see audio/synth.py
    modulation: list = field(default_factory=list)       # list of route dicts
    camera: dict = field(default_factory=dict)           # 3D camera/depth config
    # Unused since the laser was removed; kept so saved scenes round-trip
    # byte-for-byte instead of losing the key on their next save.
    pps: int | None = None
    soundscape: dict = field(default_factory=dict)        # AI/GUI synth spec
    audio_link: float = 1.0                               # audio<->visual coupling level (the "level effect")
    # Per-scene MIDI pins: {"voice.shimmer_lead.pan": 47}. Name-based (not
    # slot-based like the global map in settings.json) because their whole
    # purpose is to tie a knob to one named voice in this one scene. Empty
    # for every scene that hasn't been pinned, which is most of them —
    # see midi.py's module docstring for why the global map is the default.
    midi_overrides: dict = field(default_factory=dict)
    # Generation metadata — the prompts and settings used to create this scene
    image_prompt: str = ""
    audio_prompt: str = ""
    generation_settings: dict = field(default_factory=dict)
    # Shape modulation: [{"shape": "log", "voice": "ant_body", "param": "level", "dest": "scale", "range": [0.5, 1.5]}]
    shape_modulation: list = field(default_factory=list)
    # Per-scene LFO rates, e.g. {"lfo_slow": 0.05, "lfo_mid": 0.2}. These used
    # to be global engine state that no scene remembered: a scene routed from
    # lfo_slow would play back at whatever rate the last scene happened to
    # leave behind, and the value reset on restart. Empty means "use the
    # engine defaults", so every pre-existing scene keeps its old behaviour.
    lfo: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["layers"] = [asdict(l) if isinstance(l, Layer) else l for l in self.layers]
        return d

    @staticmethod
    def from_dict(d: dict) -> "SceneSpec":
        layers = [Layer(**l) if isinstance(l, dict) else l for l in d.get("layers", [])]
        return SceneSpec(
            name=d.get("name", "untitled"),
            layers=layers,
            palette=d.get("palette", []),
            audio_patch=d.get("audio_patch", {}),
            modulation=d.get("modulation", []),
            camera=d.get("camera", {}),
            pps=d.get("pps"),
            soundscape=d.get("soundscape", {}),
            audio_link=d.get("audio_link", 1.0),
            midi_overrides=d.get("midi_overrides", {}),
            image_prompt=d.get("image_prompt", ""),
            audio_prompt=d.get("audio_prompt", ""),
            generation_settings=d.get("generation_settings", {}),
            shape_modulation=d.get("shape_modulation", []),
            lfo=d.get("lfo", {}),
        )


# --- live scene --------------------------------------------------------------

# 0 is reserved for the test pattern's geometry (see gpu_pack.TEST_PATTERN_ID).
_GPU_IDS = itertools.count(1)

class Scene:
    """A built, renderable scene."""

    def __init__(self, spec: SceneSpec):
        self.spec = spec
        self._gens = []
        self._layer_schema_cache: list[dict] | None = None
        self.is_3d = False
        for layer in spec.layers:
            g = layer if isinstance(layer, Layer) else Layer(**layer)
            try:
                generator = gen.create(g.generator)
            except KeyError:
                # A removed generator (see MissingGenerator) must not stop the
                # rest of the scene loading — the old behaviour was a
                # KeyError out of scene load, i.e. a scene that could not be
                # opened, edited or even deleted from the UI's point of view.
                print(f"[promptwaver] scene {spec.name!r}: generator "
                      f"{g.generator!r} is not available in this build — that "
                      f"layer is skipped")
                generator = gen.MissingGenerator(g.generator)
            self._gens.append((generator, dict(g.params)))
            if getattr(generator, "is_3d", False):
                self.is_3d = True
        self.camera = None
        if self.is_3d:
            from .scene3d import make_camera
            self.camera = make_camera(spec.camera)
        # Identifies this scene's geometry to the browser renderer. A new
        # Scene object (load, crossfade, rebuild after a geometry edit) is new
        # geometry; scalar param edits mutate this one and change uniforms only.
        self.gpu_id = next(_GPU_IDS)
        self._gpu_blob: bytes | None = None
        self._shape_scales: dict = {}
        self._tick_t = 0.0
        self._tick_state: list = []

    def camera_modes(self) -> list[str]:
        """Which camera modes actually work for this scene.

        Not every mode suits every scene, and offering one that can't work is
        worse than hiding it:

        `fly` advances the camera along +Z and WRAPS the world against the
        generator's `field_depth`, for an endless corridor of a field. A
        bounded generator has nothing to wrap — `World` sets 1000.0, meaning
        "effectively no wrap" — so flying just leaves the composed scene
        behind, while costing more to render than the modes that suit it
        (it's excluded from the stroke-budget-bounded transform for the same
        reason: its camera position is a travelled distance, not a place).

        `path` needs a route to walk. Without waypoints it silently falls
        back to drift, so listing it would be a control that does nothing.
        """
        modes = ["orbit", "drift"]
        if self.is_3d:
            fd = min((getattr(g, "field_depth", 1e9) for g, _ in self._gens
                      if getattr(g, "is_3d", False)), default=1e9)
            if fd < 500:
                modes.append("fly")
        wp = (self.spec.camera or {}).get("waypoints")
        if wp and len(wp) >= 3:
            modes.append("path")
        return modes

    @property
    def kind(self) -> str:
        return "3d" if self.is_3d else "2d"

    def layer_schemas(self) -> list[dict]:
        """Each layer's generator schema plus this scene's current values.

        The UI builds its layer panel from this instead of hardcoding param
        keys, which is what previously stranded most generators: only
        `speed`/`turbulence`/`hue` had controls, so `flow_field` was
        half-adjustable and `ripples`/`attractor` had nothing meaningful at
        all. Values fall back to the generator default for any param the
        scene didn't author.

        Cached, because this rides the ~20Hz state broadcast. The schema half
        is immutable, but `values` is not — a scalar param change now applies
        to the live scene instead of rebuilding it (see `set_layer_param`),
        so that method invalidates this cache rather than relying on a new
        Scene to replace it.
        """
        if self._layer_schema_cache is None:
            out = []
            for i, (g, base) in enumerate(self._gens):
                sch = g.schema()
                sch["index"] = i
                sch["values"] = {p["key"]: base.get(p["key"], p["default"])
                                 for p in sch["params"]}
                out.append(sch)
            self._layer_schema_cache = out
        return self._layer_schema_cache

    def coerce_layer_param(self, index: int, key: str, value):
        """Cast `value` using the declared type of that layer's generator."""
        return type(self._gens[index][0]).coerce(key, value)

    def set_layer_param(self, index: int, key: str, value) -> bool:
        """Apply a scalar layer param to the LIVE scene. True if it took.

        `render` resolves each frame from the dict held in `_gens`, so writing
        there is exactly equivalent to rebuilding the Scene for any param the
        generator declares in `schema()` — which is scalars only, the things
        sliders and MIDI address. False means the key isn't a declared knob
        (`world`'s `nodes`/`defs` are authored geometry, not parameters) and
        the caller should rebuild instead.

        Rebuilding was the original behaviour and it had two costs that only
        showed up once worlds got big. It reconstructs every generator, which
        throws away the def/primitive geometry caches and re-derives them on
        the next frame — on a 1200-node scene that is real work, repeated for
        every pixel of a slider drag. And it resets any runtime state a
        generator accumulates, which silently broke `world`'s shape clock:
        that clock integrates its rate so motion doesn't teleport when the
        rate changes (see World._shape_time), and a rebuild mid-drag snapped
        every shape back to its t=0 pose — the exact jump the accumulator
        exists to prevent.
        """
        if not (0 <= index < len(self._gens)):
            return False
        generator, base_params = self._gens[index]
        # schema() is a wrapper — {name, description, kind, params:[{key,...}]}
        # — so the declared knobs are the `key` field of each entry, not the
        # top-level dict's own keys.
        declared = {p["key"] for p in type(generator).schema().get("params", [])}
        if key not in declared:
            return False
        base_params[key] = value
        # `layer_schemas()` caches the current values alongside the schema and
        # used to be replaced wholesale by the rebuild this call avoids.
        self._layer_schema_cache = None
        return True

    def _resolve(self, generator, base_params, matrix):
        p = dict(generator.defaults)
        p.update(base_params)
        if matrix is not None:
            for key in list(p.keys()):
                p[key] = matrix.value(f"visual.{key}", p[key])
        return p

    def _lfo_value(self, lfo_config: dict, t: float = 0.0) -> float:
        """Compute current LFO value (-1..1 bipolar, or 0..1 unipolar for level)."""
        if not lfo_config:
            return 0.0
        rate = float(lfo_config.get("rate", 0.06))
        shape = lfo_config.get("shape", "sine")
        phase = float(lfo_config.get("phase", 0.0))
        x = t * rate + phase

        if shape == "triangle":
            v = 4.0 * abs((x % 1.0) - 0.5) - 1.0
        elif shape == "saw":
            v = 2.0 * (x % 1.0) - 1.0
        elif shape == "square":
            v = 1.0 if (x % 1.0) < 0.5 else -1.0
        elif shape == "random":
            step = math.floor(x)
            h = math.sin(step * 12.9898) * 43758.5453
            v = 2.0 * (h - math.floor(h)) - 1.0
        else:  # sine
            v = math.sin(2.0 * math.pi * x)
        return v

    def _shape_mod_scales(self, synth) -> dict:
        """`{shape name: scale multiplier}` from this scene's shape_modulation
        routes, read off the synth's live voice params / LFOs this tick.

        Split out of the old `_apply_shape_modulation` so the GPU path can
        send the handful of multipliers instead of copying every node. Empty
        when nothing applies, which is the common case."""
        mods = self.spec.shape_modulation
        if not mods or not synth or not getattr(synth, "online", False):
            return {}

        scape = synth.soundscape() or {}
        voices = {v["name"]: v for v in scape.get("voices", [])}

        # Build a map of shape -> list of modulations
        shape_mods = {}
        for mod in mods:
            shape = mod.get("shape")
            voice = mod.get("voice")
            dest = mod.get("dest")
            source = mod.get("source", "param")  # "param" or "lfo"
            r = mod.get("range", [0.0, 1.0])

            if voice not in voices or not shape or not dest:
                continue

            v = voices[voice]

            # Read modulation value from source (param or LFO)
            if source == "lfo":
                lfo_cfg = v.get("lfo") or {}
                if not lfo_cfg.get("on"):
                    continue
                lfo_val = self._lfo_value(lfo_cfg)  # -1..1
                # Map bipolar LFO to 0..1 range
                val = (lfo_val + 1.0) * 0.5
            else:  # source == "param"
                param = mod.get("param")
                val = v.get(param)
                if val is None:
                    continue

            # Map the value to the destination transform range
            min_val, max_val = r[0], r[1] if len(r) > 1 else r[0]
            xf = min_val + val * (max_val - min_val)

            if shape not in shape_mods:
                shape_mods[shape] = {}
            shape_mods[shape][dest] = xf
        # Only `scale` is applied (position/rotation were never implemented).
        return {shape: m["scale"] for shape, m in shape_mods.items() if "scale" in m}

    @staticmethod
    def _apply_shape_scales(base_params, scales: dict):
        """The CPU-render half of shape modulation: a params dict whose matched
        nodes carry their scale multiplied in. Copies only what it changes."""
        p = dict(base_params)
        nodes = p.get("nodes", [])
        if not nodes or not scales:
            return p
        new_nodes = []
        for node in nodes:
            node_shape = node.get("shape")
            if node_shape in scales:
                node = dict(node)  # shallow copy
                node["scale"] = node.get("scale", 1.0) * scales[node_shape]
            new_nodes.append(node)
        p["nodes"] = new_nodes
        return p

    def tick(self, t: float, dt: float = 0.0, matrix=None, disable_plane: bool = False,
             synth=None) -> list:
        """Advance everything time-driven for this tick and resolve this tick's
        params — WITHOUT drawing. Returns `[(generator, params, uniforms)]`.

        This is what the GPU path runs every tick: the browser evaluates the
        geometry itself from `uniforms` (see each generator's `gpu_tick`) plus
        the camera's `gpu_state()`. `draw()` turns the same state into a CPU
        frame for the reference renderer. They are separate so that running
        both in one tick advances the camera and the shape clocks ONCE —
        `render()` is just the two back to back."""
        if self.camera is not None:
            self.camera.update(t, dt, matrix)
        self._shape_scales = self._shape_mod_scales(synth)
        state = []
        for g, base_params in self._gens:
            p = self._resolve(g, base_params, matrix)
            if getattr(g, "is_3d", False):
                # "Disable scene plane" (Camera controls): world.py's World
                # generator looks for this key and skips any node whose
                # shape/primitive name looks like a floor/ground/grid/plane —
                # a loose key on the params dict rather than a new method
                # signature. Generators that don't look for it just ignore it.
                p["_disable_plane"] = disable_plane
                # Same loose-key convention. The World generator uses this to
                # decide visibility and spend its stroke budget per NODE before
                # doing any per-stroke transform work (see
                # World._render_budgeted).
                p["_camera"] = self.camera
            state.append((g, p, g.gpu_tick(t, p)))
        self._tick_t = t
        self._tick_state = state
        return state

    def draw(self) -> Frame:
        """The CPU frame for the state the last `tick()` left. Advances nothing."""
        frame: Frame = []
        t = self._tick_t
        for g, p, u in self._tick_state:
            if getattr(g, "is_3d", False):
                if self._shape_scales and g.__class__.__name__ == "World":
                    p = self._apply_shape_scales(p, self._shape_scales)
                if "t" in u:
                    p = dict(p)
                    p["_shape_t"] = u["t"]     # the clock tick() already advanced
                paths3d = g.render3d(t, p)
                frame.extend(self.camera.project(paths3d, g.field_depth))
            else:
                frame.extend(_clip_2d(g.render(t, p)))
        return clamp_frame(frame)

    def render(self, t: float, dt: float = 0.0, matrix=None, disable_plane: bool = False, synth=None) -> Frame:
        self.tick(t, dt, matrix, disable_plane, synth)
        return self.draw()

    def gpu_blob(self) -> bytes:
        """This scene's geometry, packed once for the browser renderer."""
        if self._gpu_blob is None:
            from .gpu_pack import pack_scene
            self._gpu_blob = pack_scene(self)
        return self._gpu_blob


_CLIP_NUMPY_FROM = 64


def _clip_2d(frame: Frame) -> Frame:
    """Cut flat-generator strokes at the frame edge instead of squashing them.

    `clamp_frame` below clips each COORDINATE into [-1,1]. For 3D that is a
    no-op — the camera has already clipped every stroke properly — but a 2D
    pattern reaching past the frame (a node at 2.2 is common in generated
    scenes) had every outside point pushed onto the nearest edge, so the
    off-frame part of each stroke was drawn as a line running along the
    border. This cuts the stroke where it crosses the edge and drops the rest,
    reusing the camera's own box clipper.

    That clipper walks segments in Python, and a flat pattern can be 11,000+
    points, so only strokes that actually leave the frame pay for it; a pattern
    that stays inside is returned untouched, object for object.
    """
    from .geometry import Path
    from .scene3d import _clip_frame
    out: Frame = []
    for path in frame:
        pts = path.points
        if len(pts) == 0 or float(np.abs(pts).max()) <= 1.0:
            out.append(path)
            continue
        if path.closed and len(pts) > 1:
            # The renderer joins last->first for a closed stroke; once cut, the
            # pieces are open runs, so that closing edge must be clipped too.
            pts = np.vstack([pts, pts[:1]])
        # Two clippers, split by length: measured on 2,520 real off-frame
        # strokes, the Python loop costs ~2.5us a point and the numpy one a
        # flat ~140us, so short strokes (most of them) stay in Python and long
        # rings go to numpy (a 64+ point stroke: 1790us -> 205us).
        runs = _clip_frame(pts.tolist()) if len(pts) < _CLIP_NUMPY_FROM else _clip_polyline(pts)
        for run in runs:
            out.append(Path(run, path.color, False, path.glow))
    return out


def _clip_polyline(P: np.ndarray) -> list:
    """Liang-Barsky against [-1,1]^2 over every segment at once; returns the
    in-frame runs as (M,2) arrays.

    Same result as `scene3d._clip_frame` (identical on 2,520 real strokes; it
    differs only where a stroke leaves and re-enters within 1e-4, which that
    one bridges and this one leaves as two runs), vectorised: that one loops over
    segments in Python, which measured `crop circles` at 40ms a frame against
    11ms unclipped — its big rings straddle the edge, so nearly every point
    went through the loop. Numpy calls here are per STROKE, not per segment.
    """
    P = np.asarray(P, dtype=np.float64)
    a, d = P[:-1], P[1:] - P[:-1]
    n = len(a)
    t0 = np.zeros(n)
    t1 = np.ones(n)
    ok = np.ones(n, dtype=bool)
    for pe, qe in ((-d[:, 0], a[:, 0] + 1), (d[:, 0], 1 - a[:, 0]),
                   (-d[:, 1], a[:, 1] + 1), (d[:, 1], 1 - a[:, 1])):
        par = pe == 0
        ok &= ~(par & (qe < 0))                  # parallel to and outside this edge
        with np.errstate(divide="ignore", invalid="ignore"):
            r = np.where(par, 0.0, qe / np.where(par, 1.0, pe))
        ent = ~par & (pe < 0)                    # entering: raises t0
        ext = ~par & (pe > 0)                    # exiting: lowers t1
        t0 = np.where(ent, np.maximum(t0, r), t0)
        t1 = np.where(ext, np.minimum(t1, r), t1)
    ok &= t0 <= t1
    if not ok.any():
        return []
    c0 = a + t0[:, None] * d
    c1 = a + t1[:, None] * d
    # Consecutive kept segments are one run only if the point they share is
    # inside — the first ends un-clipped AND the next starts un-clipped.
    joined = ok[:-1] & ok[1:] & (t1[:-1] >= 1.0) & (t0[1:] <= 0.0)
    idx = np.flatnonzero(ok)
    brk = np.flatnonzero((np.diff(idx) != 1) | ~joined[idx[:-1]])
    starts = np.concatenate(([idx[0]], idx[brk + 1]))
    ends = np.concatenate((idx[brk], [idx[-1]]))
    return [np.vstack([c0[s:s + 1], c1[s:e + 1]]) for s, e in zip(starts, ends)]


# --- manager -----------------------------------------------------------------

class SceneManager:
    def __init__(self, library_dir: str):
        self.library_dir = library_dir
        os.makedirs(library_dir, exist_ok=True)
        self.current: Scene | None = None
        self._next: Scene | None = None
        self._xfade = 0.0        # 0..1 progress
        self._xfade_dur = 0.0
        self._lib_cache: list[dict] | None = None
        self.live: list = []      # [(scene, gain|None)] shown by the last tick()

    # library ---------------------------------------------------------------
    def library(self) -> list[dict]:
        """`[{"name":..., "kind": "2d"|"3d"}, ...]` for the scene list.

        `state()` (and so this) is polled by the websocket broadcaster at
        ~20Hz regardless of whether the library changed — an unconditional
        os.listdir()+sort here was real, avoidable disk I/O on the same
        process the render thread shares the GIL with, 20 times a second,
        for a result that only ever changes on save/delete. Cached and
        invalidated explicitly by the two calls below that can change it.

        `kind` is DERIVED from the layers' generators, not stored in the JSON,
        so it can't drift from what the scene actually is — the same rule
        `Scene.is_3d` follows. Reading each file is affordable only because it
        happens on cache rebuild, never per poll; keep it that way. A file
        that won't parse is listed as 2d rather than dropped, so a broken
        scene stays visible (and deletable) in the UI.
        """
        if self._lib_cache is None:
            out = []
            for f in os.listdir(self.library_dir):
                if not f.endswith(".json"):
                    continue
                name = os.path.splitext(f)[0]
                try:
                    with open(os.path.join(self.library_dir, f)) as fh:
                        d = json.load(fh)
                    kind = gen.kind_of(
                        l.get("generator", "") for l in d.get("layers", []))
                except Exception:
                    kind = "2d"
                out.append({"name": name, "kind": kind})
            self._lib_cache = sorted(out, key=lambda e: e["name"])
        return self._lib_cache

    def names(self) -> list[str]:
        return [e["name"] for e in self.library()]

    def path_for(self, name: str) -> str:
        safe = "".join(c for c in name if c.isalnum() or c in " _-").strip()
        return os.path.join(self.library_dir, f"{safe or 'untitled'}.json")

    def save(self, name: str, spec: SceneSpec):
        spec.name = name
        tmp = self.path_for(name) + ".tmp"
        with open(tmp, "w") as f:
            json.dump(spec.to_dict(), f, indent=2)
        os.replace(tmp, self.path_for(name))
        self._lib_cache = None

    def load_spec(self, name: str) -> SceneSpec:
        with open(self.path_for(name)) as f:
            return SceneSpec.from_dict(json.load(f))

    def delete(self, name: str):
        p = self.path_for(name)
        if os.path.exists(p):
            os.remove(p)
        self._lib_cache = None

    # switching -------------------------------------------------------------
    def set_scene(self, spec: SceneSpec, crossfade: float = 0.0):
        new = Scene(spec)
        if crossfade <= 0 or self.current is None:
            self.current, self._next, self._xfade = new, None, 0.0
        else:
            self._next = new
            self._xfade = 0.0
            self._xfade_dur = crossfade

    def transition_state(self) -> dict | None:
        """For the UI's scene-transition indicator — None when no crossfade
        is in flight, else the outgoing/incoming names and 0..1 progress."""
        if self._next is None or self.current is None:
            return None
        return {
            "from": self.current.spec.name,
            "to": self._next.spec.name,
            "progress": min(1.0, self._xfade),
        }

    def tick(self, t: float, dt: float, matrix=None, disable_plane: bool = False, synth=None):
        """Advance the live scene(s) and the crossfade. Leaves `live` — the
        `[(scene, gain)]` this tick shows — for `draw()` and the GPU snapshot.

        The pair is captured BEFORE a completing crossfade swaps `current`, so
        the frame where the fade ends still contains both scenes (the outgoing
        one at gain 0), exactly as the single-pass render always produced."""
        if self.current is None:
            self.live = []
            return
        self.current.tick(t, dt, matrix, disable_plane, synth)
        if self._next is None:
            self.live = [(self.current, None)]      # None: full brightness, untouched
            return
        # MVP crossfade: dim the outgoing, bring up the incoming, both drawn.
        self._xfade += dt / max(self._xfade_dur, 1e-4)
        a = min(1.0, self._xfade)
        self._next.tick(t, dt, matrix, disable_plane, synth)
        self.live = [(self.current, 1 - a), (self._next, a)]
        if self._xfade >= 1.0:
            self.current, self._next = self._next, None

    def draw(self) -> Frame:
        """The CPU frame for the last `tick()`."""
        frame: Frame = []
        for scene, gain in getattr(self, "live", []):
            part = scene.draw()
            if gain is not None:
                for p in part:
                    p.color = tuple(c * gain for c in p.color)
            frame = frame + part
        return frame

    def render(self, t: float, dt: float, matrix=None, disable_plane: bool = False, synth=None) -> Frame:
        self.tick(t, dt, matrix, disable_plane, synth)
        return self.draw()
