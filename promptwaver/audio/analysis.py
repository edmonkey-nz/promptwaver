"""Audio input analysis -> a modulation source.

Captures the default input (mic/line) and exposes a smoothed RMS level in
[0, 1] via `.level`. The engine copies this into the matrix's "audio_level"
Value source each tick, so incoming sound can drive the visuals. Uses
sounddevice if available; otherwise `.level` stays 0 and everything still runs.

Kiosk mode also *keeps* the frames. The stream was already open and already
discarding them, so recording a visitor's spoken prompt costs one buffer and no
second device — see `arm()` below. `.level` is computed identically whether or
not the recorder is armed, so the mic modulation source behaves the same in
both modes.
"""

from __future__ import annotations

import numpy as np


class AudioAnalysis:
    def __init__(self, gain: float = 4.0, smooth: float = 0.15):
        self.level = 0.0
        self.gain = gain
        self.smooth = smooth
        self._stream = None
        # Recording state. All three are only touched by the callback while
        # `_recording` is True, and only set up/torn down while it's False.
        self._buf: np.ndarray | None = None   # preallocated on arm(), never grown
        self._n = 0                           # frames written into _buf
        self._recording = False
        self.samplerate = 0
        self.error = ""
        try:
            import sounddevice as sd
            self._sd = sd
        except Exception:
            self._sd = None

    @property
    def online(self) -> bool:
        return self._sd is not None

    @property
    def armed(self) -> bool:
        return self._buf is not None

    @property
    def recording(self) -> bool:
        return self._recording

    def _callback(self, indata, frames, time_info, status):
        rms = float(np.sqrt(np.mean(indata.astype(np.float32) ** 2)))
        target = min(1.0, rms * self.gain)
        self.level += (target - self.level) * self.smooth
        # This is a PortAudio REALTIME callback. The copy below writes into a
        # slice of a buffer allocated once in arm() — no append, no resize, no
        # allocation. Don't turn this into a list of blocks: that reintroduces
        # per-callback allocation on the realtime thread, and an unbounded one.
        # Running past the end simply stops recording (the hold cap), rather
        # than wrapping — a truncated prompt is recoverable, a wrapped one is
        # word salad.
        if self._recording:
            buf = self._buf
            if buf is None:
                return
            n = self._n
            room = buf.shape[0] - n
            if room <= 0:
                self._recording = False
                return
            take = frames if frames < room else room
            buf[n:n + take] = indata[:take, 0]
            self._n = n + take

    # --- recording ---------------------------------------------------------

    def arm(self, max_seconds: float):
        """Allocate the capture buffer. Called when kiosk mode is enabled.

        Sized for the longest hold we allow, so `_callback` can never need more
        room than it has: at 48kHz mono float32 that's ~2.9MB for 15s.
        """
        if not self.online or self._stream is None:
            return False
        self._recording = False
        self._n = 0
        frames = max(1, int(self.samplerate * max(1.0, float(max_seconds))))
        self._buf = np.zeros(frames, dtype=np.float32)
        return True

    def disarm(self):
        self._recording = False
        self._buf = None
        self._n = 0

    def start_record(self) -> bool:
        if self._buf is None:
            return False
        self._n = 0
        self._recording = True
        return True

    def stop_record(self) -> tuple[np.ndarray, int]:
        """Return `(mono float32 frames, samplerate)` captured since start_record."""
        self._recording = False
        if self._buf is None or self._n <= 0:
            return np.zeros(0, dtype=np.float32), self.samplerate
        # Copy: the buffer is reused by the next recording.
        return self._buf[:self._n].copy(), self.samplerate

    # --- stream ------------------------------------------------------------

    def start(self):
        # Never raises. A box with no input device at all is a perfectly normal
        # PromptWaver rig (the instrument makes its own sound), and this used to
        # take the whole engine down with it — Engine.start() calls this before
        # the render thread exists, so an InputStream that wouldn't open meant
        # no visuals, no audio and no UI, from a subsystem nothing requires.
        # `.level` staying 0 is the documented no-mic behaviour; kiosk mode
        # checks `_stream` (via arm()) and reports the reason itself.
        self.error = ""
        if not self.online:
            self.error = "sounddevice not installed"
            return
        try:
            self._stream = self._sd.InputStream(
                channels=1, callback=self._callback, blocksize=1024)
            self._stream.start()
            # Whatever the device gave us — asking for 16k outright fails on
            # plenty of interfaces, so take the native rate and resample at
            # transcribe time instead.
            self.samplerate = int(self._stream.samplerate)
        except Exception as e:
            self._stream = None
            self.error = str(e)
            print(f"[promptwaver] no audio input: {e}")

    def stop(self):
        self.disarm()
        if self._stream is not None:
            self._stream.stop()
            self._stream.close()
            self._stream = None
