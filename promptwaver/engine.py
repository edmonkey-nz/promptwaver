"""The realtime engine.

Owns the transport clock, the modulation matrix, the current scene, the synth,
audio analysis, and the DAC output. Runs a fixed-rate loop on its own thread:

    update matrix  ->  push audio level in  ->  render scene (modulated)
                   ->  drive synth cutoff   ->  write frame to laser

All UI actions (set param, load/save/generate scene) are queued and applied at
the top of the loop so the render thread never races the websocket thread.
"""

from __future__ import annotations

import json
import threading
import time

from .modulation import ModMatrix, LFO, Envelope, Value
from .scenes import SceneManager, SceneSpec
from .director import SceneDirector
from .audio import make_synth, AudioAnalysis
from .output import make_output


def _apply_scape_param(scape: dict, path: str, value):
    """Mirror a live synth param edit into a soundscape dict (for saving)."""
    parts = path.split(".")
    if len(parts) == 1:
        scape[parts[0]] = value
    elif parts[0] == "delay" and len(parts) == 2:
        scape.setdefault("delay", {})[parts[1]] = value
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
                 enable_laser=False, enable_audio=True, model=None):
        self.fps = fps
        self.pps = pps
        self.crossfade = 2.0

        # shared spine
        self.matrix = ModMatrix()
        self.matrix.add_source("lfo_slow", LFO(rate=0.05, shape="sine"))
        self.matrix.add_source("lfo_mid", LFO(rate=0.2, shape="triangle"))
        self.matrix.add_source("env", Envelope())
        self._audio_src = self.matrix.add_source("audio_level", Value(smooth=0.1))

        self.scenes = SceneManager(library_dir)
        self.director = SceneDirector(cache_dir, model=model)
        from . import settings as _settings
        self._audio_cfg = {
            "device": _settings.get("audio_device"),
            "blocksize": int(_settings.get("audio_blocksize", 8192)),
            "latency": _settings.get("audio_latency", "high"),
        }
        self.synth, self.audio_error = make_synth(enable_audio, **self._audio_cfg)
        self._device_list = None
        self.rescan_audio_devices()
        self.analysis = AudioAnalysis()
        self.output = make_output(
            enable_laser, max_step=max_step, invert_x=invert_x,
            keystone_h=keystone_h, keystone_v=keystone_v)

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
        self._enqueue(lambda: self.scenes.set_scene(
            self.scenes.load_spec(name), crossfade=self.crossfade))

    def save_scene(self, name: str):
        def apply():
            if self.scenes.current:
                self.scenes.save(name, self.scenes.current.spec)
        self._enqueue(apply)

    def generate_scene(self, keyword: str, name: str | None = None, audio: str | None = None):
        # the director call may hit the network; run it off the loop then queue
        spec = self.director.generate(keyword, audio=audio)
        # add every new generation to the library by default
        name = (name or "").strip() or spec.name or keyword
        spec.name = name
        try:
            self.scenes.save(name, spec)
        except Exception as e:
            print(f"[laserflow] could not save generation: {e}")
        self._enqueue(lambda: self._install_spec(spec))

    def set_active(self, value: bool):
        """Master Start/Stop. While inactive: scene time is frozen (not just
        stopped — resuming continues from where it left off, no time-jump),
        the laser is sent an explicit blanked frame every tick, and audio is
        muted at the DSP level (the scene's own mix/levels are untouched, so
        nothing needs re-tuning after Start)."""
        def apply():
            self.active = bool(value)
            self.synth.set_muted(not self.active)
            if not self.active:
                self.output.blank()
                self._last_frame = []
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

    def set_model(self, choice: str):
        self._enqueue(lambda: self.director.set_model(choice))

    def set_effort(self, effort: str):
        self._enqueue(lambda: self.director.set_effort(effort))

    def apply_audio_to_scene(self, scene_name: str, audio_prompt: str):
        """Regenerate just the soundscape for an existing library scene, leaving
        its visuals untouched. Runs off the render loop (network call)."""
        try:
            spec = self.scenes.load_spec(scene_name)
        except Exception as e:
            print(f"[laserflow] could not load scene {scene_name!r}: {e}")
            return
        scape = self.director.generate_audio(spec.name, audio_prompt)
        spec.soundscape = scape
        try:
            self.scenes.save(scene_name, spec)
        except Exception as e:
            print(f"[laserflow] could not save scene {scene_name!r}: {e}")

        def apply():
            # if this scene is the one currently playing, hear the change now
            if self.scenes.current and self.scenes.current.spec.name == scene_name:
                self.scenes.current.spec.soundscape = scape
                if getattr(self.synth, "online", False):
                    self.synth.set_soundscape(scape)
        self._enqueue(apply)

    def configure_audio(self, *, device=None, blocksize=None, latency=None):
        """Live-reconfigure the audio output (device/blocksize/latency).
        Requests a change; the actual applied config (which may differ, e.g.
        if the backend doesn't support the requested blocksize) is read back
        from the synth afterwards, not assumed from the request."""
        def apply():
            if hasattr(self.synth, "reconfigure"):
                self.synth.reconfigure(device=device, blocksize=blocksize, latency=latency)
            else:
                # NullSynth (or a synth that never opened) — try to start one
                self.synth, self.audio_error = make_synth(True, **{
                    "device": device if device is not None else self._audio_cfg["device"],
                    "blocksize": blocksize if blocksize is not None else self._audio_cfg["blocksize"],
                    "latency": latency if latency is not None else self._audio_cfg["latency"],
                })
                if hasattr(self.synth, "reconfigure"):
                    self.synth.reconfigure(use_ladder=True)   # fresh start — find anything that works
            self._sync_audio_cfg_from_synth()
        self._enqueue(apply)

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

    def update_current_scene(self):
        """Save the live config (current camera settings) back into the current
        scene, overwriting its stored settings under the same name."""
        def apply():
            sc = self.scenes.current
            if sc is None:
                return
            cam = getattr(sc, "camera", None)
            if cam is not None:
                sc.spec.camera.update({
                    "mode": cam.mode, "speed": round(cam.base_speed, 3),
                    "orbit_radius": cam.orbit_radius, "orbit_height": cam.orbit_height,
                    "fov": cam.fov, "near": cam.near, "far": cam.far,
                    "max_strokes": cam.max_strokes,
                })
            # capture the live soundscape (GUI tweaks) back into the scene
            if getattr(self.synth, "online", False):
                cur = self.synth.soundscape()
                if cur:
                    sc.spec.soundscape = json.loads(json.dumps(cur))  # deep copy
            self.scenes.save(sc.spec.name, sc.spec)
        self._enqueue(apply)

    def _install_spec(self, spec: SceneSpec):
        self.scenes.set_scene(spec, crossfade=self.crossfade)
        self._apply_modulation(spec)
        # per-scene PPS override if set, else the global hardware ceiling
        self.pps = min(spec.pps, self.director.max_pps) if spec.pps else self.director.max_pps
        if getattr(self.synth, "online", False):
            scape = spec.soundscape or _patch_to_soundscape(spec.audio_patch)
            self.synth.set_soundscape(scape)

    def _apply_modulation(self, spec: SceneSpec):
        self.matrix.clear_routes()
        for r in spec.modulation:
            self.matrix.add_route(r.get("source", "lfo_slow"), r.get("dest", ""),
                                  float(r.get("depth", 1.0)), float(r.get("bias", 0.0)))
        # global audio<->visual coupling level (the "level effect"): scales every
        # route sourced from live audio, independent of each route's own depth
        self.matrix.set_source_scale("audio_level", float(getattr(spec, "audio_link", 1.0)))

    def trigger(self):
        self._enqueue(lambda: self.matrix.sources["env"].trigger())

    def release(self):
        self._enqueue(lambda: self.matrix.sources["env"].release())

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
                    print(f"[laserflow] action error: {e}")

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

            # render + output
            frame = self.scenes.render(t, dt, self.matrix)
            self._last_frame = frame
            self.output.write(frame, self.pps)

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
                      "far": cam.far, "max_strokes": cam.max_strokes}
        return {
            "version": __import__("laserflow").__version__,
            "active": self.active,
            "scene": self.scenes.current.spec.name if self.scenes.current else None,
            "library": self.scenes.names(),
            "generators": __import__("laserflow.generators", fromlist=["available"]).available(),
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
            "audio_diag": self.synth.diagnostics() if getattr(self.synth, "online", False) else None,
            "audio_devices": self._device_list,
            "audio_cfg": self._audio_cfg,
            "audio_error": self.audio_error,
            "crossfade": self.crossfade,
            "audio_level": round(self.analysis.level, 3),
        }

    def preview(self, max_points: int = 400):
        """A light polyline list for the browser canvas (normalized coords)."""
        out = []
        for p in self._last_frame:
            pts = p.points
            if len(pts) > 60:  # thin dense strokes for the preview
                pts = pts[:: max(1, len(pts) // 60)]
            out.append({
                "c": [round(float(v), 3) for v in p.color],
                "p": [[round(float(x), 3), round(float(y), 3)] for x, y in pts],
            })
            if sum(len(s["p"]) for s in out) > max_points:
                break
        return out
