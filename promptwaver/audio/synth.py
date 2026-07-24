"""Realtime soundscape synth — wraps the numpy DSP core (dsp.py) with a
sounddevice output stream. No C compilation (unlike pyo); needs the PortAudio
*runtime* (`sudo apt install libportaudio2`) which sounddevice binds to.

If sounddevice or PortAudio isn't available, `make_synth` returns a NullSynth so
the rest of PromptWaver runs silently and unaffected.

Every callback is instrumented via `diagnostics.CallbackStats` so glitches can
be measured (render duration vs. budget, hardware-reported xruns, callback
interval jitter) instead of guessed at — see `promptwaver/audio/diagnostics.py`.
"""

from __future__ import annotations

import threading
import time

from .dsp import SoundscapeMixer, default_soundscape, SR
from .diagnostics import CallbackStats, list_devices


class NullSynth:
    online = False

    def start(self): pass
    def stop(self): pass
    def set_soundscape(self, spec, fade=0.0): pass
    def set_audio_param(self, path, value): pass
    def set_muted(self, muted, fade=0.0): pass
    def soundscape(self): return None
    def diagnostics(self): return None
    def vu(self): return None
    # legacy no-ops (old pad-synth interface)
    def set_patch(self, patch): pass
    def set_cutoff(self, hz): pass


class SoundscapeSynth:
    def __init__(self, sr: int = SR, blocksize: int = 8192, device=None,
                 latency="high"):
        import sounddevice as sd
        self._sd = sd
        self.sr = sr
        self.blocksize = blocksize
        self.device = device
        self.latency = latency
        self._lock = threading.Lock()
        self._scape = SoundscapeMixer(default_soundscape(), sr=sr)
        self._stream = None
        self.stats = CallbackStats(sr, blocksize)
        self.last_error = None
        self.requested_blocksize = blocksize
        # `online` used to be a fixed class attribute (True the moment this
        # class was instantiated, regardless of whether the stream actually
        # opened) — meaning a synth stuck in a broken/reverted state could
        # still report itself as healthy. It's now a real instance flag set
        # only on an actual successful stream start.
        self.online = False

    def _callback(self, outdata, frames, time_info, status):
        t0 = time.perf_counter()
        with self._lock:
            outdata[:] = self._scape.render(frames)
        self.stats.record(time.perf_counter() - t0, status)

    def start(self):
        self.stats = CallbackStats(self.sr, self.blocksize)
        self._stream = self._sd.OutputStream(
            samplerate=self.sr, channels=2, dtype="float32",
            blocksize=self.blocksize, latency=self.latency, device=self.device,
            callback=self._callback)
        self._stream.start()
        self.online = True

    def stop(self):
        if self._stream is not None:
            try:
                self._stream.stop(); self._stream.close()
            except Exception:
                pass
            self._stream = None
        self.online = False

    # Blocksizes tried, largest first, when a requested size fails to open.
    # Some backends (notably PulseAudio/PipeWire virtual devices, which is
    # what "default"/"pipewire"/"Default Sink" in the device list usually are)
    # cap how large a period/buffer they'll accept, independent of anything
    # PromptWaver does — so instead of just reverting to whatever was running
    # before, step down and use the largest size that actually opens.
    _SIZE_LADDER = [32768, 16384, 8192, 4096, 2048, 1024, 512]

    def reconfigure(self, *, device=None, blocksize=None, latency=None, sr=None,
                    use_ladder=False):
        """Stop and restart the stream with new I/O parameters, live.

        `use_ladder=True` (startup autodetect only) steps down through
        smaller sizes if the requested one won't open, so a fresh launch
        finds *something* that works. `use_ladder=False` (the default — used
        for explicit user requests, e.g. clicking Apply) tries ONLY the
        requested size: if it fails, the error is reported honestly and the
        last known-good config is restored, rather than silently substituting
        a different size and persisting it as the new normal. Silent
        substitution-and-remember was the actual bug behind "it keeps
        resetting to a smaller size" — a transient failure on an explicit
        8192 request would fall back to (say) 2048, persist 2048, and the
        *next* session would start from 2048 with no way back up without the
        user knowing that's what happened.
        """
        last_good = (self.device, self.blocksize, self.latency, self.sr)
        was_online = self.online
        self.stop()
        if device is not None:
            self.device = device
        if latency is not None:
            self.latency = latency
        if sr is not None:
            self.sr = int(sr)
            self._scape = SoundscapeMixer(self._scape.spec, sr=self.sr)

        requested = int(blocksize) if blocksize is not None else self.blocksize
        self.requested_blocksize = requested
        candidates = [requested]
        if use_ladder:
            candidates += [b for b in self._SIZE_LADDER if b < requested]

        self.last_error = None
        last_exc = None
        for size in candidates:
            self.blocksize = size
            try:
                self.start()
                if size != requested:
                    self.last_error = (f"requested {requested} not supported by this "
                                       f"device; running at {size} instead")
                    print(f"[promptwaver] audio: {self.last_error}")
                return
            except Exception as e:
                last_exc = e
                self.online = False
                continue

        # every candidate failed — report it plainly, and try to restore
        # whatever was actually working before rather than leaving audio dead
        detail = f"{type(last_exc).__name__}: {last_exc}" if last_exc else "unknown error"
        self.last_error = f"requested {requested} failed to open ({detail})"
        if was_online:
            self.device, self.blocksize, self.latency, self.sr = last_good
            try:
                self.start()
                self.last_error += f"; restored previous working blocksize {self.blocksize}"
            except Exception as e2:
                self.last_error += f"; could not restore previous config either: {e2}"
        print(f"[promptwaver] audio reconfigure failed: {self.last_error}")

    def set_soundscape(self, spec, fade=0.0):
        if not spec:
            return
        with self._lock:
            self._scape.set_spec(spec, fade=fade)

    def set_audio_param(self, path, value):
        with self._lock:
            self._scape.set_param(path, value)

    def set_muted(self, muted: bool, fade: float = 0.0):
        with self._lock:
            self._scape.set_muted(bool(muted), fade)

    def soundscape(self):
        return self._scape.spec

    def vu(self):
        """Post-master output level of the last rendered block, for the VU
        meter — read under the same lock the audio callback renders under, so
        it can't observe a torn mid-render state."""
        with self._lock:
            return {"peak": self._scape.last_peak, "clipping": self._scape.last_clip}

    def diagnostics(self):
        d = self.stats.summary()
        d["device"] = self.device
        d["latency"] = self.latency
        d["error"] = self.last_error
        d["requested_blocksize"] = self.requested_blocksize
        return d

    # legacy interface kept so older engine calls don't break
    def set_patch(self, patch):
        pass

    def set_cutoff(self, hz):
        pass


def make_synth(enable_audio: bool, **kw):
    """Returns (synth, error). error is None on success, else a short reason
    string surfaced to the UI so 'no audio' isn't a silent dead end."""
    if not enable_audio:
        return NullSynth(), None
    try:
        import sounddevice  # noqa: F401
        return SoundscapeSynth(**kw), None
    except Exception as e:
        msg = str(e)
        print(f"[promptwaver] audio unavailable ({msg}); running silent. "
              f"On Linux try: sudo apt install libportaudio2")
        return NullSynth(), msg
