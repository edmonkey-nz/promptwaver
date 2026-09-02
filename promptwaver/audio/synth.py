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

import queue
import sys
import threading
import time

from .dsp import SoundscapeMixer, default_soundscape, SR
from .diagnostics import CallbackStats, list_devices

#: Blocks rendered AHEAD of the audio callback, by a normal Python thread.
#:
#: The callback used to call `Soundscape.render()` directly, which meant the
#: realtime audio thread had to win the GIL against the 45fps visual render
#: thread and the ~20Hz websocket broadcaster before it could produce a single
#: sample. Measured: a soundscape costing 14.5ms on its own took 28ms with one
#: competing CPU-bound thread and spiked to 43ms; in the real app the callback
#: reported a 75ms average and a 368ms max against a 186ms budget, with the
#: callback itself being *delivered* late (avg interval 200ms vs 186 expected).
#: That is what the dropouts were — not DSP cost, which had plenty of headroom.
#:
#: Now the callback only copies an already-rendered block. It does no numpy
#: work, allocates nothing, and takes no lock, so GIL pressure can delay the
#: *producer* without the output ever gapping: the producer needs ~15-30ms to
#: fill a block the callback consumes every 186ms, so it can fall behind by an
#: order of magnitude and still keep up.
#:
#: The cost is latency — a parameter change is heard this many blocks later,
#: and `output_latency` accounts for it so audio-driven visuals stay in sync.
#: So this is kept at the smallest value that measures clean rather than the
#: largest that fits: at 1 (370ms total) there were zero underruns with six
#: competing CPU-bound threads AND repeated 250ms stalls holding the render
#: lock; depth 2 was no better and costs another 186ms of lag.
#:
#: **Depth does not buy resilience here — measured, don't raise it.** Against a
#: real 45fps render loop on `hot lava`, starvation went 18.8% at depth 1 to
#: 21.1% at 2 and 49.1% at 4. That shape is the signature of a producer that
#: cannot keep up on AVERAGE rather than one suffering jitter: a deeper queue
#: only means it runs flat out for longer, competing harder for the GIL. The
#: fix for that is GIL_SWITCH_INTERVAL below, not more buffering.
PRERENDER_BLOCKS = 1

#: CPython's GIL handover period, seconds. The default is 5ms.
#:
#: The producer above is a *plain* Python thread, so unlike the PortAudio
#: callback it replaced it gets no realtime scheduling priority — it competes
#: with the visual render loop on equal terms. On a scene whose frame cost
#: fills the frame budget the render thread effectively never yields, and at
#: the default switch interval the producer's ~22ms of work stretched to
#: 200-350ms and missed the 186ms deadline outright.
#:
#: Measured on `hot lava` (3D, ~20-23ms a frame against a 22ms budget at
#: 45fps, i.e. the render thread saturated):
#:
#:     switch interval   starved callbacks   producer p95   engine fps
#:     5ms (default)     34.3%               648ms          40.0
#:     0.5ms             0.0%                103ms          38.8
#:
#: Handing the GIL over ten times more often costs ~3% of the frame rate on
#: that scene and nothing measurable on lighter ones (`jupiter`, `magic harp`
#: were already clean at either setting), and it is the difference between
#: audible dropouts and none. Set once when a real stream starts, because it
#: is process-global and only matters when both threads exist.
GIL_SWITCH_INTERVAL = 0.0005


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
    def bands(self): return {"level": 0.0, "low": 0.0, "mid": 0.0, "high": 0.0, "voices": {}}
    # legacy no-ops (old pad-synth interface)
    def set_patch(self, patch): pass
    def set_cutoff(self, hz): pass


class SoundscapeSynth:
    def __init__(self, sr: int = SR, blocksize: int = 8192, device=None,
                 latency="high", enable_diagnostics=True):
        import sounddevice as sd
        self._sd = sd
        self.sr = sr
        self.blocksize = blocksize
        self.device = device
        self.latency = latency
        self._lock = threading.Lock()
        self._scape = SoundscapeMixer(default_soundscape(), sr=sr)
        self._last_vu = {"peak": 0.0, "clipping": False}
        self._last_bands = {"level": 0.0, "low": 0.0, "mid": 0.0, "high": 0.0, "voices": {}}
        self._stream = None
        # Off with --no-diag: skips the timing calls and stats.record() in
        # _callback below — for isolating whether the instrumentation itself
        # (running inside the realtime callback) is a source of the very
        # glitching it's meant to help diagnose.
        self._diag_enabled = enable_diagnostics
        self.stats = CallbackStats(sr, blocksize)
        self.last_error = None
        self.requested_blocksize = blocksize
        # `online` used to be a fixed class attribute (True the moment this
        # class was instantiated, regardless of whether the stream actually
        # opened) — meaning a synth stuck in a broken/reverted state could
        # still report itself as healthy. It's now a real instance flag set
        # only on an actual successful stream start.
        self.online = False
        # Prerender pipeline — see PRERENDER_BLOCKS.
        self._q: queue.Queue | None = None
        self._producer: threading.Thread | None = None
        self._producer_stop: threading.Event | None = None
        self._residual = None       # tail of a block the last callback didn't use

    def _produce(self):
        """Render blocks ahead of the callback, forever, on a normal thread.

        `pending` is held across loop iterations on purpose: if the queue is
        full we must retry putting the SAME block rather than rendering a
        fresh one, or every full-queue moment would silently drop a block of
        audio and put a gap in the output.
        """
        pending = None
        while not self._producer_stop.is_set():
            if pending is None:
                try:
                    with self._lock:
                        crossfading = self._scape._phase is not None
                        pending = (self._scape.render(self.blocksize), crossfading)
                except Exception:
                    # A broken soundscape must not kill the audio thread and
                    # take the stream down with it — emit silence and carry on,
                    # so the app stays usable and the next scene can recover.
                    import numpy as np
                    pending = (np.zeros((self.blocksize, 2), np.float32), False)
            try:
                self._q.put(pending, timeout=0.1)
                pending = None
            except queue.Full:
                pass                       # re-check the stop flag, then retry

    def _callback(self, outdata, frames, time_info, status):
        """Realtime thread. Copies only — no synthesis, no allocation, no lock.

        Loops rather than assuming `frames == blocksize`: PortAudio is free to
        ask for a different count than requested, and a partially-consumed
        block carries over in `_residual`.
        """
        t0 = time.perf_counter() if self._diag_enabled else 0.0
        crossfading = False
        starved = False
        pos = 0
        while pos < frames:
            if self._residual is None or len(self._residual) == 0:
                try:
                    self._residual, crossfading = self._q.get_nowait()
                except queue.Empty:
                    # Starvation: the producer hasn't kept up. Silence for the
                    # remainder — never block the realtime thread waiting for
                    # it. Flagged for the stats because this returns ON TIME
                    # with valid (silent) data, so PortAudio sets no underflow
                    # status and nothing else in the diagnostics moves. See
                    # CallbackStats.record.
                    outdata[pos:] = 0.0
                    pos = frames
                    starved = True
                    break
            take = min(frames - pos, len(self._residual))
            outdata[pos:pos + take] = self._residual[:take]
            self._residual = self._residual[take:]
            pos += take
        if self._diag_enabled:
            self.stats.record(time.perf_counter() - t0, status,
                              crossfading=crossfading, starved=starved)

    def start(self):
        # See GIL_SWITCH_INTERVAL. Done here rather than at import so a run
        # with no audio (or with NullSynth) keeps the interpreter default.
        sys.setswitchinterval(GIL_SWITCH_INTERVAL)
        self.stats = CallbackStats(self.sr, self.blocksize)
        self._q = queue.Queue(maxsize=PRERENDER_BLOCKS)
        self._residual = None
        self._producer_stop = threading.Event()
        self._producer = threading.Thread(target=self._produce, daemon=True,
                                          name="soundscape-render")
        # Started BEFORE the stream, then primed: opening the stream takes only
        # a few ms while the first block needs 15-30ms to render, so without
        # waiting here the very first callback reliably finds an empty queue
        # and opens with a click. Bounded so a pathologically slow first render
        # delays startup rather than hanging it.
        self._producer.start()
        deadline = time.perf_counter() + 2.0 * self.blocksize / float(self.sr)
        while self._q.empty() and time.perf_counter() < deadline:
            time.sleep(0.002)
        try:
            self._stream = self._sd.OutputStream(
                samplerate=self.sr, channels=2, dtype="float32",
                blocksize=self.blocksize, latency=self.latency, device=self.device,
                callback=self._callback)
            self._stream.start()
        except Exception:
            # `reconfigure` walks a ladder of blocksizes and expects failures;
            # each failed attempt must not leak a producer thread rendering
            # into an orphaned queue.
            self._stop_producer()
            raise
        self.online = True

    def _stop_producer(self):
        if self._producer_stop is not None:
            self._producer_stop.set()
        if self._producer is not None:
            # The producer only ever blocks on a 0.1s queue put, so this joins
            # promptly; the timeout is a backstop, not the expected path.
            self._producer.join(timeout=1.0)
        self._producer = None
        self._producer_stop = None
        self._q = None
        self._residual = None

    def stop(self):
        if self._stream is not None:
            try:
                self._stream.stop(); self._stream.close()
            except Exception:
                pass
            self._stream = None
        self._stop_producer()
        self.online = False

    @property
    def output_latency(self) -> float:
        """Seconds between a rendered block being handed to the device and it
        being audible, as PortAudio reports it — not a guess.

        Needed because the modulation matrix reads each block's energy at the
        moment the block is *generated*, so anything driven by the engine's
        own sound would otherwise react this far ahead of it. Measured on this
        machine PortAudio reports exactly one blocksize (186ms at 8192).

        Since blocks are now rendered ahead of the callback (see
        PRERENDER_BLOCKS), the queue sits between generation and playback and
        counts toward this too — without it, audio-reactive visuals would lead
        the sound by the whole queue depth. Returns 0.0 when no stream is
        open, which correctly means "nothing to compensate".
        """
        st = self._stream
        if st is None:
            return 0.0
        block_s = self.blocksize / float(self.sr)
        try:
            device_s = float(st.latency)
        except Exception:
            # Some backends don't report it; fall back to the one figure we
            # can always derive, which is what it measured as anyway.
            device_s = block_s
        return device_s + PRERENDER_BLOCKS * block_s

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
        if muted and fade <= 0.0:
            # An UNFADED mute is the safety gate behind Stop/Blank, and it has
            # to be immediate. Blocks already queued were rendered before the
            # mute and would keep playing for the whole queue depth, so drop
            # them — silence is exactly what was asked for, so the gap a flush
            # would otherwise cause is the desired output here. A faded mute is
            # left alone: it is a musical fade, and cutting the queue would
            # defeat the fade it is asking for.
            self._flush()

    def _flush(self):
        """Discard prerendered audio. Only safe where silence is acceptable."""
        q = self._q
        self._residual = None
        if q is None:
            return
        try:
            while True:
                q.get_nowait()
        except queue.Empty:
            pass

    def soundscape(self):
        return self._scape.spec

    def vu(self):
        """Post-master output level of the last rendered block, for the VU
        meter. Non-blocking: this is called from engine.state(), on the
        asyncio broadcaster thread, at ~20Hz — if it *waited* for the lock
        while the audio callback is mid-render (the callback holds this same
        lock for the whole of Soundscape.render(), which we've measured
        spiking to hundreds of ms on heavier scenes), the entire websocket
        broadcaster would stall for that long, freezing every connected
        browser's video preview — completely independent of whether the
        engine's own render loop is keeping up. That's a real "audio glitch
        -> visual freeze" coupling that has nothing to do with render cost.
        Falling back to the last known reading on contention (a peak meter
        one tick stale is imperceptible) removes it entirely."""
        if self._lock.acquire(blocking=False):
            try:
                self._last_vu = {"peak": self._scape.last_peak, "clipping": self._scape.last_clip}
            finally:
                self._lock.release()
        return self._last_vu

    def bands(self):
        """Level + low/mid/high energy of the live soundscape, as modulation
        sources. Same non-blocking discipline as `vu()` above — this is read
        from the engine's render loop every tick, and blocking on the audio
        lock there would couple a slow audio callback straight into the frame
        rate. One tick stale is imperceptible for modulation."""
        if self._lock.acquire(blocking=False):
            try:
                self._last_bands = self._scape.bands()
            finally:
                self._lock.release()
        return self._last_bands

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
