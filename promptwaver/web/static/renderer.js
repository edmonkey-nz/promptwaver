/**
 * PromptWaver WebGL2 Renderer
 * Unified rendering module for output.html and index.html
 * Handles thick anti-aliased lines, colored glow/bloom, trail feedback, and post-fx
 */

class PromptWaverRenderer {
  // Bloom fallbacks, used only when the caller's filters don't carry the
  // values — normally they arrive per-scene from engine state. SPREAD is the
  // blur's tap spacing as a fraction of the smaller canvas dimension (four
  // taps each side, so the visible radius is roughly four times this);
  // INTENSITY is the gain applied to the blurred layer when it's added back
  // over the sharp strokes. Keep in step with the same defaults in
  // Engine._install_spec, so a scene saved before these were adjustable
  // renders identically whichever path supplies them.
  static BLOOM_SPREAD = 0.005;
  static BLOOM_INTENSITY = 2.5;

  // options.lineWidthMode: "viewport" (projector output, stroke scales with
  // window size) or "fixed" (in-page preview hairline). See _getLineWidth.
  constructor(canvas, options = {}) {
    this.canvas = canvas;
    this.lineWidthMode = options.lineWidthMode || "fixed";
    this.gl = null;
    this.contextLost = false;
    this.programs = {};
    this.fbos = {};
    this.buffers = {};
    this.init();
  }

  // Chromium is a hard requirement (see the 0.77.0 notes) — the renderer is
  // built against Chrome's WebGL2 and there is no Canvas2D fallback. This is
  // deliberately separate from the WebGL2 check below: Firefox and Safari BOTH
  // give you a working webgl2 context, so the context test passes and the page
  // then misbehaves in ways that don't point at the browser. Warn on the
  // browser itself, up front, before anything renders.
  //
  // `userAgentData` is Chromium-only, so its presence is already most of the
  // answer; the UA-string arm is the fallback for Chromium builds that don't
  // expose it. `window.chrome` is checked too because Safari's UA contains
  // "Chrome" in some webviews while Firefox's never does.
  static isChromium() {
    const brands = navigator.userAgentData && navigator.userAgentData.brands;
    if (brands && brands.length) {
      return brands.some(b => b.brand === "Chromium" || b.brand === "Google Chrome");
    }
    return /Chrome\/\d+/.test(navigator.userAgent) && !!window.chrome;
  }

  // Fixed banner, shown once per page. Not a modal or an alert(): both pages
  // are things you leave running for hours — the output window in particular
  // is on a projector with no keyboard near it — so this must be dismissible
  // and must never block the show.
  static warnIfNotChromium() {
    if (PromptWaverRenderer.isChromium() || PromptWaverRenderer._browserWarned) return;
    PromptWaverRenderer._browserWarned = true;
    console.warn("[PromptWaver] Non-Chromium browser — WebGL2 rendering is only supported in Chrome.");
    const el = document.createElement("div");
    el.style.cssText = "position:fixed;z-index:99999;left:0;right:0;top:0;padding:10px 44px 10px 14px;" +
      "text-align:center;color:#04211f;background:#f2a623;" +
      "font:13px ui-monospace,Menlo,Consolas,monospace;line-height:1.5";
    el.innerHTML = "<b>Unsupported browser.</b> PromptWaver renders with WebGL2 and is only "
      + "tested in Chrome (or another Chromium browser). Expect wrong or missing visuals here.";
    const x = document.createElement("button");
    x.textContent = "×";
    x.title = "Dismiss";
    x.style.cssText = "position:absolute;top:4px;right:8px;background:none;border:0;cursor:pointer;"
      + "color:#04211f;font:18px/1 ui-monospace,Menlo,Consolas,monospace;padding:4px 6px";
    x.onclick = () => el.remove();
    el.appendChild(x);
    const put = () => document.body.appendChild(el);
    if (document.body) put(); else document.addEventListener("DOMContentLoaded", put);
  }

  init() {
    PromptWaverRenderer.warnIfNotChromium();

    // Try to get WebGL2 context
    this.gl = this.canvas.getContext("webgl2", {
      preserveDrawingBuffer: true,
      antialias: false,
      depth: false,
      powerPreference: "high-performance"
    });

    if (!this.gl) {
      this.showWebGLError("WebGL2 not supported. Chrome required.");
      return;
    }

    // Set up context-loss recovery
    this.canvas.addEventListener("webglcontextlost", (e) => {
      e.preventDefault();
      this.contextLost = true;
      console.warn("[PromptWaver] WebGL context lost, waiting for restoration");
    });

    this.canvas.addEventListener("webglcontextrestored", () => {
      console.log("[PromptWaver] WebGL context restored");
      this.contextLost = false;
      this.rebuildGLResources();
    });

    // Check for required extensions
    const ext = this.gl.getExtension("EXT_color_buffer_float");
    if (!ext) {
      this.showWebGLError("EXT_color_buffer_float not supported (required for bloom)");
      return;
    }

    // Set up initial GL state
    const gl = this.gl;
    gl.clearColor(0, 0, 0, 1);
    // ONE, ONE_MINUS_SRC_ALPHA — premultiplied alpha blending. Matches the
    // fragment shader's premultiplied output (see the line-shader comment);
    // needed because adjacent line-segment quads deliberately overlap at
    // joints, and non-premultiplied blending double-composites that overlap
    // into visible seams.
    gl.blendFunc(gl.ONE, gl.ONE_MINUS_SRC_ALPHA);
    gl.enable(gl.BLEND);

    this.buildGLResources();
  }

  buildGLResources() {
    if (!this.gl || this.contextLost) return;
    this.programs = {};
    this.fbos = {};
    this.buffers = {};

    // Phase 1: line rendering with capsule-SDF
    const gl = this.gl;

    // Vertex shader: expand quads around line segments, apply transforms.
    // Everything here stays in normalized [-1,1] engine space until the very
    // last line (gl_Position = projection * ...), which converts directly to
    // GL clip space. lineHalfWidth/aaEdge are pre-converted to normalized-space
    // units by the caller (see render()) — they must NOT be raw pixel values,
    // since this shader never touches pixel space.
    const lineVS = `#version 300 es
precision highp float;

// Per-segment instance data
in vec2 p0, p1;
in vec3 color;
in float capStart, capEnd;
in float glow;         // this stroke's own 0..1 bloom (Path.glow)

// Per-frame uniforms — projection maps normalized [-1,1] engine space
// straight to GL clip space [-1,1] (see _buildProjectionMatrix).
uniform mat4 projection;
uniform float lineHalfWidth;  // normalized-space half-width (SDF radius)
uniform float aaEdge;         // normalized-space AA feather half-width
uniform vec2 keystoneHV;
uniform float globalGlow;     // scene-wide glow, acts as a floor per stroke

// Per-vertex quad corner, x in [-1,1] along the segment, y in [-1,1] across it
in vec2 quadPos;

out vec3 vColor;
out vec2 vPos;
out vec2 vLineStart, vLineEnd;
out float vRadius;
out float vEdge;
out float vGlow;

vec2 applyKeystone(vec2 p) {
  float kh = keystoneHV.x;
  float kv = keystoneHV.y;
  float xk = p.x * (1.0 + kh * p.y);
  float yk = p.y * (1.0 + kv * xk);
  return vec2(
    max(-1.0, min(1.0, xk)),
    max(-1.0, min(1.0, yk))
  );
}

void main() {
  vColor = color;
  vRadius = lineHalfWidth;
  vEdge = aaEdge;
  // Same rule the Canvas2D renderer used: a stroke's own glow, floored by
  // the scene-wide glow slider.
  vGlow = max(globalGlow, glow);

  vec2 p0k = applyKeystone(p0);
  vec2 p1k = applyKeystone(p1);
  vLineStart = p0k;
  vLineEnd = p1k;

  vec2 delta = p1k - p0k;
  float len = length(delta);
  vec2 dir = len > 0.00001 ? delta / len : vec2(1.0, 0.0);
  vec2 perp = vec2(-dir.y, dir.x);

  // Margin so a round join/cap (which bulges past the true segment
  // endpoints by up to the SDF radius) has geometry to rasterize into —
  // without this the capsule's rounded ends get silently clipped by the
  // quad itself before the fragment shader ever sees them.
  float margin = vRadius + vEdge;

  // quadPos.x arrives as [-1,1]; remap to [0,1] so t=0 is p0 and t=1 is p1
  // (using quadPos.x directly here, without this remap, doubles the quad's
  // span and mis-centers it on p0 instead of spanning p0->p1).
  float t = quadPos.x * 0.5 + 0.5;
  float alongDist = mix(-margin, len + margin, t);
  vec2 along = dir * alongDist;
  vec2 across = perp * quadPos.y * margin;

  vPos = p0k + along + across;
  gl_Position = projection * vec4(vPos, 0.0, 1.0);
}
`;

    // Fragment shader: capsule SDF with round joins and AA. vPos/vLineStart/
    // vLineEnd/vRadius/vEdge are all in normalized engine space (see vertex
    // shader comment) — this shader never converts to or compares against
    // pixel units, so there's nothing here that needs canvas size.
    const lineFS = `#version 300 es
precision highp float;

in vec3 vColor;
in vec2 vPos;
in vec2 vLineStart, vLineEnd;
in float vRadius;
in float vEdge;
in float vGlow;

// Two targets in one geometry pass: the crisp strokes, and the same strokes
// scaled by their glow to seed the bloom blur. Drawing the geometry twice
// instead would double the vertex work for no benefit.
layout(location = 0) out vec4 outSharp;
layout(location = 1) out vec4 outGlowSrc;

float sdfCapsule(vec2 p, vec2 a, vec2 b, float r) {
  vec2 pa = p - a, ba = b - a;
  float h = clamp(dot(pa, ba) / max(dot(ba, ba), 1e-8), 0.0, 1.0);
  return length(pa - ba * h) - r;
}

void main() {
  float d = sdfCapsule(vPos, vLineStart, vLineEnd, vRadius);
  float alpha = 1.0 - smoothstep(-vEdge, vEdge, d);
  if (alpha < 0.01) discard;
  // Premultiplied alpha: adjacent segments' quads deliberately overlap at
  // joints (see the margin comment in the vertex shader) so round joins have
  // geometry to rasterize into. Un-premultiplied SRC_ALPHA blending double-
  // composites that overlap and shows up as visible seams/dots at every
  // joint; premultiplied output + ONE/ONE_MINUS_SRC_ALPHA blending (set in
  // init()) composites overlapping semi-transparent edges correctly.
  //
  // Both targets stay premultiplied and share one blend function (WebGL2
  // core has no per-attachment blending), so the glow target scales only
  // its colour and keeps the same coverage alpha — the bloom is a dimmer
  // copy of the same shape, not a differently-shaped one. Blurring
  // premultiplied colour is well-defined, which is why the blur pass can
  // filter this directly without haloing.
  outSharp = vec4(vColor * alpha, alpha);
  outGlowSrc = vec4(vColor * alpha * vGlow, alpha);
}
`;

    this.programs.line = this.linkProgram(lineVS, lineFS, "line");

    // Fullscreen pass geometry, generated from gl_VertexID — a single
    // oversized triangle rather than two quad triangles, so there's no
    // diagonal seam where the halves meet and no vertex buffer to manage.
    const fullscreenVS = `#version 300 es
precision highp float;
out vec2 vUv;
void main() {
  vec2 p = vec2((gl_VertexID << 1) & 2, gl_VertexID & 2);
  vUv = p;
  gl_Position = vec4(p * 2.0 - 1.0, 0.0, 1.0);
}
`;

    // Separable Gaussian, run once horizontally then once vertically. Nine
    // taps with the standard normalised weights; separability is what makes
    // the radius essentially free, unlike Canvas2D's shadowBlur where cost
    // scaled with radius and forced the old 16px cap.
    const blurFS = `#version 300 es
precision highp float;
in vec2 vUv;
uniform sampler2D uSrc;
uniform vec2 uStep;   // UV offset per tap; direction encodes the axis
out vec4 outColor;

const float W0 = 0.2270270270;
const float W1 = 0.1945945946;
const float W2 = 0.1216216216;
const float W3 = 0.0540540541;
const float W4 = 0.0162162162;

void main() {
  vec4 c = texture(uSrc, vUv) * W0;
  c += texture(uSrc, vUv + uStep * 1.0) * W1;
  c += texture(uSrc, vUv - uStep * 1.0) * W1;
  c += texture(uSrc, vUv + uStep * 2.0) * W2;
  c += texture(uSrc, vUv - uStep * 2.0) * W2;
  c += texture(uSrc, vUv + uStep * 3.0) * W3;
  c += texture(uSrc, vUv - uStep * 3.0) * W3;
  c += texture(uSrc, vUv + uStep * 4.0) * W4;
  c += texture(uSrc, vUv - uStep * 4.0) * W4;
  outColor = c;
}
`;

    // Combine, with trail feedback. The sharp layer is premultiplied, so its
    // colour is already its own contribution and it composites over what's
    // behind it with (1 - alpha); bloom is added on top, the additive look
    // real glow has.
    //
    // The trail term reproduces what the Canvas2D version was actually doing.
    // It faded by filling black at alpha (1 - trail) in source-over, i.e.
    // `existing = existing * trail`, then drew the new frame over that. So
    // `trail` is a per-frame retention factor, not a duration. At 0 this
    // reduces exactly to the previous no-feedback combine.
    const compositeFS = `#version 300 es
precision highp float;
in vec2 vUv;
uniform sampler2D uSharp;
uniform sampler2D uBloom;
uniform sampler2D uPrev;
uniform float uBloomIntensity;
uniform float uTrail;
out vec4 outColor;

void main() {
  vec4 sharp = texture(uSharp, vUv);
  vec3 bloom = texture(uBloom, vUv).rgb * uBloomIntensity;
  vec3 prev = texture(uPrev, vUv).rgb * uTrail;
  outColor = vec4(sharp.rgb + bloom + prev * (1.0 - sharp.a), 1.0);
}
`;

    // Monitor post-effects, composed as one set of source lookups instead of
    // the old chain of full-canvas blits (the kaleidoscope alone used to cost
    // one clipped blit per wedge, every frame).
    //
    // Applied in reverse of the Canvas2D order — that ran kaleidoscope, then
    // mirror, then flip over the rasterised image, and asking "where does
    // this output pixel read from" walks that chain backwards.
    const postFS = `#version 300 es
precision highp float;
in vec2 vUv;
uniform sampler2D uSrc;
uniform float uSegments;   // 0 = off, otherwise 3..12
uniform vec2 uMirror;      // x, y as 0/1
uniform vec2 uFlip;        // x, y as 0/1
uniform float uAspect;     // canvas w/h, so wedges stay circular
out vec4 outColor;

const float PI = 3.14159265359;

void main() {
  vec2 uv = vUv;

  // Whole-image reversal (per-monitor orientation), self-inverse.
  if (uFlip.x > 0.5) uv.x = 1.0 - uv.x;
  if (uFlip.y > 0.5) uv.y = 1.0 - uv.y;

  // Mirror is an asymmetric overwrite, not a symmetric fold: the old code
  // copied one half over the other and left the source half untouched. The
  // y test is inverted relative to x because texture v runs bottom-up while
  // the canvas it was ported from ran top-down.
  if (uMirror.x > 0.5 && uv.x > 0.5) uv.x = 1.0 - uv.x;
  if (uMirror.y > 0.5 && uv.y < 0.5) uv.y = 1.0 - uv.y;

  if (uSegments >= 3.0) {
    vec2 p = uv - 0.5;
    p.x *= uAspect;
    float r = length(p);
    float a = atan(p.y, p.x);
    if (a < 0.0) a += 2.0 * PI;
    float seg = 2.0 * PI / uSegments;
    float idx = floor(a / seg);
    float local = a - idx * seg;
    // Mirror alternate wedges so neighbours meet along their shared edge.
    if (mod(idx, 2.0) >= 1.0) local = seg - local;
    p = vec2(cos(local), sin(local)) * r;
    p.x /= uAspect;
    uv = p + 0.5;
  }

  outColor = vec4(texture(uSrc, uv).rgb, 1.0);
}
`;

    this.programs.blur = this.linkProgram(fullscreenVS, blurFS, "blur");
    this.programs.composite = this.linkProgram(fullscreenVS, compositeFS, "composite");
    this.programs.post = this.linkProgram(fullscreenVS, postFS, "post");

    // The fullscreen triangle uses no attributes, but WebGL2 still requires
    // a bound VAO to draw.
    this.buffers.emptyVao = gl.createVertexArray();

    // Create the quad geometry (4 corners, 6 indices)
    const quadVerts = new Float32Array([
      -1, -1,  // BL
       1, -1,  // BR
       1,  1,  // TR
      -1,  1   // TL
    ]);
    const quadIndices = new Uint16Array([0, 1, 2, 0, 2, 3]);

    const quadVbo = gl.createBuffer();
    gl.bindBuffer(gl.ARRAY_BUFFER, quadVbo);
    gl.bufferData(gl.ARRAY_BUFFER, quadVerts, gl.STATIC_DRAW);

    const ebo = gl.createBuffer();
    gl.bindBuffer(gl.ELEMENT_ARRAY_BUFFER, ebo);
    gl.bufferData(gl.ELEMENT_ARRAY_BUFFER, quadIndices, gl.STATIC_DRAW);

    this.buffers.quadVbo = quadVbo;
    this.buffers.quadEbo = ebo;
    this.buffers.quadIndexCount = quadIndices.length;

    // Instance buffers (will be populated per-frame)
    this.buffers.instanceVao = gl.createVertexArray();
    gl.bindVertexArray(this.buffers.instanceVao);

    // Bind quad geometry
    gl.bindBuffer(gl.ARRAY_BUFFER, quadVbo);
    const quadPosLoc = gl.getAttribLocation(this.programs.line, 'quadPos');
    gl.enableVertexAttribArray(quadPosLoc);
    gl.vertexAttribPointer(quadPosLoc, 2, gl.FLOAT, false, 8, 0);

    // Bind element buffer
    gl.bindBuffer(gl.ELEMENT_ARRAY_BUFFER, ebo);

    // Create instance buffers (but don't fill them yet)
    const p0Vbo = gl.createBuffer();
    const p1Vbo = gl.createBuffer();
    const colorVbo = gl.createBuffer();
    const capVbo = gl.createBuffer();
    const glowVbo = gl.createBuffer();

    const p0Loc = gl.getAttribLocation(this.programs.line, 'p0');
    const p1Loc = gl.getAttribLocation(this.programs.line, 'p1');
    const colorLoc = gl.getAttribLocation(this.programs.line, 'color');
    const capStartLoc = gl.getAttribLocation(this.programs.line, 'capStart');
    const capEndLoc = gl.getAttribLocation(this.programs.line, 'capEnd');
    const glowLoc = gl.getAttribLocation(this.programs.line, 'glow');

    this.buffers.instanceBuffers = {
      p0: { vbo: p0Vbo, loc: p0Loc, size: 2 },
      p1: { vbo: p1Vbo, loc: p1Loc, size: 2 },
      color: { vbo: colorVbo, loc: colorLoc, size: 3 },
      capStart: { vbo: capVbo, loc: capStartLoc, size: 1 },
      capEnd: { vbo: capVbo, loc: capEndLoc, size: 1 },
      glow: { vbo: glowVbo, loc: glowLoc, size: 1 }
    };

    gl.bindVertexArray(null);
  }

  rebuildGLResources() {
    this.buildGLResources();
  }

  // Shown as a DOM overlay rather than painted into the canvas: once a
  // webgl2 context exists on an element, getContext("2d") on it returns
  // null, so drawing the message was silently impossible on exactly the
  // path that reports a missing GL feature — the machine got a black
  // screen and nothing else.
  showWebGLError(msg) {
    console.error("[PromptWaver]", msg);
    const el = document.createElement("div");
    el.textContent = `PromptWaver — ${msg}`;
    el.style.cssText = "position:absolute;z-index:9;left:0;right:0;top:40%;" +
      "text-align:center;color:#a04040;background:#000;padding:12px;" +
      "font:13px ui-monospace,Menlo,Consolas,monospace;pointer-events:none";
    (this.canvas.parentNode || document.body).appendChild(el);
  }

  render(scene, filters, canvasSize) {
    if (!this.gl || this.contextLost || !this.programs.line) return;

    const gl = this.gl;
    const w = canvasSize.width, h = canvasSize.height;
    if (w < 1 || h < 1) return;

    if (!this._ensureTargets(w, h)) return;

    const segments = (scene && scene.length) ? this._buildSegments(scene, filters) : [];

    // --- pass 1: geometry into the offscreen scene buffer (sharp + glow) ---
    gl.bindFramebuffer(gl.FRAMEBUFFER, this.fbos.scene.fb);
    gl.drawBuffers(this.fbos.scene.drawBuffers);
    gl.viewport(0, 0, w, h);
    gl.clearColor(0, 0, 0, 0);
    gl.clear(gl.COLOR_BUFFER_BIT);

    if (segments.length) {
      this._uploadSegments(segments);

      // Letterbox scale in PIXELS. This is also "pixels per normalized-space
      // unit", which is exactly the conversion factor needed to turn a desired
      // pixel line-width/AA-feather into the normalized-space units the shader
      // actually operates in.
      //
      // `contentAspect` is the display width/height of the [-1,1] box, NOT the
      // shape of this canvas and NOT the rig's output ratio — see
      // Engine.content_aspect. A 3D scene's box is already the viewport's
      // shape (its camera divided x by the aspect); a flat one's is square
      // however wide the rig is. Fitting the former into a canvas of the same
      // shape is a no-op, which is why this only visibly does anything for 2D
      // scenes on a non-1:1 ratio, or an output window whose physical shape
      // differs from the configured ratio.
      const a = filters.contentAspect || 1;
      let sx = w * 0.5, sy = sx / a;
      if (filters.fit === "stretch") {
        // Fill the canvas on both axes and accept the distortion.
        sx = w * 0.5; sy = h * 0.5;
      } else if (filters.fit === "fill") {
        // Cover: scale until the short axis is filled, cropping the long one.
        if (sy < h * 0.5) { sy = h * 0.5; sx = sy * a; }
      } else {
        // Contain (default): the whole image survives, with bars.
        if (sy > h * 0.5) { sy = h * 0.5; sx = sy * a; }
      }
      const scale = Math.min(sx, sy);

      const prog = this.programs.line;
      gl.useProgram(prog);
      gl.bindVertexArray(this.buffers.instanceVao);
      gl.enable(gl.BLEND);

      gl.uniformMatrix4fv(gl.getUniformLocation(prog, 'projection'), false,
                          this._buildProjectionMatrix(w, h, sx, sy));

      const pixelWidth = this._getLineWidth(scale, filters);
      gl.uniform1f(gl.getUniformLocation(prog, 'lineHalfWidth'), (pixelWidth / 2) / scale);
      // Half-pixel feather, so the AA transition spans ~1px total. This must
      // stay well under the line's half-width: a feather wider than the line
      // means alpha never reaches 1.0 even at the stroke's centre, leaving
      // every line semi-transparent and every overlapping joint visibly
      // brighter than the line it joins (reads as dots along the stroke).
      gl.uniform1f(gl.getUniformLocation(prog, 'aaEdge'), 0.5 / scale);
      gl.uniform2f(gl.getUniformLocation(prog, 'keystoneHV'),
                   filters.keystoneH || 0, filters.keystoneV || 0);
      gl.uniform1f(gl.getUniformLocation(prog, 'globalGlow'), filters.glow || 0);

      gl.drawElementsInstanced(gl.TRIANGLES, this.buffers.quadIndexCount,
                               gl.UNSIGNED_SHORT, 0, segments.length);
    }

    // --- passes 2 & 3: separable blur of the glow target, at half res ---
    // The blur passes and the final combine all overwrite their whole target,
    // so blending is off for them; leaving it on would composite each pass
    // against whatever the previous frame left behind.
    gl.disable(gl.BLEND);
    gl.bindVertexArray(this.buffers.emptyVao);
    gl.useProgram(this.programs.blur);
    gl.uniform1i(gl.getUniformLocation(this.programs.blur, 'uSrc'), 0);
    gl.activeTexture(gl.TEXTURE0);

    // Tap spacing in full-resolution pixels, scaled with the canvas so the
    // bloom reads the same at preview size and on a projector. Expressed in
    // UV, so it's independent of the (half-res) buffer being sampled.
    const spread = filters.bloomSpread ?? PromptWaverRenderer.BLOOM_SPREAD;
    const stepPx = Math.max(1.0, Math.min(w, h) * spread);
    const bw = this.fbos.bloomA.width, bh = this.fbos.bloomA.height;

    gl.bindFramebuffer(gl.FRAMEBUFFER, this.fbos.bloomA.fb);
    gl.viewport(0, 0, bw, bh);
    gl.bindTexture(gl.TEXTURE_2D, this.fbos.scene.textures[1]);
    gl.uniform2f(gl.getUniformLocation(this.programs.blur, 'uStep'), stepPx / w, 0);
    gl.drawArrays(gl.TRIANGLES, 0, 3);

    gl.bindFramebuffer(gl.FRAMEBUFFER, this.fbos.bloomB.fb);
    gl.viewport(0, 0, bw, bh);
    gl.bindTexture(gl.TEXTURE_2D, this.fbos.bloomA.textures[0]);
    gl.uniform2f(gl.getUniformLocation(this.programs.blur, 'uStep'), 0, stepPx / h);
    gl.drawArrays(gl.TRIANGLES, 0, 3);

    // --- pass 4: combine sharp + bloom over the faded previous frame ---
    // Into a buffer rather than straight to the screen, because the result
    // has to survive to be read as `uPrev` next frame; the default
    // framebuffer can't be relied on for that.
    const [prev, cur] = this._trailPair();
    gl.bindFramebuffer(gl.FRAMEBUFFER, cur.fb);
    gl.viewport(0, 0, w, h);

    const comp = this.programs.composite;
    gl.useProgram(comp);
    gl.activeTexture(gl.TEXTURE0);
    gl.bindTexture(gl.TEXTURE_2D, this.fbos.scene.textures[0]);
    gl.uniform1i(gl.getUniformLocation(comp, 'uSharp'), 0);
    gl.activeTexture(gl.TEXTURE1);
    gl.bindTexture(gl.TEXTURE_2D, this.fbos.bloomB.textures[0]);
    gl.uniform1i(gl.getUniformLocation(comp, 'uBloom'), 1);
    gl.activeTexture(gl.TEXTURE2);
    gl.bindTexture(gl.TEXTURE_2D, prev.textures[0]);
    gl.uniform1i(gl.getUniformLocation(comp, 'uPrev'), 2);
    gl.uniform1f(gl.getUniformLocation(comp, 'uBloomIntensity'),
                 filters.bloomIntensity ?? PromptWaverRenderer.BLOOM_INTENSITY);
    gl.uniform1f(gl.getUniformLocation(comp, 'uTrail'),
                 Math.max(0, Math.min(0.95, filters.trail || 0)));
    gl.drawArrays(gl.TRIANGLES, 0, 3);
    this._trailFlip = !this._trailFlip;

    // --- pass 5: monitor post-effects, to the screen ---
    gl.bindFramebuffer(gl.FRAMEBUFFER, null);
    gl.viewport(0, 0, w, h);
    gl.clearColor(0, 0, 0, 1);
    gl.clear(gl.COLOR_BUFFER_BIT);

    const post = this.programs.post;
    gl.useProgram(post);
    gl.activeTexture(gl.TEXTURE0);
    gl.bindTexture(gl.TEXTURE_2D, cur.textures[0]);
    gl.uniform1i(gl.getUniformLocation(post, 'uSrc'), 0);
    gl.uniform1f(gl.getUniformLocation(post, 'uSegments'),
                 Math.round(filters.kaleidoscopeSegments || 0));
    gl.uniform2f(gl.getUniformLocation(post, 'uMirror'),
                 filters.mirrorX ? 1 : 0, filters.mirrorY ? 1 : 0);
    gl.uniform2f(gl.getUniformLocation(post, 'uFlip'),
                 filters.flipX ? 1 : 0, filters.flipY ? 1 : 0);
    gl.uniform1f(gl.getUniformLocation(post, 'uAspect'), w / h);
    gl.drawArrays(gl.TRIANGLES, 0, 3);

    gl.bindVertexArray(null);
    gl.enable(gl.BLEND);
  }

  // Which trail buffer is the previous frame and which is being written.
  // Swapped after every composite so this frame's output becomes next
  // frame's feedback source.
  _trailPair() {
    return this._trailFlip
      ? [this.fbos.trailB, this.fbos.trailA]
      : [this.fbos.trailA, this.fbos.trailB];
  }

  // Allocate (or reallocate) the offscreen targets for the current canvas
  // size. Canvas resizes do not resize an attached texture, so a stale FBO
  // would keep rendering at the old resolution and show up as a scaled or
  // cropped image rather than an obvious failure.
  _ensureTargets(w, h) {
    if (this.fbos.scene && this.fbos.scene.width === w && this.fbos.scene.height === h) {
      return true;
    }
    this._destroyTargets();

    const bw = Math.max(1, w >> 1), bh = Math.max(1, h >> 1);
    // RGBA16F so bloom can carry values above 1.0 — Canvas2D's shadow was
    // clamped to the 8-bit range, which is what kept its glow flat.
    const scene = this.createFBO(w, h, "RGBA16F", 2);
    const bloomA = this.createFBO(bw, bh, "RGBA16F", 1);
    const bloomB = this.createFBO(bw, bh, "RGBA16F", 1);
    // The trail pair is deliberately 8-bit, not 16F like the rest. It feeds
    // back into itself every frame, so an unclamped format would let a bright
    // static image compound toward trail/(1-trail) — about 20x at the 0.95
    // ceiling — instead of settling. Clamping at each step is also what the
    // Canvas2D version did implicitly, so saturation behaves as before.
    const trailA = this.createFBO(w, h, "RGBA", 1);
    const trailB = this.createFBO(w, h, "RGBA", 1);

    if (!scene || !bloomA || !bloomB || !trailA || !trailB) {
      console.error("[PromptWaver] could not allocate render targets");
      this._destroyTargets();
      return false;
    }
    this.fbos = { scene, bloomA, bloomB, trailA, trailB };
    this._trailFlip = false;

    // Freshly created textures have undefined contents, and the trail pair is
    // read before it is ever fully written — without this the first frames
    // can feed back whatever was in the driver's memory.
    const gl = this.gl;
    gl.clearColor(0, 0, 0, 1);
    for (const t of [trailA, trailB]) {
      gl.bindFramebuffer(gl.FRAMEBUFFER, t.fb);
      gl.clear(gl.COLOR_BUFFER_BIT);
    }
    gl.bindFramebuffer(gl.FRAMEBUFFER, null);
    return true;
  }

  _destroyTargets() {
    if (!this.gl) return;
    const gl = this.gl;
    for (const fbo of Object.values(this.fbos)) {
      if (!fbo) continue;
      fbo.textures.forEach(t => gl.deleteTexture(t));
      gl.deleteFramebuffer(fbo.fb);
    }
    this.fbos = {};
  }

  _buildSegments(scene, filters) {
    const segments = [];
    // Bipolar: positive resamples through a spline (smooth), negative drops
    // points (angular). At 0 neither runs, so the geometry is bit-identical
    // to an unprocessed frame and the control costs nothing when centred.
    const curve = Math.max(-1, Math.min(1, filters.lineCurve || 0));
    const steps = 1 + Math.round(Math.max(0, curve) * 5);

    for (const stroke of scene) {
      let pts = stroke.p;
      if (!pts || pts.length < 2) continue;
      if (curve > 0) pts = PromptWaverRenderer._spline(pts, curve, steps);
      else if (curve < 0) pts = PromptWaverRenderer._decimate(pts, -curve);

      // Absent "g" means no per-stroke glow; the scene-wide glow still
      // applies as a floor, but that's done in the shader so the global
      // slider doesn't require re-uploading every segment.
      const glow = stroke.g || 0;

      for (let i = 0; i < pts.length - 1; i++) {
        const p0 = pts[i];
        const p1 = pts[i + 1];
        const capStart = i === 0 ? 0.0 : 1.0;  // 0 = butt (flat), 1 = round
        const capEnd = i === pts.length - 2 ? 0.0 : 1.0;

        segments.push({
          p0: [p0[0], p0[1]],
          p1: [p1[0], p1[1]],
          color: stroke.c,
          capStart, capEnd, glow
        });
      }
    }
    return segments;
  }

  // Drop points from a polyline to make it visibly faceted — the same kind
  // of coarseness the low-resolution in-page preview shows, but as a
  // deliberate look rather than a budget. Endpoints are always kept, so a
  // stroke never shortens as it coarsens; at full amount a stroke collapses
  // to about three points, which turns a circle into a triangle.
  static _decimate(pts, amount) {
    const n = pts.length;
    const target = Math.max(3, Math.round(n * (1 - amount * 0.85)));
    if (target >= n) return pts;
    // Fractional spacing, rounded to the nearest ORIGINAL vertex. A plain
    // integer stride only reaches n/1, n/2, n/3… so the slider jumped
    // between a handful of point counts instead of sweeping; sampling at
    // fractional positions makes the count continuous. Rounding to real
    // vertices rather than interpolating along the chords keeps any
    // deliberate sharp corner a corner, which is the point of this end of
    // the control.
    const out = [];
    const step = n / target;
    let prev = -1;
    for (let i = 0; i < target; i++) {
      const idx = Math.min(n - 1, Math.round(i * step));
      if (idx !== prev) { out.push(pts[idx]); prev = idx; }
    }
    if (prev !== n - 1) out.push(pts[n - 1]);
    return out.length >= 2 ? out : pts;
  }

  // Cardinal-spline resample of a polyline, passing through every original
  // point. `amount` scales the tangents: 0 leaves the path straight between
  // points (the shape as authored), 0.5 is standard Catmull-Rom. Anything
  // above that overshoots into loops at sharp corners, which is why the
  // slider's top end maps to 0.5 rather than 1.
  //
  // Interpolating here rather than asking the engine for denser strokes is
  // deliberate: this is a look, not extra fidelity, and the points arrive
  // already thinned for the wire. It is also why this is monitor-only — the
  // DAC still gets the authored path.
  static _spline(pts, amount, steps) {
    const n = pts.length;
    const a = amount * 0.5;
    // Clamped at both ends, so the first and last spans get a tangent
    // without inventing points beyond the stroke.
    const at = (i) => pts[i < 0 ? 0 : (i > n - 1 ? n - 1 : i)];
    const out = [];
    for (let i = 0; i < n - 1; i++) {
      const p0 = at(i - 1), p1 = pts[i], p2 = pts[i + 1], p3 = at(i + 2);
      const m1x = a * (p2[0] - p0[0]), m1y = a * (p2[1] - p0[1]);
      const m2x = a * (p3[0] - p1[0]), m2y = a * (p3[1] - p1[1]);
      for (let s = 0; s < steps; s++) {
        const t = s / steps, t2 = t * t, t3 = t2 * t;
        const h00 = 2 * t3 - 3 * t2 + 1;
        const h10 = t3 - 2 * t2 + t;
        const h01 = -2 * t3 + 3 * t2;
        const h11 = t3 - t2;
        out.push([
          h00 * p1[0] + h10 * m1x + h01 * p2[0] + h11 * m2x,
          h00 * p1[1] + h10 * m1y + h01 * p2[1] + h11 * m2y,
        ]);
      }
    }
    out.push(pts[n - 1]);
    return out;
  }

  _uploadSegments(segments) {
    const gl = this.gl;
    const count = segments.length;

    const p0Data = new Float32Array(count * 2);
    const p1Data = new Float32Array(count * 2);
    const colorData = new Float32Array(count * 3);
    const capData = new Float32Array(count * 2);
    const glowData = new Float32Array(count);

    for (let i = 0; i < count; i++) {
      const seg = segments[i];
      p0Data[i * 2] = seg.p0[0];
      p0Data[i * 2 + 1] = seg.p0[1];
      p1Data[i * 2] = seg.p1[0];
      p1Data[i * 2 + 1] = seg.p1[1];
      colorData[i * 3] = seg.color[0];
      colorData[i * 3 + 1] = seg.color[1];
      colorData[i * 3 + 2] = seg.color[2];
      capData[i * 2] = seg.capStart;
      capData[i * 2 + 1] = seg.capEnd;
      glowData[i] = seg.glow;
    }

    gl.bindVertexArray(this.buffers.instanceVao);

    // p0
    gl.bindBuffer(gl.ARRAY_BUFFER, this.buffers.instanceBuffers.p0.vbo);
    gl.bufferData(gl.ARRAY_BUFFER, p0Data, gl.DYNAMIC_DRAW);
    gl.enableVertexAttribArray(this.buffers.instanceBuffers.p0.loc);
    gl.vertexAttribPointer(this.buffers.instanceBuffers.p0.loc, 2, gl.FLOAT, false, 0, 0);
    gl.vertexAttribDivisor(this.buffers.instanceBuffers.p0.loc, 1);

    // p1
    gl.bindBuffer(gl.ARRAY_BUFFER, this.buffers.instanceBuffers.p1.vbo);
    gl.bufferData(gl.ARRAY_BUFFER, p1Data, gl.DYNAMIC_DRAW);
    gl.enableVertexAttribArray(this.buffers.instanceBuffers.p1.loc);
    gl.vertexAttribPointer(this.buffers.instanceBuffers.p1.loc, 2, gl.FLOAT, false, 0, 0);
    gl.vertexAttribDivisor(this.buffers.instanceBuffers.p1.loc, 1);

    // color
    gl.bindBuffer(gl.ARRAY_BUFFER, this.buffers.instanceBuffers.color.vbo);
    gl.bufferData(gl.ARRAY_BUFFER, colorData, gl.DYNAMIC_DRAW);
    gl.enableVertexAttribArray(this.buffers.instanceBuffers.color.loc);
    gl.vertexAttribPointer(this.buffers.instanceBuffers.color.loc, 3, gl.FLOAT, false, 0, 0);
    gl.vertexAttribDivisor(this.buffers.instanceBuffers.color.loc, 1);

    // glow
    gl.bindBuffer(gl.ARRAY_BUFFER, this.buffers.instanceBuffers.glow.vbo);
    gl.bufferData(gl.ARRAY_BUFFER, glowData, gl.DYNAMIC_DRAW);
    gl.enableVertexAttribArray(this.buffers.instanceBuffers.glow.loc);
    gl.vertexAttribPointer(this.buffers.instanceBuffers.glow.loc, 1, gl.FLOAT, false, 0, 0);
    gl.vertexAttribDivisor(this.buffers.instanceBuffers.glow.loc, 1);

    // Cap flags — uploaded even though the current shader doesn't read them
    // yet (every join renders round for now; butt-vs-round endpoint capping
    // is a Phase 1 refinement). GLSL strips unused attributes at compile
    // time, so their location is legitimately -1 until the shader actually
    // references them — guard rather than assume they're always bound.
    const capStartLoc = this.buffers.instanceBuffers.capStart.loc;
    const capEndLoc = this.buffers.instanceBuffers.capEnd.loc;
    if (capStartLoc !== -1 || capEndLoc !== -1) {
      gl.bindBuffer(gl.ARRAY_BUFFER, this.buffers.instanceBuffers.capStart.vbo);
      gl.bufferData(gl.ARRAY_BUFFER, capData, gl.DYNAMIC_DRAW);
      if (capStartLoc !== -1) {
        gl.enableVertexAttribArray(capStartLoc);
        gl.vertexAttribPointer(capStartLoc, 1, gl.FLOAT, false, 8, 0);
        gl.vertexAttribDivisor(capStartLoc, 1);
      }
      if (capEndLoc !== -1) {
        gl.enableVertexAttribArray(capEndLoc);
        gl.vertexAttribPointer(capEndLoc, 1, gl.FLOAT, false, 8, 4);
        gl.vertexAttribDivisor(capEndLoc, 1);
      }
    }

    gl.bindVertexArray(null);
  }

  _buildProjectionMatrix(w, h, sx, sy) {
    // gl_Position must land in GL clip space [-1,1], NOT pixel space — sx/sy
    // (pixels of letterboxed viewport per normalized-space unit) get rescaled
    // here into "clip-space units per normalized-space unit" by dividing out
    // the canvas's own pixel-to-clip ratio (canvas width/height maps to 2
    // clip units). No Y flip: clip space is Y-up, same as our normalized
    // engine space, unlike a 2D canvas's Y-down pixel space.
    const scaleX = sx * 2 / w;
    const scaleY = sy * 2 / h;

    const proj = new Float32Array(16);
    proj[0] = scaleX; proj[5] = scaleY; proj[10] = 1; proj[15] = 1;
    return proj;
  }

  _getLineWidth(scale, filters) {
    // Preserves the two surfaces' original Canvas2D widths exactly: the
    // projector window scaled its stroke to the viewport, while the small
    // in-page preview used a fixed hairline.
    //
    // `lineWidth` is a MULTIPLIER on that base rather than an absolute pixel
    // count, so the two surfaces keep their (deliberate) difference and the
    // preview goes on predicting what the projector will do. Clamped at 1
    // from below: the aaEdge feather above is a fixed half pixel, and a
    // stroke thinner than that never reaches full alpha at its own centre.
    const base = this.lineWidthMode === "viewport" ? Math.max(1, scale / 260) : 1.4;
    const mult = Math.max(1, Math.min(8, (filters && filters.lineWidth) || 1));
    return base * mult;
  }

  // Utility: compile a shader
  compileShader(source, type) {
    if (!this.gl) return null;
    const gl = this.gl;
    const shader = gl.createShader(type);
    gl.shaderSource(shader, source);
    gl.compileShader(shader);

    if (!gl.getShaderParameter(shader, gl.COMPILE_STATUS)) {
      const err = gl.getShaderInfoLog(shader);
      console.error(`[PromptWaver] Shader compile error (${type === gl.VERTEX_SHADER ? 'vertex' : 'fragment'}):`, err);
      gl.deleteShader(shader);
      return null;
    }
    return shader;
  }

  // Utility: link a program
  linkProgram(vsSource, fsSource, name = "unnamed") {
    if (!this.gl) return null;
    const gl = this.gl;

    const vs = this.compileShader(vsSource, gl.VERTEX_SHADER);
    const fs = this.compileShader(fsSource, gl.FRAGMENT_SHADER);

    if (!vs || !fs) return null;

    const program = gl.createProgram();
    gl.attachShader(program, vs);
    gl.attachShader(program, fs);
    gl.linkProgram(program);

    if (!gl.getProgramParameter(program, gl.LINK_STATUS)) {
      const err = gl.getProgramInfoLog(program);
      console.error(`[PromptWaver] Program link error (${name}):`, err);
      gl.deleteProgram(program);
      return null;
    }

    gl.deleteShader(vs);
    gl.deleteShader(fs);
    return program;
  }

  // Utility: create a framebuffer with color texture(s)
  createFBO(width, height, internalFormat = "RGBA", count = 1) {
    if (!this.gl) return null;
    const gl = this.gl;

    const fbo = {
      fb: gl.createFramebuffer(),
      textures: [],
      width, height
    };

    gl.bindFramebuffer(gl.FRAMEBUFFER, fbo.fb);

    for (let i = 0; i < count; i++) {
      const tex = gl.createTexture();
      gl.bindTexture(gl.TEXTURE_2D, tex);

      let glFormat, glInternalFormat, glType;
      if (internalFormat === "RGBA16F") {
        glFormat = gl.RGBA;
        glInternalFormat = gl.RGBA16F;
        glType = gl.HALF_FLOAT;
      } else {
        glFormat = gl.RGBA;
        glInternalFormat = gl.RGBA;
        glType = gl.UNSIGNED_BYTE;
      }

      gl.texImage2D(gl.TEXTURE_2D, 0, glInternalFormat, width, height, 0, glFormat, glType, null);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);

      const attachment = gl[`COLOR_ATTACHMENT${i}`];
      gl.framebufferTexture2D(gl.FRAMEBUFFER, attachment, gl.TEXTURE_2D, tex, 0);
      fbo.textures.push(tex);
    }

    // Without this, only attachment 0 is written and the second MRT target
    // silently stays blank — the fragment shader's extra outputs go nowhere.
    fbo.drawBuffers = fbo.textures.map((_, i) => gl[`COLOR_ATTACHMENT${i}`]);
    gl.drawBuffers(fbo.drawBuffers);

    if (gl.checkFramebufferStatus(gl.FRAMEBUFFER) !== gl.FRAMEBUFFER_COMPLETE) {
      console.error("[PromptWaver] FBO incomplete");
      gl.deleteFramebuffer(fbo.fb);
      fbo.textures.forEach(tex => gl.deleteTexture(tex));
      return null;
    }

    gl.bindFramebuffer(gl.FRAMEBUFFER, null);
    return fbo;
  }

  // Flatten JSON scene to typed arrays (Phase 1+)
  flattenScene(scene) {
    if (!scene || !scene.length) return { strokes: [], count: 0 };

    const strokes = [];
    for (const st of scene) {
      const stroke = {
        color: new Float32Array(st.c),
        points: new Float32Array(st.p.flat()),
        glow: st.g || 0.0,
        pointCount: st.p.length
      };
      strokes.push(stroke);
    }
    return { strokes, count: strokes.length };
  }

  // Clean up resources
  destroy() {
    if (!this.gl) return;
    const gl = this.gl;

    this._destroyTargets();

    // `buffers` holds VAOs and a plain count alongside the actual buffers,
    // so each kind has to be released with its matching delete call rather
    // than passing the lot to deleteBuffer.
    const b = this.buffers;
    // Deduped: capStart and capEnd deliberately share one interleaved buffer.
    const vbos = new Set([b.quadVbo, b.quadEbo]);
    Object.values(b.instanceBuffers || {}).forEach(e => vbos.add(e.vbo));
    vbos.forEach(v => v && gl.deleteBuffer(v));
    [b.instanceVao, b.emptyVao].forEach(v => v && gl.deleteVertexArray(v));
    Object.values(this.programs).forEach(p => p && gl.deleteProgram(p));

    this.buffers = {};
    this.programs = {};
  }
}

// Export for use in HTML
window.PromptWaverRenderer = PromptWaverRenderer;
