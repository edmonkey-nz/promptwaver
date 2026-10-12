# Rendering stack review guide

How chromagraph builds its 2D/3D engine and keeps the GPU fast, written so you can
check another project against it. It doesn't say "copy chromagraph". It lists each
decision, why it was made, and how to check whether the other project made a sound
choice for its own needs.

## How to use this

1. Copy this file into the other project.
2. Ask Claude there to audit the project against it (a prompt is at the end).
3. For each check, the answer should be **Matches**, **Differs (justified)**,
   **Differs (risk)** or **Not applicable**, with a file and line as evidence.
4. If there are several **Differs (risk)** answers in sections 3–5, review that project's
   approach properly.

A difference is not automatically wrong. Chromagraph's choices come from its constraints
(section 1). A project with different constraints should make different choices. What
matters is whether it made them deliberately.

---

## 1. The constraints behind chromagraph's choices

Before comparing, write down the other project's version of each of these. Most of the
differences you find will trace back to one of them.

| Constraint | Chromagraph | Other project |
|---|---|---|
| Target GPU | NVIDIA GTX 1650, 4 GB VRAM, on a hybrid-graphics laptop | |
| Output | One 1920x1080 projector, fixed resolution | |
| Frame budget | 60 fps with 200,000 instances across all layers | |
| Run length | Hours, unattended (installation playback) | |
| Failure tolerance | Never go dark. A broken layer is switched off, the frame carries on | |
| Content | Data-driven instanced marks (points, bars, boxes, lines, surfaces, text) | |
| Dimensions | 2D and 3D in the same scene, switchable camera | |
| Browser | Chrome on Ubuntu, WebGL2 | |

Measured on the target: about 214,000 isometric boxes at 227 fps uncapped. Spot checks
with shadows, blur, reflections and depth of field held 60 fps. Depth of field costs
about 1.5 ms a frame, and a reflection plane about 3 ms at 200k boxes.

---

## 2. Engine and stack choices

### 2.1 Renderer: Three.js `WebGLRenderer`, WebGL2 as the baseline

- **What:** Three.js (r186) `WebGLRenderer`. No WebGPU-only features (no TSL-only nodes, no
  compute shaders). WebGPU may come later, behind a flag.
- **Why:** WebGL2 is mature and predictable in Chrome on Linux with NVIDIA drivers.
  WebGPU on Linux was the riskier choice for a show that runs unattended. Three gives you
  a scene graph, cameras, materials, render targets and context-loss handling without
  tying you to a framework.
- **Check:** Which renderer and API does the other project use? If it's WebGPU, is that
  because it needs compute, and has it been tested on the real target OS, driver and
  browser? If it has its own raw WebGL layer, what does that buy over Three, and who
  maintains it?

### 2.2 One scene graph for 2D and 3D

- **What:** 2D and 3D share one Three scene. 2D is a pixel-space orthographic camera
  (x 0..1920, y 0..1080, y up). 3D is a perspective camera, or an isometric (orthographic)
  camera with the same orbit. A layer can be "fixed to screen", which draws it with the 2D
  camera while the rest of the scene is 3D.
- **Why:** You can move one layer from a flat chart to a 3D arrangement without changing
  engines. Effects, transforms and morphs work the same in both.
- **Check:** Does the other project run two engines, such as Canvas2D or SVG for 2D and
  WebGL for 3D, or Pixi plus Three? If so, how does it combine them? Two canvases stacked
  on each other means no shared depth, two GPU contexts and copies between them.

### 2.3 Fixed-resolution render target, then a final pass to the screen

- **What:** The scene draws into a 1920x1080 MSAA render target (4 samples) with
  `antialias: false` on the canvas. A full-screen quad then draws that target to the screen
  through a fragment shader, which applies the keystone homography, bloom composite and
  fade. The control window's preview uses the same code at half resolution with no MSAA.
- **Why:** The output looks the same whatever the window size. Every post effect has one
  known resolution to work at. MSAA on the target, not on the canvas, means the
  antialiasing survives the post passes.
- **Check:** Does the other project render straight to the canvas at `devicePixelRatio`?
  On a HiDPI laptop screen that can mean 4x the pixels you budgeted for. Is there a single
  place where the output resolution is decided?

### 2.4 D3 for maths only

- **What:** `d3-scale`, `d3-array`, `d3-hierarchy`, `d3-time`. Never D3's DOM or SVG.
- **Why:** SVG and DOM can't handle 200k marks at 60 fps. The scales and layouts are pure
  functions, so they also run in workers and in tests.
- **Check:** Does any hot path create or change DOM or SVG nodes for each data point?

### 2.5 Text in the scene: troika-three-text

- **What:** SDF text, batched (`BatchedText`) for labels and axes, so text scales and
  takes part in 3D, effects and depth.
- **Check:** Is the text a DOM overlay positioned by hand (it drifts, ignores depth and
  can't be blurred or glowed), or one mesh per label (one draw call per label)?

---

## 3. GPU performance patterns

This is the most important section for a performance review.

### 3.1 One draw call per layer, data in textures

- **What:** Each instancer layer is a single `InstancedBufferGeometry` draw. Per-row data
  is packed into three RGBA32F `DataTexture`s, 4096 texels wide (WebGL2 guarantees that
  width). The vertex shader reads row values with
  `texelFetch(tex, ivec2(row % 4096, row / 4096), 0)`, using `gl_InstanceID`.
- **Why:** 200k instances cost one draw call, not 200k. The data is uploaded once.
- **Check:** Count the draw calls in a typical scene (`renderer.info.render.calls`). One
  `Mesh` per data point, or one draw per series of thousands of points, is a red flag.

### 3.2 Mapping maths runs in the shader, so edits are uniform changes

- **What:** Domains, ranges, scale type (linear, log, sqrt, quantise), colour ramp, offset
  steps and arrangement (grid, radial, spiral) are all uniforms. The vertex shader applies
  them.
- **How rebuilds are avoided:** each layer type has a `structureKey`, a string made from
  only the settings that really need new GPU data, such as the dataset, the columns used
  and the primitive. `LayerManager` compares it with the last one. If it is the same,
  only uniforms change.
- **Why:** Dragging a slider during a show never uploads 200k rows again, so there are no
  hitches.
- **Check:** Find what happens when a user changes a colour or a scale. If it rebuilds the
  geometry or buffers, the edit costs O(rows) on the main thread, and you'll see hitches
  while editing.

### 3.3 Only the visible range is drawn

- **What:** `geometry.instanceCount = visibleRows x clones`. The shader adds the first row
  of the range as an offset. Changing the row range (the data playhead sweeping through a
  record) is a uniform change and an integer, with no buffer work.
- **Check:** Does the project hide off-range instances by moving them off-screen, or by
  setting alpha to 0? Those instances still cost vertex work and, at alpha 0, fill rate.

### 3.4 No allocation per frame

- **What:** The render loop reuses objects. `FrameContext` is a single object mutated each
  frame. Per-frame lists (visible layers, effect settings, compose plans) are pooled arrays
  with a count. Colours are set from numbers, not by parsing strings. `LayerHost.call`
  takes a bound function and an argument, so it doesn't build closures every frame.
- **Why:** Garbage-collection pauses show up as dropped frames after minutes or hours.
- **Check:** In the render loop and every per-frame `update`, look for `new`, array and
  object literals, `.map`/`.filter`/spread, template strings, and `new Color('#...')` or
  `new Vector3()`. Use Chrome's Performance panel and look for a sawtooth in the JS heap.

### 3.5 Lines are geometry, not `GL_LINES`

- **What:** Lines are screen-space quads with a width in pixels, extruded in the vertex
  shader. Ridgeline fills are geometry too.
- **Why:** WebGL ignores `lineWidth` above 1 on most platforms. One-pixel lines disappear
  on a projector.
- **Check:** Look for `THREE.Line`, `LineBasicMaterial` with a `linewidth`, or
  `gl.LINES`.

### 3.6 Float precision on the GPU

- **What:** Time columns are float64 epoch-ms on the CPU. The GPU gets offsets from the
  column's minimum, never raw epoch-ms. At 1879 dates, the float32 step is about 4 minutes.
- **What else:** Animation time comes from the wall clock as float64 on the CPU. Only small
  derived values (a phase from 0 to 1, seconds since start, wrapped) reach shaders.
- **Check:** Does any shader receive large absolute values, such as timestamps,
  `performance.now()` in ms, or world coordinates in the millions? Those lose precision
  after hours of running, and shapes start to jitter.

### 3.7 Post effects are planned, lazy and released

- **What:** A pure, tested planner (`shared/effects-plan.ts`, `shared/compose-plan.ts`)
  decides each frame which passes are needed. With no effects on, the frame is a single
  render plus the warp. Render targets for bloom, trails, blur, shadow and depth of field
  are made the first time they're needed and released 10 s after their last use.
- **Why:** You only pay for the effects that are on screen. VRAM goes back down when an
  effect is switched off, which matters with 4 GB.
- **Check:** Does the other project run a fixed `EffectComposer` chain every frame even
  when the passes do nothing? Are render targets created at start-up for every possible
  effect? Are they ever disposed?

### 3.8 Dispose everything

- **What:** Every layer implements `dispose()`. It frees its geometry, materials, textures,
  text batches and child meshes. `LayerHost` calls it on remove and rebuild.
- **Check:** Watch `renderer.info.memory.geometries` and `.textures` while you add, edit
  and remove layers repeatedly. They should go back to where they started. In Three, a
  growing count is a VRAM leak.

### 3.9 The GPU is chosen and checked

- **What:** `powerPreference: 'high-performance'`. At start-up the output logs the renderer
  string (`WEBGL_debug_renderer_info`), and the control window warns if it isn't NVIDIA.
  On this Wayland laptop Chrome only uses the NVIDIA GPU with `--ozone-platform=x11` and
  PRIME offload variables. The launcher adds those.
- **Why:** On a hybrid laptop the default is often the integrated GPU, which is much
  slower. Without a check, the first sign is poor performance.
- **Check:** Does the other project know which GPU it runs on? Does it ever report it?

---

## 4. Robustness of the render loop

Performance is not just frame time. A frame that throws is a frame of 0 fps.

| Pattern | Chromagraph | Check in the other project |
|---|---|---|
| Loop can't stop | `requestAnimationFrame(tick)` is the first line of `tick`, and the body is in try/catch | Can an exception in one frame stop all later frames? |
| Layers fenced | `LayerHost` wraps every build, update and dispose. A throwing layer is hidden and flagged, and the others carry on | Does one bad object or config take down the whole scene? |
| Shaders verified at build | Each new layer is compiled straight away (`Stage.verify`, using `renderer.debug.onShaderError`), so a GLSL error fails that layer, not the frame. Note: in three r186, `compile()` alone doesn't run the error check | Are shader errors found at first draw, mid-show? |
| Context loss | Handles `webglcontextlost` (with `preventDefault`) and `webglcontextrestored`. It recreates its render targets and rebuilds every layer | Grep for `webglcontextlost`. Without it, a driver reset means a black screen until reload |
| Clock | One clock in the output. Modulators are pure functions of wall-clock ms and timeline phase, not of summed `dt`. `dt` is clamped to 0.1 s | Is animation driven by `t += dt`? That drifts over hours and jumps after a tab stall |

---

## 5. Keeping work off the render thread

- **Parsing:** Papa Parse runs in a web worker. Columns come back as typed arrays, with
  their buffers transferred, not copied.
- **Layout:** The d3-hierarchy layout runs in its own worker. The last layout keeps drawing
  until the next one arrives.
- **Data shape:** Columns, not rows: `Float32Array` for numbers and `Float64Array` for time,
  with NaN for missing values. They go to GPU textures with very little reshaping.
- **Check:** Does any parsing, layout, sorting or aggregation of a whole dataset run on the
  main thread of the window that renders? Are rows stored as arrays of objects (slow to
  turn into GPU data, heavy on memory)?

---

## 6. Testing without a GPU

- **What:** Vitest runs in pure Node. All data preparation, scale maths, effect planning,
  layout and label placement are pure functions with tests.
- **CPU twins:** Where the CPU needs to know what the shader does (label positions, axis
  extents, picking), there is a TypeScript copy of the shader maths: `normalise()` for the
  scales and `placeLabel()` for `arrange()`. Each is tested and marked "change both
  together".
- **Check:** Is the GPU-side maths testable anywhere? Is there one source of truth for
  "where does row i end up", or do the CPU and GPU disagree?

---

## 7. Known weaknesses in chromagraph's approach

Read these as well, so the comparison goes both ways. Where the other project has solved
one of these, that's worth bringing back.

- **No automated performance test.** The fps numbers above are manual checks on the
  target laptop. Nothing in CI catches a regression.
- **The scene target is 8-bit,** so it clamps above 1.0. Glow and edge glow are faked
  around it. A half-float HDR target would be the proper fix.
- **A blurred layer loses 3D occlusion,** because it is composited as an image over the
  layers below it.
- **Per-layer glow and trails redraw those layers** in extra passes. When many layers have
  different settings, that costs extra renders.
- **No WebGPU path,** and so no compute shaders for, say, GPU-side aggregation or
  particle simulation.
- **Non-fatal layer warnings** (such as hitting a label cap) only reach the browser
  console. Only thrown errors reach the control window.

---

## 8. Quick checklist

Give each one a mark and evidence.

**Engine**
- [ ] Renderer and API chosen deliberately, and tested on the real target hardware, OS and browser
- [ ] 2D and 3D share one scene, or there is a clear reason not to
- [ ] Output resolution fixed and decided in one place, with antialiasing on the render target
- [ ] No DOM or SVG per data point. Text is in the scene and batched

**GPU performance**
- [ ] Instancing: draw calls per layer are O(1), not O(rows)
- [ ] Per-row data uploaded once (attributes or data textures)
- [ ] Style and mapping edits only change uniforms. A key decides when to rebuild
- [ ] Only the visible range is drawn (`instanceCount`), not hidden by alpha
- [ ] No allocation per frame in the loop or in updates (heap graph is flat)
- [ ] Lines are quads with a pixel width
- [ ] No large absolute values (timestamps, ms clocks) sent to float32 uniforms or attributes
- [ ] Post passes only run when needed. Targets are lazy and released
- [ ] `dispose()` everywhere. `renderer.info.memory` returns to where it started
- [ ] Discrete GPU requested, and the renderer string logged

**Robustness**
- [ ] rAF rescheduled before any work. Frame body in try/catch
- [ ] Per-layer or per-object fencing
- [ ] Shaders compiled and checked at build time, not at first draw
- [ ] WebGL context loss and restore handled
- [ ] Animation from an absolute clock, not accumulated `dt`

**Threads and tests**
- [ ] Parsing and layout in workers. Columnar typed arrays
- [ ] Shader maths has a tested CPU twin wherever the CPU depends on it

**Verdict**
- 0–2 risks: fine. Note them and move on.
- 3–5 risks, or any in "GPU performance": review those areas.
- More than that, or the renderer choice itself isn't justified: review the whole approach
  before building more on it.

---

## 9. Prompt for the other project

> Read `rendering-stack-review.md`. It describes how another project of mine (chromagraph)
> built its 2D/3D WebGL engine and kept GPU performance up, and why. Audit this project
> against it.
>
> 1. Fill in the "Other project" column of the constraints table in section 1 from what
>    you find in this codebase. Ask me about anything you can't find.
> 2. Go through every check in sections 2–6 and the checklist in section 8. For each one,
>    answer Matches, Differs (justified), Differs (risk) or Not applicable, with a file and
>    line as evidence. Call a difference "justified" only when this project's constraints
>    explain it.
> 3. Check section 7 to see whether this project already does better on any of those
>    points.
> 4. Finish with the verdict from section 8 and the three changes that would matter most,
>    in order. Don't change any code.
