"""Scenes: the unit the director produces and the UI saves/loads.

A SceneSpec is plain JSON-serialisable data: one or more visual layers (a
generator name + its params), a colour palette, an audio patch, and a set of
modulation routes. A live Scene builds generators from the spec and renders
them; the SceneManager owns the current scene, the on-disk library, and the
crossfade between scenes.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, asdict

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
    pps: int | None = None                                # per-scene PPS override
    soundscape: dict = field(default_factory=dict)        # AI/GUI synth spec
    audio_link: float = 1.0                               # audio<->visual coupling level (the "level effect")
    # Per-scene MIDI pins: {"voice.shimmer_lead.pan": 47}. Name-based (not
    # slot-based like the global map in settings.json) because their whole
    # purpose is to tie a knob to one named voice in this one scene. Empty
    # for every scene that hasn't been pinned, which is most of them —
    # see midi.py's module docstring for why the global map is the default.
    midi_overrides: dict = field(default_factory=dict)

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
        )


# --- live scene --------------------------------------------------------------

class Scene:
    """A built, renderable scene."""

    def __init__(self, spec: SceneSpec):
        self.spec = spec
        self._gens = []
        self.is_3d = False
        for layer in spec.layers:
            g = layer if isinstance(layer, Layer) else Layer(**layer)
            generator = gen.create(g.generator)
            self._gens.append((generator, dict(g.params)))
            if getattr(generator, "is_3d", False):
                self.is_3d = True
        self.camera = None
        if self.is_3d:
            from .scene3d import make_camera
            self.camera = make_camera(spec.camera)

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

    def _resolve(self, generator, base_params, matrix):
        p = dict(generator.defaults)
        p.update(base_params)
        if matrix is not None:
            for key in list(p.keys()):
                p[key] = matrix.value(f"visual.{key}", p[key])
        return p

    def render(self, t: float, dt: float = 0.0, matrix=None, disable_plane: bool = False) -> Frame:
        frame: Frame = []
        if self.camera is not None:
            self.camera.update(t, dt, matrix)
        for g, base_params in self._gens:
            p = self._resolve(g, base_params, matrix)
            if getattr(g, "is_3d", False):
                # "Disable scene plane" (Camera controls): world.py's World
                # generator looks for this key and skips any node whose
                # shape/primitive name looks like a floor/ground/grid/plane —
                # a loose key on the params dict rather than a new method
                # signature, matching how the rest of this dict is already
                # loosely extended (e.g. primitives get `t` merged in the
                # same way). Generators that don't look for it just ignore it.
                p["_disable_plane"] = disable_plane
                # Same loose-key convention as `_disable_plane` above. The
                # World generator uses this to decide visibility and spend
                # its stroke budget per NODE before doing any per-stroke
                # transform work (see World._render_budgeted) — without it
                # the whole world gets transformed every frame just for the
                # camera to discard most of it. Generators that don't look
                # for it ignore it.
                p["_camera"] = self.camera
                paths3d = g.render3d(t, p)
                frame.extend(self.camera.project(paths3d, g.field_depth))
            else:
                frame.extend(g.render(t, p))
        return clamp_frame(frame)


# --- manager -----------------------------------------------------------------

class SceneManager:
    def __init__(self, library_dir: str):
        self.library_dir = library_dir
        os.makedirs(library_dir, exist_ok=True)
        self.current: Scene | None = None
        self._next: Scene | None = None
        self._xfade = 0.0        # 0..1 progress
        self._xfade_dur = 0.0
        self._names_cache: list[str] | None = None

    # library ---------------------------------------------------------------
    def names(self) -> list[str]:
        # `state()` (and so this) is polled by the websocket broadcaster at
        # ~20Hz regardless of whether the library changed — an unconditional
        # os.listdir()+sort here was real, avoidable disk I/O on the same
        # process the render thread shares the GIL with, 20 times a second,
        # for a result that only ever changes on save/delete. Cached and
        # invalidated explicitly by the two calls below that can change it.
        if self._names_cache is None:
            self._names_cache = sorted(
                os.path.splitext(f)[0]
                for f in os.listdir(self.library_dir)
                if f.endswith(".json")
            )
        return self._names_cache

    def path_for(self, name: str) -> str:
        safe = "".join(c for c in name if c.isalnum() or c in " _-").strip()
        return os.path.join(self.library_dir, f"{safe or 'untitled'}.json")

    def save(self, name: str, spec: SceneSpec):
        spec.name = name
        tmp = self.path_for(name) + ".tmp"
        with open(tmp, "w") as f:
            json.dump(spec.to_dict(), f, indent=2)
        os.replace(tmp, self.path_for(name))
        self._names_cache = None

    def load_spec(self, name: str) -> SceneSpec:
        with open(self.path_for(name)) as f:
            return SceneSpec.from_dict(json.load(f))

    def delete(self, name: str):
        p = self.path_for(name)
        if os.path.exists(p):
            os.remove(p)
        self._names_cache = None

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

    def render(self, t: float, dt: float, matrix=None, disable_plane: bool = False) -> Frame:
        if self.current is None:
            return []
        frame = self.current.render(t, dt, matrix, disable_plane)
        if self._next is not None:
            # MVP crossfade: dim the outgoing, bring up the incoming, both drawn.
            # A later version resamples both to a common point budget and
            # interpolates positions (shape-tween) — see README roadmap.
            self._xfade += dt / max(self._xfade_dur, 1e-4)
            a = min(1.0, self._xfade)
            for p in frame:
                p.color = tuple(c * (1 - a) for c in p.color)
            nxt = self._next.render(t, dt, matrix, disable_plane)
            for p in nxt:
                p.color = tuple(c * a for c in p.color)
            frame = frame + nxt
            if self._xfade >= 1.0:
                self.current, self._next = self._next, None
        return frame
