// WebGL2 renderer for the 3D map (no libraries). Draw order per frame:
//   1. terrain: grid mesh draped with the ground photos (true colours on flat ground), a faint
//      metric grid, and a soft fade into the background beyond the explored area;
//   2. ground overlays (trail, rings, shadows, markers), blended, no depth writes;
//   3. lit 3D objects (rover, pins, stations), with a back-face outline for the selected pin.
import { type Heightfield, type Mesh, type Rect, box, cylinder, roverMarker, sphere, terrainMesh } from './geometry';
import type { GroundImage } from './ground';
import { norm } from './mat';
import type { Scene, ShapeName } from './scene';

const TERRAIN_VS = `#version 300 es
in vec3 aPos; in vec3 aNrm;
uniform mat4 uVP;
out vec3 vPos; out vec3 vNrm;
void main() { vPos = aPos; vNrm = aNrm; gl_Position = uVP * vec4(aPos, 1.0); }`;

const TERRAIN_FS = `#version 300 es
precision highp float;
in vec3 vPos; in vec3 vNrm;
uniform sampler2D uTex;
uniform float uHasTex;
uniform vec4 uTexRect;   // maxX, maxY, spanX (along v), spanY (along u)
uniform vec3 uBase;      // ground colour where there are no photos yet
uniform vec3 uBg;        // background colour the terrain fades into
uniform vec3 uLight;
uniform float uGrid;     // grid spacing (m)
uniform vec4 uFade;      // centre x, centre y, fade start, fade end (m)
out vec4 outColor;
float gridLine(vec2 p, float step) {
  vec2 q = p / step;
  vec2 g = abs(fract(q - 0.5) - 0.5) / max(fwidth(q), vec2(1e-6));
  return 1.0 - clamp(min(g.x, g.y), 0.0, 1.0);
}
void main() {
  vec2 uv = vec2((uTexRect.y - vPos.y) / uTexRect.w, (uTexRect.x - vPos.x) / uTexRect.z);
  vec4 t = texture(uTex, uv) * uHasTex;
  float inside = step(0.0, uv.x) * step(0.0, uv.y) * step(uv.x, 1.0) * step(uv.y, 1.0);
  t *= inside;
  vec3 c = mix(uBase, t.rgb, t.a);
  // Shade by slope only: flat ground keeps the photos' true colours.
  vec3 n = normalize(vNrm);
  c *= (0.45 + 0.55 * max(dot(n, uLight), 0.0)) / (0.45 + 0.55 * uLight.z);
  float g = gridLine(vPos.xy, uGrid);
  float gMajor = gridLine(vPos.xy, uGrid * 5.0);
  c = mix(c, vec3(1.0), max(g * 0.07, gMajor * 0.12) * (1.0 - 0.55 * t.a));
  float d = length(vPos.xy - uFade.xy);
  c = mix(c, uBg, smoothstep(uFade.z, uFade.w, d));
  outColor = vec4(c, 1.0);
}`;

const LIT_VS = `#version 300 es
in vec3 aPos; in vec3 aNrm; in vec4 aCol;
uniform mat4 uVP; uniform mat4 uModel; uniform mat3 uNrmMat;
uniform float uInflate;
out vec3 vNrm; out vec4 vCol; out vec3 vWorld;
void main() {
  vec4 w = uModel * vec4(aPos + aNrm * uInflate, 1.0);
  vWorld = w.xyz; vNrm = uNrmMat * aNrm; vCol = aCol;
  gl_Position = uVP * w;
}`;

const LIT_FS = `#version 300 es
precision highp float;
in vec3 vNrm; in vec4 vCol; in vec3 vWorld;
uniform vec4 uColor; uniform vec3 uLight; uniform vec3 uEye; uniform float uUnlit;
out vec4 outColor;
void main() {
  vec4 base = vCol * uColor;
  if (uUnlit > 0.5) { outColor = base; return; }
  vec3 n = normalize(vNrm);
  vec3 vdir = normalize(uEye - vWorld);
  float diff = max(dot(n, uLight), 0.0);
  float hemi = 0.5 + 0.5 * n.z;
  vec3 h = normalize(uLight + vdir);
  float spec = pow(max(dot(n, h), 0.0), 40.0) * 0.35;
  float rim = pow(1.0 - max(dot(n, vdir), 0.0), 3.0) * 0.18;
  vec3 c = base.rgb * (0.30 + 0.25 * hemi + 0.62 * diff) + vec3(spec + rim);
  outColor = vec4(c, base.a);
}`;

const FLAT_VS = `#version 300 es
in vec3 aPos; in vec4 aCol;
uniform mat4 uVP;
out vec4 vCol;
void main() { vCol = aCol; gl_Position = uVP * vec4(aPos, 1.0); }`;

const FLAT_FS = `#version 300 es
precision highp float;
in vec4 vCol;
out vec4 outColor;
void main() { outColor = vCol; }`;

interface GpuMesh {
  vao: WebGLVertexArrayObject;
  buffers: WebGLBuffer[];
  count: number;
}

type Program = { prog: WebGLProgram; u: Record<string, WebGLUniformLocation | null> };

export const BG: [number, number, number] = [0.059, 0.051, 0.047]; // #0f0d0c, the map's background
const BASE: [number, number, number] = [0.13, 0.115, 0.105];
const LIGHT = norm([-0.45, 0.35, 0.82]);

export class MapRenderer {
  readonly gl: WebGL2RenderingContext;
  private terrainProg: Program;
  private litProg: Program;
  private flatProg: Program;
  private shapes: Record<ShapeName, GpuMesh>;
  private terrain?: GpuMesh;
  private overlay?: GpuMesh;
  private tex: WebGLTexture;
  private hasTex = false;
  private texRect: [number, number, number, number] = [0, 0, 1, 1];
  private fade: [number, number, number, number] = [0, 0, 50, 60];

  constructor(canvas: HTMLCanvasElement) {
    const gl = canvas.getContext('webgl2', { antialias: true, alpha: false, premultipliedAlpha: false, powerPreference: 'high-performance' });
    if (!gl) throw new Error('WebGL2 unavailable');
    this.gl = gl;
    this.terrainProg = program(gl, TERRAIN_VS, TERRAIN_FS, ['uVP', 'uTex', 'uHasTex', 'uTexRect', 'uBase', 'uBg', 'uLight', 'uGrid', 'uFade']);
    this.litProg = program(gl, LIT_VS, LIT_FS, ['uVP', 'uModel', 'uNrmMat', 'uInflate', 'uColor', 'uLight', 'uEye', 'uUnlit']);
    this.flatProg = program(gl, FLAT_VS, FLAT_FS, ['uVP']);
    this.shapes = {
      sphere: this.upload(sphere(), gl.STATIC_DRAW),
      cylinder: this.upload(cylinder(), gl.STATIC_DRAW),
      box: this.upload(box(), gl.STATIC_DRAW),
      rover: this.upload(roverMarker(), gl.STATIC_DRAW),
    };
    this.tex = gl.createTexture()!;
    gl.bindTexture(gl.TEXTURE_2D, this.tex);
    gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, 1, 1, 0, gl.RGBA, gl.UNSIGNED_BYTE, new Uint8Array([0, 0, 0, 0]));
  }

  /** Terrain mesh over `rect` (heights from `hf`), and the area beyond which it fades out. */
  setTerrain(rect: Rect, content: Rect, hf?: Heightfield): void {
    const gl = this.gl;
    if (this.terrain) this.free(this.terrain);
    this.terrain = this.upload(terrainMesh(rect, hf ? 160 : 8, hf), gl.STATIC_DRAW);
    const cx = (content.minX + content.maxX) / 2;
    const cy = (content.minY + content.maxY) / 2;
    const r = Math.hypot(content.maxX - content.minX, content.maxY - content.minY) / 2;
    this.fade = [cx, cy, r + 0.6, r + 0.6 + Math.max(1.5, r * 0.8)];
  }

  /** Upload the composited ground photos (or clear them). */
  setGround(img: GroundImage | undefined): void {
    const gl = this.gl;
    this.hasTex = !!img;
    if (!img) return;
    const { canvas, layout } = img;
    gl.bindTexture(gl.TEXTURE_2D, this.tex);
    gl.pixelStorei(gl.UNPACK_FLIP_Y_WEBGL, false);
    gl.pixelStorei(gl.UNPACK_PREMULTIPLY_ALPHA_WEBGL, false);
    gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, gl.RGBA, gl.UNSIGNED_BYTE, canvas);
    gl.generateMipmap(gl.TEXTURE_2D);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR_MIPMAP_LINEAR);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
    const aniso = gl.getExtension('EXT_texture_filter_anisotropic');
    if (aniso) gl.texParameterf(gl.TEXTURE_2D, aniso.TEXTURE_MAX_ANISOTROPY_EXT, Math.min(8, gl.getParameter(aniso.MAX_TEXTURE_MAX_ANISOTROPY_EXT)));
    const r = layout.rect;
    this.texRect = [r.maxX, r.maxY, layout.hPx * layout.res, layout.wPx * layout.res];
  }

  get maxTextureSize(): number {
    return this.gl.getParameter(this.gl.MAX_TEXTURE_SIZE) as number;
  }

  render(scene: Scene, widthPx: number, heightPx: number): void {
    const gl = this.gl;
    const vp = scene.view.vp;
    gl.viewport(0, 0, widthPx, heightPx);
    gl.clearColor(BG[0], BG[1], BG[2], 1);
    gl.clear(gl.COLOR_BUFFER_BIT | gl.DEPTH_BUFFER_BIT);
    gl.enable(gl.DEPTH_TEST);
    gl.depthFunc(gl.LEQUAL);
    gl.disable(gl.CULL_FACE);

    // 1. Terrain.
    if (this.terrain) {
      const p = this.terrainProg;
      gl.useProgram(p.prog);
      gl.disable(gl.BLEND);
      gl.depthMask(true);
      gl.uniformMatrix4fv(p.u.uVP, false, vp);
      gl.activeTexture(gl.TEXTURE0);
      gl.bindTexture(gl.TEXTURE_2D, this.tex);
      gl.uniform1i(p.u.uTex, 0);
      gl.uniform1f(p.u.uHasTex, this.hasTex ? 1 : 0);
      gl.uniform4fv(p.u.uTexRect, this.texRect);
      gl.uniform3fv(p.u.uBase, BASE);
      gl.uniform3fv(p.u.uBg, BG);
      gl.uniform3fv(p.u.uLight, LIGHT);
      gl.uniform1f(p.u.uGrid, scene.gridStepM);
      gl.uniform4fv(p.u.uFade, this.fade);
      this.draw(this.terrain);
    }

    // 2. Ground overlays.
    gl.enable(gl.BLEND);
    gl.blendFuncSeparate(gl.SRC_ALPHA, gl.ONE_MINUS_SRC_ALPHA, gl.ONE, gl.ONE_MINUS_SRC_ALPHA);
    if (scene.overlay) {
      if (this.overlay) this.free(this.overlay);
      this.overlay = this.upload(scene.overlay, gl.STREAM_DRAW);
      const p = this.flatProg;
      gl.useProgram(p.prog);
      gl.uniformMatrix4fv(p.u.uVP, false, vp);
      gl.depthMask(false);
      gl.enable(gl.POLYGON_OFFSET_FILL);
      gl.polygonOffset(-2, -4);
      this.draw(this.overlay);
      gl.disable(gl.POLYGON_OFFSET_FILL);
      gl.depthMask(true);
    }

    // 3. Lit objects (outline pass first so the object draws over it).
    const p = this.litProg;
    gl.useProgram(p.prog);
    gl.uniformMatrix4fv(p.u.uVP, false, vp);
    gl.uniform3fv(p.u.uLight, LIGHT);
    gl.uniform3fv(p.u.uEye, scene.view.eye);
    for (const o of scene.objects) {
      gl.uniformMatrix4fv(p.u.uModel, false, o.model);
      gl.uniformMatrix3fv(p.u.uNrmMat, false, o.nmat);
      if (o.outline) {
        gl.enable(gl.CULL_FACE);
        gl.cullFace(gl.FRONT);
        gl.uniform1f(p.u.uInflate, 0.28);
        gl.uniform1f(p.u.uUnlit, 1);
        gl.uniform4fv(p.u.uColor, [1, 1, 1, 1]);
        this.draw(this.shapes[o.shape]);
        gl.disable(gl.CULL_FACE);
      }
      gl.uniform1f(p.u.uInflate, 0);
      gl.uniform1f(p.u.uUnlit, 0);
      gl.uniform4fv(p.u.uColor, o.color);
      this.draw(this.shapes[o.shape]);
    }
  }

  dispose(): void {
    const gl = this.gl;
    for (const m of Object.values(this.shapes)) this.free(m);
    if (this.terrain) this.free(this.terrain);
    if (this.overlay) this.free(this.overlay);
    gl.deleteTexture(this.tex);
    for (const p of [this.terrainProg, this.litProg, this.flatProg]) gl.deleteProgram(p.prog);
  }

  private draw(m: GpuMesh): void {
    const gl = this.gl;
    gl.bindVertexArray(m.vao);
    gl.drawElements(gl.TRIANGLES, m.count, gl.UNSIGNED_INT, 0);
    gl.bindVertexArray(null);
  }

  private upload(mesh: Mesh, usage: number): GpuMesh {
    const gl = this.gl;
    const vao = gl.createVertexArray()!;
    gl.bindVertexArray(vao);
    const buffers: WebGLBuffer[] = [];
    const attr = (loc: number, data: Float32Array, size: number) => {
      const b = gl.createBuffer()!;
      gl.bindBuffer(gl.ARRAY_BUFFER, b);
      gl.bufferData(gl.ARRAY_BUFFER, data, usage);
      gl.enableVertexAttribArray(loc);
      gl.vertexAttribPointer(loc, size, gl.FLOAT, false, 0, 0);
      buffers.push(b);
    };
    // Every program binds aPos = 0, aNrm = 1, aCol = 2 (see program()).
    attr(0, mesh.pos, 3);
    attr(1, mesh.nrm, 3);
    attr(2, mesh.col, 4);
    const ib = gl.createBuffer()!;
    gl.bindBuffer(gl.ELEMENT_ARRAY_BUFFER, ib);
    gl.bufferData(gl.ELEMENT_ARRAY_BUFFER, mesh.idx, usage);
    buffers.push(ib);
    gl.bindVertexArray(null);
    return { vao, buffers, count: mesh.idx.length };
  }

  private free(m: GpuMesh): void {
    this.gl.deleteVertexArray(m.vao);
    for (const b of m.buffers) this.gl.deleteBuffer(b);
  }
}

function program(gl: WebGL2RenderingContext, vs: string, fs: string, uniforms: string[]): Program {
  const compile = (type: number, src: string) => {
    const s = gl.createShader(type)!;
    gl.shaderSource(s, src);
    gl.compileShader(s);
    if (!gl.getShaderParameter(s, gl.COMPILE_STATUS) && !gl.isContextLost()) throw new Error(`shader: ${gl.getShaderInfoLog(s)}`);
    return s;
  };
  const prog = gl.createProgram()!;
  const v = compile(gl.VERTEX_SHADER, vs);
  const f = compile(gl.FRAGMENT_SHADER, fs);
  gl.attachShader(prog, v);
  gl.attachShader(prog, f);
  gl.bindAttribLocation(prog, 0, 'aPos');
  gl.bindAttribLocation(prog, 1, 'aNrm');
  gl.bindAttribLocation(prog, 2, 'aCol');
  gl.linkProgram(prog);
  if (!gl.getProgramParameter(prog, gl.LINK_STATUS) && !gl.isContextLost()) throw new Error(`link: ${gl.getProgramInfoLog(prog)}`);
  gl.deleteShader(v);
  gl.deleteShader(f);
  const u: Record<string, WebGLUniformLocation | null> = {};
  for (const name of uniforms) u[name] = gl.getUniformLocation(prog, name);
  return { prog, u };
}
