"""The realtime engine.

Owns the transport clock, the modulation matrix, the current scene, the synth,
audio analysis, and the DAC output. Runs a fixed-rate loop on its own thread:

    update matrix  ->  push audio level in  ->  render scene (modulated)
                   ->  drive synth cutoff   ->  write frame to laser

All UI actions (set param, load/save/generate scene) are queued and applied at
the top of the loop so the render thread never races the websocket thread.
"""

from __future__ import annotations

import colorsys
import json
import threading
import time

from .modulation import ModMatrix, LFO, Value
from .scenes import SceneManager, SceneSpec
from .director import SceneDirector
from .audio import make_synth, AudioAnalysis
from .geometry import test_pattern_frame
from .output import make_output
from .perf import LoopStats


class MidiRouter:
    """The seam between MIDI and the engine.

    `midi.MidiInput` knows about CCs, encoder modes and soft takeover but
    nothing about the engine; this turns its binding keys into the calls the
    engine already has. Three jobs:

      * route a key to the right setter — `master` and `voice.*` are synth
        params (`set_audio_param`), `camera.speed` and friends are engine
        params (`set_param`)
      * resolve voice SLOTS (`voice#2.pan`) against the live soundscape, so
        a binding survives a scene change (see midi.py's docstring)
      * read a key's current value, which soft takeover and relative
        encoders both need before they can decide what to do

    Reads come straight off the live synth/scene rather than a cached copy,
    so a knob is always comparing against the value that is actually
    sounding — including one a scene load just changed underneath it.
    """

    def __init__(self, engine):
        self.engine = engine

    def _voices(self) -> list:
        synth = self.engine.synth
        if getattr(synth, "online", False):
            scape = synth.soundscape() or {}
        else:
            sc = self.engine.scenes.current
            scape = (sc.spec.soundscape if sc else {}) or {}
        return scape.get("voices", []) or []

    def scene_overrides(self) -> dict:
        sc = self.engine.scenes.current
        return dict(sc.spec.midi_overrides) if sc else {}

    def _resolve(self, key: str):
        from . import midi as _midi
        if _midi.slot_index(key) is None:
            return key
        return _midi.resolve_slot(key, self._voices())

    def set(self, key: str, value):
        from . import midi as _midi
        key = self._resolve(key)
        if key is None:
            return                      # slot points past the end of this scene
        if not _midi.is_audio_key(key):
            self.engine.set_param(key, value)
            return
        # `env`, `lfo` and `arp` are the fields the synth won't take a scalar
        # path for — dsp.set_param only understands `voice.<name>.<field>`
        # (three parts) and applies each of these as a whole dict. So a knob
        # on one stage or one LFO setting has to read the current dict, change
        # its own key, and send the lot back. The UI's own controls for these
        # already work exactly this way.
        m = _midi._NAME_RE.match(key)
        if m and "." in m.group(2):
            group, field = m.group(2).split(".", 1)
            if group in ("env", "lfo", "arp"):
                name = m.group(1)
                for v in self._voices():
                    if v.get("name") == name:
                        d = dict(v.get(group) or {})
                        d[field] = value
                        # Moving an LFO's rate or depth implies wanting to
                        # hear it: a knob that silently does nothing until you
                        # also find the on switch is a knob that reads broken.
                        if group == "lfo":
                            d.setdefault("dest", "level")
                            d["on"] = True
                        self.engine.set_audio_param(f"voice.{name}.{group}", d)
                        return
                return
        self.engine.set_audio_param(key, value)

    def get(self, key: str):
        """Current value of a binding key, or None if it isn't readable right
        now (an unresolved slot, or a scene with no soundscape yet). None
        makes the caller skip the move rather than guess a value — guessing
        is what produces a jump, which is the thing soft takeover exists to
        prevent."""
        from . import midi as _midi
        key = self._resolve(key)
        if key is None:
            return None
        eng = self.engine
        if key.startswith("camera."):
            cam = getattr(eng.scenes.current, "camera", None) if eng.scenes.current else None
            if cam is None:
                return None
            attr = {"speed": "base_speed"}.get(key.split(".", 1)[1], key.split(".", 1)[1])
            v = getattr(cam, attr, None)
            return float(v) if v is not None else None
        if key in ("crossfade", "audio_fade", "glow", "trail", "hue_value"):
            return float(getattr(eng, key))
        if key.startswith("lfo_slow.") or key.startswith("lfo_mid."):
            name, attr = key.split(".", 1)
            src = eng.matrix.sources.get(name)
            return float(getattr(src, attr)) if src is not None else None
        if key == "audio_link":
            return float(eng.scenes.current.spec.audio_link) if eng.scenes.current else None
        m = _midi._NAME_RE.match(key)
        if m:
            name, fields = m.group(1), m.group(2).split(".")
            for v in self._voices():
                if v.get("name") == name:
                    cur = v
                    for f in fields:
                        if not isinstance(cur, dict) or f not in cur:
                            return None
                        cur = cur[f]
                    return float(cur) if isinstance(cur, (int, float)) else None
            return None
        # soundscape globals: master, tempo, delay.*, eq.*, swell_*
        scape = None
        if getattr(eng.synth, "online", False):
            scape = eng.synth.soundscape()
        elif eng.scenes.current:
            scape = eng.scenes.current.spec.soundscape
        if not scape:
            return None
        cur = scape
        for f in key.split("."):
            if not isinstance(cur, dict) or f not in cur:
                return None
            cur = cur[f]
        return float(cur) if isinstance(cur, (int, float)) else None


#: Output viewport ratios offered in Settings, widest last. 1:1 is the laser's
#: native shape (galvos scan a square field); the rest are for data projectors
#: and monitors.
OUTPUT_RATIOS = ("1:1", "4:3", "16:10", "16:9", "21:9")


def ratio_to_aspect(ratio: str) -> float:
    """"16:9" -> 1.777…, i.e. width / height. Anything unparseable falls back
    to square, which is the shape everything behaved as before ratios existed."""
    try:
        w, h = ratio.split(":")
        a = float(w) / float(h)
        return a if 0.2 <= a <= 5.0 else 1.0
    except Exception:
        return 1.0


def _apply_scape_param(scape: dict, path: str, value):
    """Mirror a live synth param edit into a soundscape dict (for saving)."""
    parts = path.split(".")
    if len(parts) == 1:
        scape[parts[0]] = value
    elif parts[0] == "delay" and len(parts) == 2:
        scape.setdefault("delay", {})[parts[1]] = value
    elif parts[0] == "eq" and len(parts) == 2:
        scape.setdefault("eq", {})[parts[1]] = value
    elif parts[0] == "voice" and len(parts) == 3:
        for v in scape.get("voices", []):
            if v.get("name") == parts[1]:
                v[parts[2]] = value


def _patch_to_soundscape(patch: dict) -> dict:
    """Back-compat: turn an old audio_patch into a minimal soundscape so older
    scenes still make sound under the new synth."""
    from .audio import default_soundscape
    if not patch:
        return default_soundscape()
    base = default_soundscape()
    base["voices"][0]["note"] = patch.get("base_note", 36)
    base["voices"][0]["chord"] = patch.get("chord", [0, 7, 12])
    base["voices"][0]["waveform"] = patch.get("waveform", "saw")
    return base


class Engine:
    def __init__(self, *, library_dir, cache_dir, fps=45, pps=11000,
                 max_step=0.03, invert_x=False, keystone_h=0.0, keystone_v=0.0,
                 enable_laser=False, enable_audio=True, model=None,
                 enable_diagnostics=False, midi_port=None):
        self.fps = fps
        self.pps = pps
        self.crossfade = 2.0
        # Off by default — a small but real cost (measured ~2.5% of a frame,
        # plus a fixed instrumentation tax on the audio callback) that most
        # sessions don't need paying for. Toggle live in Settings, or launch
        # with --diag, whenever actually diagnosing a performance question —
        # that also lets "is the instrumentation itself costing performance"
        # be answered directly by A/B rather than assumed —
        # skips the render/output timing calls and the perf.record() call
        # in _loop below, and tells the audio callback to skip its own
        # timing the same way (see audio/synth.py).
        self._diag_enabled = enable_diagnostics
        self.perf = LoopStats(fps)
        self.audio_fade = 2.0     # soundscape crossfade duration (s) on scene switch, 0-16
        # Start/Stop audio ramp (s). Separate from `audio_fade` (which is the
        # scene-to-scene soundscape crossfade) because they answer different
        # questions — this one is "how long does the room take to go quiet",
        # and it wants to be short enough to feel like a button press.
        self.start_fade = 1.5

        # shared spine
        self.matrix = ModMatrix()
        self.matrix.add_source("lfo_slow", LFO(rate=0.05, shape="sine"))
        self.matrix.add_source("lfo_mid", LFO(rate=0.2, shape="triangle"))
        self._audio_src = self.matrix.add_source("audio_level", Value(smooth=0.1))

        self.scenes = SceneManager(library_dir)
        self.director = SceneDirector(cache_dir, model=model)
        from . import settings as _settings
        # Output viewport ratio (Settings > Output). A rig property, not a
        # scene one — like keystone, it describes the surface being projected
        # onto and stays put across scene switches, so it lives in
        # settings.json rather than in any scene file. Widening it widens the
        # camera's horizontal field of view; `fov` remains the VERTICAL angle,
        # so changing ratio reveals more at the sides rather than cropping.
        self.output_ratio = str(_settings.get("output_ratio", "1:1"))
        self._audio_cfg = {
            "device": _settings.get("audio_device"),
            "blocksize": int(_settings.get("audio_blocksize", 8192)),
            "latency": _settings.get("audio_latency", "high"),
        }
        self.synth, self.audio_error = make_synth(enable_audio, enable_diagnostics=enable_diagnostics,
                                                  **self._audio_cfg)
        self._device_list = None
        self.rescan_audio_devices()
        self.analysis = AudioAnalysis()
        self.output = make_output(
            enable_laser, max_step=max_step, invert_x=invert_x,
            keystone_h=keystone_h, keystone_v=keystone_v)

        # MIDI control surface. Constructed unconditionally — with no mido
        # installed or no controller plugged in it simply reports itself as
        # unavailable, which the UI shows as a greyed-out panel rather than
        # a missing feature. The router is what lets it address engine and
        # synth params through the same key namespace the web UI uses.
        from .midi import MidiInput
        self.midi_router = MidiRouter(self)
        self.midi = MidiInput(self.midi_router, _settings, port_hint=midi_port)

        self._t0 = time.monotonic()
        self._running = False
        self._thread = None
        self._lock = threading.Lock()
        self._queue = []                 # list of callables applied on the loop
        self._last_frame = []            # for preview
        # master gate: nothing draws/plays/animates until Start is clicked.
        # Muted from the outset so the very first audio callback (which can
        # fire before the render thread's first tick) is silent, not a pop.
        self.active = False
        self.synth.set_muted(True)
        # Separate safety gate from `active`: even while the engine is
        # running (visuals rendering, audio playing, browser preview live),
        # the real DAC should not receive a non-blanked frame until this is
        # explicitly turned on — off by default regardless of whether
        # --laser was passed. Only matters for a real HeliosOutput; NullOutput
        # (no hardware) always renders normally so the preview/point-counter
        # stay accurate with no laser connected. See _loop's output-write step.
        self.laser_on = False
        # Keystone (Settings > Keystone) — physical rig alignment, not a
        # scene setting: it stays fixed across scene switches for as long
        # as this rig is mounted where it is. The actual values live on
        # self.output.planner (see output/ilda.py's PathPlanner, set at
        # construction from --keystone-h/-v); set_keystone below just makes
        # them live-adjustable instead of launch-only.
        #
        # Test pattern: a fixed calibration frame (geometry.py's
        # test_pattern_frame) that _write_output substitutes for the live
        # scene when this is on — lets keystone be tuned against a known
        # shape (straight border, diagonals, crosshair) rather than
        # whatever a scene happens to be showing. Output windows mirror
        # this via state()'s test_pattern_on and draw their own client-side
        # copy of the same pattern (see index.html/output.html).
        self.test_pattern_on = False
        # "Disable Audio"/"Disable Visuals" (Global section): independent,
        # gracefully-fadeable gates layered on top of the instant active/blank
        # ones above — see _sync_audio_mute and the visual-fade block in _loop.
        self.audio_disabled = False
        self.visuals_disabled = False
        self._visual_gain = 1.0
        self._visual_from = 1.0
        self._visual_to = 1.0
        self._visual_fade_dur = 0.0
        self._visual_fade_pos = 0.0
        # Global "hue override" — recolours every output stroke to a single
        # hue (preserving each point's own saturation/value, so depth cueing
        # and per-scene brightness still read), independent of whatever the
        # scene/generator itself chose.
        self.hue_override_on = False
        self.hue_value = 0.5
        # "Disable scene plane" (Camera controls) — hides floor/ground/grid/
        # plane-named nodes from a 3D scene's own geometry, in both the
        # laser and the browser/output display alike (it's applied in
        # World.render3d, before the frame is built at all — see
        # scenes.py's Scene.render — not a display-only filter).
        #
        # These five are per-scene display settings (Camera controls /
        # Monitor filters in the UI): saved into spec.camera by
        # update_current_scene and restored by _install_spec when a scene
        # loads, so they travel with the scene like any other camera
        # setting rather than leaking into whatever gets loaded next.
        # Absent from a scene's saved file (any scene that's never had them
        # explicitly set, including every scene that predates this feature)
        # means off/0 — see _install_spec's `.get(key, default)` reads.
        self.disable_plane = False
        self.mirror_x = False
        self.mirror_y = False
        self.glow = 0.0
        self.trail = 0.0
        # library key of the currently-loaded scene, tracked separately from
        # spec.name: a handful of shipped example scenes have a free-text
        # internal name that doesn't match their filename (e.g.
        # "forest_flythrough.json" internally named "forest flythrough"), so
        # matching the library grid against spec.name silently fails for
        # those — this is the identifier the UI should actually highlight on.
        self._current_library_name = None

    # lifecycle -------------------------------------------------------------
    def start(self):
        if hasattr(self.synth, "reconfigure"):
            self.synth.reconfigure(use_ladder=True, **self._audio_cfg)
            self._sync_audio_cfg_from_synth()
        else:
            self.synth.start()
        self.analysis.start()
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def _sync_audio_cfg_from_synth(self):
        """After any (re)configure attempt, make the engine's own config
        record match what actually ended up running — not just what was
        requested — so the UI always shows the truth (e.g. after a fallback
        to a smaller blocksize) rather than a value that silently diverged."""
        if not hasattr(self.synth, "blocksize"):
            return
        from . import settings as _settings
        self._audio_cfg["device"] = self.synth.device
        self._audio_cfg["blocksize"] = self.synth.blocksize
        self._audio_cfg["latency"] = self.synth.latency
        _settings.set("audio_device", self.synth.device)
        _settings.set("audio_blocksize", self.synth.blocksize)
        _settings.set("audio_latency", self.synth.latency)

    def stop(self):
        self._running = False
        if self._thread:
            self._thread.join(timeout=1.0)
        self.synth.stop()
        self.analysis.stop()
        self.output.close()
        self.midi.close()

    # queued UI actions -----------------------------------------------------
    def _enqueue(self, fn):
        with self._lock:
            self._queue.append(fn)

    def set_param(self, key: str, value):
        """key like 'lfo_slow.rate', 'crossfade', 'layer0.<param>'."""
        def apply():
            self._apply_param(key, value)
        self._enqueue(apply)

    def _apply_param(self, key, value):
        if key == "crossfade":
            self.crossfade = float(value)
        elif key == "audio_fade":
            self.audio_fade = max(0.0, min(16.0, float(value)))
        elif key == "start_fade":
            self.start_fade = max(0.0, min(8.0, float(value)))
        elif key == "output_ratio":
            self.set_output_ratio(str(value))
        elif key == "hue_override":
            self.hue_override_on = bool(value)
        elif key == "disable_plane":
            self.disable_plane = bool(value)
        elif key == "mirror_x":
            self.mirror_x = bool(value)
        elif key == "mirror_y":
            self.mirror_y = bool(value)
        elif key == "glow":
            self.glow = max(0.0, min(1.0, float(value)))
        elif key == "trail":
            self.trail = max(0.0, min(0.95, float(value)))
        elif key == "hue_value":
            self.hue_value = max(0.0, min(1.0, float(value)))
        elif key == "pps":
            self.pps = int(value)
        elif key == "max_pps":
            v = max(1000, int(value))
            self.director.set_max_pps(v)
            # only move the live default if this scene has no explicit override
            if self.scenes.current is None or self.scenes.current.spec.pps is None:
                self.pps = v
        elif key == "scene.pps":
            if self.scenes.current is None:
                return
            spec = self.scenes.current.spec
            if value in (None, ""):
                spec.pps = None
                self.pps = self.director.max_pps
            else:
                spec.pps = max(1000, int(value))
                self.pps = min(spec.pps, self.director.max_pps)
        elif key == "audio_link":
            v = max(0.0, float(value))
            self.matrix.set_source_scale("audio_level", v)
            if self.scenes.current is not None:
                self.scenes.current.spec.audio_link = v
        elif key == "shape_modulation":
            if self.scenes.current is not None:
                self.scenes.current.spec.shape_modulation = value or []
        elif key.startswith("route."):
            # key like "route.1.depth" — index into the current scene's
            # modulation list, mutating both the live matrix and the spec so
            # the mapping's intensity ("level effect" per relationship) can be
            # saved back via Update scene from config.
            _, idx_s, attr = key.split(".", 2)
            idx = int(idx_s)
            if self.scenes.current is None or attr != "depth":
                return
            mods = self.scenes.current.spec.modulation
            if 0 <= idx < len(mods):
                mods[idx]["depth"] = float(value)
            if 0 <= idx < len(self.matrix.routes):
                self.matrix.routes[idx].depth = float(value)
        elif key.startswith("camera."):
            attr = key.split(".", 1)[1]
            for sc in (self.scenes.current, self.scenes._next):
                cam = getattr(sc, "camera", None) if sc else None
                if cam is None:
                    continue
                if attr == "mode":
                    cam.mode = str(value)
                elif attr == "speed":
                    cam.base_speed = float(value)
                elif attr == "orbit_radius":
                    cam.orbit_radius = float(value)
                elif attr == "fov":
                    cam.fov = float(value)
                elif attr == "far":
                    cam.far = float(value)
                elif attr == "max_strokes":
                    cam.max_strokes = int(value)
        elif key.startswith("lfo_slow.") or key.startswith("lfo_mid."):
            name, attr = key.split(".", 1)
            setattr(self.matrix.sources[name], attr, float(value))
        elif key.startswith("layer0.") and self.scenes.current:
            attr = key.split(".", 1)[1]
            layer = self.scenes.current.spec.layers[0]
            params = layer.params if hasattr(layer, "params") else layer["params"]
            params[attr] = float(value)
            # rebuild so the change takes effect
            self.scenes.set_scene(self.scenes.current.spec, crossfade=0)

    def load_scene(self, name: str):
        def apply():
            # was previously a bespoke self.scenes.set_scene(...) call that
            # only crossfaded the *visuals* — it never reached _install_spec,
            # so the synth's soundscape, the modulation routes, and the PPS
            # ceiling all silently stayed on whatever the previously-loaded
            # scene had. Routing through _install_spec (same as a fresh
            # generation) fixes all three.
            self._install_spec(self.scenes.load_spec(name))
            self._current_library_name = name
        self._enqueue(apply)

    def save_scene(self, name: str):
        def apply():
            if self.scenes.current:
                self.scenes.save(name, self.scenes.current.spec)
                self._current_library_name = name
        self._enqueue(apply)

    def generate_scene(self, keyword: str, name: str | None = None, audio: str | None = None,
                       size: str = "small", warmth: float | None = None,
                       energy: float | None = None, evolution: float | None = None):
        # the director call may hit the network; run it off the loop then queue
        spec = self.director.generate(keyword, audio=audio, size=size,
                                      warmth=warmth, energy=energy, evolution=evolution)
        # add every new generation to the library by default
        name = (name or "").strip() or spec.name or keyword
        spec.name = name
        # capture generation metadata
        spec.image_prompt = keyword
        spec.audio_prompt = audio or ""
        spec.generation_settings = {
            "size": size,
            "warmth": warmth,
            "energy": energy,
            "evolution": evolution,
        }
        try:
            self.scenes.save(name, spec)
        except Exception as e:
            print(f"[promptwaver] could not save generation: {e}")
        def apply():
            self._install_spec(spec)
            self._current_library_name = name
        self._enqueue(apply)

    def _sync_audio_mute(self, fade: float = 0.0):
        """Audio should be muted if EITHER the engine is inactive/blanked OR
        the independent "Disable Audio" gate (Global section) is on — this is
        the single place that combines the two into the one mute call the
        synth actually takes."""
        self.synth.set_muted(self.audio_disabled or not self.active, fade)

    def set_active(self, value: bool):
        """Master Start/Stop. While inactive: scene time is frozen (not just
        stopped — resuming continues from where it left off, no time-jump),
        the laser is sent an explicit blanked frame every tick, and audio is
        faded down at the DSP level (the scene's own mix/levels are untouched,
        so nothing needs re-tuning after Start).

        Both directions are faded, over `start_fade` seconds. Audio is not the
        safety-critical part of a Stop — the BEAM is — so the two are handled
        separately here: `output.blank()` still lands instantly on this very
        tick, while the sound rides down. An audible hard cut was the only
        thing the instant mute bought, and it cost a click/pop on every stop.

        The fade continues after `active` goes False because the synth renders
        on its own callback thread; the render loop's inactive branch only
        stops drawing, it doesn't stop the audio callback."""
        def apply():
            self.active = bool(value)
            self._sync_audio_mute(fade=self.start_fade)
            if not self.active:
                # Beam first, and unfaded — see the docstring. This is the one
                # part of a Stop that must not wait for anything.
                self.output.blank()
                self._last_frame = []
        self._enqueue(apply)

    def disable_audio(self, fade: float = 2.0):
        """Independent audio-only gate (Global section) — gracefully fades
        out over `fade` seconds rather than Start/Stop's instant cut, and
        doesn't touch visuals or the active/blank state at all."""
        def apply():
            self.audio_disabled = True
            self._sync_audio_mute(fade=fade)
        self._enqueue(apply)

    def enable_audio(self, fade: float = 2.0):
        def apply():
            self.audio_disabled = False
            self._sync_audio_mute(fade=fade)
        self._enqueue(apply)

    def disable_visuals(self, fade: float = 2.0):
        """Independent visuals-only gate (Global section) — fades the frame's
        own colours to black over `fade` seconds (same per-point dimming
        SceneManager already uses for a scene crossfade) rather than blanking
        outright, and doesn't touch audio or the active/blank state at all."""
        def apply():
            self.visuals_disabled = True
            self._visual_from = self._visual_gain
            self._visual_to = 0.0
            self._visual_fade_dur = max(0.0, float(fade))
            self._visual_fade_pos = 0.0
        self._enqueue(apply)

    def enable_visuals(self, fade: float = 2.0):
        def apply():
            self.visuals_disabled = False
            self._visual_from = self._visual_gain
            self._visual_to = 1.0
            self._visual_fade_dur = max(0.0, float(fade))
            self._visual_fade_pos = 0.0
        self._enqueue(apply)

    def blank(self):
        """Immediate 'beam off' — stops playback (like set_active(False)) and
        sends an explicit blanked frame to the DAC on this same tick, rather
        than waiting for the next one. Safety action, not a toggle."""
        def apply():
            self.active = False
            self.synth.set_muted(True)
            self.output.blank()
            self._last_frame = []
        self._enqueue(apply)

    def set_laser(self, value: bool):
        """Independent gate for the real DAC only (see `laser_on` in
        __init__) — deliberately does NOT touch `active`/audio/preview: the
        point is that visuals+audio can keep running and the browser preview
        keeps showing the live scene while the physical beam stays off, e.g.
        while composing/previewing a scene before it's safe to send to the
        rig. Turning it off blanks the real output immediately on this tick;
        turning it on just stops that override on the next _loop tick."""
        def apply():
            self.laser_on = bool(value)
            if not self.laser_on:
                self.output.blank()
        self._enqueue(apply)

    def set_output_ratio(self, ratio: str):
        """Set the output viewport shape and push it at the live camera(s).

        Applied here rather than baked into scene files: the ratio describes
        the surface you're projecting onto, so it has to survive a scene load
        — which is also why `_install_spec` re-applies it to every incoming
        scene."""
        from . import settings as _settings
        ratio = ratio if ratio in OUTPUT_RATIOS else "1:1"
        self.output_ratio = ratio
        _settings.set("output_ratio", ratio)
        self._apply_aspect()

    def _apply_aspect(self):
        a = ratio_to_aspect(self.output_ratio)
        for sc in (self.scenes.current, self.scenes._next):
            cam = getattr(sc, "camera", None) if sc else None
            if cam is not None:
                cam.aspect = a
        # The DAC needs it too — the galvo field is square, so a wide viewport
        # gets letterboxed there (see PathPlanner). Without this the beam would
        # draw a stretched version of what the browser shows.
        planner = getattr(self.output, "planner", None)
        if planner is not None:
            planner.aspect = a

    def set_keystone(self, h: float | None = None, v: float | None = None):
        """Live horizontal/vertical keystone for the laser (Settings >
        Keystone) — was launch-only (--keystone-h/-v); this makes it
        adjustable while watching the beam (or the test pattern below)
        without restarting. Applied in output/ilda.py's PathPlanner,
        exactly the same as before, just settable at runtime now."""
        def apply():
            if h is not None:
                self.output.planner.keystone_h = float(h)
            if v is not None:
                self.output.planner.keystone_v = float(v)
        self._enqueue(apply)

    def set_test_pattern(self, value: bool):
        """Show/hide the keystone calibration pattern (Settings > Keystone)
        — see `test_pattern_on`'s docstring in __init__."""
        def apply():
            self.test_pattern_on = bool(value)
        self._enqueue(apply)

    def set_diagnostics(self, value: bool):
        """Live toggle for perf/audio instrumentation (Settings modal) — the
        CLI --no-diag flag's runtime equivalent, for A/B-testing whether the
        instrumentation itself costs anything without a relaunch."""
        def apply():
            self._diag_enabled = bool(value)
            if hasattr(self.synth, "_diag_enabled"):
                self.synth._diag_enabled = bool(value)
        self._enqueue(apply)

    def set_model(self, choice: str):
        self._enqueue(lambda: self.director.set_model(choice))

    def set_effort(self, effort: str):
        self._enqueue(lambda: self.director.set_effort(effort))

    def apply_audio_to_scene(self, scene_name: str, audio_prompt: str, warmth: float | None = None,
                             energy: float | None = None, evolution: float | None = None):
        """Regenerate just the soundscape for an existing library scene, leaving
        its visuals untouched. Runs off the render loop (network call)."""
        try:
            spec = self.scenes.load_spec(scene_name)
        except Exception as e:
            print(f"[promptwaver] could not load scene {scene_name!r}: {e}")
            return
        scape = self.director.generate_audio(spec.name, audio_prompt, warmth=warmth,
                                             energy=energy, evolution=evolution)
        spec.soundscape = scape
        try:
            self.scenes.save(scene_name, spec)
        except Exception as e:
            print(f"[promptwaver] could not save scene {scene_name!r}: {e}")

        def apply():
            # if this scene is the one currently playing, hear the change now
            if self.scenes.current and self.scenes.current.spec.name == scene_name:
                self.scenes.current.spec.soundscape = scape
                if getattr(self.synth, "online", False):
                    self.synth.set_soundscape(scape, fade=self.audio_fade)
        self._enqueue(apply)

    def configure_audio(self, *, device=None, blocksize=None, latency=None) -> threading.Event:
        """Live-reconfigure the audio output (device/blocksize/latency).
        Requests a change; the actual applied config (which may differ, e.g.
        if the backend doesn't support the requested blocksize) is read back
        from the synth afterwards, not assumed from the request.

        Returns an Event set once the (enqueued, applied on the render
        thread) reconfigure has actually finished — the caller (the
        websocket handler) waits on this instead of guessing how long a
        stream stop/restart takes. That used to be a fixed `sleep(0.3)` in
        server.py: fine most of the time, but a reconfigure that took a
        little longer than 300ms (a slower device, PulseAudio under load)
        made the handler report "audio failed to (re)start" — a false
        negative — even though the reconfigure went on to succeed a moment
        later. From the UI this looked exactly like "the setting didn't
        take", indistinguishable from an actual failure.
        """
        done = threading.Event()

        def apply():
            try:
                if hasattr(self.synth, "reconfigure"):
                    self.synth.reconfigure(device=device, blocksize=blocksize, latency=latency)
                else:
                    # NullSynth (or a synth that never opened) — try to start one
                    self.synth, self.audio_error = make_synth(True, enable_diagnostics=self._diag_enabled, **{
                        "device": device if device is not None else self._audio_cfg["device"],
                        "blocksize": blocksize if blocksize is not None else self._audio_cfg["blocksize"],
                        "latency": latency if latency is not None else self._audio_cfg["latency"],
                    })
                    if hasattr(self.synth, "reconfigure"):
                        self.synth.reconfigure(use_ladder=True)   # fresh start — find anything that works
                self._sync_audio_cfg_from_synth()
            finally:
                done.set()
        self._enqueue(apply)
        return done

    def rescan_audio_devices(self):
        from .audio import list_devices
        self._device_list = list_devices()

    def set_audio_param(self, path: str, value):
        """Live synth control (master, tempo, distortion, delay.*, voice.*.*)."""
        def apply():
            if getattr(self.synth, "online", False):
                self.synth.set_audio_param(path, value)
            # mirror into the current scene spec so it can be saved
            sc = self.scenes.current
            if sc is not None and sc.spec.soundscape:
                _apply_scape_param(sc.spec.soundscape, path, value)
        self._enqueue(apply)

    # MIDI ------------------------------------------------------------------
    def pin_midi_map(self) -> dict:
        """"Pin MIDI map to scene" (Global section) — freeze the currently
        resolved voice-slot bindings into the loaded scene as name-based
        overrides, and save it.

        The global map stays slot-based and untouched; this only adds a
        scene-local layer that wins while this scene is loaded. Use it on a
        scene dialled in for a set, where you want a knob tied to *that*
        voice regardless of where it sits in the ordering. Nothing needs
        pressing after a normal generate — slots already track the director's
        voice ordering on their own."""
        sc = self.scenes.current
        if sc is None:
            return {"ok": False, "error": "no scene loaded"}
        voices = self.midi_router._voices()
        if not voices:
            return {"ok": False, "error": "this scene has no voices to pin"}
        pins = self.midi.pin_map_for(voices)
        if not pins:
            return {"ok": False, "error": "no voice controls are mapped"}
        sc.spec.midi_overrides = pins
        target = self._current_library_name or sc.spec.name
        self.scenes.save(target, sc.spec)
        return {"ok": True, "count": len(pins), "scene": target}

    def clear_midi_pins(self) -> dict:
        sc = self.scenes.current
        if sc is None:
            return {"ok": False, "error": "no scene loaded"}
        sc.spec.midi_overrides = {}
        target = self._current_library_name or sc.spec.name
        self.scenes.save(target, sc.spec)
        return {"ok": True, "count": 0, "scene": target}

    def update_current_scene(self, camera: bool = True, soundscape: bool = True):
        """Save the live config back into the current scene, overwriting its
        stored settings under the same name. `camera`/`soundscape` select which
        half to update — "Save Camera settings" / "Save Soundscape settings" /
        "Save all scene settings" (Scene settings section) are this same call
        with different flags; the other half of the saved file is left as-is."""
        def apply():
            sc = self.scenes.current
            if sc is None:
                return
            if camera:
                cam = getattr(sc, "camera", None)
                if cam is not None:
                    sc.spec.camera.update({
                        "mode": cam.mode, "speed": round(cam.base_speed, 3),
                        "orbit_radius": cam.orbit_radius, "orbit_height": cam.orbit_height,
                        "fov": cam.fov, "near": cam.near, "far": cam.far,
                        "max_strokes": cam.max_strokes,
                    })
                # Display settings apply to every scene, 2D or 3D (a flat
                # generator has no `cam` object above, but can still have
                # mirror/glow/trail set) — outside the `cam is not None`
                # guard for that reason, still bundled into spec.camera
                # since that's what these same buttons already write.
                sc.spec.camera.update({
                    "disable_plane": self.disable_plane,
                    "mirror_x": self.mirror_x,
                    "mirror_y": self.mirror_y,
                    "glow": self.glow,
                    "trail": self.trail,
                })
            if soundscape and getattr(self.synth, "online", False):
                # capture the live soundscape (GUI tweaks) back into the scene
                cur = self.synth.soundscape()
                if cur:
                    sc.spec.soundscape = json.loads(json.dumps(cur))  # deep copy
            # Save under the library key that was actually loaded, not
            # sc.spec.name — a handful of shipped example scenes have a
            # free-text internal name that doesn't match their filename (see
            # _current_library_name's own docstring), and saving under
            # spec.name there would silently create a *new*, differently-named
            # duplicate file instead of updating the one that's open.
            target = self._current_library_name or sc.spec.name
            self.scenes.save(target, sc.spec)
        self._enqueue(apply)

    def _install_spec(self, spec: SceneSpec):
        # A scene load replaces every soundscape value at once, so no MIDI
        # knob's physical position reflects what it controls any more. Re-arm
        # soft takeover so each one has to sweep back across its value before
        # it moves anything — otherwise the first knob touched after a scene
        # change snaps that parameter to wherever the hardware happens to be.
        self.midi.rearm_takeover()
        # A crossfade only advances as frames are rendered, and the loop skips
        # rendering entirely while stopped — so a scene loaded before pressing
        # Start would sit as a pending transition and look like it simply
        # hadn't loaded. There's nothing on screen to fade from in that state
        # anyway, so snap to it and let Start reveal the right scene.
        self.scenes.set_scene(spec, crossfade=self.crossfade if self.active else 0.0)
        # The output ratio belongs to the rig, not the scene, so a freshly
        # built camera has to be told about it or every scene load would
        # silently revert the viewport to square.
        self._apply_aspect()
        self._apply_modulation(spec)
        # per-scene PPS override if set, else the global hardware ceiling
        self.pps = min(spec.pps, self.director.max_pps) if spec.pps else self.director.max_pps
        if getattr(self.synth, "online", False):
            scape = spec.soundscape or _patch_to_soundscape(spec.audio_patch)
            self.synth.set_soundscape(scape, fade=self.audio_fade)
        # Per-scene display settings (Camera controls / Monitor filters) —
        # bundled into spec.camera (see update_current_scene). `.get(key,
        # default)` means any scene that has never had these saved (every
        # scene predating this feature, and every new one until explicitly
        # set) loads as off/0, not carrying over whatever the PREVIOUS
        # scene happened to have live.
        cam_cfg = spec.camera or {}
        self.disable_plane = bool(cam_cfg.get("disable_plane", False))
        self.mirror_x = bool(cam_cfg.get("mirror_x", False))
        self.mirror_y = bool(cam_cfg.get("mirror_y", False))
        self.glow = float(cam_cfg.get("glow", 0.0))
        self.trail = float(cam_cfg.get("trail", 0.0))

    def _apply_modulation(self, spec: SceneSpec):
        self.matrix.clear_routes()
        for r in spec.modulation:
            self.matrix.add_route(r.get("source", "lfo_slow"), r.get("dest", ""),
                                  float(r.get("depth", 1.0)), float(r.get("bias", 0.0)))
        # global audio<->visual coupling level (the "level effect"): scales every
        # route sourced from live audio, independent of each route's own depth
        self.matrix.set_source_scale("audio_level", float(getattr(spec, "audio_link", 1.0)))

    def _write_output(self, frame):
        """Real hardware only sends non-blanked frames once `laser_on` is
        explicitly set — see `set_laser`'s docstring. NullOutput (no
        hardware attached) always writes normally regardless, so the
        browser preview and point-count stay accurate when there's no
        physical beam to protect."""
        if self.output.name == "helios" and not self.laser_on:
            self.output.blank()
        else:
            self.output.write(frame, self.pps)

    # loop ------------------------------------------------------------------
    def _loop(self):
        period = 1.0 / self.fps
        prev = time.monotonic()
        while self._running:
            now = time.monotonic()
            dt = now - prev
            prev = now

            with self._lock:
                q, self._queue = self._queue, []
            for fn in q:
                try:
                    fn()
                except Exception as e:
                    print(f"[promptwaver] action error: {e}")

            if not self.active:
                # Shift the epoch forward by exactly this tick's dt so the
                # scene clock (t = now - t0) stays frozen at the paused value
                # rather than jumping forward when Start is clicked again.
                self._t0 += dt
                self.output.blank()
                self._last_frame = []
                sleep = period - (time.monotonic() - now)
                if sleep > 0:
                    time.sleep(sleep)
                continue

            t = now - self._t0

            # feed live audio into the matrix, then update all sources
            self._audio_src.current = self.analysis.level
            self.matrix.update(t, dt)

            # crossfades render two full scenes for the transition's duration
            # (see SceneManager.render) — captured before render() below,
            # since a crossfade completing on this exact tick clears it. Only
            # needed for the perf.record() call below, so skip it too with
            # diagnostics off.
            crossfading = self._diag_enabled and self.scenes.transition_state() is not None

            # render + output
            if self._diag_enabled:
                t_render0 = time.monotonic()
            frame = self.scenes.render(t, dt, self.matrix, disable_plane=self.disable_plane, synth=self.synth)

            # "Disable Visuals" fade — dims every point's colour toward black
            # over _visual_fade_dur seconds, the same per-point scaling trick
            # SceneManager already uses for a scene crossfade. Independent of
            # active/blank: audio keeps playing normally through this.
            if self._visual_fade_pos < self._visual_fade_dur:
                self._visual_fade_pos += dt
                prog = min(1.0, self._visual_fade_pos / self._visual_fade_dur)
                self._visual_gain = self._visual_from + (self._visual_to - self._visual_from) * prog
            else:
                self._visual_gain = self._visual_to
            if self._visual_gain < 1.0:
                g = self._visual_gain
                for p in frame:
                    p.color = tuple(c * g for c in p.color)

            # Global "hue override" — recolour every stroke to one hue,
            # keeping each point's own saturation/value (so depth cueing and
            # relative brightness still read, just under a single colour).
            if self.hue_override_on:
                hv = self.hue_value
                for p in frame:
                    _, s, v = colorsys.rgb_to_hsv(*p.color)
                    p.color = colorsys.hsv_to_rgb(hv, s, v)

            if self.test_pattern_on:
                # Overrides whatever the scene rendered this tick, for BOTH
                # the real laser (via _write_output below) and the preview/
                # output-window broadcast (self._last_frame) — one pattern,
                # shown everywhere, so keystone reads the same regardless of
                # which output you're looking at while tuning it.
                frame = test_pattern_frame()
            self._last_frame = frame
            self.perf.tick()   # cheap interval/fps tracking — always on, see LoopStats.tick
            if self._diag_enabled:
                render_dur = time.monotonic() - t_render0
                t_out0 = time.monotonic()
                self._write_output(frame)
                output_dur = time.monotonic() - t_out0
                total_dur = time.monotonic() - now
                self.perf.record(render_s=render_dur, output_s=output_dur, total_s=total_dur,
                                 n_points=len(frame), crossfading=crossfading)
            else:
                self._write_output(frame)

            sleep = period - (time.monotonic() - now)
            if sleep > 0:
                time.sleep(sleep)

    # introspection for the UI ---------------------------------------------
    def state(self) -> dict:
        cam = getattr(self.scenes.current, "camera", None) if self.scenes.current else None
        camera = None
        if cam is not None:
            camera = {"mode": cam.mode, "speed": round(cam.base_speed, 2),
                      "orbit_radius": cam.orbit_radius, "fov": cam.fov,
                      "far": cam.far, "max_strokes": cam.max_strokes,
                      # The UI builds its mode dropdown from this rather than
                      # a fixed list — see Scene.camera_modes for why the
                      # available modes depend on the scene.
                      "modes": self.scenes.current.camera_modes()}
        return {
            "version": __import__("promptwaver").__version__,
            "active": self.active,
            "laser_on": self.laser_on,
            "keystone_h": getattr(self.output.planner, "keystone_h", 0.0),
            "keystone_v": getattr(self.output.planner, "keystone_v", 0.0),
            "test_pattern_on": self.test_pattern_on,
            "audio_disabled": self.audio_disabled,
            "visuals_disabled": self.visuals_disabled,
            "scene": self.scenes.current.spec.name if self.scenes.current else None,
            "library_name": self._current_library_name,
            "library": self.scenes.names(),
            "generators": __import__("promptwaver.generators", fromlist=["available"]).available(),
            "points": getattr(self.output, "last_points", 0),
            "output": self.output.name,
            "audio": getattr(self.synth, "online", False),
            "director_online": self.director.online,
            "director_model": self.director.model,
            "director_source": self.director.last_source,
            "director_error": self.director.last_error,
            "director_choice": self.director.model_choice,
            "director_effort": self.director.effort,
            "director_progress": self.director.last_progress,
            "director_generating": self.director.generating,
            "scene_3d": bool(self.scenes.current and getattr(self.scenes.current, "is_3d", False)),
            "camera": camera,
            "pps": self.pps,
            "max_pps": self.director.max_pps,
            "scene_pps_override": self.scenes.current.spec.pps if self.scenes.current else None,
            "soundscape": self.synth.soundscape() if getattr(self.synth, "online", False) else (
                self.scenes.current.spec.soundscape if self.scenes.current else None),
            "modulation": self.scenes.current.spec.modulation if self.scenes.current else [],
            "audio_link": self.scenes.current.spec.audio_link if self.scenes.current else 1.0,
            "scene_spec": self.scenes.current.spec.to_dict() if self.scenes.current else None,
            "shape_modulation": self.scenes.current.spec.shape_modulation if self.scenes.current else [],
            "audio_diag": self.synth.diagnostics() if getattr(self.synth, "online", False) else None,
            "perf_diag": self.perf.summary(),
            "diagnostics_enabled": self._diag_enabled,
            "vu": self.synth.vu() if getattr(self.synth, "online", False) else None,
            "audio_devices": self._device_list,
            "audio_cfg": self._audio_cfg,
            "audio_error": self.audio_error,
            "crossfade": self.crossfade,
            "audio_fade": self.audio_fade,
            "start_fade": self.start_fade,
            "output_ratio": self.output_ratio,
            "output_ratios": list(OUTPUT_RATIOS),
            "output_aspect": round(ratio_to_aspect(self.output_ratio), 4),
            "hue_override": self.hue_override_on,
            "disable_plane": self.disable_plane,
            "mirror_x": self.mirror_x,
            "mirror_y": self.mirror_y,
            "glow": self.glow,
            "trail": self.trail,
            "hue_value": self.hue_value,
            "scene_transition": self.scenes.transition_state(),
            "audio_level": round(self.analysis.level, 3),
            "midi": self.midi.state(),
        }

    def preview(self, max_points: int = 400, stroke_thin: int = 60):
        """A polyline list for a browser canvas (normalized coords). Default
        args give the light, thinned version used for the small in-page
        control-UI preview; the standalone output window (server.py, a "hq"
        websocket connection) asks for a much higher ceiling since it's the
        actual thing being watched, not just a status glance."""
        out = []
        for p in self._last_frame:
            pts = p.points
            if len(pts) > stroke_thin:  # thin dense strokes for the preview
                pts = pts[:: max(1, len(pts) // stroke_thin)]
            out.append({
                "c": [round(float(v), 3) for v in p.color],
                "p": [[round(float(x), 3), round(float(y), 3)] for x, y in pts],
            })
            if sum(len(s["p"]) for s in out) > max_points:
                break
        return out
