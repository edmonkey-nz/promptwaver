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
    // Phase 0: stub — just clear to black
    // Shaders and FBOs will be added in Phase 1+
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

    // Phase 0: just clear to black
    gl.clear(gl.COLOR_BUFFER_BIT);
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
