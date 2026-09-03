"""MIDI control surface — hardware knobs onto the same parameter keys the web
UI already speaks.

Ported from the sibling laser-laser-laser project's MidiInput, with one
addition that this project needs and that one doesn't: **voice slots**.

The problem
-----------
Camera and master-audio parameters are easy. `camera.speed`, `master`,
`eq.low`, `delay.mix` are fixed strings that mean the same thing in every
scene, so binding a CC to one is a plain dict entry.

The instruments are not. A scene's voices are addressed by NAME —
`voice.deep_bass.level` — and the names are invented per scene by the
director. Bind a CC to `voice.deep_bass.level` and it dies the moment the
next scene loads with a voice called something else.

Voice slots
-----------
So bindings for instruments are stored against an ORDINAL, not a name:

    voice#0.level, voice#1.pan, voice#2.env.attack, ...

and the slot is resolved to whatever name currently occupies that index in
the live soundscape, at the moment the CC arrives. Nothing downstream
changes: `voice#2.pan` becomes `voice.shimmer_lead.pan` and goes through the
existing `Engine.set_audio_param` path untouched.

What makes slots meaningful rather than arbitrary is that the director is
asked to emit voices in a consistent priority order — foundation/bass first,
then pads, then leads and detail (see director/claude_director.py's prompt).
So CC 20 is "the low end" on every scene, and muscle memory survives a
scene change.

Two levels of binding
---------------------
    settings.json   the persistent, slot-based layout. This is the
                    CONTROLLER, and it deliberately does not change when a
                    scene loads.
    scene JSON      an optional name-based `midi_overrides` block, which
                    wins over the global map while that scene is loaded.

Resolution order for an incoming CC is: scene override -> global map ->
unmapped. Storing the whole map per-scene was considered and rejected — it
would change the controller layout on every scene load, which is the exact
problem slots exist to avoid.

Encoder modes (per binding, same three as the sibling)
    absolute   0..127 maps straight onto the value range
    relative   encoder deltas; auto-detects the two common signed encodings
    catch      absolute, but ignored until the knob crosses the current
               value ("soft takeover")

`catch` is the default for voice parameters specifically: loading a scene
replaces every level at once, so an absolute knob would snap the whole mix
the instant it was touched.
"""

from __future__ import annotations

import math
import re
import threading
import time

# Per-field value ranges. Kept SERVER-side rather than shipped up from the
# browser so MIDI keeps working with no UI open — the control surface is not
# a dependency of the control surface.
# These deliberately mirror the matching slider bounds in
# web/static/index.html one-for-one. A knob and its on-screen slider covering
# different ranges is the kind of mismatch that only shows up mid-set; if a
# bound changes there, change it here too.
ENGINE_RANGES = {
    "camera.speed": (0.0, 2.0),
    "camera.orbit_radius": (3.0, 20.0),
    "camera.fov": (30.0, 100.0),
    "camera.far": (6.0, 40.0),
    "camera.max_strokes": (20.0, 400.0),
    "crossfade": (0.0, 8.0),
    "audio_fade": (0.0, 16.0),
    "glow": (0.0, 1.0),
    "trail": (0.0, 0.95),
    "hue_value": (0.0, 1.0),
    "audio_link": (0.0, 2.0),
    "lfo_slow.rate": (0.01, 0.4),
    "lfo_mid.rate": (0.01, 0.8),
}

# `layer<N>.<param>` ranges, refreshed by the engine on every scene load from
# the generator registry's own `param_meta`. They can't be a constant here:
# which params exist, and over what range, is a property of whichever
# generator the loaded scene uses. Keeping them in one mutable dict means the
# registry stays the single source of truth for a param's bounds — the UI
# slider and a MIDI knob bound to the same param cover the same range by
# construction.
DYNAMIC_RANGES: dict[str, tuple] = {}


def set_dynamic_ranges(ranges: dict) -> None:
    DYNAMIC_RANGES.clear()
    DYNAMIC_RANGES.update(ranges)

# Soundscape globals — these go through Engine.set_audio_param, not set_param.
AUDIO_RANGES = {
    "master": (0.0, 1.0),
    "tempo": (20.0, 160.0),
    "distortion": (0.0, 1.0),
    "delay.time": (0.05, 1.2),
    "delay.feedback": (0.0, 0.9),
    "delay.mix": (0.0, 0.9),
    "eq.low": (-10.0, 10.0),
    "eq.mid": (-10.0, 10.0),
    "eq.high": (-10.0, 10.0),
    "swell_amount": (0.0, 1.0),
    "swell_period": (5.0, 120.0),
    # Surround only — the room breathing front-to-back. Mappable by MIDI learn
    # but given no default CC: it does nothing on a stereo rig, and the low
    # CCs are the scarce ones (a controller's first row of knobs).
    "swell_depth_amount": (0.0, 1.0),
}

# Per-voice fields, addressed by slot. Ranges mirror the UI's own knob bounds
# in web/static/index.html — if one changes there, change it here too.
VOICE_RANGES = {
    "level": (0.0, 1.0),
    "pan": (-1.0, 1.0),
    "depth": (0.0, 1.0),        # front/back; unipolar, unlike pan. Surround only.
    "sweep": (0.0, 1.0),        # this voice's share of the scene filter sweep
    "tone": (0.0, 1.0),
    "detune": (0.0, 0.05),
    "sub": (0.0, 1.0),
    "rate": (0.1, 4.0),
    "decay": (0.1, 6.0),
    "unison": (1.0, 7.0),
    "env.attack": (0.01, 15.0),
    "env.decay": (0.01, 8.0),
    "env.sustain": (0.0, 1.0),
    "env.release": (0.05, 10.0),
    # Per-voice LFO. Rate matches dsp.LFO_MAX_RATE — this is an ambient
    # instrument, and a knob that spends most of its travel on speeds nobody
    # wants is a knob with no useful resolution where it matters.
    "lfo.rate": (0.0, 0.5),
    "lfo.depth": (0.0, 1.0),
}

N_SLOTS = 8          # voice slots addressable from MIDI
_SLOT_RE = re.compile(r"^voice#(\d+)\.(.+)$")
_NAME_RE = re.compile(r"^voice\.([^.]+)\.(.+)$")


def _slot_layout(base_cc: int, field: str, n=N_SLOTS):
    return {base_cc + i: f"voice#{i}.{field}" for i in range(n)}


# Default layout. Low CCs are the always-there globals (a controller's first
# row of knobs); voice slots occupy contiguous banks of 8 from CC 20 up, so a
# typical 8-knob-per-row controller maps one row to one field across all
# voices — which is how you actually mix live (all levels, then all pans),
# rather than one voice's whole strip at a time.
DEFAULT_CC_MAP = {
    1: "master",
    2: "camera.speed",
    3: "distortion",
    4: "delay.mix",
    5: "eq.low",
    6: "eq.mid",
    7: "eq.high",
    8: "swell_amount",
    9: "camera.far",
    10: "camera.max_strokes",
    11: "audio_link",
    12: "crossfade",
    13: "glow",
    14: "trail",
    15: "camera.orbit_radius",
    16: "tempo",
    **_slot_layout(20, "level"),
    **_slot_layout(30, "pan"),
    **_slot_layout(40, "tone"),
    **_slot_layout(50, "env.attack"),
    **_slot_layout(60, "env.release"),
    # Depth sits at the far end of the default map on purpose: it is the one
    # bank that does nothing on a stereo rig, so it should not displace a
    # field that always works on a controller with fewer rows.
    **_slot_layout(70, "depth"),
}

DEFAULT_MODE = "catch"


def range_for(key: str):
    """Value range for a binding key, or None if it isn't a known parameter."""
    if key in ENGINE_RANGES:
        return ENGINE_RANGES[key]
    if key in DYNAMIC_RANGES:
        return DYNAMIC_RANGES[key]
    if key in AUDIO_RANGES:
        return AUDIO_RANGES[key]
    m = _SLOT_RE.match(key) or _NAME_RE.match(key)
    if m:
        return VOICE_RANGES.get(m.group(2))
    return None


def is_audio_key(key: str) -> bool:
    """Whether a key belongs to the synth (set_audio_param) rather than the
    engine (set_param)."""
    return (key in AUDIO_RANGES or _SLOT_RE.match(key) is not None
            or _NAME_RE.match(key) is not None)


def slot_index(key: str):
    m = _SLOT_RE.match(key)
    return int(m.group(1)) if m else None


def resolve_slot(key: str, voices: list) -> str | None:
    """`voice#2.pan` + the live voice list -> `voice.shimmer_lead.pan`.

    Returns None when the current scene simply has fewer voices than the
    slot being addressed — a knob pointing at a voice that doesn't exist
    right now is a no-op, not an error. That is the normal case: the
    controller has 8 slots, most scenes have 4-6 voices.
    """
    m = _SLOT_RE.match(key)
    if not m:
        return key
    idx, field = int(m.group(1)), m.group(2)
    if idx >= len(voices):
        return None
    name = voices[idx].get("name")
    return f"voice.{name}.{field}" if name else None


class MidiInput:
    """Owns the port, the bindings, and the CC -> parameter dispatch.

    `router` is the object that actually applies a change — see
    engine.MidiRouter. Kept behind that indirection so this module has no
    opinion about the engine's internals and can be tested with a stub.
    """

    def __init__(self, router, settings, port_hint=None):
        self.router = router
        self.settings = settings
        self.port = None
        self.port_name = "none"
        self.last_msg = 0.0
        self.msg_count = 0
        self.learn_key = None          # key awaiting a CC, or None
        self.last_cc = None            # (cc, value) — shown in the UI for sanity
        self.error = None
        self._lock = threading.Lock()
        self._caught = {}              # catch mode: has this knob caught up?
        self._catch_last = {}          # catch mode: previous raw CC value
        # Bindings are held in memory and written through on change.
        # settings.get() re-reads the JSON file on every call, which is fine
        # for a config page but not for these: the map is consulted once per
        # incoming CC (fast, continuous while a knob moves) and once per
        # state broadcast (20 Hz), and neither should be a disk read.
        self._cc_map = {int(cc): k for cc, k in
                        (settings.get("midi_cc_map", {}) or {}).items()}
        self._cc_mode = dict(settings.get("midi_cc_mode", {}) or {})
        self._ports = []
        self._ports_t = 0.0
        names = self.list_ports()
        chosen = self._pick(names, port_hint, settings.get("midi_port"))
        if chosen:
            self.open_port(chosen)
        elif names:
            print(f"[midi] no controller auto-selected (available: {names}) "
                  f"— pick one in Settings")
        else:
            print("[midi] no MIDI input ports found")

    # ---- ports -----------------------------------------------------------
    @staticmethod
    def list_ports():
        try:
            import mido
            return mido.get_input_names()
        except ImportError:
            return []
        except Exception as e:
            print(f"[midi] backend unavailable ({e}) — MIDI disabled")
            return []

    def ports_cached(self, ttl=2.0):
        """Port list for the UI, refreshed at most every `ttl` seconds.
        Enumerating ALSA/CoreMIDI ports is a real system call and `state()`
        is built 20 times a second for every connected client; a controller
        being plugged in is not something that needs noticing faster than
        this."""
        now = time.monotonic()
        if now - self._ports_t > ttl:
            self._ports_t = now
            self._ports = self.list_ports()
        return self._ports

    @staticmethod
    def available() -> bool:
        try:
            import mido           # noqa: F401
            return True
        except ImportError:
            return False

    @staticmethod
    def _pick(names, hint, saved):
        """CLI hint > saved setting > first real device. 'Midi Through' (the
        ALSA loopback that is always present) is never auto-picked — it
        swallows everything silently and looks exactly like a dead
        controller."""
        if not names:
            return None
        if hint:
            for n in names:
                if hint.lower() in n.lower():
                    return n
            print(f"[midi] no port matching '{hint}', have: {names}")
            return None
        if saved:
            if saved in names:
                return saved
            # ALSA client:port suffixes shift across reboots — match on the
            # device name with the numeric suffix stripped.
            base = re.sub(r"\s+\d+:\d+$", "", saved).lower()
            for n in names:
                if re.sub(r"\s+\d+:\d+$", "", n).lower() == base:
                    return n
            print(f"[midi] saved port '{saved}' not present")
        real = [n for n in names if "midi through" not in n.lower()]
        return real[0] if real else None

    def open_port(self, name):
        """(Re)connect at runtime. An empty name disconnects."""
        try:
            import mido
        except ImportError:
            self.error = "mido not installed (pip install mido python-rtmidi)"
            return False
        if self.port:
            try:
                self.port.close()
            except Exception:
                pass
        self.port = None
        self.port_name = "none"
        if not name:
            print("[midi] disconnected")
            return True
        try:
            self.port = mido.open_input(name, callback=self._on_msg)
        except Exception as e:
            self.error = f"could not open '{name}': {e}"
            print(f"[midi] {self.error}")
            return False
        self.error = None
        self.port_name = name
        self.settings.set("midi_port", name)
        print(f"[midi] listening on: {name}")
        return True

    def close(self):
        if self.port:
            try:
                self.port.close()
            except Exception:
                pass

    def active(self, window=0.6):
        return (time.monotonic() - self.last_msg) < window

    # ---- bindings --------------------------------------------------------
    @property
    def cc_map(self) -> dict:
        """Effective CC -> key map: the defaults, overlaid with the user's
        own bindings. A custom binding both moves its key off whatever
        default CC it had and takes the target CC from whatever default was
        using it — so a learned binding always wins outright and can never
        end up shadowed by the default it replaced."""
        custom = self._cc_map
        customized = set(custom.values())
        eff = {cc: k for cc, k in DEFAULT_CC_MAP.items()
               if k not in customized and cc not in custom}
        eff.update(custom)
        return eff

    def effective_map(self) -> dict:
        """`cc_map` with the loaded scene's own `midi_overrides` laid on top.

        Scene overrides are stored name-based (`voice.shimmer_lead.pan`)
        rather than slot-based, because their whole point is to pin a
        control to one specific voice in one specific scene — see this
        module's docstring."""
        eff = self.cc_map
        over = self.router.scene_overrides()
        if not over:
            return eff
        pinned = set(over)
        eff = {cc: k for cc, k in eff.items()
               if k not in pinned and cc not in {int(c) for c in over.values()}}
        for key, cc in over.items():
            eff[int(cc)] = key
        return eff

    def _persist(self):
        self.settings.set("midi_cc_map", {str(c): k for c, k in self._cc_map.items()})
        self.settings.set("midi_cc_mode", self._cc_mode)

    def bind(self, key: str, cc: int):
        """Bind a CC to a key, taking it from whatever held it before."""
        self._cc_map = {c: k for c, k in self._cc_map.items() if k != key}
        self._cc_map[int(cc)] = key
        self._persist()
        # A freshly-learned knob should take effect on the very next move
        # rather than making the user sweep it back to wherever the value
        # already was — you just told it what to control, that IS the
        # takeover. (Soft takeover still applies on later scene loads.)
        self._caught[key] = True
        print(f"[midi] CC {cc} -> {key}")

    def unmap(self, key: str):
        self._cc_map = {c: k for c, k in self._cc_map.items() if k != key}
        self._cc_mode.pop(key, None)
        self._caught.pop(key, None)
        self._persist()

    def set_mode(self, key: str, mode: str):
        if mode not in ("absolute", "relative", "catch"):
            return
        self._cc_mode[key] = mode
        self._caught.pop(key, None)
        self._persist()

    def mode_for(self, key: str) -> str:
        return self._cc_mode.get(key, DEFAULT_MODE)

    def rearm_takeover(self):
        """Re-arm soft takeover on every binding. Called on scene load: the
        scene has just replaced every value under the knobs, so a knob's
        physical position no longer reflects what it controls and must earn
        its way back before it moves anything."""
        self._caught.clear()
        self._catch_last.clear()

    def state(self) -> dict:
        return {
            "available": self.available(),
            "port": self.port_name,
            "ports": self.ports_cached(),
            "connected": self.port is not None,
            "active": self.active(),
            "count": self.msg_count,
            "learn": self.learn_key,
            "last_cc": self.last_cc,
            "map": {str(cc): k for cc, k in self.effective_map().items()},
            "overrides": self.router.scene_overrides(),
            "modes": self._cc_mode,
            "error": self.error,
        }

    # ---- dispatch --------------------------------------------------------
    def _on_msg(self, msg):
        """Called on mido's own listener thread. Everything it touches is
        either atomic or goes through Engine's action queue, so it never
        blocks the render loop."""
        self.last_msg = time.monotonic()
        self.msg_count += 1
        if msg.type != "control_change":
            return
        self.last_cc = [msg.control, msg.value]

        # Learn takes priority over any existing binding for this CC.
        with self._lock:
            learning = self.learn_key
            self.learn_key = None
        if learning:
            self.bind(learning, msg.control)
            return

        key = self.effective_map().get(msg.control)
        if not key:
            return
        rng = range_for(key)
        if rng is None:
            return
        self._apply(key, msg.value, rng[0], rng[1], self.mode_for(key))

    def _apply(self, key, value, lo, hi, mode):
        span = hi - lo
        if mode == "relative":
            # Auto-detect the two common signed encodings, which share this
            # shape: 1..63 is +delta, 65..127 is -delta. 0 and 64 are no-ops.
            if value in (0, 64):
                return
            delta = value if value < 64 else value - 128
            cur = self.router.get(key)
            if cur is None:
                return
            self.router.set(key, min(hi, max(lo, cur + delta * span / 127.0)))
            return

        target = lo + (value / 127.0) * span
        if mode == "catch":
            cur = self.router.get(key)
            if cur is None:
                return
            last = self._catch_last.get(key)
            self._catch_last[key] = value
            if not self._caught.get(key):
                # Catch when the knob sweeps ACROSS the current value (or
                # lands essentially on it) — comparing against the previous
                # knob position, not just the current one, so a fast sweep
                # past the value still catches instead of skipping over it.
                last_t = (lo + last / 127.0 * span) if last is not None else target
                if not (min(last_t, target) - 1e-9 <= cur <= max(last_t, target) + 1e-9):
                    return                     # still waiting for the knob
                self._caught[key] = True
        self.router.set(key, target)

    # ---- pinning ---------------------------------------------------------
    def pin_map_for(self, voices: list) -> dict:
        """Freeze the currently-resolved slot bindings into name-based ones
        for this specific set of voices — what the "Pin MIDI map to scene"
        button saves into the scene file.

        Only voice slots are pinned. Globals (`master`, `camera.speed`) mean
        the same thing in every scene, so pinning them would add noise to
        the scene file and, worse, freeze a layout the user later changes
        globally and can't work out why one scene ignores.
        """
        out = {}
        for cc, key in self.cc_map.items():
            if slot_index(key) is None:
                continue
            resolved = resolve_slot(key, voices)
            if resolved:
                out[resolved] = int(cc)
        return out

    # ---- learn -----------------------------------------------------------
    def arm_learn(self, key: str | None):
        """Arm (or, with the same key again / None, cancel) MIDI learn."""
        with self._lock:
            self.learn_key = None if (key is None or key == self.learn_key) else key
        return self.learn_key
