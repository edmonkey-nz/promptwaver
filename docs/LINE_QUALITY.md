# Smoother vector lines in WebGL

Notes from PromptWaver 0.97's line-quality work, written to be **portable to
chromagraph**. Both projects draw lines the same way: each segment is an
instanced quad, a fragment shader trims it to a capsule with a signed distance,
and the edge is a one-pixel coverage ramp. So all three techniques here are
candidates there. One of them must **not** be copied as-is, because chromagraph
already does it another way (see §1).

PromptWaver file references point at `promptwaver/web/static/renderer.js`, so
you can copy the implementation. Numbers marked *measured* were measured in
PromptWaver. Anything said about chromagraph comes from reading its code
(`src/client/output/stage.ts`, `layers/line.ts`) and has **not** been measured
there.

---

## Summary

| technique | fixes | cost | for chromagraph |
|---|---|---|---|
| Gamma-correct edges | bright lines on black look thin and stepped | free | **already done** by the linear pipeline. Don't add it again. The 8-bit linear target may be quantising the edges instead (§1). |
| Soft lines + MAX blend | joints "bead" when the feather is wide or the line translucent | free | only for **additive** layers, because MAX ignores alpha (§2) |
| Supersampling | stair-steps on fine lines, shimmer in motion | 2.25× / 4× fill | yes, but the AA feather must move to target pixels, and **the 4× MSAA does nothing for line edges** (§3) |

---

## 1. Gamma-correct edges

**The problem.** An antialiased edge pixel with coverage 0.5 is written as
colour × 0.5. If that value goes to the display as-is, the display's gamma
curve turns it into roughly 22% of the light, not 50%. Every edge pixel comes
out too dark, so bright lines on black look thinner than they are and the
stair-steps show.

**What PromptWaver does.** It raises coverage to 1/2.2 in the line shader:

```glsl
float alpha = 1.0 - smoothstep(-vEdge, vEdge, d);
alpha = pow(alpha, coverageGamma);   // 1/2.2, or 1.0 to disable
```

(`renderer.js:272`, uniform set at `:741`.) Over black, this is **exactly** what
blending in linear light and then encoding for display gives. Nearly all of a
PromptWaver frame is black, so the shortcut is close to exact. It was chosen
over a real linear pipeline because that would have changed how bloom and
trails look in every existing scene.

**For chromagraph: don't do this.** Its pipeline is already linear. Three
renders scene materials into the render target in linear space, and the warp
pass encodes to sRGB with `#include <colorspace_fragment>` (`stage.ts`,
`WARP_FRAGMENT`). Coverage is already blended in linear light. Adding
`pow(1/2.2)` would correct twice and make every edge too bright and too wide.
Unlike PromptWaver's shortcut, the linear pipeline is also correct over
non-black backgrounds.

**What may be worth measuring instead.** The scene target in `makeTarget()` is
the default `UnsignedByteType`, i.e. **8-bit linear**. Linear 8-bit spends very
few codes on dark values. The smallest non-zero step, 1/255 linear, encodes to
about 13/255 in sRGB, and the next to about 26/255. The faint tail of a
one-pixel edge ramp therefore lands on a few coarse display levels, which reads
as stepping. That is the same symptom gamma-correction was meant to fix, with a
different cause.

If chromagraph's edges look stepped, try a `HalfFloatType` scene target before
anything else. `effects.ts` already picks half-float when
`EXT_color_buffer_float` is present. Check the MSAA target still allocates with
it, and what it costs at 200k instances. This is a hypothesis from the maths,
not a measurement.

---

## 2. Soft lines, and why the joints bead

**The problem.** Consecutive segments' quads overlap at each joint. Both
projects lengthen the quad by half the width so the capsule has room for a
round cap. Where the two capsules overlap, any pixel with alpha < 1 is blended
twice and comes out brighter, so every joint shows as a dot along the line.
With a hard half-pixel feather this only affects the thin edge, and nobody
sees it. PromptWaver kept the feather at half a pixel for exactly that reason.
chromagraph's `line.ts` header notes the same effect for translucent lines.

**What PromptWaver does** (`line_softness`, 0..1):

- **Widens the feather**: `featherPx = 0.5 + soft * (pixelWidth/2 + 1.5)`
  (`renderer.js:738`).
- **Renormalises the centre** so a wide feather doesn't dim the whole line.
  Coverage at the centreline is computed and divided out (`:264`):
  ```glsl
  float centre = 1.0 - smoothstep(-vEdge, vEdge, -vRadius);
  alpha = min(1.0, alpha / max(centre, 1e-3));
  ```
  With the default half-pixel feather, `centre` is exactly 1, so this step is
  skipped.
- **Blends with `gl.MAX` while soft > 0** (`:753`). The overlap at a joint then
  composites as the brighter of the two capsules instead of their sum, so no
  bead forms. Hard lines keep normal blending, exactly as before.

**For chromagraph: additive layers only.** `MAX` ignores the blend factors and
takes the per-channel maximum of source and destination. That is correct for
light on black, and for a layer chromagraph already draws with
`AdditiveBlending`. It is **wrong** for `NormalBlending` over a coloured
background or a fill: a dim line over a brighter fill would vanish, and alpha
would stop meaning anything.

For normal-blended translucent lines, the joint overlap needs a different fix,
for example drawing each line so a pixel is written once (stencil, or a depth
trick). That is not something PromptWaver had to solve.

---

## 3. Supersampling

**The problem.** A one-pixel coverage ramp can only do so much for lines thinner
than about a pixel, or for near-horizontal lines moving slowly. Stair-steps
remain, and in motion they crawl ("shimmer").

**What PromptWaver does.** It draws the whole frame at 1.5× or 2× the canvas
size, then filters it down in the final pass:

- `_innerSize()` (`renderer.js:566`) gives the drawn size. Every pass before the
  final one (lines, bloom, trails) runs at that size.
- The final pass (`src()`, `:388`) reads the large frame with **four bilinear
  taps a quarter of a screen pixel either side of the centre**. At exactly 2×,
  each tap lands on a texel centre, so it is an exact 2×2 box average. At 1.5×,
  the bilinear blends cover the 1.5 × 1.5 footprint closely.
  ```glsl
  vec3 src(vec2 uv) {
    if (uSS <= 1.0) return texture(uSrc, uv).rgb;
    vec2 o = uTexel * (0.25 * uSS);          // uTexel = 1 / drawn size
    return 0.25 * (texture(uSrc, uv + vec2(-o.x, -o.y)).rgb + texture(uSrc, uv + vec2(o.x, -o.y)).rgb
                 + texture(uSrc, uv + vec2(-o.x,  o.y)).rgb + texture(uSrc, uv + vec2(o.x,  o.y)).rgb);
  }
  ```
- The AA feather is set in **drawn** pixels (`0.5 / scale`). At 2×, the shader's
  ramp is half an output pixel wide and the box filter supplies the rest. That
  is where the gain comes from.

*Measured* on a frozen frame of the Mandala scene, mean error on lit pixels
against a 4× reference:

| factor | error | GTX 1650, 1080p | AMD iGPU, 1080p |
|---|---|---|---|
| 1× | 19.0 | 60 fps | 50 fps |
| 1.5× | 12.1 (−36%) | 60 fps | 41 fps |
| 2× | 7.2 (−62%) | 60 fps | 31 fps |

### The trap: don't let the browser downscale

The obvious version is to make the canvas 2× larger, shrink it with CSS, and
let the browser scale it down. **That does nothing at 2×.** Chrome's compositor
was *measured* point-sampling at exactly 2×: it keeps every other pixel, which
is the 1× image back again (identical lit-pixel count). Do the downsample
yourself, in your own last pass.

### Mapping it onto chromagraph

chromagraph draws into a fixed 1920×1080 target with 4× MSAA, then warps it to
the screen. The pieces line up well, with three things to watch:

1. **The 4× MSAA does nothing for line edges.** MSAA only multisamples
   *triangle* coverage. The fragment shader still runs once per pixel and its
   alpha is copied to every sample. A capsule line's visible edge is inside its
   quad, where the shader computes the alpha, and the quad's own triangle edges
   are out where alpha is already 0. MSAA still helps real geometry edges
   (instanced meshes, fills). It does not help `line.ts`, `multiples.ts` or
   `hierarchy.ts`, which all antialias with `fwidth` or a distance ramp in the
   shader. Supersampling helps both kinds. At 2× it gives four samples per
   output pixel anyway, so the MSAA could probably drop to 0 when supersampling
   is on. Measure that before relying on it.
2. **Put the downsample in the warp pass.** `WARP_FRAGMENT` already samples
   `uImage` once per screen pixel. Replace that `texture2D` with the four-tap
   `src()` above. Keystoning changes the footprint per pixel, but the four taps
   still beat one bilinear read wherever the warp is near 1:1. The bloom texture
   is half-res and blurry already, so it needs no change.
3. **The feather has to move to target pixels.** `line.ts` sets `uResolution`
   to the constant `OUTPUT_WIDTH × OUTPUT_HEIGHT` and measures the edge ramp
   in those units (`clamp(0.5*uWidth - d + 0.5, …)`). Line *width* in output
   px is right and should stay. But if only the target grows, the one-pixel
   ramp becomes two target pixels, which downsample back to the same soft
   one-pixel edge, and most of the gain is lost. Keep the ramp one *target*
   pixel wide instead: `clamp((0.5*uWidth - d) * ss + 0.5, 0.0, 1.0)`. The
   `fwidth`-based layers adapt automatically, because `fwidth` measures in the
   pixels actually being drawn.

**Cost.** Everything that runs at target size scales with it: 2.25× the fill at
1.5×, 4× at 2×. In chromagraph that is the scene draw, bloom's bright pass,
trails, depth of field and the per-layer images. The 200k-instance, 60 fps
budget on the GTX 1650 is the number to defend. PromptWaver's frame is light by
comparison, so its "60 fps at 2×" says little about chromagraph. Memory at 2×:
a 3840×2160 4-sample RGBA8 colour buffer is about 130 MB, plus depth. That fits
in 4 GB, but it is another reason to try dropping the MSAA.

Make it a setting (1 / 1.5 / 2) rather than a constant, and keep the control
window's preview (`targetScale < 1`) out of it, as PromptWaver does.

---

## How the numbers were taken, so they can be repeated

- **Freeze the frame.** Compare one fixed scene time, not live output, or motion
  swamps the difference being measured.
- **Reference = the same frame at 4×**, box-filtered down. Score each setting
  as the mean absolute difference **over lit pixels only**. Black dominates the
  frame, and averaging it in makes every setting look nearly perfect.
- **Count lit pixels** to catch the browser silently undoing your work. The
  compositor point-sampling trap above showed up as a 2× canvas with exactly the
  1× lit-pixel count.
- **Read fps off the real output window on the target GPU.** On a hybrid laptop,
  check which GPU the browser actually got (`WEBGL_debug_renderer_info`). The
  iGPU numbers above are what you get when it picks the wrong one.
