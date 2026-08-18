/**
 * PromptWaver WebGL2 Renderer
 * Unified rendering module for output.html and index.html
 * Handles thick anti-aliased lines, colored glow/bloom, trail feedback, and post-fx
 */

class PromptWaverRenderer {
  constructor(canvas) {
    this.canvas = canvas;
    this.gl = null;
    this.contextLost = false;
    this.programs = {};
    this.fbos = {};
    this.buffers = {};
    this.init();
  }

  init() {
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
    gl.blendFunc(gl.SRC_ALPHA, gl.ONE_MINUS_SRC_ALPHA);
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

    // Vertex shader: expand quads around line segments, apply transforms
    const lineVS = `#version 300 es
precision highp float;

// Per-segment instance data
in vec2 p0, p1;
in vec3 color;
in float capStart, capEnd;

// Per-frame uniforms
uniform mat4 projection;
uniform float lineWidth;
uniform vec2 keystoneH_keystoneV;

// Per-vertex quad data (0-3 for the four corners of the bounding quad)
in vec2 quadPos;  // (-1,-1) to (1,1), expanded by line width

out vec3 vColor;
out vec2 vPos;
out vec2 vLineStart, vLineEnd;
out float vCapStart, vCapEnd;
out float vLineLen;

vec2 applyKeystone(vec2 p) {
  float kh = keystoneH_keystoneV.x;
  float kv = keystoneH_keystoneV.y;
  float xk = p.x * (1.0 + kh * p.y);
  float yk = p.y * (1.0 + kv * xk);
  return vec2(
    max(-1.0, min(1.0, xk)),
    max(-1.0, min(1.0, yk))
  );
}

void main() {
  vColor = color;
  vCapStart = capStart;
  vCapEnd = capEnd;

  // Apply keystone to endpoints
  vec2 p0k = applyKeystone(p0);
  vec2 p1k = applyKeystone(p1);

  vLineStart = p0k;
  vLineEnd = p1k;

  vec2 delta = p1k - p0k;
  vLineLen = length(delta);

  vec2 dir = normalize(delta);
  vec2 perp = vec2(-dir.y, dir.x);

  // Expand quad around the line segment
  vec2 quad = quadPos * (lineWidth / 2.0);
  vec2 along = dir * quad.x * vLineLen;
  vec2 across = perp * quad.y;

  vPos = p0k + along + across;

  gl_Position = projection * vec4(vPos, 0.0, 1.0);
}
`;

    // Fragment shader: capsule SDF with round joins and AA
    const lineFS = `#version 300 es
precision highp float;

in vec3 vColor;
in vec2 vPos;
in vec2 vLineStart, vLineEnd;
in float vCapStart, vCapEnd;
in float vLineLen;

out vec4 outColor;

float sdfCapsule(vec2 p, vec2 a, vec2 b, float r) {
  vec2 pa = p - a, ba = b - a;
  float h = clamp(dot(pa, ba) / dot(ba, ba), 0.0, 1.0);
  return length(pa - ba * h) - r;
}

void main() {
  float r = 1.0;  // radius for SDF
  float d = sdfCapsule(vPos, vLineStart, vLineEnd, r);

  // Smooth antialiasing over ~1.5px
  float edge = 1.5;
  float alpha = 1.0 - smoothstep(-edge, edge, d);

  if (alpha < 0.01) discard;

  outColor = vec4(vColor, alpha);
}
`;

    this.programs.line = this.linkProgram(lineVS, lineFS, "line");

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

    const p0Loc = gl.getAttribLocation(this.programs.line, 'p0');
    const p1Loc = gl.getAttribLocation(this.programs.line, 'p1');
    const colorLoc = gl.getAttribLocation(this.programs.line, 'color');
    const capStartLoc = gl.getAttribLocation(this.programs.line, 'capStart');
    const capEndLoc = gl.getAttribLocation(this.programs.line, 'capEnd');

    this.buffers.instanceBuffers = {
      p0: { vbo: p0Vbo, loc: p0Loc, size: 2 },
      p1: { vbo: p1Vbo, loc: p1Loc, size: 2 },
      color: { vbo: colorVbo, loc: colorLoc, size: 3 },
      capStart: { vbo: capVbo, loc: capStartLoc, size: 1 },
      capEnd: { vbo: capVbo, loc: capEndLoc, size: 1 }
    };

    gl.bindVertexArray(null);
  }

  rebuildGLResources() {
    this.buildGLResources();
  }

  showWebGLError(msg) {
    console.error("[PromptWaver]", msg);
    const canvas = this.canvas;
    const ctx = canvas.getContext("2d");
    if (ctx) {
      ctx.fillStyle = "#000";
      ctx.fillRect(0, 0, canvas.width, canvas.height);
      ctx.fillStyle = "#a04040";
      ctx.font = "14px monospace";
      ctx.fillText(msg, 20, 30);
    }
  }

  render(scene, filters, canvasSize) {
    if (!this.gl || this.contextLost) return;

    const gl = this.gl;
    const w = canvasSize.width, h = canvasSize.height;

    // Clear
    gl.clear(gl.COLOR_BUFFER_BIT);

    if (!scene || !scene.length) return;

    // Flatten scene into line segments
    const segments = this._buildSegments(scene, filters);
    if (segments.length === 0) return;

    // Upload instance data
    this._uploadSegments(segments);

    // Set up projection
    const proj = this._buildProjectionMatrix(w, h, filters);

    // Render
    gl.useProgram(this.programs.line);
    gl.bindVertexArray(this.buffers.instanceVao);

    const projLoc = gl.getUniformLocation(this.programs.line, 'projection');
    gl.uniformMatrix4fv(projLoc, false, proj);

    const lineWidthLoc = gl.getUniformLocation(this.programs.line, 'lineWidth');
    const lineWidth = this._getLineWidth(w, h);
    gl.uniform1f(lineWidthLoc, lineWidth);

    const ksLoc = gl.getUniformLocation(this.programs.line, 'keystoneH_keystoneV');
    gl.uniform2f(ksLoc, filters.keystoneH || 0, filters.keystoneV || 0);

    gl.drawElementsInstanced(gl.TRIANGLES, this.buffers.quadIndexCount, gl.UNSIGNED_SHORT, 0, segments.length);
  }

  _buildSegments(scene, filters) {
    const segments = [];
    for (const stroke of scene) {
      const pts = stroke.p;
      if (!pts || pts.length < 2) continue;

      for (let i = 0; i < pts.length - 1; i++) {
        const p0 = pts[i];
        const p1 = pts[i + 1];
        const capStart = i === 0 ? 0.0 : 1.0;  // 0 = butt (flat), 1 = round
        const capEnd = i === pts.length - 2 ? 0.0 : 1.0;

        segments.push({
          p0: [p0[0], p0[1]],
          p1: [p1[0], p1[1]],
          color: stroke.c,
          capStart, capEnd
        });
      }
    }
    return segments;
  }

  _uploadSegments(segments) {
    const gl = this.gl;
    const count = segments.length;

    const p0Data = new Float32Array(count * 2);
    const p1Data = new Float32Array(count * 2);
    const colorData = new Float32Array(count * 3);
    const capData = new Float32Array(count * 2);

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

    // cap flags (both in the same buffer, different offsets)
    gl.bindBuffer(gl.ARRAY_BUFFER, this.buffers.instanceBuffers.capStart.vbo);
    gl.bufferData(gl.ARRAY_BUFFER, capData, gl.DYNAMIC_DRAW);
    gl.enableVertexAttribArray(this.buffers.instanceBuffers.capStart.loc);
    gl.vertexAttribPointer(this.buffers.instanceBuffers.capStart.loc, 1, gl.FLOAT, false, 8, 0);
    gl.vertexAttribDivisor(this.buffers.instanceBuffers.capStart.loc, 1);

    gl.enableVertexAttribArray(this.buffers.instanceBuffers.capEnd.loc);
    gl.vertexAttribPointer(this.buffers.instanceBuffers.capEnd.loc, 1, gl.FLOAT, false, 8, 4);
    gl.vertexAttribDivisor(this.buffers.instanceBuffers.capEnd.loc, 1);

    gl.bindVertexArray(null);
  }

  _buildProjectionMatrix(w, h, filters) {
    // Letterbox the -1..1 space into the window, preserving aspect ratio
    const a = filters.aspect || 1;
    let sx = w * 0.5, sy = sx / a;
    if (sy > h * 0.5) { sy = h * 0.5; sx = sy * a; }

    const cx = w / 2, cy = h / 2;

    // Orthographic projection: map normalized coords to pixel coords
    // [-1,1] -> letterboxed viewport
    const proj = new Float32Array(16);
    proj[0] = sx;    proj[1] = 0;     proj[2] = 0;  proj[3] = 0;
    proj[4] = 0;     proj[5] = -sy;   proj[6] = 0;  proj[7] = 0;
    proj[8] = 0;     proj[9] = 0;     proj[10] = 1; proj[11] = 0;
    proj[12] = cx;   proj[13] = cy;   proj[14] = 0; proj[15] = 1;

    return proj;
  }

  _getLineWidth(w, h) {
    // output.html: scale with viewport, index.html: fixed 1.4
    // For now, use the fixed value; we'll make it per-page in Phase 1 refinement
    return 1.4;
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

    Object.values(this.fbos).forEach(fbo => {
      fbo.textures.forEach(tex => gl.deleteTexture(tex));
      gl.deleteFramebuffer(fbo.fb);
    });

    Object.values(this.buffers).forEach(buf => gl.deleteBuffer(buf));
    Object.values(this.programs).forEach(prog => gl.deleteProgram(prog));
  }
}

// Export for use in HTML
window.PromptWaverRenderer = PromptWaverRenderer;
