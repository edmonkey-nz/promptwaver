/**
 * PromptWaver frame clock — turns the engine's per-tick snapshots, which
 * arrive over a websocket with network and scheduling jitter, into a smooth
 * snapshot for any display instant.
 *
 * The engine is the clock: every snapshot carries `te`, the engine's
 * monotonic time in seconds when it was built. The browser estimates the
 * offset between that clock and its own `performance.now()` as the windowed
 * MINIMUM of (arrival - te) — the fastest delivery seen recently, i.e. the
 * latency floor, which drifts slowly if at all. Everything later than that
 * floor is jitter.
 *
 * Drawing then targets engine time `now - offset - delay` and interpolates
 * between the two snapshots either side of it. `delay` adapts: one engine
 * tick plus the recent 95th-percentile jitter, so the snapshot AFTER the
 * target has nearly always arrived and motion never has to stall or guess.
 * It is the visual pipeline's whole added latency, and the engine subtracts
 * it from the audio-sync delay (Engine.mod_delay_auto), so audio-driven
 * visuals still land on the sound.
 */
(function (root) {
  "use strict";

  const WINDOW_MS = 4000;          // offset-minimum and jitter window
  const MAX_DELAY_MS = 150;

  class FrameClock {
    constructor() {
      this.snaps = [];             // recent snapshots, oldest first
      this.samples = [];           // [arrivalMs, offMs] within WINDOW_MS
      this.offset = null;          // ms: browser time - engine time, floor
      this.period = 1000 / 60;     // ms between engine ticks (measured)
      this.delay = 1000 / 60;      // ms of interpolation delay (adaptive)
      this._lastJitterCalc = 0;
    }

    push(snap, arrivalMs) {
      const off = arrivalMs - snap.te * 1000;
      this.samples.push([arrivalMs, off]);
      while (this.samples.length && this.samples[0][0] < arrivalMs - WINDOW_MS) this.samples.shift();
      let mn = Infinity;
      for (const s of this.samples) if (s[1] < mn) mn = s[1];
      this.offset = mn;
      const last = this.snaps[this.snaps.length - 1];
      if (last && snap.te > last.te) {
        // a slow EMA of the tick period, ignoring gaps (pause, reconnect)
        const d = (snap.te - last.te) * 1000;
        if (d < 200) this.period += (d - this.period) * 0.05;
      }
      if (last && snap.seq <= last.seq) this.snaps.length = 0;   // engine restarted
      this.snaps.push(snap);
      if (this.snaps.length > 90) this.snaps.splice(0, this.snaps.length - 90);
      if (arrivalMs - this._lastJitterCalc > 500) {
        this._lastJitterCalc = arrivalMs;
        const j = this.samples.map((s) => s[1] - mn).sort((a, b) => a - b);
        const p95 = j.length ? j[Math.min(j.length - 1, Math.floor(j.length * 0.95))] : 0;
        this.delay = Math.min(MAX_DELAY_MS, this.period + p95);
      }
    }

    /** The newest snapshot, uninterpolated (for UIs that just want state). */
    latest() { return this.snaps.length ? this.snaps[this.snaps.length - 1] : null; }

    /** A snapshot for display time `nowMs`, or null before the first one. */
    sample(nowMs) {
      const S = this.snaps;
      if (!S.length) return null;
      const target = (nowMs - this.offset - this.delay) / 1000;
      if (target >= S[S.length - 1].te) return S[S.length - 1];   // hold the newest
      if (target <= S[0].te) return S[0];
      let i = S.length - 1;
      while (i > 0 && S[i - 1].te > target) i--;
      const s0 = S[i - 1], s1 = S[i];
      const span = s1.te - s0.te;
      const a = span > 0 ? (target - s0.te) / span : 1;
      return interpolate(s0, s1, a);
    }
  }

  const lerp = (x, y, a) => x + (y - x) * a;
  const lerp3 = (p, q, a) => [lerp(p[0], q[0], a), lerp(p[1], q[1], a), lerp(p[2], q[2], a)];

  function lerpDict(d0, d1, a) {
    if (!d0) return d1;
    const out = {};
    for (const k in d1) {
      const v1 = d1[k], v0 = d0[k];
      out[k] = (typeof v1 === "number" && typeof v0 === "number") ? lerp(v0, v1, a) : v1;
    }
    return out;
  }

  /** Blend two snapshots. Continuous values are lerped; anything discrete
   *  (which scenes are live, flags, the camera mode, integer budgets) comes
   *  from the later one, which is authoritative. */
  function interpolate(s0, s1, a) {
    const byId = new Map((s0.scenes || []).map((s) => [s.id, s]));
    const scenes = (s1.scenes || []).map((sc1) => {
      const sc0 = byId.get(sc1.id);
      if (!sc0) return sc1;
      let cam = sc1.cam;
      const c0 = sc0.cam;
      if (cam && c0 && c0.mode === cam.mode) {
        cam = Object.assign({}, cam, {
          f: lerp(c0.f, cam.f, a), fov: lerp(c0.fov, cam.fov, a),
          near: lerp(c0.near, cam.near, a), far: lerp(c0.far, cam.far, a),
        });
        if (cam.mode === "fly") {
          cam.z = lerp(c0.z, sc1.cam.z, a); cam.yaw = lerp(c0.yaw, sc1.cam.yaw, a);
          cam.pitch = lerp(c0.pitch, sc1.cam.pitch, a); cam.height = lerp(c0.height, sc1.cam.height, a);
        } else {
          cam.pos = lerp3(c0.pos, sc1.cam.pos, a); cam.target = lerp3(c0.target, sc1.cam.target, a);
        }
      }
      const layers = (sc1.layers || []).map((u1, i) => {
        const u = lerpDict(sc0.layers && sc0.layers[i], u1, a);
        if ("max_strokes" in u1) u.max_strokes = u1.max_strokes;     // an integer budget
        return u;
      });
      return { id: sc1.id, a: lerp(sc0.a, sc1.a, a), cam, layers, sm: lerpDict(sc0.sm, sc1.sm, a) };
    });
    const f = lerpDict(s0.f, s1.f, a);
    if (s1.f) f.kal = a < 0.5 ? s0.f.kal : s1.f.kal;                    // a segment count
    const hue = (typeof s0.hue === "number" && typeof s1.hue === "number") ? lerp(s0.hue, s1.hue, a) : s1.hue;
    return {
      type: "frame", seq: s1.seq, te: lerp(s0.te, s1.te, a),
      gain: lerp(s0.gain, s1.gain, a), hue, test: s1.test, plane: s1.plane, f, scenes,
    };
  }

  const api = { FrameClock, interpolate };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.PWFrameClock = api;
})(typeof window !== "undefined" ? window : globalThis);
