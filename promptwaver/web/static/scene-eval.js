/**
 * PromptWaver scene evaluator — the browser half of the GPU output path.
 *
 * The engine sends each scene's static geometry ONCE (gpu_pack.py, a binary
 * message) and a small JSON snapshot every tick (Engine._snapshot). This
 * module turns the two into the frame the engine's CPU renderer would have
 * drawn for that tick: node motion, the camera's max_strokes budget,
 * perspective projection, near/far visibility, frame clipping, depth colour,
 * crossfade/fade gains and the hue override. The result is a StrokeList the
 * WebGL renderer draws (renderer.js renderList).
 *
 * IT IS A TWIN of Python code and must agree with it — change both together:
 *
 *   evalWorld      generators/world.py  _motion, World._render_budgeted,
 *                                       World._render_all, _emit
 *                  scene3d.py           Camera.project, _project_lookat,
 *                                       _clip_and_project, _clip_frame,
 *                                       _lb_clip, DepthCue.color_for,
 *                  world.py             _cone_cos
 *   evalPattern    generators/pattern2d.py  Pattern2D.render (via gpu_pack)
 *                  scenes.py            _clip_2d, clamp_frame
 *   evalFrame      scenes.py            SceneManager.draw (crossfade gains)
 *                  engine.py            _loop post-processing (visual fade,
 *                                       hue override, test pattern)
 *
 * twin_test (see the repo's .temp/gpu-build-log.md) runs this file in node
 * against Python's own frames for every library scene.
 *
 * Loaded as a classic script (window.PWSceneEval) and as a CommonJS module
 * (node), so it touches no DOM.
 */
(function (root) {
  "use strict";

  // --- geometry (gpu_pack.py wire format) -----------------------------------

  /** Parse one binary geometry message into typed-array views (no copies). */
  function parseGeometry(buf) {
    const dv = new DataView(buf);
    const hlen = dv.getUint32(0, true);
    const bytes = new Uint8Array(buf, 4, hlen);
    const text = typeof TextDecoder !== "undefined"
      ? new TextDecoder().decode(bytes)
      : Buffer.from(bytes).toString("utf8");
    const head = JSON.parse(text);
    const layers = head.layers.map((L) => {
      const out = Object.assign({}, L, { arrays: {} });
      for (const [name, [off, n, dt, cols]] of Object.entries(L.arrays)) {
        const arr = dt === "i4" ? new Int32Array(buf, off, n) : new Float32Array(buf, off, n);
        out.arrays[name] = arr;
        out.arrays[name + "_cols"] = cols;
      }
      return out;
    });
    return { id: head.id, is3d: head.is3d, layers };
  }

  // --- output: a growable, reusable stroke list ------------------------------

  /** Strokes as flat typed arrays, reused frame to frame (no per-frame
   *  allocation once grown). Point coordinates are interleaved x,y. */
  class StrokeList {
    constructor() {
      this.pts = new Float32Array(8192);
      this.start = new Int32Array(1024);
      this.len = new Int32Array(1024);
      this.col = new Float32Array(3072);
      this.glow = new Float32Array(1024);
      this.count = 0;
      this.npts = 0;
      this._open = -1;
    }
    reset() { this.count = 0; this.npts = 0; this._open = -1; }
    _growPts(need) {
      if (need <= this.pts.length) return;
      let n = this.pts.length;
      while (n < need) n *= 2;
      const a = new Float32Array(n); a.set(this.pts.subarray(0, this.npts * 2)); this.pts = a;
    }
    _growStrokes(need) {
      if (need <= this.start.length) return;
      let n = this.start.length;
      while (n < need) n *= 2;
      const s = new Int32Array(n); s.set(this.start); this.start = s;
      const l = new Int32Array(n); l.set(this.len); this.len = l;
      const g = new Float32Array(n); g.set(this.glow); this.glow = g;
      const c = new Float32Array(n * 3); c.set(this.col); this.col = c;
    }
    begin(r, g, b, glow) {
      this._growStrokes(this.count + 1);
      const k = this.count;
      this.start[k] = this.npts; this.len[k] = 0;
      this.col[k * 3] = r; this.col[k * 3 + 1] = g; this.col[k * 3 + 2] = b;
      this.glow[k] = glow;
      this._open = k;
    }
    push(x, y) {
      this._growPts((this.npts + 1) * 2);
      this.pts[this.npts * 2] = x; this.pts[this.npts * 2 + 1] = y;
      this.npts++;
      this.len[this._open]++;
    }
    /** Close the open stroke; one with fewer than 2 points is discarded. */
    end() {
      const k = this._open;
      if (k < 0) return;
      if (this.len[k] < 2) { this.npts = this.start[k]; }
      else this.count++;
      this._open = -1;
    }
    /** Multiply the colour of strokes [from, count) by g. */
    scaleColour(from, g) {
      for (let k = from; k < this.count; k++) {
        this.col[k * 3] *= g; this.col[k * 3 + 1] *= g; this.col[k * 3 + 2] *= g;
      }
    }
  }

  // --- frame clipping (scene3d._lb_clip / _clip_frame) ------------------------

  const _seg = new Float64Array(4);

  /** Liang-Barsky against [-1,1]^2. Writes the clipped segment into _seg and
   *  returns true, or false if it misses. Same arithmetic as _lb_clip. */
  function lbClip(x0, y0, x1, y1) {
    const dx = x1 - x0, dy = y1 - y0;
    let t0 = 0.0, t1 = 1.0;
    const P = [-dx, dx, -dy, dy], Q = [x0 + 1, 1 - x0, y0 + 1, 1 - y0];
    for (let i = 0; i < 4; i++) {
      const p = P[i], q = Q[i];
      if (p === 0) {
        if (q < 0) return false;
      } else {
        const r = q / p;
        if (p < 0) {
          if (r > t1) return false;
          if (r > t0) t0 = r;
        } else {
          if (r < t0) return false;
          if (r < t1) t1 = r;
        }
      }
    }
    if (t0 > t1) return false;
    _seg[0] = x0 + t0 * dx; _seg[1] = y0 + t0 * dy;
    _seg[2] = x0 + t1 * dx; _seg[3] = y0 + t1 * dy;
    return true;
  }

  const f32 = Math.fround;

  /** _clip_frame: split polyline xs/ys[a..b) into in-frame runs, emitting each
   *  run of >= 2 points as its own stroke with the given colour. The points
   *  are float32-rounded first, as Python's (xy is a float32 array). */
  function emitClipped(out, xs, ys, a, b, r, g, bl, glow) {
    let open = false, lx = 0, ly = 0;
    for (let i = a; i < b - 1; i++) {
      if (!lbClip(f32(xs[i]), f32(ys[i]), f32(xs[i + 1]), f32(ys[i + 1]))) {
        if (open) { out.end(); open = false; }
        continue;
      }
      const p0x = _seg[0], p0y = _seg[1], p1x = _seg[2], p1y = _seg[3];
      if (!open) {
        out.begin(r, g, bl, glow); out.push(p0x, p0y); out.push(p1x, p1y); open = true;
      } else if (Math.abs(lx - p0x) < 1e-4 && Math.abs(ly - p0y) < 1e-4) {
        out.push(p1x, p1y);
      } else {
        out.end(); out.begin(r, g, bl, glow); out.push(p0x, p0y); out.push(p1x, p1y);
      }
      lx = p1x; ly = p1y;
    }
    if (open) out.end();
  }

  // --- world (3D) --------------------------------------------------------------

  /** world._cone_cos */
  function coneCos(fovDeg, aspect) {
    const t = Math.tan(fovDeg * Math.PI / 180 * 0.5);
    const a = aspect ? aspect : 1.0;
    const corner = Math.atan(t * Math.sqrt(1.0 + a * a));
    return Math.cos(Math.min(Math.PI * 0.5 - 1e-6, corner * 1.08));
  }

  // Scratch buffers, grown on demand and reused.
  let _wx = new Float64Array(4096), _wy = new Float64Array(4096), _wz = new Float64Array(4096);
  let _px = new Float64Array(4096), _py = new Float64Array(4096);
  function ensureScratch(n) {
    if (n <= _wx.length) return;
    let m = _wx.length; while (m < n) m *= 2;
    _wx = new Float64Array(m); _wy = new Float64Array(m); _wz = new Float64Array(m);
    _px = new Float64Array(m); _py = new Float64Array(m);
  }

  /** world._motion -> [scale, rotation 3x3 row-major | null, ox, oy, oz] */
  const _mot = { s: 1, rot: null, ox: 0, oy: 0, oz: 0 };
  const _rot = new Float64Array(9);
  function motion(code, axis, speed, amp, t) {
    _mot.s = 1.0; _mot.rot = null; _mot.ox = 0; _mot.oy = 0; _mot.oz = 0;
    if (code === 1) {                      // spin: _rot_axis(axis, t*speed)
      const ang = t * speed, c = Math.cos(ang), s = Math.sin(ang);
      if (axis === 0) _rot.set([1, 0, 0, 0, c, -s, 0, s, c]);
      else if (axis === 2) _rot.set([c, -s, 0, s, c, 0, 0, 0, 1]);
      else _rot.set([c, 0, s, 0, 1, 0, -s, 0, c]);
      for (let i = 0; i < 9; i++) _rot[i] = f32(_rot[i]);   // np.float32 matrix
      _mot.rot = _rot;
    } else if (code === 2) {               // bob
      _mot.oy = f32(Math.sin(t * speed) * amp);
    } else if (code === 3) {               // drift
      _mot.ox = f32(Math.sin(t * speed) * amp);
      _mot.oy = f32(Math.sin(t * speed * 0.7 + 1) * amp * 0.5);
      _mot.oz = f32(Math.cos(t * speed * 0.9) * amp);
    } else if (code === 4) {               // pulse
      _mot.s = 1.0 + Math.sin(t * speed) * amp * 0.5;
    }
    return _mot;
  }

  /** Python's % on floats (floor modulo, result has the divisor's sign). */
  function pymod(a, m) { const r = a % m; return (r !== 0 && (r < 0) !== (m < 0)) ? r + m : r; }

  // per-layer reusable selection state
  function worldScratch(L) {
    if (!L._scratch) {
      const N = L.arrays.node_i.length / 8, S = L.arrays.stroke_i.length / 4;
      L._scratch = {
        candKey: new Float64Array(N), candIdx: new Int32Array(N), candS: new Float64Array(N),
        order: new Int32Array(N),
        strokeNode: new Int32Array(S), strokeOrd: new Int32Array(S), strokeKey: new Float64Array(S),
        // per selected stroke: offset into the world-point scratch, plus z/lateral stats
        sOff: new Int32Array(S), sN: new Int32Array(S), sZmin: new Float64Array(S),
        sZmax: new Float64Array(S), sOffscreen: new Uint8Array(S), sCol: new Float32Array(S * 3),
      };
      const si = L.arrays.stroke_i, ni = L.arrays.node_i;
      for (let n = 0; n < N; n++) {
        const s0 = ni[n * 8 + 5], cnt = ni[n * 8 + 6];
        for (let k = 0; k < cnt; k++) L._scratch.strokeNode[s0 + k] = n;
      }
    }
    return L._scratch;
  }

  /** Stable sort of idx[0..n) by key[idx] ascending (insertion sort for the
   *  small n here — budgets are hundreds — and stable like Python's sort). */
  function stableSortBy(idx, n, key) {
    for (let i = 1; i < n; i++) {
      const v = idx[i], kv = key[v];
      let j = i - 1;
      while (j >= 0 && key[idx[j]] > kv) { idx[j + 1] = idx[j]; j--; }
      idx[j + 1] = v;
    }
  }

  /**
   * One `world` layer for one tick. `cam` is Camera.gpu_state(); `u` the
   * layer's gpu_tick ({t}); `sm` shape_modulation scales by shape name;
   * `plane` the Disable-scene-plane switch.
   */
  function evalWorld(L, cam, u, sm, plane, out) {
    const nf = L.arrays.node_f, ni = L.arrays.node_i, si = L.arrays.stroke_i, P = L.arrays.pts;
    const N = ni.length / 8;
    if (!N || !cam) return;
    const W = worldScratch(L);
    const t = u.t || 0;
    const ms = cam.ms;
    const shapes = L.shapes || [];
    const nodeScale = (n) => {
      let s = nf[n * 12 + 3];
      const sidx = ni[n * 8 + 3];
      if (sidx >= 0 && sm) {
        const k = shapes[sidx];
        if (k in sm) s = s * sm[k];
      }
      return s;
    };

    // ---- choose the strokes to transform (World._render_budgeted / _render_all)
    let nSel = 0;                       // nodes selected, in draw order -> W.order
    if (cam.mode === "fly") {
      for (let n = 0; n < N; n++) {
        if (plane && ni[n * 8 + 4]) continue;
        W.order[nSel++] = n;
        W.candS[n] = nodeScale(n);
      }
    } else {
      const px = cam.pos[0], py = cam.pos[1], pz = cam.pos[2];
      let fx = cam.target[0] - px, fy = cam.target[1] - py, fz = cam.target[2] - pz;
      const fn = Math.hypot(fx, fy, fz);
      const haveFwd = fn > 1e-6;
      if (haveFwd) { fx /= fn; fy /= fn; fz /= fn; }
      const cosHalf = coneCos(cam.fov, cam.aspect || 1.0);
      const far = cam.far;
      let nc = 0;
      for (let n = 0; n < N; n++) {
        if (plane && ni[n * 8 + 4]) continue;
        const s = nodeScale(n);
        const radius = nf[n * 12 + 9];
        let r;
        if (ni[n * 8 + 2]) {
          const amp = Math.abs(nf[n * 12 + 8]);
          r = radius * Math.abs(s) * (1.0 + amp * 0.5) + amp;
        } else r = radius * Math.abs(s);
        const rx = nf[n * 12] - px, ry = nf[n * 12 + 1] - py, rz = nf[n * 12 + 2] - pz;
        const d = Math.sqrt(rx * rx + ry * ry + rz * rz);
        const nearEdge = d - r;
        if (haveFwd) {
          const along = rx * fx + ry * fy + rz * fz;
          if (along - r > far) continue;
          if (d > r) {
            const ang = Math.acos(Math.max(-1.0, Math.min(1.0, along / d))) - Math.asin(Math.min(1.0, r / d));
            if (ang > 0.0 && Math.cos(ang) < cosHalf) continue;
          }
        } else if (nearEdge > far) continue;
        W.candKey[n] = nearEdge; W.candS[n] = s;
        W.candIdx[nc++] = n;
      }
      stableSortBy(W.candIdx, nc, W.candKey);
      const limit = Math.max(1, Math.floor(ms * 3.0));
      let nstr = 0;
      for (let c = 0; c < nc; c++) {
        if (nstr >= limit) break;
        const n = W.candIdx[c];
        W.order[nSel++] = n;
        nstr += ni[n * 8 + 6];
      }
    }

    // ---- transform the selected nodes' strokes to world space (_emit)
    let totalPts = 0, nStrokes = 0;
    for (let k = 0; k < nSel; k++) {
      const n = W.order[k];
      const s0 = ni[n * 8 + 5], cnt = ni[n * 8 + 6];
      for (let j = 0; j < cnt; j++) totalPts += si[(s0 + j) * 4 + 1];
    }
    ensureScratch(totalPts);
    let wp = 0;
    for (let k = 0; k < nSel; k++) {
      const n = W.order[k];
      const m = motion(ni[n * 8], ni[n * 8 + 1], nf[n * 12 + 7], nf[n * 12 + 8], t);
      const s = f32(W.candS[n] * m.s);
      const ox = f32(nf[n * 12] + m.ox), oy = f32(nf[n * 12 + 1] + m.oy), oz = f32(nf[n * 12 + 2] + m.oz);
      const R = m.rot;
      const cr = nf[n * 12 + 4], cg = nf[n * 12 + 5], cb = nf[n * 12 + 6];
      const s0 = ni[n * 8 + 5], cnt = ni[n * 8 + 6];
      for (let j = 0; j < cnt; j++) {
        const st = s0 + j, p0 = si[st * 4], np = si[st * 4 + 1];
        W.sOff[nStrokes] = wp; W.sN[nStrokes] = np;
        W.strokeOrd[nStrokes] = st;
        W.sCol[nStrokes * 3] = cr; W.sCol[nStrokes * 3 + 1] = cg; W.sCol[nStrokes * 3 + 2] = cb;
        for (let q = 0; q < np; q++) {
          let x = f32(P[(p0 + q) * 3] * s), y = f32(P[(p0 + q) * 3 + 1] * s), z = f32(P[(p0 + q) * 3 + 2] * s);
          if (R) {
            const xx = f32(x * R[0] + y * R[1] + z * R[2]);
            const yy = f32(x * R[3] + y * R[4] + z * R[5]);
            const zz = f32(x * R[6] + y * R[7] + z * R[8]);
            x = xx; y = yy; z = zz;
          }
          _wx[wp] = f32(x + ox); _wy[wp] = f32(y + oy); _wz[wp] = f32(z + oz);
          wp++;
        }
        nStrokes++;
      }
    }

    // ---- camera projection (Camera.project / _project_lookat)
    const dm = cam.depth || ["hue", [0.4, 0.9, 1.0], [0.1, 0.2, 0.5], false];
    const hueMode = dm[0] === "hue" || dm[0] === "both";
    const cull = dm[0] === "cull" || dm[0] === "both";
    const near = cam.near, far = cam.far, f = cam.f, aspect = cam.aspect || 1.0;
    const ord = W.strokeKey;            // sort key per transformed stroke
    if (!W.sortIdx || W.sortIdx.length < nStrokes) W.sortIdx = new Int32Array(Math.max(nStrokes, 16));
    const sortIdx = W.sortIdx;

    if (cam.mode === "fly") {
      const fd = L.field_depth;
      const cy = Math.cos(cam.yaw), sy = Math.sin(cam.yaw);
      const cp = Math.cos(cam.pitch), sp = Math.sin(cam.pitch);
      for (let k = 0; k < nStrokes; k++) {
        let sum = 0;
        const o = W.sOff[k], np = W.sN[k];
        for (let q = 0; q < np; q++) sum += _wz[o + q];
        ord[k] = pymod(f32(sum) / np - cam.z, fd);
        sortIdx[k] = k;
      }
      stableSortBy(sortIdx, nStrokes, ord);
      let emitted = 0;
      for (let ii = 0; ii < nStrokes; ii++) {
        if (emitted >= ms) break;
        const k = sortIdx[ii];
        const st = W.strokeOrd[k];
        if (si[st * 4 + 2] > 0 && ord[k] > far * 0.6) continue;
        const o = W.sOff[k], np = W.sN[k];
        let zmin = Infinity, zmax = -Infinity;
        for (let q = 0; q < np; q++) {
          const x0 = _wx[o + q], y0 = f32(_wy[o + q] - cam.height);
          const z0 = f32(pymod(f32(_wz[o + q] - cam.z), fd));
          let x = x0 * cy + z0 * sy, z = -x0 * sy + z0 * cy;
          const y = y0 * cp - z * sp; z = y0 * sp + z * cp;
          _px[o + q] = x; _py[o + q] = y; _wz[o + q] = z;   // reuse: view-space x,y,z
          if (z < zmin) zmin = z; if (z > zmax) zmax = z;
        }
        if (zmax <= near || (cull && zmin >= far)) continue;
        if (zmin > near) {
          const kx = f / aspect;
          let allR = true, allL = true, allT = true, allB = true;
          for (let q = 0; q < np; q++) {
            const zc = Math.max(_wz[o + q], 1e-3), x = _px[o + q], y = _py[o + q];
            if (!(x * kx > zc)) allR = false;
            if (!(x * kx < -zc)) allL = false;
            if (!(y * f > zc)) allT = false;
            if (!(y * f < -zc)) allB = false;
          }
          if (allR || allL || allT || allB) continue;
        }
        emitted += clipAndProject(out, _px, _py, _wz, o, np, f, near, far, aspect, dm, hueMode, cull,
                                  W.sCol[k * 3], W.sCol[k * 3 + 1], W.sCol[k * 3 + 2]);
      }
      return;
    }

    // look-at
    const px = cam.pos[0], py = cam.pos[1], pz = cam.pos[2];
    let fx = cam.target[0] - px, fy = cam.target[1] - py, fz = cam.target[2] - pz;
    const fn = Math.hypot(fx, fy, fz);
    if (fn < 1e-6) return;
    fx /= fn; fy /= fn; fz /= fn;
    // right = fwd x up0, up0 = (0,1,0)
    let rx = -fz, ry = 0, rz = fx;
    const rn = Math.hypot(rx, ry, rz);
    if (rn > 1e-6) { rx /= rn; ry /= rn; rz /= rn; } else { rx = 1; ry = 0; rz = 0; }
    const ux = ry * fz - rz * fy, uy = rz * fx - rx * fz, uz = rx * fy - ry * fx;
    for (let k = 0; k < nStrokes; k++) {
      const o = W.sOff[k], np = W.sN[k];
      let sx = 0, sy2 = 0, sz = 0, zmin = Infinity, zmax = -Infinity;
      let mR = Infinity, mL = -Infinity, mT = Infinity, mB = -Infinity;
      const kx = f / aspect;
      for (let q = 0; q < np; q++) {
        const wx = _wx[o + q], wy = _wy[o + q], wz = _wz[o + q];
        // float32 accumulation, as np.add.reduceat over the float32 points
        // does: a float64 sum shifts centroids by ~1e-6, enough to reorder
        // near-ties, and the budget below cuts on that order.
        sx = f32(sx + wx); sy2 = f32(sy2 + wy); sz = f32(sz + wz);
        const ax = wx - px, ay = wy - py, az = wz - pz;
        const x = ax * rx + ay * ry + az * rz;
        const y = ax * ux + ay * uy + az * uz;
        const z = ax * fx + ay * fy + az * fz;
        _px[o + q] = x; _py[o + q] = y; _wz[o + q] = z;
        if (z < zmin) zmin = z; if (z > zmax) zmax = z;
        const zc = Math.max(z, 1e-3);
        const r1 = x * kx - zc, l1 = x * kx + zc, t1 = y * f - zc, b1 = y * f + zc;
        if (r1 < mR) mR = r1; if (l1 > mL) mL = l1; if (t1 < mT) mT = t1; if (b1 > mB) mB = b1;
      }
      const cx = sx / np - px, cyy = sy2 / np - py, cz = sz / np - pz;
      ord[k] = Math.sqrt(cx * cx + cyy * cyy + cz * cz);
      W.sZmin[k] = zmin; W.sZmax[k] = zmax;
      W.sOffscreen[k] = (zmin > near) && (mR > 0 || mL < 0 || mT > 0 || mB < 0) ? 1 : 0;
      sortIdx[k] = k;
    }
    stableSortBy(sortIdx, nStrokes, ord);
    let emitted = 0;
    for (let ii = 0; ii < nStrokes; ii++) {
      if (emitted >= ms) break;
      const k = sortIdx[ii];
      if (W.sZmax[k] <= near || (cull && W.sZmin[k] >= far)) continue;
      if (W.sOffscreen[k]) continue;
      emitted += clipAndProject(out, _px, _py, _wz, W.sOff[k], W.sN[k], f, near, far, aspect, dm,
                                hueMode, cull, W.sCol[k * 3], W.sCol[k * 3 + 1], W.sCol[k * 3 + 2]);
    }
  }

  let _qx = new Float64Array(1024), _qy = new Float64Array(1024);

  /** _clip_and_project for one stroke whose VIEW-space coords are in
   *  xs/ys/zs[o..o+n). Emits each in-frame run; returns how many it emitted
   *  (the camera's budget counts runs, not strokes). */
  function clipAndProject(out, xs, ys, zs, o, n, f, near, far, aspect, dm, hueMode, cull, cr, cg, cb) {
    if (n > _qx.length) { let m = _qx.length; while (m < n) m *= 2; _qx = new Float64Array(m); _qy = new Float64Array(m); }
    for (let q = 0; q < n; q++) {
      const zc = Math.max(zs[o + q], 1e-3);
      _qx[q] = xs[o + q] / zc * f / aspect;
      _qy[q] = ys[o + q] / zc * f;
    }
    let emitted = 0;
    let s = 0;
    while (s < n) {
      const vis = (q) => cull ? (zs[o + q] > near && zs[o + q] < far) : (zs[o + q] > near);
      if (!vis(s)) { s++; continue; }
      let e = s;
      while (e < n && vis(e)) e++;
      const len = e - s;
      if (len >= 2) {
        let mnx = Infinity, mxx = -Infinity, mny = Infinity, mxy = -Infinity, zsum = 0;
        for (let q = s; q < e; q++) {
          const x = _qx[q], y = _qy[q];
          if (x < mnx) mnx = x; if (x > mxx) mxx = x; if (y < mny) mny = y; if (y > mxy) mxy = y;
          zsum += zs[o + q];
        }
        if (!(mnx > 1.0 || mxx < -1.0 || mny > 1.0 || mxy < -1.0)) {
          let r = cr, g = cg, b = cb;
          if (hueMode) {
            const d = zsum / len;
            const tt = Math.min(1.0, Math.max(0.0, (d - near) / Math.max(far - near, 1e-4)));
            const nc = dm[1], fc = dm[2];
            r = nc[0] + (fc[0] - nc[0]) * tt; g = nc[1] + (fc[1] - nc[1]) * tt; b = nc[2] + (fc[2] - nc[2]) * tt;
            if (dm[3]) { r = r >= 0.5 ? 1 : 0; g = g >= 0.5 ? 1 : 0; b = b >= 0.5 ? 1 : 0; }
          }
          const before = out.count;
          emitClipped(out, _qx, _qy, s, e, r, g, b, 0.0);
          emitted += out.count - before;
        }
      }
      s = e;
    }
    return emitted;
  }

  // --- pattern2d (2D) ------------------------------------------------------------

  let _cx = new Float64Array(4096), _cy = new Float64Array(4096);

  function evalPattern(L, u, out) {
    const sI = L.arrays.stroke_i, sF = L.arrays.stroke_f, P = L.arrays.pts;
    const S = sI.length / 4;
    const budget = Math.min(S, Math.max(1, u.max_strokes === undefined ? 420 : u.max_strokes));
    const gs = u.scale === undefined ? 1.0 : u.scale;
    const grot = u.rotate || 0.0;
    const spread = u.spread === undefined ? 1.0 : u.spread;
    const gglow = Math.max(0.0, Math.min(1.0, u.glow || 0.0));
    const k = spread - 1.0;
    const a = grot * 2 * Math.PI, ca = f32(Math.cos(a)), sa = f32(Math.sin(a));
    for (let st = 0; st < budget; st++) {
      const p0 = sI[st * 4], n = sI[st * 4 + 1], closed = sI[st * 4 + 2];
      const cx = sF[st * 8], cy = sF[st * 8 + 1], ax = sF[st * 8 + 2], ay = sF[st * 8 + 3];
      const r = sF[st * 8 + 4], g = sF[st * 8 + 5], b = sF[st * 8 + 6];
      const glow = Math.min(1.0, sF[st * 8 + 7] + gglow);
      if (n + 1 > _cx.length) { let m = _cx.length; while (m < n + 1) m *= 2; _cx = new Float64Array(m); _cy = new Float64Array(m); }
      let maxAbs = 0;
      const atx = ax * spread, aty = ay * spread;
      for (let q = 0; q < n; q++) {
        let x = P[(p0 + q) * 2], y = P[(p0 + q) * 2 + 1];
        if (spread !== 1.0) { x += cx * k; y += cy * k; }
        x = (x + atx) * gs; y = (y + aty) * gs;
        if (grot) { const xx = x * ca - y * sa; y = x * sa + y * ca; x = xx; }
        x = f32(x); y = f32(y);
        _cx[q] = x; _cy[q] = y;
        const m = Math.max(Math.abs(x), Math.abs(y));
        if (m > maxAbs) maxAbs = m;
      }
      if (n === 0) continue;
      if (maxAbs <= 1.0) {                 // _clip_2d: wholly inside, untouched
        out.begin(r, g, b, glow);
        for (let q = 0; q < n; q++) out.push(_cx[q], _cy[q]);
        out.end();
        continue;
      }
      let m = n;
      if (closed && n > 1) { _cx[n] = _cx[0]; _cy[n] = _cy[0]; m = n + 1; }
      emitClipped(out, _cx, _cy, 0, m, r, g, b, glow);
    }
  }

  // --- colour (Python colorsys) ----------------------------------------------------

  function rgbToHsv(r, g, b) {
    const maxc = Math.max(r, g, b), minc = Math.min(r, g, b);
    const v = maxc;
    if (minc === maxc) return [0.0, 0.0, v];
    const rangec = maxc - minc;
    const s = rangec / maxc;
    const rc = (maxc - r) / rangec, gc = (maxc - g) / rangec, bc = (maxc - b) / rangec;
    let h;
    if (r === maxc) h = bc - gc;
    else if (g === maxc) h = 2.0 + rc - bc;
    else h = 4.0 + gc - rc;
    h = pymod(h / 6.0, 1.0);
    return [h, s, v];
  }

  function hsvToRgb(h, s, v) {
    if (s === 0.0) return [v, v, v];
    let i = Math.trunc(h * 6.0);
    const f = h * 6.0 - i;
    const p = v * (1.0 - s), q = v * (1.0 - s * f), t = v * (1.0 - s * (1.0 - f));
    i = ((i % 6) + 6) % 6;
    if (i === 0) return [v, t, p];
    if (i === 1) return [q, v, p];
    if (i === 2) return [p, v, t];
    if (i === 3) return [p, q, v];
    if (i === 4) return [t, p, v];
    return [v, p, q];
  }

  // --- a whole frame ------------------------------------------------------------------

  /**
   * Fill `out` with this snapshot's frame. `geoms` maps geometry id ->
   * parseGeometry() result; a scene whose geometry hasn't arrived yet is
   * skipped (it arrives before the first snapshot that names it).
   */
  function evalFrame(snap, geoms, out) {
    out.reset();
    if (snap.test) {
      const g = geoms.get ? geoms.get(0) : geoms[0];
      if (g) for (const L of g.layers) evalPattern(L, { max_strokes: 1 << 30 }, out);
      return out;
    }
    const scenes = snap.scenes || [];
    const multi = scenes.length > 1;
    for (const sc of scenes) {
      const g = geoms.get ? geoms.get(sc.id) : geoms[sc.id];
      if (!g) continue;
      const from = out.count, fromPts = out.npts;
      g.layers.forEach((L, i) => {
        const u = (sc.layers && sc.layers[i]) || {};
        if (L.kind === "world") evalWorld(L, sc.cam, u, sc.sm, snap.plane, out);
        else if (L.kind === "pattern2d") evalPattern(L, u, out);
      });
      // clamp_frame — a no-op after clipping, kept for exactness
      for (let q = fromPts * 2; q < out.npts * 2; q++) {
        const v = out.pts[q];
        if (v > 1) out.pts[q] = 1; else if (v < -1) out.pts[q] = -1;
      }
      if (multi) out.scaleColour(from, sc.a);
    }
    if (snap.gain !== undefined && snap.gain < 1.0) out.scaleColour(0, snap.gain);
    if (snap.hue !== null && snap.hue !== undefined) {
      const hv = snap.hue;
      for (let k = 0; k < out.count; k++) {
        const [, s, v] = rgbToHsv(out.col[k * 3], out.col[k * 3 + 1], out.col[k * 3 + 2]);
        const c = hsvToRgb(hv, s, v);
        out.col[k * 3] = c[0]; out.col[k * 3 + 1] = c[1]; out.col[k * 3 + 2] = c[2];
      }
    }
    return out;
  }

  const api = { parseGeometry, StrokeList, evalFrame, evalWorld, evalPattern, rgbToHsv, hsvToRgb };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.PWSceneEval = api;
})(typeof window !== "undefined" ? window : globalThis);
