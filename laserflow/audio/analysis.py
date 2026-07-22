"""Audio input analysis -> a modulation source.

Captures the default input (mic/line) and exposes a smoothed RMS level in
[0, 1] via `.level`. The engine copies this into the matrix's "audio_level"
Value source each tick, so incoming sound can drive the visuals. Uses
sounddevice if available; otherwise `.level` stays 0 and everything still runs.
"""

from __future__ import annotations

import numpy as np


class AudioAnalysis:
    def __init__(self, gain: float = 4.0, smooth: float = 0.15):
        self.level = 0.0
        self.gain = gain
        self.smooth = smooth
        self._stream = None
        try:
            import sounddevice as sd
            self._sd = sd
        except Exception:
            self._sd = None

    @property
    def online(self) -> bool:
        return self._sd is not None

    def _callback(self, indata, frames, time_info, status):
        rms = float(np.sqrt(np.mean(indata.astype(np.float32) ** 2)))
        target = min(1.0, rms * self.gain)
        self.level += (target - self.level) * self.smooth

    def start(self):
        if not self.online:
            return
        self._stream = self._sd.InputStream(
            channels=1, callback=self._callback, blocksize=1024)
        self._stream.start()

    def stop(self):
        if self._stream is not None:
            self._stream.stop()
            self._stream.close()
            self._stream = None
