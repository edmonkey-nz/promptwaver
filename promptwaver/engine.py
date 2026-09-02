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

import numpy as np

from .modulation import ModMatrix, LFO, Value
from .scenes import SceneManager, SceneSpec
from . import generators as gen
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
        if key.startswith("layer") and "." in key:
            # Soft takeover needs the CURRENT value of a generator param, so a
            # knob bound to e.g. layer0.scale doesn't jump the pattern when
            # first touched. Read from the layer schema, which already merges
            # the scene's authored value over the generator default.
            sc = eng.scenes.current
            if sc is None:
                return None
            head, attr = key.split(".", 1)
            try:
                idx = int(head[5:])
            except ValueError:
                return None
            for layer in sc.layer_schemas():
                if layer["index"] == idx and attr in layer["values"]:
                    return float(layer["values"][attr])
            return None
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

#: How content whose own shape differs from the output surface is placed in it.
#: Only 2D scenes can differ: a 3D camera divides x by the viewport aspect, so
#: its [-1,1] box IS the viewport's shape by construction, while `pattern2d`
#: composes in a square and knows nothing about the ratio. Before this existed
#: every surface assumed the [-1,1] box filled the viewport, so a flat pattern
#: came out stretched on anything but 1:1.
OUTPUT_FITS = ("fit", "fill", "stretch")


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
        # `audio_level` is the source every generated scene routes from, so it
        # has to be the one that "just works": it follows the ENGINE'S OWN
        # OUTPUT by default. It used to be wired to the microphone, which
        # meant that on any machine not playing sound into a mic — most of
        # them — every audio->visual route in every scene sat at zero and
        # looked broken. An instrument that generates its own sound should
        # react to that sound.
        #
        # The mic is still available, explicitly, as `mic_level`, and
        # `audio_react` switches which one `audio_level` follows for people
        # visualising an external source.
        from . import settings as _settings0
        self.audio_react = _settings0.get("audio_react", "engine")
        self._audio_src = self.matrix.add_source("audio_level", Value(smooth=0.1))
        self._mic_src = self.matrix.add_source("mic_level", Value(smooth=0.1))
        self._synth_srcs = {
            "synth_level": self.matrix.add_source("synth_level", Value(smooth=0.08)),
            "synth_low": self.matrix.add_source("synth_low", Value(smooth=0.08)),
            "synth_mid": self.matrix.add_source("synth_mid", Value(smooth=0.08)),
            "synth_high": self.matrix.add_source("synth_high", Value(smooth=0.06)),
        }
        # `voice.<name>` sources, added as soundscapes load — see _feed_voice_sources
        self._voice_srcs: dict[str, Value] = {}
        # Audio-latency compensation for engine-driven modulation. A rig
        # property like keystone or output ratio — it tracks the audio device
        # and blocksize, not the scene. See mod_delay_auto().
        self.mod_delay_mode = _settings0.get("mod_delay_mode", "auto")
        self.mod_delay_manual = float(_settings0.get("mod_delay_manual", 0.0))
        self.mod_delay_live = 0.0

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
        # Companion rig setting, stored the same way: what to do when the
        # content's own shape isn't the surface's (see OUTPUT_FITS).
        _fit = str(_settings.get("output_fit", "fit"))
        self.output_fit = _fit if _fit in OUTPUT_FITS else "fit"
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
        # Scene clock. Accumulated from SCALED dt rather than read off the wall
        # clock, so `motion` below can decelerate every time-driven thing at
        # once — LFO phase, node motion, camera travel — instead of each
        # generator needing its own speed control.
        self._scene_t = 0.0
        self.motion = 1.0          # target rate: 1 = normal, 0 = frozen
        self._motion_cur = 1.0     # actual rate, slewed toward the target
        self.motion_ramp = 2.0     # seconds to travel the full 0..1 range
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
        self.kaleidoscope_segments = 0
        # Shape of the GPU bloom that `glow` feeds: how wide the halo spreads
        # (as a fraction of the smaller canvas dimension) and how hard it is
        # added back over the strokes. Deliberately NOT modulation
        # destinations — `glow` already is one, and it drives the same bloom,
        # so these stay as the per-scene character of the effect rather than
        # something else for a route to fight over.
        self.bloom_spread = 0.005
        self.bloom_intensity = 2.5
        # Bipolar. 0 draws polylines exactly as authored; positive resamples
        # them through a spline (smooth); negative drops points (angular, the
        # faceted look the low-resolution in-page preview has). Centred on 0
        # rather than running 0..1 so the existing default keeps every scene's
        # geometry untouched. MONITOR ONLY, like the filters above: the DAC
        # still receives the authored path, so a laser looks angular where the
        # screen looks curved.
        self.line_curve = 0.0
        self._mod_line_curve = 0.0
        # Modulated versions (live values, updated every tick)
        self._mod_glow = 0.0
        self._mod_trail = 0.0
        self._mod_kaleidoscope_segments = 0
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
        # Blocksize and device are exactly what the compensation is derived
        # from, so recompute it here rather than at each call site — including
        # the fallback path, where the blocksize that opened may not be the
        # one that was asked for.
        self._apply_mod_delay()

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
        elif key == "motion":
            # 1 = normal, 0 = frozen. The ramp is in motion_ramp seconds.
            self.motion = max(0.0, min(1.0, float(value)))
        elif key == "motion_ramp":
            self.motion_ramp = max(0.05, min(20.0, float(value)))
        elif key == "audio_react":
            v = "mic" if str(value) == "mic" else "engine"
            self.audio_react = v
            from . import settings as _s
            _s.set("audio_react", v)
            # `audio_level` switches feed, and only the engine feed leads.
            self._apply_mod_delay()
        elif key == "output_ratio":
            self.set_output_ratio(str(value))
        elif key == "output_fit":
            self.set_output_fit(str(value))
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
        elif key == "kaleidoscope_segments":
            self.kaleidoscope_segments = max(0, int(value))
        elif key == "bloom_spread":
            self.bloom_spread = max(0.0, min(0.02, float(value)))
        elif key == "bloom_intensity":
            self.bloom_intensity = max(0.0, min(5.0, float(value)))
        elif key == "line_curve":
            self.line_curve = max(-1.0, min(1.0, float(value)))
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
            self._set_audio_link(v)
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
            if self.scenes.current is None or attr not in ("depth", "source", "dest"):
                return
            mods = self.scenes.current.spec.modulation
            if not (0 <= idx < len(mods)):
                return
            if attr == "depth":
                mods[idx]["depth"] = float(value)
            else:
                # Never store an empty source/dest. A <select> asked for an
                # option it doesn't have reports "", and silently accepting
                # that writes a route into the scene that can never fire and
                # renders as a blank row.
                v = str(value).strip()
                if not v:
                    return
                mods[idx][attr] = v
            # Rebuild rather than patching the live Route in place: source and
            # dest are matched by string on every lookup, and keeping the spec
            # as the single thing that's edited means the matrix can never
            # drift from what will be saved.
            self._rebuild_routes()
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
                    # Always the MONITOR value. Editing the live `max_strokes`
                    # directly would mean the slider silently changed meaning
                    # depending on whether the beam happened to be armed.
                    cam.monitor_max_strokes = int(value)
                    cam.apply_profile(self.laser_on)
                elif attr == "laser_max_strokes":
                    v = int(value)
                    cam.laser_max_strokes = v if v > 0 else None   # 0 = no override
                    cam.apply_profile(self.laser_on)
        elif key.startswith("lfo_slow.") or key.startswith("lfo_mid."):
            name, attr = key.split(".", 1)
            setattr(self.matrix.sources[name], attr, float(value))
            # Mirror into the scene so the rate travels with it. Without this
            # a scene routed from an LFO played back at whatever rate the
            # previously-loaded scene left behind.
            if attr == "rate" and self.scenes.current is not None:
                self.scenes.current.spec.lfo[name] = float(value)
        elif key.startswith("layer") and "." in key and self.scenes.current:
            # "layer<N>.<param>" — N was previously hardcoded to 0 and every
            # value forced to float. Now any layer is addressable and the
            # value is cast to the type the generator declares, so int params
            # (rings, segments, rails) don't arrive as floats.
            head, attr = key.split(".", 1)
            try:
                idx = int(head[5:])
            except ValueError:
                return
            sc = self.scenes.current
            layers = sc.spec.layers
            if not (0 <= idx < len(layers)):
                return
            layer = layers[idx]
            params = layer.params if hasattr(layer, "params") else layer["params"]
            params[attr] = sc.coerce_layer_param(idx, attr, value)
            # Apply to the live scene where possible and only rebuild when it
            # isn't a declared knob. A rebuild drops every generator's
            # geometry cache and resets its runtime state — see
            # Scene.set_layer_param for why that mattered once worlds got big.
            if not sc.set_layer_param(idx, attr, params[attr]):
                self.scenes.set_scene(sc.spec, crossfade=0)

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
                       size: str | int = "small", warmth: float | None = None,
                       energy: float | None = None, evolution: float | None = None,
                       kind: str = "3d"):
        # the director call may hit the network; run it off the loop then queue
        spec = self.director.generate(keyword, audio=audio, size=size, warmth=warmth,
                                      energy=energy, evolution=evolution, kind=kind)
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
            # Recorded for the "show scene prompts" panel and to seed a
            # regeneration. The scene's ACTUAL kind is still derived from its
            # generators (Scene.is_3d) — this is what was asked for, not the
            # source of truth for what it is.
            "kind": kind,
            # What this generation cost. None on a cache hit or the offline
            # fallback, which is meaningful in itself: the field says "free",
            # not "unknown". A scene generated before this existed simply has
            # no key, which the UI reports differently again.
            "cost": self.director.last_cost,
            # {"authored": n, "total": n} when the world was grown past what
            # the model wrote (director/expand.py), else absent/None. Kept on
            # the scene so "show scene prompts" can say how much of it Claude
            # authored directly.
            "expansion": self.director.last_expansion,
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
            # Swap detail profile with the beam: a frame dense enough to look
            # good on a monitor overruns the DAC's PPS budget and flickers.
            cam = getattr(self.scenes.current, "camera", None) if self.scenes.current else None
            if cam is not None:
                cam.apply_profile(self.laser_on)
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

    def set_output_fit(self, mode: str):
        """Set how content is placed when its shape isn't the surface's.
        Same rig-setting lifetime as `output_ratio` — see OUTPUT_FITS."""
        from . import settings as _settings
        mode = mode if mode in OUTPUT_FITS else "fit"
        self.output_fit = mode
        _settings.set("output_fit", mode)
        self._apply_aspect()

    def content_aspect(self) -> float:
        """Display width/height of the [-1,1] box the renderers receive.

        NOT the same as the output ratio. A 3D camera already divides x by the
        viewport aspect (`scene3d._clip_and_project`), so its normalised box is
        the viewport's shape and must be drawn that wide. A 2D generator
        composes in a square and never sees the ratio, so its box is square
        whatever the surface is — drawing it at the viewport's shape is what
        stretched every flat pattern on a non-1:1 ratio.

        This is derived from `Scene.is_3d` for the same reason the library's
        2D/3D badge is: there's no stored "kind" field to disagree with."""
        sc = self.scenes.current
        if sc is not None and getattr(sc, "is_3d", False):
            return ratio_to_aspect(self.output_ratio)
        return 1.0

    def _apply_aspect(self):
        a = ratio_to_aspect(self.output_ratio)
        for sc in (self.scenes.current, self.scenes._next):
            cam = getattr(sc, "camera", None) if sc else None
            if cam is not None:
                cam.aspect = a
                # For a 3D scene the fit is resolved at the CAMERA, not by the
                # renderer's letterbox: a wide ratio widens the field of view,
                # and it is that extra view — not any letterboxing — that
                # leaves empty bands down the sides of a `world` scene. See
                # Camera._focal.
                cam.fit = self.output_fit
        # The DAC needs it too — the galvo field is square, so content wider
        # than it gets letterboxed there (see PathPlanner). Without this the
        # beam would draw a stretched version of what the browser shows. It
        # takes the CONTENT aspect, not the viewport's: a square 2D pattern on
        # a 16:9 rig needs no squeeze at all.
        planner = getattr(self.output, "planner", None)
        if planner is not None:
            planner.aspect = self.content_aspect()
            planner.fit = self.output_fit

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
        # Recorded separately from the scene's own `cost`: this call replaced
        # only the audio, so folding it into the scene figure would misreport
        # what composing the visuals cost. Also refresh the stored audio
        # prompt, which used to keep showing the original after a regenerate.
        spec.audio_prompt = audio_prompt
        spec.generation_settings["audio_cost"] = self.director.last_cost
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
                        # The monitor value, not the live one — saving while the
                        # beam is armed would otherwise persist the laser's
                        # reduced density as the scene's normal detail.
                        "max_strokes": cam.monitor_max_strokes,
                        "laser_max_strokes": cam.laser_max_strokes,
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
                    "kaleidoscope_segments": self.kaleidoscope_segments,
                    "bloom_spread": self.bloom_spread,
                    "bloom_intensity": self.bloom_intensity,
                    "line_curve": self.line_curve,
                })
                # LFO rates travel with the scene like the rest of the
                # modulation setup. Captured from the live sources rather than
                # from spec.lfo so a MIDI-driven change is saved too.
                for name in self.LFO_DEFAULT_RATES:
                    src = self.matrix.sources.get(name)
                    if src is not None:
                        sc.spec.lfo[name] = round(float(src.rate), 4)
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
        # Same reason as the aspect call above: the camera is rebuilt per scene,
        # so a freshly-built one starts on its monitor profile regardless of
        # whether the beam is currently armed.
        cam = getattr(self.scenes.current, "camera", None) if self.scenes.current else None
        if cam is not None:
            cam.apply_profile(self.laser_on)
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
        self.kaleidoscope_segments = int(cam_cfg.get("kaleidoscope_segments", 0))
        # Defaults chosen to look like the shipped bloom, so every scene saved
        # before these existed loads with the tuning they were authored under.
        self.bloom_spread = float(cam_cfg.get("bloom_spread", 0.005))
        self.bloom_intensity = float(cam_cfg.get("bloom_intensity", 2.5))
        # Defaults to 0 so every existing scene keeps the exact geometry it
        # was authored against.
        self.line_curve = float(cam_cfg.get("line_curve", 0.0))

    #: Every sound-driven source. The "audio <-> visual" slider scales all of
    #: them together — it means "how much does sound move the picture", and a
    #: scene routed from synth_low rather than audio_level shouldn't quietly
    #: escape the one control that's supposed to govern exactly that.
    AUDIO_SOURCES = ("audio_level", "mic_level", "synth_level",
                     "synth_low", "synth_mid", "synth_high")

    def _set_audio_link(self, v: float):
        for name in self.AUDIO_SOURCES:
            self.matrix.set_source_scale(name, v)
        for name in self._voice_srcs:
            self.matrix.set_source_scale(name, v)

    #: Sources fed from the synth's own output, and therefore measured BEFORE
    #: the audio is audible. These are the only ones latency compensation can
    #: apply to. `mic_level` is deliberately absent: it measures sound that has
    #: already played, so it lags rather than leads and nothing can advance it.
    #: `audio_level` is here because it follows the synth by default — when
    #: `audio_react` is "mic" the delay is dropped, below.
    SYNTH_SOURCES = ("audio_level", "synth_level", "synth_low",
                     "synth_mid", "synth_high")

    def mod_delay_auto(self) -> float:
        """Seconds of hold-back that would align engine-driven modulation with
        the sound you actually hear.

        Three terms, all measured rather than assumed:

        * the output stream's own latency, as PortAudio reports it;
        * half a block, because the band figure is one scalar describing a
          whole block of audio and is applied at that block's start, so it
          represents the middle of it;
        * minus the slew already in the sources, which is a first-order lag
          whose group delay is roughly its time constant and which is already
          pulling in the corrective direction.

        Clamped at zero: at small blocksizes the existing smoothing already
        overshoots the correction, and adding delay there would make visuals
        late rather than early.
        """
        if not getattr(self.synth, "online", False):
            return 0.0
        lead = self.synth.output_latency + (self.synth.blocksize / float(self.synth.sr)) * 0.5
        slew = 0.08          # the smooth= constant the synth sources are built with
        return max(0.0, lead - slew)

    def _apply_mod_delay(self):
        """Push the configured compensation onto the synth-derived sources."""
        want = (0.0 if self.mod_delay_mode == "off"
                else self.mod_delay_auto() if self.mod_delay_mode == "auto"
                else self.mod_delay_manual)
        # Following the mic means the source no longer leads anything, so the
        # correction must come off or it would add lag to an already-late feed.
        for name in self.SYNTH_SOURCES:
            src = self.matrix.sources.get(name)
            if src is None:
                continue
            on_mic = (name == "audio_level" and self.audio_react == "mic")
            src.delay = 0.0 if on_mic else want
        for name, src in self._voice_srcs.items():
            src.delay = want
        self.mod_delay_live = want

    def set_mod_delay(self, mode: str, seconds: float | None = None):
        """`mode` is "auto", "off", or "manual". A rig property, not a scene
        one — it follows the audio device and blocksize, which survive scene
        changes — so it persists in settings.json alongside them."""
        from . import settings as _s
        if mode in ("auto", "off", "manual"):
            self.mod_delay_mode = mode
            _s.set("mod_delay_mode", mode)
        if seconds is not None:
            self.mod_delay_manual = max(0.0, min(1.0, float(seconds)))
            _s.set("mod_delay_manual", self.mod_delay_manual)
        self._apply_mod_delay()

    def _rebuild_routes(self):
        """Re-derive the live matrix routes from the current scene's spec."""
        self.matrix.clear_routes()
        if self.scenes.current is None:
            return
        for r in self.scenes.current.spec.modulation:
            self.matrix.add_route(r.get("source", "lfo_slow"), r.get("dest", ""),
                                  float(r.get("depth", 1.0)), float(r.get("bias", 0.0)))

    def add_route(self, source: str, dest: str, depth: float = 0.3):
        def apply():
            if self.scenes.current is None or not source.strip() or not dest.strip():
                return
            self.scenes.current.spec.modulation.append(
                {"source": source, "dest": dest, "depth": float(depth)})
            self._rebuild_routes()
        self._enqueue(apply)

    def remove_route(self, index: int):
        def apply():
            if self.scenes.current is None:
                return
            mods = self.scenes.current.spec.modulation
            if 0 <= index < len(mods):
                mods.pop(index)
                self._rebuild_routes()
        self._enqueue(apply)

    #: Human labels for routable destinations, keyed by full destination key.
    #: Anything absent falls back to the key with underscores spaced out, so a
    #: new generator param still reads sensibly with no edit here.
    #:
    #: Two entries exist purely to break a collision: on a 2D scene
    #: `visual.glow` (pattern2d's authored per-stroke glow, which reaches a
    #: laser through RGB) and `glow` (the monitor blur filter, which never
    #: leaves the browser) both rendered as the single word "glow" in the same
    #: dropdown. They are unrelated controls.
    DEST_LABELS = {
        "camera.speed": "speed",
        "camera.orbit_radius": "orbit radius",
        "camera.fov": "field of view",
        "camera.max_strokes": "stroke budget",
        "visual.max_strokes": "stroke budget",
        "visual.shape_speed": "shape speed",
        "visual.glow": "pattern glow",
        "glow": "screen glow",
        "trail": "trails",
        "kaleidoscope_segments": "kaleidoscope",
        "line_curve": "line curve",
    }

    #: Generator names are registry identifiers, not UI copy. Groups not listed
    #: fall through to the generator's own name.
    DEST_GROUPS = {
        "world": "Shapes",
        "pattern2d": "Pattern",
        "flow_field": "Flow field",
        "attractor": "Attractor",
        "ripples": "Ripples",
    }

    def mod_destinations(self) -> list[dict]:
        """What a route may target, derived rather than hardcoded.

        Generator params come from the layer schema — the same registry that
        builds the layer panel — so a new generator param becomes routable the
        moment it exists, with no list to update here. (Scene._resolve already
        exposes every top-level param as `visual.<key>`; this just makes the
        UI aware of them.)

        Each entry carries both a `key` (what the matrix and the scene JSON
        use) and a human `label`. The browser shows the label and puts the key
        on hover, so the dropdown reads as English without cutting the tie to
        what's in the file and in TECHNICAL.md.
        """
        def entry(key: str, group: str, fallback: str | None = None) -> dict:
            return {"key": key,
                    "label": self.DEST_LABELS.get(key,
                                                  (fallback or key).replace("_", " ")),
                    "group": group}

        out = []
        seen = set()
        sc = self.scenes.current
        if sc is not None:
            for layer in sc.layer_schemas():
                group = self.DEST_GROUPS.get(layer["name"], layer["name"])
                for p in layer["params"]:
                    key = f"visual.{p['key']}"
                    if key in seen:
                        continue
                    seen.add(key)
                    out.append(entry(key, group, p["key"]))
            if sc.is_3d:
                for k in ("speed", "orbit_radius", "fov", "max_strokes"):
                    out.append(entry(f"camera.{k}", "Camera", k))
        # Flagged in the group name because it is a real behavioural
        # difference, not a caveat: monitor filters are drawn in the browser
        # and never touch the vector data sent to the DAC, so a route here
        # does nothing at all on a laser.
        for k in ("glow", "trail", "kaleidoscope_segments", "line_curve"):
            out.append(entry(k, "Monitor · screen only"))
        return out

    #: Human labels for modulation SOURCES. `voice.*` is handled separately —
    #: those names come from whichever soundscape is loaded.
    SOURCE_LABELS = {
        "audio_level": "audio level",
        "mic_level": "mic level",
        "synth_level": "synth · level",
        "synth_low": "synth · low",
        "synth_mid": "synth · mid",
        "synth_high": "synth · high",
        # Both LFOs need qualifying. Labelling `lfo_slow` as plain "LFO" put
        # "LFO" and "lfo mid" next to each other in the same list, reading as
        # though one were the LFO and the other something else.
        "lfo_slow": "LFO · slow",
        "lfo_mid": "LFO · mid",
        "env": "envelope",
    }

    def mod_source_label(self, name: str) -> str:
        if name.startswith("voice."):
            return name[6:]
        return self.SOURCE_LABELS.get(name, name.replace("_", " "))

    def _feed_voice_sources(self, peaks: dict):
        """Publish each voice's live output level as `voice.<name>`.

        Registered on demand rather than up front because the voice list is a
        property of whichever soundscape is loaded — it changes with every
        scene. Sources for voices that have gone away are left in place but
        decay to zero; removing them mid-tick would break any route still
        pointing at one while a crossfade is still sounding it.
        """
        link = float(self.scenes.current.spec.audio_link) if self.scenes.current else 1.0
        for vname, peak in peaks.items():
            key = f"voice.{vname}"
            src = self._voice_srcs.get(key)
            if src is None:
                src = self._voice_srcs[key] = self.matrix.add_source(key, Value(smooth=0.05))
                self.matrix.set_source_scale(key, link)
                # Voice sources appear as soundscapes load, i.e. long after
                # _apply_mod_delay last ran — they are read off the same block
                # as the synth bands and lead by exactly as much.
                src.delay = self.mod_delay_live
            src.current = peak

        # Drop voices that aren't in the live soundscape. An earlier version
        # left them registered and merely decayed them to zero, so the source
        # list accumulated every voice from every scene loaded this session —
        # picking a source meant scrolling past instruments belonging to
        # scenes that aren't even open.
        #
        # A voice still referenced by one of the current scene's routes is
        # kept regardless: during a crossfade the outgoing soundscape stops
        # reporting peaks a moment before its scene is actually replaced, and
        # pulling the source out from under a live route in that window would
        # break it mid-fade.
        if len(self._voice_srcs) > len(peaks):
            routed = {r.source for r in self.matrix.routes}
            for key in [k for k in self._voice_srcs
                        if k[6:] not in peaks and k not in routed]:
                self._voice_srcs.pop(key, None)
                self.matrix.sources.pop(key, None)
                self.matrix.source_scale.pop(key, None)

    #: Engine defaults, restored for any scene that doesn't carry its own rate.
    #: Without this reset a rate set by one scene would leak into the next.
    LFO_DEFAULT_RATES = {"lfo_slow": 0.05, "lfo_mid": 0.2}

    def _apply_lfo(self, spec: SceneSpec):
        rates = spec.lfo or {}
        for name, default in self.LFO_DEFAULT_RATES.items():
            src = self.matrix.sources.get(name)
            if src is not None:
                src.rate = float(rates.get(name, default))

    def _refresh_midi_ranges(self):
        """Publish the current scene's generator param ranges to the MIDI
        layer, so a knob bound to `layer0.scale` sweeps the same 0.1-5.0 the
        on-screen slider does. Derived from the registry rather than a table
        here — see midi.DYNAMIC_RANGES."""
        from . import midi as _midi
        ranges = {}
        if self.scenes.current is not None:
            for layer in self.scenes.current.layer_schemas():
                for p in layer["params"]:
                    ranges[f"layer{layer['index']}.{p['key']}"] = (
                        float(p.get("min", 0.0)), float(p.get("max", 1.0)))
        _midi.set_dynamic_ranges(ranges)

    def _apply_modulation(self, spec: SceneSpec):
        self._apply_lfo(spec)
        self._refresh_midi_ranges()
        self.matrix.clear_routes()
        for r in spec.modulation:
            self.matrix.add_route(r.get("source", "lfo_slow"), r.get("dest", ""),
                                  float(r.get("depth", 1.0)), float(r.get("bias", 0.0)))
        # global audio<->visual coupling level (the "level effect"): scales every
        # route sourced from live audio, independent of each route's own depth
        self._set_audio_link(float(getattr(spec, "audio_link", 1.0)))

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
                # The scene clock simply doesn't advance while paused — it's
                # an accumulator now, so it holds its value on its own. (It
                # used to be `now - _t0`, which needed the epoch shifted by
                # each paused tick to stop it jumping forward on resume.)
                self.output.blank()
                self._last_frame = []
                sleep = period - (time.monotonic() - now)
                if sleep > 0:
                    time.sleep(sleep)
                continue

            # Ease the motion rate toward its target, then advance the scene
            # clock by the scaled dt. Freezing `t` is what actually stops the
            # picture: LFO phase, node motion and camera angle are all
            # functions of it, so they decelerate together and in proportion
            # rather than each snapping to a halt on its own schedule.
            if self._motion_cur != self.motion:
                step = dt / max(self.motion_ramp, 1e-4)
                self._motion_cur = (min(self.motion, self._motion_cur + step)
                                    if self._motion_cur < self.motion
                                    else max(self.motion, self._motion_cur - step))
            eff_dt = dt * self._motion_cur
            self._scene_t += eff_dt
            t = self._scene_t

            # feed live audio into the matrix, then update all sources.
            # REAL dt here, not eff_dt: the audio_level source slews toward the
            # live mic level over dt, and an envelope releases over dt. Motion
            # being frozen shouldn't also deafen the instrument — a stopped
            # pattern still pulses with the sound, which is the point of
            # stopping it. Only `t` is frozen, which is what the LFOs read.
            self._mic_src.current = self.analysis.level
            synth_level = 0.0
            if getattr(self.synth, "online", False):
                b = self.synth.bands()
                synth_level = b.get("level", 0.0)
                self._synth_srcs["synth_level"].current = synth_level
                self._synth_srcs["synth_low"].current = b.get("low", 0.0)
                self._synth_srcs["synth_mid"].current = b.get("mid", 0.0)
                self._synth_srcs["synth_high"].current = b.get("high", 0.0)
                self._feed_voice_sources(b.get("voices") or {})
            # `audio_level` follows whichever feed the user picked. Engine
            # output is the default because that's what a scene's own routes
            # are written against.
            self._audio_src.current = (self.analysis.level
                                       if self.audio_react == "mic" else synth_level)
            self.matrix.update(t, dt)
            # Apply modulation to effect parameters (kaleidoscope_segments
            # is LFO-modulatable display filter, live on this tick)
            self._mod_glow = self.matrix.value("glow", self.glow)
            self._mod_trail = self.matrix.value("trail", self.trail)
            self._mod_kaleidoscope_segments = self.matrix.value("kaleidoscope_segments", self.kaleidoscope_segments)
            self._mod_line_curve = self.matrix.value("line_curve", self.line_curve)

            # crossfades render two full scenes for the transition's duration
            # (see SceneManager.render) — captured before render() below,
            # since a crossfade completing on this exact tick clears it. Only
            # needed for the perf.record() call below, so skip it too with
            # diagnostics off.
            crossfading = self._diag_enabled and self.scenes.transition_state() is not None

            # render + output
            if self._diag_enabled:
                t_render0 = time.monotonic()
            # eff_dt here: the camera integrates its position by dt, so a
            # scaled dt is what brings a fly-through to a stop rather than
            # leaving it coasting at full speed through a frozen world.
            frame = self.scenes.render(t, eff_dt, self.matrix, disable_plane=self.disable_plane, synth=self.synth)

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
                      "far": cam.far,
                      # `max_strokes` is the MONITOR setting (what the slider
                      # edits); `max_strokes_live` is what the renderer is
                      # actually using, which differs while the beam is armed.
                      "max_strokes": cam.monitor_max_strokes,
                      "max_strokes_live": cam.max_strokes,
                      "laser_max_strokes": cam.laser_max_strokes or 0,
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
            # name -> "2d"|"3d" for the library list's type badge. Derived from
            # each scene's generators (see SceneManager.library), never stored.
            "library_kinds": {e["name"]: e["kind"] for e in self.scenes.library()},
            # Full registry schema: what generators exist, what kind each is,
            # and what knobs each has. The UI and the director both read this
            # rather than hardcoding names — see generators/base.py.
            "generators": gen.catalog(),
            "layers": self.scenes.current.layer_schemas() if self.scenes.current else [],
            "scene_kind": self.scenes.current.kind if self.scenes.current else None,
            "points": getattr(self.output, "last_points", 0),
            "output": self.output.name,
            "audio": getattr(self.synth, "online", False),
            "director_online": self.director.online,
            "director_model": self.director.model,
            "director_source": self.director.last_source,
            "director_error": self.director.last_error,
            "director_choice": self.director.model_choice,
            "director_effort": self.director.effort,
            "director_cost_cap": self.director.cost_cap,
            "director_progress": self.director.last_progress,
            "director_generating": self.director.generating,
            # Cost of the last billed generation; None for cache hits and the
            # offline fallback, which cost nothing.
            "director_last_cost": self.director.last_cost,
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
            "motion": self.motion,
            # the slewed value, so the button can show the ramp in flight
            "motion_now": round(self._motion_cur, 3),
            "motion_ramp": self.motion_ramp,
            "output_ratio": self.output_ratio,
            "output_ratios": list(OUTPUT_RATIOS),
            "output_aspect": round(ratio_to_aspect(self.output_ratio), 4),
            # The shape of the SURFACE (above) and the shape of the [-1,1] box
            # the preview carries (below) are different numbers — see
            # Engine.content_aspect. Both browser surfaces need the second one
            # to letterbox correctly; sent from here rather than re-derived in
            # each page so they can't drift.
            "output_fit": self.output_fit,
            "output_fits": list(OUTPUT_FITS),
            "content_aspect": round(self.content_aspect(), 4),
            "hue_override": self.hue_override_on,
            "disable_plane": self.disable_plane,
            "mirror_x": self.mirror_x,
            "mirror_y": self.mirror_y,
            "glow": max(0.0, min(1.0, self._mod_glow)),
            "trail": max(0.0, min(0.95, self._mod_trail)),
            "kaleidoscope_segments": max(0, round(self._mod_kaleidoscope_segments)),
            "bloom_spread": self.bloom_spread,
            "bloom_intensity": self.bloom_intensity,
            "line_curve": max(-1.0, min(1.0, self._mod_line_curve)),
            "hue_value": self.hue_value,
            "scene_transition": self.scenes.transition_state(),
            "audio_level": round(self.analysis.level, 3),
            # Live value of every modulation source, so the UI can show which
            # ones are actually moving. Without this, a route whose source is
            # flat (a silent mic) is indistinguishable from a broken route.
            "mod_sources": {n: round(self.matrix.source_value(n), 3)
                            for n in self.matrix.sources},
            "mic_online": self.analysis.online,
            "audio_react": self.audio_react,
            "mod_delay_mode": self.mod_delay_mode,
            "mod_delay_ms": round(self.mod_delay_live * 1000),
            "mod_delay_auto_ms": round(self.mod_delay_auto() * 1000),
            "mod_destinations": self.mod_destinations(),
            # name -> human label, so the browser never carries its own copy
            # of the naming (same reason the cost estimate is server-side).
            "mod_source_labels": {n: self.mod_source_label(n)
                                  for n in self.matrix.sources},
            # Dotted keys so the UI's readStateValue finds them directly —
            # without these the LFO rate slider never reflected a scene load.
            "lfo_slow.rate": round(getattr(self.matrix.sources.get("lfo_slow"), "rate", 0.05), 4),
            "lfo_mid.rate": round(getattr(self.matrix.sources.get("lfo_mid"), "rate", 0.2), 4),
            "midi": self.midi.state(),
        }

    def preview(self, max_points: int = 1800, stroke_thin: int = 60):
        """A polyline list for a browser canvas (normalized coords). Default
        args give the light, thinned version used for the small in-page
        control-UI preview; the standalone output window (server.py, a "hq"
        websocket connection) asks for a much higher ceiling since it's the
        actual thing being watched, not just a status glance.

        Over budget, this drops RESOLUTION, not strokes. The previous version
        emitted whole strokes until it passed `max_points` and then stopped,
        which is fine for a 3D scene — `max_strokes` already holds those to
        ~90-130 short strokes — but silently truncated flat `pattern2d`
        scenes, which are stroke-dense by nature: a 263-stroke pattern showed
        as the ~27 strokes that fit, i.e. a fragment of the composition
        presented as if it were the whole thing. Since the preview is what
        you author against, a coarser complete picture beats an exact
        fraction of one. Two points per stroke are always kept, so the
        composition's shape survives any amount of thinning.
        """
        frame = self._last_frame
        total = sum(len(p.points) for p in frame)
        # One stride for the whole frame, so thinning is uniform rather than
        # penalising whichever strokes happen to come last.
        stride = 1
        if total > max_points:
            stride = max(1, -(-total // max(1, max_points)))   # ceil division
        out = []
        for p in frame:
            pts = p.points
            if len(pts) > stroke_thin:  # thin dense strokes for the preview
                pts = pts[:: max(1, len(pts) // stroke_thin)]
            if stride > 1 and len(pts) > 2:
                kept = pts[::stride]
                # never lose an endpoint: an open stroke that loses its last
                # point visibly shortens, and a closed one springs open
                if len(kept) < 2 or not np.array_equal(kept[-1], pts[-1]):
                    kept = np.vstack([kept, pts[-1:]])
                pts = kept
            # Rounded with numpy rather than a per-point Python loop: this was
            # the most expensive step in the whole ~20Hz broadcast, and
            # `np.round(...).tolist()` yields the identical nested lists of
            # Python floats with the rounding and float conversion both down
            # in C (~7x on this operation alone).
            #
            # The .astype is load-bearing — Path.points is float32, and
            # rounding at float32 precision before widening on .tolist()
            # leaks the representation error into the payload: 0.105 comes
            # out as 0.10499999672174454, a different number than this used
            # to send and roughly four times the bytes to serialise.
            st = {
                "c": np.round(p.color, 3).tolist(),
                "p": np.round(pts.astype(np.float64), 3).tolist(),
            }
            # Per-stroke glow rides along only when a scene actually uses it,
            # so the ~20Hz payload for every existing scene is byte-identical
            # to before. Quantised to 2dp because the canvas buckets it anyway
            # (see paintPreview) — full float precision would just cost bytes.
            g = getattr(p, "glow", 0.0)
            if g:
                st["g"] = round(float(g), 2)
            out.append(st)
        return out
