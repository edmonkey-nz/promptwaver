// The GPU path's twin test, half 2 — see twin_gen.py for what and why.
//   node tools/gpu_twin/twin_test.js CASEDIR promptwaver/web/static/scene-eval.js
// Strokes are compared as a SET: draw order is invisible under additive
// blending, and near-tied camera distances legitimately sort differently in
// float32 (Python) and float64 (JS). Which strokes are drawn must match.
const fs = require("fs"), path = require("path");
const [dir, evalPath] = process.argv.slice(2);
const E = require(path.resolve(evalPath));
const cases = JSON.parse(fs.readFileSync(path.join(dir, "cases.json")));
let failed = 0;
const TOL = 1e-4;
for (const c of cases) {
  const d = path.join(dir, c.replace(/ /g, "_"));
  const geoms = new Map();
  for (const f of fs.readdirSync(d)) {
    const m = /^geom_(\d+)\.bin$/.exec(f);
    if (!m) continue;
    const b = fs.readFileSync(path.join(d, f));
    const ab = b.buffer.slice(b.byteOffset, b.byteOffset + b.byteLength);
    geoms.set(+m[1], E.parseGeometry(ab));
  }
  const frames = JSON.parse(fs.readFileSync(path.join(d, "frames.json")));
  const out = new E.StrokeList();
  let countMis = 0, maxP = 0, maxC = 0, maxG = 0, strokes = 0, firstBad = null;
  frames.forEach((fr, i) => {
    E.evalFrame(fr.snap, geoms, out);
    const ex = fr.expect;
    if (out.count !== ex.length) {
      countMis++;
      if (!firstBad) firstBad = `frame ${i}: js ${out.count} strokes vs py ${ex.length}`;
    }
    // Order-insensitive: draw order is invisible under additive blending, and
    // near-tied distances legitimately sort differently in float32 vs float64.
    // WHICH strokes are drawn is what has to match.
    const used = new Uint8Array(ex.length);
    const bucket = new Map();
    ex.forEach((e, k) => {
      const key = e.p.length + ":" + (Math.round(e.p[0][0] * 100) | 0) + "," + (Math.round(e.p[0][1] * 100) | 0);
      if (!bucket.has(key)) bucket.set(key, []);
      bucket.get(key).push(k);
    });
    for (let k = 0; k < out.count; k++) {
      strokes++;
      const s0 = out.start[k], n = out.len[k];
      let best = -1, bestErr = Infinity;
      const kx = Math.round(out.pts[s0 * 2] * 100) | 0, ky = Math.round(out.pts[s0 * 2 + 1] * 100) | 0;
      for (const dx of [0, -1, 1]) for (const dy of [0, -1, 1]) {
        const key = n + ":" + (kx + dx) + "," + (ky + dy);
        for (const j of bucket.get(key) || []) {
          if (used[j]) continue;
          let err = 0;
          for (let q = 0; q < n; q++) err = Math.max(err, Math.abs(out.pts[(s0 + q) * 2] - ex[j].p[q][0]), Math.abs(out.pts[(s0 + q) * 2 + 1] - ex[j].p[q][1]));
          for (let c = 0; c < 3; c++) err = Math.max(err, Math.abs(out.col[k * 3 + c] - ex[j].c[c]));
          err = Math.max(err, Math.abs(out.glow[k] - ex[j].g));
          if (err < bestErr) { bestErr = err; best = j; }
        }
      }
      if (best < 0 || bestErr > TOL) {
        countMis++;
        if (!firstBad) firstBad = `frame ${i}: js stroke ${k} (${n} pts) has no python match (best err ${bestErr.toExponential(1)})`;
        continue;
      }
      used[best] = 1;
      maxP = Math.max(maxP, bestErr);
      for (let j = 0; j < 3; j++) maxC = Math.max(maxC, Math.abs(out.col[k * 3 + j] - ex[best].c[j]));
      maxG = Math.max(maxG, Math.abs(out.glow[k] - ex[best].g));
    }
  });
  const ok = countMis === 0 && maxP < TOL && maxC < TOL && maxG < TOL;
  if (!ok) failed++;
  console.log(`${ok ? "PASS" : "FAIL"} ${c.padEnd(32)} frames=${frames.length} strokes=${strokes} ` +
    `mismatch=${countMis} maxPt=${maxP.toExponential(1)} maxCol=${maxC.toExponential(1)} maxGlow=${maxG.toExponential(1)}` +
    (firstBad ? `  [${firstBad}]` : ""));
}
console.log(failed ? `${failed} FAILED` : "ALL PASS");
process.exitCode = failed ? 1 : 0;
