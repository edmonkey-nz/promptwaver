"""Recording and replaying a performance as a list of parameter changes.

WHAT IS RECORDED IS INTENT, NOT EFFECT. Every mutation in this app reaches the
engine through `Engine._enqueue(fn)` as a *closure*, which cannot be
serialised — so the tap is one level up, at `Engine.set_param` /
`set_audio_param`, where a change is still `(key, value)`. That is also the
point both input paths have already converged on: `MidiRouter.set` resolves a
CC to exactly those two calls (engine.py), so a knob and a mouse drag record
identically and neither needs its own handling.

THE CLOCK IS WALL TIME, deliberately, not `Engine._scene_t`. The scene clock
is `real dt * motion_rate` accumulated, and `motion_rate` is itself a recorded
parameter — so a Freeze during a take replays as a Freeze, and the accumulator
sums to the same place over the same wall interval whatever frame rate the
loop happens to be running (24 idle vs 45 with a beam armed). Recording
against the scene clock would have needed Freeze modelled as a special case.

WHAT REPLAYS EXACTLY: every gesture, LFO phase (derived from the absolute
clock, no stored state), the `world` shape accumulator, camera, monitor
filters. WHAT DOES NOT: anything audio-reactive, because `Soundscape._rng`
(note drift), the per-voice swell phase/period and the noise generator are
unseeded — so `audio_level`/`synth_*`/`voice.*` differ in detail and anything
routed from them jitters differently. The gestures are the performance; this
does not chase bit-exactness.

A TAKE IS SELF-CONTAINED. The header carries the full `SceneSpec` as it stood
when recording began, not the scene's name — `scenes/*.json` is mutable and
scenes get deleted, and a take that dies with its scene is worth very little.
It also means live edits made before you hit record are captured.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time

from .paths import data_dir

HEADER_VERSION = 1

#: Two writes to the same parameter closer together than this collapse into
#: one. A MIDI knob sweep arrives at 100-400 CC/s, but the render loop drains
#: its queue once per tick and the last write within a tick is the only one
#: that ever mattered — so this discards nothing playback could reproduce. At
#: the loop's 24-45fps a tick is 22-42ms; 20ms stays under the faster of those.
COALESCE_S = 0.02

#: Commands a take may contain. An ALLOWLIST rather than "record everything":
#: a good number of the websocket commands are rig configuration, not
#: performance — audio device, MIDI port, model, cost cap, kiosk — and several
#: write to the shared settings.json. Replaying those would silently
#: reconfigure the machine when someone opened a take.
COMMANDS = ("set", "set_audio", "active", "laser", "blank")


def _safe(name: str) -> str:
    """Filename-safe, and cannot escape the recordings directory.

    Same rule as SceneManager.path_for: strip to alnum/space/_/- so a crafted
    take name can't traverse out of the folder.
    """
    cleaned = re.sub(r"[^A-Za-z0-9 _-]", "", str(name)).strip()
    return cleaned[:80] or "untitled"


class Take:
    """One recording: a header plus time-ordered events."""

    def __init__(self, path: str, header: dict, events: list):
        self.path = path
        self.header = header
        self.events = events

    @property
    def name(self) -> str:
        return os.path.splitext(os.path.basename(self.path))[0]

    @property
    def scene(self) -> str:
        return str(self.header.get("scene") or "")

    @property
    def duration(self) -> float:
        return float(self.events[-1]["t"]) if self.events else 0.0

    def info(self) -> dict:
        return {"name": self.name, "scene": self.scene,
                "duration": round(self.duration, 2),
                "events": len(self.events),
                "created": self.header.get("created", ""),
                "punched_from": self.header.get("punched_from")}


class SessionStore:
    """The recordings folder: list, load, delete.

    Lives under `data_dir()` with settings.json and the scene library, not in
    the bundle — a packaged build unpacks to a temp directory that is deleted
    on exit, and a take written there would vanish with it.
    """

    def __init__(self, directory: str | None = None):
        self.dir = directory or os.path.join(data_dir(), "recordings")
        os.makedirs(self.dir, exist_ok=True)

    def path_for(self, name: str) -> str:
        return os.path.join(self.dir, _safe(name) + ".jsonl")

    def load(self, name: str) -> Take | None:
        path = self.path_for(name)
        try:
            with open(path) as f:
                lines = [ln for ln in f.read().splitlines() if ln.strip()]
        except OSError:
            return None
        if not lines:
            return None
        try:
            header = json.loads(lines[0])
        except ValueError:
            return None
        events = []
        for ln in lines[1:]:
            # A take is appended to and flushed per line while recording, so a
            # crash mid-write leaves a torn final line. Skip it rather than
            # refusing to load the nine good minutes in front of it.
            try:
                e = json.loads(ln)
            except ValueError:
                continue
            if isinstance(e, dict) and "t" in e and e.get("c") in COMMANDS:
                events.append(e)
        events.sort(key=lambda e: e["t"])
        return Take(path, header, events)

    def list(self, scene: str | None = None) -> list[dict]:
        """Takes, newest first, optionally only those for one scene.

        Reads every file, so this is answered on demand rather than from
        `state()` — the same reason the kiosk archive has its own command.
        """
        out = []
        try:
            names = os.listdir(self.dir)
        except OSError:
            return out
        for fn in names:
            if not fn.endswith(".jsonl"):
                continue
            take = self.load(os.path.splitext(fn)[0])
            if take is None:
                continue
            if scene is not None and take.scene != scene:
                continue
            out.append(take.info())
        out.sort(key=lambda d: d.get("created", ""), reverse=True)
        return out

    def delete(self, name: str) -> bool:
        try:
            os.remove(self.path_for(name))
            return True
        except OSError:
            return False


class Recorder:
    """Appends events to a take while it is running.

    Written and flushed PER LINE, the same discipline `run.py` applies to
    error.txt and for the same reason: a segfault in a native audio or MIDI
    library leaves no Python traceback, and the last flushed line is then the
    only evidence. A crash nine minutes into a ten-minute take should cost the
    last event, not the take.
    """

    def __init__(self, store: SessionStore):
        self.store = store
        self._lock = threading.Lock()
        self._fh = None
        self._t0 = 0.0
        self._events: list[dict] = []
        self.name = ""
        self.scene = ""

    @property
    def active(self) -> bool:
        return self._fh is not None

    @property
    def elapsed(self) -> float:
        return (time.monotonic() - self._t0) if self.active else 0.0

    def start(self, scene: str, spec: dict, seed_events: list | None = None,
              punched_from: float | None = None) -> str:
        """Begin a take. `seed_events` pre-loads a retained prefix (punch-in).

        Punch-in writes a NEW take rather than truncating the one being
        played. Takes are a few MB and this project has already been bitten
        once by an in-place overwrite (regenerating audio replaces a scene's
        soundscape on disk); losing minutes 4-10 of a take you liked, live,
        is the same bug wearing a nicer name.
        """
        self.stop()
        base = _safe(scene) or "take"
        stamp = time.strftime("%Y%m%d-%H%M%S")
        name = f"{base} {stamp}"
        path = self.store.path_for(name)
        header = {"v": HEADER_VERSION, "scene": scene, "spec": spec,
                  "created": time.strftime("%Y-%m-%dT%H:%M:%S")}
        if punched_from is not None:
            header["punched_from"] = round(float(punched_from), 3)
        with self._lock:
            self._fh = open(path, "w")
            self._fh.write(json.dumps(header) + "\n")
            self._events = []
            for e in (seed_events or []):
                self._events.append(e)
                self._fh.write(json.dumps(e) + "\n")
            self._fh.flush()
            self.name, self.scene = name, scene
            # The clock is rebased so a punched take's timeline still starts at
            # zero: the retained prefix already carries absolute offsets, and
            # new events must continue from the punch point, not from now.
            self._t0 = time.monotonic() - float(punched_from or 0.0)
        return name

    def note(self, cmd: str, key: str, value):
        if cmd not in COMMANDS:
            return
        with self._lock:
            if self._fh is None:
                return
            t = time.monotonic() - self._t0
            # Collapse a rapid re-write of the same parameter. Rewinding the
            # file for this would cost a seek per event, so the superseded
            # line is left on disk and `load` keeps the LAST of any duplicate
            # pair — sort is stable, so later wins naturally.
            if self._events:
                last = self._events[-1]
                if (last["c"] == cmd and last.get("k") == key
                        and t - last["t"] < COALESCE_S):
                    last["t"], last["v"] = t, value
                    self._fh.write(json.dumps(last) + "\n")
                    self._fh.flush()
                    return
            e = {"t": round(t, 4), "c": cmd, "k": key, "v": value}
            self._events.append(e)
            self._fh.write(json.dumps(e) + "\n")
            self._fh.flush()

    def stop(self) -> str:
        with self._lock:
            name = self.name
            if self._fh is not None:
                try:
                    self._fh.close()
                except OSError:
                    pass
            self._fh = None
            self.name = ""
            return name


class Player:
    """Replays a take by re-injecting its events.

    Advanced from the engine's own render loop rather than a thread of its
    own: the loop already drains a command queue once per tick, and adding a
    second writer to engine state would break the invariant the whole control
    surface rests on. Tick resolution is 22-42ms, which is finer than the
    coalescing window events were recorded at.
    """

    def __init__(self, take: Take, inject):
        self.take = take
        self._inject = inject
        self._i = 0
        self._t0 = time.monotonic()
        self.finished = False
        #: Pause holds the take's clock, not the scene's. Everything the take
        #: has already set stays set and the scene keeps running — pausing a
        #: replay is "stop feeding me changes", not "freeze the picture",
        #: which is what Freeze is for.
        self._paused_at: float | None = None
        #: Path prefixes to NOT inject, so the performer can take one section
        #: (say the camera) while the take keeps driving everything else.
        self.muted: tuple[str, ...] = ()

    @property
    def elapsed(self) -> float:
        if self._paused_at is not None:
            return self._paused_at
        return time.monotonic() - self._t0

    @property
    def paused(self) -> bool:
        return self._paused_at is not None

    def set_paused(self, value: bool):
        """Resume rebases the clock so the take continues from where it
        stopped rather than jumping forward by however long the pause was."""
        if value and self._paused_at is None:
            self._paused_at = time.monotonic() - self._t0
        elif not value and self._paused_at is not None:
            self._t0 = time.monotonic() - self._paused_at
            self._paused_at = None

    def _skip(self, e: dict) -> bool:
        k = str(e.get("k") or "")
        return any(k.startswith(p) for p in self.muted)

    def advance(self):
        """Inject everything due. Called once per render tick."""
        if self.finished or self._paused_at is not None:
            return
        now = self.elapsed
        ev = self.take.events
        while self._i < len(ev) and ev[self._i]["t"] <= now:
            e = ev[self._i]
            self._i += 1
            if not self._skip(e):
                try:
                    self._inject(e["c"], e.get("k"), e.get("v"))
                except Exception:
                    # One bad event must not kill the render thread — that is
                    # also what drains the action queue, so it would look like
                    # the whole UI had stopped responding.
                    pass
        if self._i >= len(ev):
            self.finished = True

    def remaining_prefix(self) -> list[dict]:
        """Events already played, for seeding a punch-in take."""
        return [dict(e) for e in self.take.events[:self._i]]
