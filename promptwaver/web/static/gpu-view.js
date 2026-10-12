/**
 * PromptWaver GPU view — what each page (output, kiosk, the control page's
 * preview) uses to draw on the GPU path.
 *
 * The page keeps its own websocket and its own `filters` object (the slow,
 * per-scene display settings it already reads off the 20Hz state); it hands
 * this view the two message kinds that matter here:
 *
 *   binary   a scene's packed geometry (gpu_pack.py)  -> handleBinary()
 *   "frame"  the engine's per-tick snapshot          -> handleFrame()
 *
 * Drawing happens on requestAnimationFrame — the display's own rate — from
 * the FrameClock's interpolated snapshot, never inside a websocket handler,
 * so a burst of messages is coalesced instead of replayed and the frame rate
 * no longer depends on the network.
 *
 * The loop reschedules itself FIRST and wraps its body in try/catch: a frame
 * that throws costs that one frame, never every frame after it.
 */
(function (root) {
  "use strict";

  /** Should this page use the GPU path? `?renderer=cpu` selects the CPU
   *  reference renderer (the engine's own preview frames) instead. */
  function gpuWanted() {
    try { return new URLSearchParams(location.search).get("renderer") !== "cpu"; }
    catch (e) { return true; }
  }

  /** "&gpu=1" for the websocket URL when on the GPU path, else "". */
  function wsQuery() { return gpuWanted() ? "gpu=1" : ""; }

  class PWGpuView {
    /**
     * opts.canvas    the canvas the renderer draws into
     * opts.renderer  a PromptWaverRenderer
     * opts.filters   the page's live filters object (mutated here with the
     *                snapshot's modulated values: glow, trail, kaleidoscope,
     *                line curve, line width)
     * opts.role      "output" | "kiosk" | "preview" (for client_stats)
     * opts.send      (msg) => void, to report client_stats
     * opts.onDraw    optional (snapshot, ms) after each drawn frame
     */
    constructor(opts) {
      this.o = opts;
      this.clock = new root.PWFrameClock.FrameClock();
      this.geoms = new Map();
      this.list = new root.PWSceneEval.StrokeList();
      this.errors = 0;
      this._stats = { frames: 0, ms: 0, max: 0, t0: performance.now() };
      this._running = false;
      this._gpuName = null;
      this._statsTimer = null;
    }

    handleBinary(buf) {
      const g = root.PWSceneEval.parseGeometry(buf);
      g._arrived = performance.now();
      this.geoms.set(g.id, g);
    }

    handleFrame(snap) {
      this.clock.push(snap, performance.now());
      // Geometry no recent snapshot names is released (the test pattern, 0,
      // is small and kept). A scene comes back with a new id and new geometry.
      // Never anything that arrived in the last 10 s: geometry is sent just
      // BEFORE the first snapshot naming it, and the server sends each id
      // only once, so dropping it in that gap would lose it for good.
      if (this.geoms.size > 3) {
        const live = new Set([0]);
        for (const s of this.clock.snaps.slice(-30)) for (const sc of s.scenes || []) live.add(sc.id);
        const now = performance.now();
        for (const [id, g] of [...this.geoms]) {
          if (!live.has(id) && now - g._arrived > 10000) this.geoms.delete(id);
        }
      }
    }

    start() {
      if (this._running) return;
      this._running = true;
      const tick = () => {
        if (!this._running) return;
        requestAnimationFrame(tick);
        try { this._draw(); }
        catch (e) {
          this.errors++;
          if (this.errors <= 5 || this.errors % 600 === 0) console.error("[PromptWaver] frame error", e);
        }
      };
      requestAnimationFrame(tick);
      if (this.o.send) this._statsTimer = setInterval(() => this._report(), 2000);
    }

    stop() {
      this._running = false;
      if (this._statsTimer) clearInterval(this._statsTimer);
    }

    _draw() {
      const now = performance.now();
      const snap = this.clock.sample(now);
      const c = this.o.canvas, F = this.o.filters;
      if (!snap) { this.o.renderer.renderList(this.list, F, { width: c.width, height: c.height }); return; }
      const t0 = performance.now();
      root.PWSceneEval.evalFrame(snap, this.geoms, this.list);
      if (snap.f) {
        F.glow = snap.f.glow; F.trail = snap.f.trail; F.kaleidoscopeSegments = snap.f.kal;
        F.lineCurve = snap.f.curve; F.lineWidth = snap.f.width;
      }
      this.o.renderer.renderList(this.list, F, { width: c.width, height: c.height });
      const ms = performance.now() - t0;
      const st = this._stats;
      st.frames++; st.ms += ms; if (ms > st.max) st.max = ms;
      this.lastAge = now - this.clock.offset - snap.te * 1000;
      if (this.o.onDraw) this.o.onDraw(snap, ms);
    }

    /** fps, cost and latency for the control page's perf panel. */
    stats() {
      const st = this._stats, dt = (performance.now() - st.t0) / 1000;
      return {
        fps: dt > 0 ? st.frames / dt : 0,
        draw_ms: st.frames ? st.ms / st.frames : 0,
        draw_max_ms: st.max,
        strokes: this.list.count,
        delay_ms: this.clock.delay,
        age_ms: this.lastAge || 0,
        errors: this.errors,
      };
    }

    gpuName() {
      if (this._gpuName !== null) return this._gpuName;
      try {
        const gl = this.o.renderer.gl;
        const ext = gl && gl.getExtension("WEBGL_debug_renderer_info");
        this._gpuName = gl ? (ext ? gl.getParameter(ext.UNMASKED_RENDERER_WEBGL) : gl.getParameter(gl.RENDERER)) : "";
      } catch (e) { this._gpuName = ""; }
      return this._gpuName;
    }

    _report() {
      const s = this.stats();
      const c = this.o.canvas;
      try {
        this.o.send({ type: "client_stats", role: this.o.role || "output", gpu: this.gpuName(),
                      w: c.width, h: c.height,
                      fps: +s.fps.toFixed(1), draw_ms: +s.draw_ms.toFixed(2),
                      draw_max_ms: +s.draw_max_ms.toFixed(2), strokes: s.strokes,
                      delay_ms: +s.delay_ms.toFixed(1), age_ms: +s.age_ms.toFixed(1),
                      errors: s.errors });
      } catch (e) { /* socket closed: the next report will try again */ }
      this._stats = { frames: 0, ms: 0, max: 0, t0: performance.now() };
    }
  }

  root.PWGpuView = PWGpuView;
  root.PWGpuView.gpuWanted = gpuWanted;
  root.PWGpuView.wsQuery = wsQuery;
})(window);
