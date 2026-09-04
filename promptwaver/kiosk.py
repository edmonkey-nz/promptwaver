"""Kiosk mode — a one-shot, speak-a-world surface for public installations.

A visitor holds one button, says what they want to see, and their world fades
in and plays. No library, no parameters, no settings: everything the dev UI
exposes is simply absent from `web/static/kiosk.html`.

This is a runtime TOGGLE, not a separate program. `KioskSession` is constructed
unconditionally and costs nothing while disabled; `enable()` is the arming step
that loads the speech model, allocates the mic buffer and installs the attract
scene. That way the same process can go back to being a development instrument
without a restart.

Three things here deliberately diverge from the rest of the engine:

1. **`generate()` installs its scene while the engine stays ACTIVE.**
   `Engine.generate_scene` stops the engine first (so the dev UI's "Start
   scene" button sits on top of a scene that hasn't started), and
   `_install_spec` skips the crossfade when inactive. The kiosk wants the
   opposite: the attract scene must cross-fade *into* the visitor's world.

2. **Effort is set in memory, and restored on disable.** `director.set_effort`
   persists to the shared settings.json, which would silently rewrite the
   operator's own configuration on the same checkout.

3. **Generated scenes are archived to their own directory**, not the tracked
   `scenes/` library, so an unattended run doesn't churn the working tree.
"""

from __future__ import annotations

import json
import os
import random
import threading
import time

import numpy as np

from . import settings
from . import generators as gen
from .scenes import Layer, SceneManager

# Phases the kiosk page renders. `playing` is a resting state like `idle` —
# both accept a button press; nothing else does.
IDLE = "idle"
RECORDING = "recording"
TRANSCRIBING = "transcribing"
CONFIRM = "confirm"
GENERATING = "generating"
PLAYING = "playing"
ERROR = "error"

# The visitor is holding a button down, so these are generous rather than tight.
MIN_HOLD = 0.7          # shorter than this is a stray tap, not a prompt
MAX_HOLD = 15.0         # also the size of the mic buffer
ERROR_LINGER = 6.0      # how long an error stays on screen before returning to idle

# What happens after a visitor's world is playing. It plays clean for
# PLAY_HINT_AFTER seconds — that's their moment, and a button over it would be
# the wrong thing — then a small "make your own" prompt fades in so the NEXT
# person knows the installation is theirs to use. After PLAY_TIMEOUT with
# nobody pressing, it returns to the attract loop on its own, so a room that
# emptied out doesn't sit on one visitor's scene all evening.
PLAY_HINT_AFTER = 25.0
PLAY_TIMEOUT = 300.0

# How long the "is this right?" screen waits before giving up. Someone who
# speaks and walks away must not leave the kiosk holding a prompt — and must
# not be billed for a scene nobody is standing there to watch, so this returns
# to idle rather than proceeding.
CONFIRM_TIMEOUT = 45.0

DEFAULT_MODEL = "base.en"

# --- what a visitor's scene is asked for, and what it looks like ------------
#
# Two different mechanisms, deliberately kept in one settings dict because the
# operator doesn't care which is which — they're all "how kiosk scenes come
# out". The split matters when editing:
#
#   PROMPT-SIDE  (interpretation, exclude_figures, warmth/energy/evolution)
#       change what Claude is ASKED for, so they are part of the cache key and
#       only take effect on a fresh generation.
#   POST-GENERATION  (shape_speed, glow, glow_random, trail_chance)
#       are applied to whatever comes back, cache hit or not, so they change
#       the look immediately and re-roll per visitor.
DEFAULT_GEN = {
    # Show the visitor what was heard before spending anything on it. Whisper
    # mishears, and without this the first time they see their words is halfway
    # through a generation they've already paid for.
    "confirm_prompt": True,
    # Scene richness. `nodes` scales the world; `effort` is the token budget and
    # object-count hint (low/med/high). The kiosk asks for the node count with
    # want_path=False — it wants the size control WITHOUT the camera route,
    # which measured at roughly double the time and cost.
    "nodes": 120,
    "effort": "low",
    # Free text appended to every visitor's prompt, after the interpretation
    # and figures directives. House style, a theme for the night, whatever.
    "prompt_suffix": "",
    "interpretation": 0.5,   # 0 = literal and recognisable, 1 = loose and abstract
    "exclude_figures": True, # wireframe people read as stick men; off by default
    "warmth": 0.5,
    "energy": 0.5,
    "evolution": 0.5,
    "shape_speed": 1.0,      # world's own motion-rate param
    "glow": 0.35,            # base bloom for every visitor scene
    "glow_random": 0.25,     # +/- spread rolled per scene
    "trail_chance": 0.25,    # how often a scene gets trails at all
}

# --- rejecting audio that isn't a prompt ------------------------------------
#
# Whisper HALLUCINATES CONFIDENT TEXT FROM SILENCE. Measured here: 2.5s of an
# empty room produced "My turn. I'll tell you that. . . . . ." — which passed
# a naive "is the transcript non-empty" check, and cost a real API call to
# render as a scene. In a public space that happens on every stray touch, so
# the guards below are about money and nonsense on screen, not tidiness.
MIN_PEAK = 0.01          # ~-40dBFS; below this nobody spoke into the mic
MAX_NO_SPEECH = 0.6      # faster-whisper's own per-segment silence probability
MIN_AVG_LOGPROB = -1.0   # and its confidence; hallucinations score poorly on both
MIN_WORDS = 2
MIN_LETTERS = 6


def _clean_transcript(text: str) -> str:
    """Collapse the punctuation runs Whisper emits on silence, and normalise."""
    import re
    text = re.sub(r"[\s.]*\.[\s.]*\.[\s.]*", " ", text)   # ". . . ." runs
    return re.sub(r"\s+", " ", text).strip()


def _is_prompt(text: str) -> bool:
    """Enough real words to be someone describing a world."""
    import re
    words = [w for w in re.findall(r"[A-Za-z']+", text) if w]
    letters = sum(len(w) for w in words)
    return len(words) >= MIN_WORDS and letters >= MIN_LETTERS

# Model files are fetched over plain HTTPS into our own cache rather than
# through huggingface_hub. Its current transfer backend (hf_xet) was measured
# stalling indefinitely at 0 bytes on the main weights file while curl pulled
# the same URL at 16MB/s — and a hang inside enable() is the worst possible
# failure for this feature, because it looks like the app froze. Four files,
# one predictable directory, a timeout on every request.
_MODEL_REPO = "https://huggingface.co/Systran/faster-whisper-{size}/resolve/main"
_MODEL_FILES = ("model.bin", "config.json", "tokenizer.json", "vocabulary.txt")
_MODEL_CACHE = os.path.join(os.path.expanduser("~"), ".cache", "promptwaver")
_DOWNLOAD_TIMEOUT = 120.0


# Anything not listed is a plain 0..1 slider.
GEN_RANGES = {"shape_speed": (0.0, 3.0), "nodes": (40, 600)}
EFFORTS = ("low", "med", "high")
MAX_SUFFIX = 400        # it rides in every request; a runaway paste is a bill


def _model_dir(size: str) -> str:
    return os.path.join(_MODEL_CACHE, f"whisper-{size}")


class _ForceIPv4:
    """Temporarily hide AAAA records from `socket.getaddrinfo`.

    Some hosts advertise an IPv6 route that doesn't actually carry traffic.
    Python's HTTP stack takes the first address `getaddrinfo` returns and
    blocks on it, so the download stalls at zero bytes with no error — curl
    survives the same network because it races both families (Happy Eyeballs)
    and Python does not. Only used as a RETRY, so a working IPv6 network is
    still used normally.
    """

    def __enter__(self):
        import socket
        self._real = socket.getaddrinfo

        def ipv4_only(host, port, family=0, *a, **kw):
            return self._real(host, port, socket.AF_INET, *a, **kw)

        socket.getaddrinfo = ipv4_only
        return self

    def __exit__(self, *exc):
        import socket
        socket.getaddrinfo = self._real
        return False


class _NullCtx:
    def __enter__(self): return self
    def __exit__(self, *exc): return False


def _ipv6_usable(host: str = "huggingface.co", timeout: float = 2.5) -> bool:
    """Can we actually open a TCP connection over IPv6?

    Probed rather than inferred, and probed CHEAPLY: retrying the real download
    to discover this costs a full connect timeout (minutes on a 145MB file)
    before the fallback gets a turn, which is indistinguishable from a hang.
    """
    import socket
    try:
        info = socket.getaddrinfo(host, 443, socket.AF_INET6, socket.SOCK_STREAM)
    except Exception:
        return False
    if not info:
        return False
    s = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect(info[0][4])
        return True
    except Exception:
        return False
    finally:
        s.close()


def _fetch(url: str, dest: str):
    """Download one file to `dest`, via .part so a partial can't look complete."""
    import urllib.request
    tmp = dest + ".part"
    with urllib.request.urlopen(url, timeout=_DOWNLOAD_TIMEOUT) as r, open(tmp, "wb") as out:
        while True:
            chunk = r.read(1 << 20)
            if not chunk:
                break
            out.write(chunk)
    os.replace(tmp, dest)


def _ensure_model(size: str) -> tuple[str, bool]:
    """Return `(local model directory, downloaded_now)`, fetching if needed.

    A `size` that is already a readable directory is taken as-is, so an
    operator can point `kiosk_model` at a model they placed themselves.
    """
    if os.path.isdir(size):
        return size, False
    d = _model_dir(size)
    if all(os.path.exists(os.path.join(d, f)) for f in _MODEL_FILES):
        return d, False

    os.makedirs(d, exist_ok=True)
    base = _MODEL_REPO.format(size=size)
    # One probe for the whole download, not one retry per file.
    guard = _NullCtx() if _ipv6_usable() else _ForceIPv4()
    # Smallest first: a broken network fails in a second on config.json rather
    # than after minutes of a stalled 145MB weights file.
    with guard:
        # Smallest first: a broken network fails in a second on config.json
        # rather than after minutes of a stalled 145MB weights file.
        for f in sorted(_MODEL_FILES, key=lambda n: n == "model.bin"):
            dest = os.path.join(d, f)
            if os.path.exists(dest):
                continue
            _fetch(f"{base}/{f}", dest)
    return d, True


def _resample_to_16k(pcm: np.ndarray, samplerate: int) -> np.ndarray:
    """Linear resample to the 16kHz mono float32 faster-whisper expects.

    Deliberately not scipy: this runs on a few seconds of speech, where linear
    interpolation is inaudible against the model's own error, and the project
    already carries numpy but not scipy.
    """
    if samplerate == 16000 or pcm.size == 0:
        return pcm.astype(np.float32, copy=False)
    n_out = int(round(pcm.size * 16000.0 / float(samplerate)))
    if n_out <= 1:
        return np.zeros(0, dtype=np.float32)
    x = np.linspace(0.0, pcm.size - 1.0, n_out, dtype=np.float64)
    return np.interp(x, np.arange(pcm.size, dtype=np.float64), pcm).astype(np.float32)


class KioskSession:
    """Owns kiosk phase, the speech model, and the one-visitor-at-a-time lock."""

    def __init__(self, engine, archive_dir: str, model_size: str | None = None):
        self.engine = engine
        self.archive_dir = archive_dir
        self.model_size = model_size or settings.get("kiosk_model", DEFAULT_MODEL)

        self.enabled = False
        self.phase = IDLE
        self.transcript = ""
        self.error = ""
        self._phase_t = time.monotonic()
        self._hold_t = 0.0
        self._lock = threading.Lock()

        self.gen = dict(DEFAULT_GEN)
        self.gen.update(settings.get("kiosk_gen", {}) or {})

        self._model = None          # kept resident across disable/enable
        self._archive: SceneManager | None = None
        self._prev_effort: str | None = None

    # --- toggle ------------------------------------------------------------

    @property
    def attract(self) -> str:
        return str(settings.get("kiosk_attract", "") or "")

    def enable(self, attract: str | None = None) -> tuple[bool, str]:
        """Arm the kiosk. Returns `(ok, detail)`; stays disabled on failure.

        Idempotent, and cheap the second time — the speech model is kept
        resident when disabled precisely so toggling back on is instant.
        """
        if attract is not None:
            settings.set("kiosk_attract", attract)
        ok, detail = self._load_model()
        if not ok:
            self.error = detail
            return False, detail

        if not self.engine.analysis.arm(MAX_HOLD):
            why = self.engine.analysis.error or "no input device"
            detail = f"no microphone available — {why}"
            self.error = detail
            return False, detail

        os.makedirs(self.archive_dir, exist_ok=True)
        self._archive = SceneManager(self.archive_dir)

        # In memory only — see the module docstring.
        if self._prev_effort is None:
            self._prev_effort = self.engine.director.effort
        self.engine.director.effort = self.gen.get("effort", "low")

        self.enabled = True
        self.error = ""
        self._set_phase(IDLE)
        self._install_attract()
        settings.set("kiosk_enabled", True)
        return True, f"kiosk armed ({self.model_size})"

    def disable(self) -> tuple[bool, str]:
        """Disarm. Deliberately gentle: whatever is playing keeps playing.

        Yanking the visuals back to a dev scene would cut off whoever is
        watching, and the operator can pick a scene themselves afterwards.
        """
        self.enabled = False
        self.engine.analysis.disarm()
        if self._prev_effort is not None:
            self.engine.director.effort = self._prev_effort
            self._prev_effort = None
        self._set_phase(IDLE)
        self.transcript = ""
        self.error = ""
        settings.set("kiosk_enabled", False)
        return True, "kiosk disarmed"

    def _load_model(self) -> tuple[bool, str]:
        if self._model is not None:
            return True, "already loaded"
        try:
            from faster_whisper import WhisperModel
        except Exception:
            return False, ("faster-whisper is not installed — "
                           "pip install 'faster-whisper'")
        try:
            path, fetched = _ensure_model(self.model_size)
        except Exception as e:
            return False, (f"could not download the speech model "
                           f"{self.model_size!r}: {e}")
        try:
            # int8 on CPU: this box is also rendering at 45fps and synthesising
            # audio, so the small/fast configuration is the right trade.
            self._model = WhisperModel(path, device="cpu", compute_type="int8")
        except Exception as e:
            return False, f"could not load speech model {self.model_size!r}: {e}"
        return True, "downloaded and loaded" if fetched else "loaded"

    def _install_attract(self):
        """Put the attract loop on screen and make sure it's playing."""
        name = self.attract
        spec = None
        if name:
            try:
                spec = self.engine.scenes.load_spec(name)
            except Exception:
                spec = None
        if spec is None:
            from .director import local_scene
            spec = local_scene("slow shifting pattern", "2d")
            spec.name = "kiosk attract"

        def apply():
            self.engine._install_spec(spec)
            self.engine._current_library_name = name or ""
        self.engine._enqueue(apply)
        self.engine.set_active(True)

    def set_gen(self, values: dict) -> dict:
        """Merge operator settings from /kiosk-settings and persist them.

        Unknown keys are dropped rather than stored: this dict is read straight
        into prompt text and scene params, so it stays a closed set.
        """
        for k, v in (values or {}).items():
            if k not in DEFAULT_GEN:
                continue
            if isinstance(DEFAULT_GEN[k], bool):
                self.gen[k] = bool(v)
                continue
            if k == "effort":
                if v in EFFORTS:
                    self.gen[k] = v
                continue
            if isinstance(DEFAULT_GEN[k], str):
                self.gen[k] = str(v)[:MAX_SUFFIX]
                continue
            lo, hi = GEN_RANGES.get(k, (0.0, 1.0))
            val = max(lo, min(hi, float(v)))
            self.gen[k] = int(val) if isinstance(DEFAULT_GEN[k], int) else val
        settings.set("kiosk_gen", self.gen)
        return self.gen

    def _style(self) -> str:
        """The extra direction appended to the director's prompt.

        Only the ends of the interpretation slider say anything — the middle is
        silence, so a neutral setting costs no tokens and biases nothing.
        """
        out = []
        interp = float(self.gen.get("interpretation", 0.5))
        if interp >= 0.65:
            out.append(
                "Interpretation: LOOSE AND ABSTRACT. Evoke the feeling, rhythm and "
                "forms of the subject rather than depicting it literally. Favour "
                "geometric abstraction, repetition and structure over recognisable "
                "objects — someone should feel the subject before they can name it.")
        elif interp <= 0.35:
            out.append(
                "Interpretation: LITERAL. Build the actual objects and place the "
                "words name, clearly readable as what they are. Favour recognisable "
                "silhouettes and correct proportions over abstraction.")
        if self.gen.get("exclude_figures", True):
            out.append(
                "IMPORTANT: include NO human or animal figures — no people, no "
                "bodies, no faces, no limbs, no creatures. Wireframe figures read as "
                "crude stick men and spoil the scene. Build the PLACE and its "
                "objects, never its inhabitants.")
        extra = str(self.gen.get("prompt_suffix", "") or "").strip()
        if extra:
            out.append(extra)
        return " ".join(out)

    def _apply_look(self, spec):
        """Post-generation look, re-rolled per visitor.

        Applied to cache hits too — which is the point: two visitors saying the
        same words get the same world, and it should not look identical.
        """
        g = self.gen
        speed = float(g.get("shape_speed", 1.0))
        for layer in spec.layers or []:
            # `spec.layers` is list[Layer|dict] — SceneSpec.from_dict turns
            # layer dicts into Layer dataclasses, so anything coming back from
            # the director (or off disk) holds Layers, while a spec built by
            # hand may still hold dicts. Reading only one shape is what broke
            # every kiosk generation once this method existed.
            if isinstance(layer, Layer):
                params = layer.params
            elif isinstance(layer, dict):
                params = layer.get("params")
            else:
                continue
            if not isinstance(params, dict):
                continue
            # Set it whether or not the key is already there. The model writes
            # only `defs` and `nodes` — `shape_speed` is a DEFAULT on the
            # generator class, so a "only overwrite what exists" test silently
            # did nothing on every real generated scene. Which generators own
            # the param comes from the registry rather than a hardcoded
            # "world", so a second generator declaring it is covered for free.
            cls = gen.get(layer.generator if isinstance(layer, Layer)
                          else layer.get("generator", ""))
            if cls is not None and "shape_speed" in (getattr(cls, "defaults", None) or {}):
                params["shape_speed"] = speed

        spread = float(g.get("glow_random", 0.0))
        glow = float(g.get("glow", 0.0)) + random.uniform(-spread, spread)
        cam = dict(spec.camera or {})
        cam["glow"] = max(0.0, min(1.0, glow))
        cam["trail"] = (round(random.uniform(0.3, 0.65), 3)
                        if random.random() < float(g.get("trail_chance", 0.0)) else 0.0)
        spec.camera = cam
        return spec

    # --- the archive of visitors' scenes ------------------------------------

    def _archive_mgr(self):
        """The archive SceneManager, built on demand.

        `enable()` also builds one, but the settings page must be able to list
        and prune scenes while the kiosk is disarmed.
        """
        if self._archive is None:
            os.makedirs(self.archive_dir, exist_ok=True)
            self._archive = SceneManager(self.archive_dir)
        return self._archive

    def archive_list(self) -> list[dict]:
        """`[{name, prompt, ts}, ...]`, newest first.

        Reads every file, so it is answered on demand over its own websocket
        command and deliberately NOT included in `state()` — that runs 20x a
        second forever and this is disk I/O on the render thread's GIL.
        """
        out = []
        try:
            names = os.listdir(self.archive_dir)
        except OSError:
            return out
        for f in names:
            if not f.endswith(".json"):
                continue
            path = os.path.join(self.archive_dir, f)
            prompt = ""
            try:
                with open(path) as fh:
                    prompt = (json.load(fh).get("image_prompt") or "").strip()
            except Exception:
                pass    # a half-written or corrupt file still lists, so it can be deleted
            try:
                ts = os.path.getmtime(path)
            except OSError:
                ts = 0.0
            name = os.path.splitext(f)[0]
            out.append({"name": name, "prompt": prompt or name, "ts": round(ts, 3)})
        out.sort(key=lambda e: e["ts"], reverse=True)
        return out

    def archive_delete(self, names: list[str] | None = None, every: bool = False) -> int:
        """Delete named archived scenes, or all of them. Returns how many went."""
        mgr = self._archive_mgr()
        if every:
            names = [e["name"] for e in self.archive_list()]
        gone = 0
        for n in names or []:
            try:
                # Goes through SceneManager.delete, whose path_for() strips the
                # name to alnum/space/_/- — so a crafted name can't escape the
                # archive directory.
                mgr.delete(n)
                gone += 1
            except Exception as e:
                print(f"[promptwaver] kiosk: could not delete {n!r}: {e}")
        return gone

    # --- state -------------------------------------------------------------

    def _set_phase(self, phase: str):
        self.phase = phase
        self._phase_t = time.monotonic()

    def state(self) -> dict:
        if not self.enabled:
            # `attract` is carried even while off so the Settings field can
            # show the saved value on a fresh page load.
            return {"enabled": False, "attract": self.attract, "gen": dict(self.gen)}
        return {
            "enabled": True,
            "attract": self.attract,
            "phase": self.phase,
            "transcript": self.transcript,
            "error": self.error,
            "elapsed": round(time.monotonic() - self._phase_t, 2),
            "max_hold": MAX_HOLD,
            "hint_after": PLAY_HINT_AFTER,
            "confirm_timeout": CONFIRM_TIMEOUT,
            "gen": dict(self.gen),
        }

    def tick(self):
        """Called from the render loop: retire the error, and time out a scene.

        Cheap enough to run at `fps` — two float compares while armed, and an
        immediate return when not.
        """
        if not self.enabled:
            return
        age = time.monotonic() - self._phase_t
        if self.phase == ERROR and age > ERROR_LINGER:
            self.error = ""
            self.transcript = ""
            self._set_phase(IDLE)
        elif self.phase == CONFIRM and age > CONFIRM_TIMEOUT:
            self.transcript = ""
            self._set_phase(IDLE)
        elif self.phase == PLAYING and age > PLAY_TIMEOUT:
            # Back to the attract loop, ready for whoever turns up next.
            self.transcript = ""
            self._set_phase(IDLE)
            self._install_attract()

    # --- the visitor's sequence --------------------------------------------

    def press(self) -> bool:
        """Button down. Ignored unless we're resting — this is the busy lock.

        `SceneDirector` has no concurrency guard of its own (two generates share
        one director's `last_*` fields), so nothing else may start one while a
        session is in flight.
        """
        with self._lock:
            # CONFIRM is included so holding the button again is a second way
            # to say "no, let me redo that" — the same gesture they already know.
            if not self.enabled or self.phase not in (IDLE, PLAYING, ERROR, CONFIRM):
                return False
            if not self.engine.analysis.start_record():
                self.error = "microphone is not armed"
                self._set_phase(ERROR)
                return False
            self.transcript = ""
            self.error = ""
            self._hold_t = time.monotonic()
            self._set_phase(RECORDING)
            return True

    def release(self) -> np.ndarray | None:
        """Button up. Returns the captured audio, or None if this wasn't a real hold."""
        with self._lock:
            if self.phase != RECORDING:
                return None
            held = time.monotonic() - self._hold_t
            pcm, rate = self.engine.analysis.stop_record()
            if held < MIN_HOLD or pcm.size == 0:
                self.error = "Hold the button while you speak"
                self._set_phase(ERROR)
                return None
            self._set_phase(TRANSCRIBING)
        self._pcm_rate = rate
        return pcm

    def run(self, pcm: np.ndarray):
        """Transcribe then generate. Blocking — call it in an executor."""
        # Cheapest gate first: if nothing was loud enough to be a voice, don't
        # even run the model — that's what invents words out of room tone.
        peak = float(np.abs(pcm).max()) if pcm.size else 0.0
        if peak < MIN_PEAK:
            self._fail("I didn't hear anything — hold the button and speak up")
            return
        text = ""
        try:
            text = self._transcribe(pcm, getattr(self, "_pcm_rate", 16000))
        except Exception as e:
            self._fail(f"could not understand the audio ({e})", e)
            return
        text = _clean_transcript(text or "")
        if not _is_prompt(text):
            self._fail("Didn't catch that — try again")
            return
        self.transcript = text
        if self.gen.get("confirm_prompt", True):
            # Stop here and let them read it back. `confirm()` picks it up.
            self._set_phase(CONFIRM)
            return
        self._build(text)

    def _build(self, text: str):
        """Generate and install. Shared by the confirmed and unconfirmed paths."""
        self._set_phase(GENERATING)
        try:
            self._generate(text)
        except Exception as e:
            self._fail(f"could not build that scene ({e})", e)
            return
        self._set_phase(PLAYING)

    def confirm(self) -> bool:
        """Visitor accepted the transcript. Blocking — call it in an executor."""
        with self._lock:
            if self.phase != CONFIRM:
                return False
            text = self.transcript
        self._build(text)
        return True

    def retry(self) -> bool:
        """Visitor rejected the transcript — back to idle, ready to record again."""
        with self._lock:
            if self.phase != CONFIRM:
                return False
            self.transcript = ""
            self.error = ""
            self._set_phase(IDLE)
        return True

    def _fail(self, message: str, exc: BaseException | None = None):
        # The visitor gets `message`; the operator's terminal gets the whole
        # traceback. Reporting only the former is what made an AttributeError
        # in _apply_look look like a mysterious "could not build that scene".
        if exc is not None:
            import traceback
            print(f"[promptwaver] kiosk: {message}")
            traceback.print_exception(type(exc), exc, exc.__traceback__)
        self.error = message
        self._set_phase(ERROR)

    def _transcribe(self, pcm: np.ndarray, samplerate: int) -> str:
        if self._model is None:
            raise RuntimeError("speech model not loaded")
        audio = _resample_to_16k(pcm, samplerate)
        segments, _info = self._model.transcribe(audio, language="en",
                                                 beam_size=1, vad_filter=True)
        # Drop segments the model itself is unsure about. `no_speech_prob` and
        # `avg_logprob` are exactly how a silence hallucination announces
        # itself, and they cost nothing to read.
        kept = [s.text for s in segments
                if getattr(s, "no_speech_prob", 0.0) <= MAX_NO_SPEECH
                and getattr(s, "avg_logprob", 0.0) >= MIN_AVG_LOGPROB]
        return " ".join(kept)

    def _generate(self, keyword: str):
        """Mirror of Engine.generate_scene, with the two kiosk divergences."""
        director = self.engine.director
        g = self.gen
        # use_cache=False ON PURPOSE, and it is the one place in the app that
        # does this. A cache hit would be free and instant, but the prompts
        # likely to collide are the short common ones — "space", "the ocean",
        # "a forest" — which are also the ones most likely to have produced a
        # weak scene, because they give the model least to work with. So a poor
        # result would stick to that phrase for every future visitor and never
        # self-correct, invisibly. Uniqueness is also the premise here: two
        # people saying the same words should not get the same world.
        #
        # Note this skips the cache READ only; generate() still writes the
        # entry, which is harmless (idempotent, and the scene is archived
        # separately anyway). The Generate panel keeps its cache, where
        # re-running a prompt while iterating genuinely saves money.
        # `size="small"` deliberately, and it is a SPEED choice with a known
        # side effect: `_resolve_size("small")` emits no size directive, and the
        # "mode":"path" + waypoints instruction lives entirely in that
        # directive — so kiosk scenes get orbit/drift, never a camera route.
        # Measured on one prompt: 27.2s / $0.018 this way against 58.4s /
        # $0.040 at 120 nodes with a path. A visitor waiting is the scarcer
        # resource here, so the route goes.
        # want_path=False: the node count controls scale, but the camera route
        # it would otherwise pull in measured 58.4s/$0.040 against 27.2s/$0.018.
        director.effort = g.get("effort", "low")
        spec = director.generate(keyword, use_cache=False, want_path=False,
                                 size=int(g.get("nodes", 120)), kind="3d",
                                 warmth=g.get("warmth"), energy=g.get("energy"),
                                 evolution=g.get("evolution"), style=self._style())
        self._apply_look(spec)
        name = f"{time.strftime('%Y%m%d-%H%M%S')} {keyword}"[:80]
        spec.name = name
        spec.image_prompt = keyword
        spec.audio_prompt = ""
        spec.generation_settings = {
            "size": int(g.get("nodes", 120)),
            "effort": g.get("effort", "low"),
            "warmth": g.get("warmth"), "energy": g.get("energy"),
            "evolution": g.get("evolution"),
            "kind": "3d", "cost": director.last_cost,
            "expansion": director.last_expansion,
            # Recorded so an archived scene says which kiosk settings produced
            # it — otherwise a room full of scenes is unattributable later.
            "kiosk": True,
            "kiosk_gen": dict(g),
        }
        if self._archive is not None:
            try:
                self._archive.save(name, spec)
            except Exception as e:
                print(f"[promptwaver] kiosk: could not archive generation: {e}")

        def apply():
            # NO _set_active_now(False) here — that's the whole point. The
            # engine stays active, so _install_spec uses the real crossfade and
            # the attract scene dissolves into the visitor's world.
            self.engine._install_spec(spec)
            self.engine._current_library_name = ""
        self.engine._enqueue(apply)
