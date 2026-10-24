// Geometry for the 3D map: terrain grid (with optional heights), unit shapes for objects, and a
// batch builder for flat ground overlays (trail, rings, shadows, markers).
//
// Heights: no row in the data contract carries elevation today (docs/00 §4: "z up (unused)"), so
// MapView passes no heightfield and the terrain is flat. Everything that sits on the ground asks
// heightAt(), so a real elevation source (e.g. a per-station height grid) plugs in here without
// touching the rest. Never fill this with made-up hills: the map must stay true to the data.

export interface Heightfield {
  /** World position of sample (0, 0) (metres). */
  minX: number;
  minY: number;
  /** Sample spacing (metres). */
  res: number;
  nx: number;
  ny: number;
  /** Heights (metres), z[iy * nx + ix] at (minX + ix·res, minY + iy·res). */
  z: Float32Array;
}

/** Bilinear height at (x, y); 0 outside the field or with no field. */
export function heightAt(hf: Heightfield | undefined, x: number, y: number): number {
  if (!hf) return 0;
  const fx = (x - hf.minX) / hf.res;
  const fy = (y - hf.minY) / hf.res;
  if (fx < 0 || fy < 0 || fx > hf.nx - 1 || fy > hf.ny - 1) return 0;
  const ix = Math.min(hf.nx - 2, Math.floor(fx));
  const iy = Math.min(hf.ny - 2, Math.floor(fy));
  const tx = fx - ix;
  const ty = fy - iy;
  const z = (i: number, j: number) => hf.z[j * hf.nx + i];
  const a = z(ix, iy) * (1 - tx) + z(ix + 1, iy) * tx;
  const b = z(ix, iy + 1) * (1 - tx) + z(ix + 1, iy + 1) * tx;
  return a * (1 - ty) + b * ty;
}

export interface Mesh {
  pos: Float32Array;
  nrm: Float32Array;
  col: Float32Array;
  idx: Uint32Array;
}

export interface Rect {
  minX: number;
  maxX: number;
  minY: number;
  maxY: number;
}

/** n×n grid over `r` with heights from `hf` and normals from central differences. */
export function terrainMesh(r: Rect, n: number, hf?: Heightfield): Mesh {
  const verts = (n + 1) * (n + 1);
  const pos = new Float32Array(verts * 3);
  const nrm = new Float32Array(verts * 3);
  const col = new Float32Array(verts * 4).fill(1);
  const dx = (r.maxX - r.minX) / n;
  const dy = (r.maxY - r.minY) / n;
  const e = Math.max(1e-3, Math.min(dx, dy));
  for (let j = 0; j <= n; j++)
    for (let i = 0; i <= n; i++) {
      const k = j * (n + 1) + i;
      const x = r.minX + i * dx;
      const y = r.minY + j * dy;
      pos.set([x, y, heightAt(hf, x, y)], k * 3);
      const gx = (heightAt(hf, x + e, y) - heightAt(hf, x - e, y)) / (2 * e);
      const gy = (heightAt(hf, x, y + e) - heightAt(hf, x, y - e)) / (2 * e);
      const l = Math.hypot(gx, gy, 1);
      nrm.set([-gx / l, -gy / l, 1 / l], k * 3);
    }
  const idx = new Uint32Array(n * n * 6);
  let o = 0;
  for (let j = 0; j < n; j++)
    for (let i = 0; i < n; i++) {
      const a = j * (n + 1) + i;
      const b = a + 1;
      const c = a + n + 1;
      const d = c + 1;
      idx.set([a, b, d, a, d, c], o);
      o += 6;
    }
  return { pos, nrm, col, idx };
}

// --- unit shapes (lit) ---------------------------------------------------------------------------

class Builder {
  pos: number[] = [];
  nrm: number[] = [];
  col: number[] = [];
  idx: number[] = [];
  vert(p: [number, number, number], n: [number, number, number], c: readonly number[] = [1, 1, 1, 1]): number {
    this.pos.push(...p);
    this.nrm.push(...n);
    this.col.push(c[0], c[1], c[2], c[3] ?? 1);
    return this.pos.length / 3 - 1;
  }
  /** Flat-shaded polygon (convex, CCW seen from outside); normal by Newell's method. */
  face(pts: [number, number, number][], c?: readonly number[]): void {
    const n: [number, number, number] = [0, 0, 0];
    for (let i = 0; i < pts.length; i++) {
      const a = pts[i];
      const b = pts[(i + 1) % pts.length];
      n[0] += (a[1] - b[1]) * (a[2] + b[2]);
      n[1] += (a[2] - b[2]) * (a[0] + b[0]);
      n[2] += (a[0] - b[0]) * (a[1] + b[1]);
    }
    const l = Math.hypot(...n);
    if (l < 1e-12) return; // degenerate
    const nn: [number, number, number] = [n[0] / l, n[1] / l, n[2] / l];
    const base = pts.map((p) => this.vert(p, nn, c));
    for (let i = 1; i + 1 < base.length; i++) this.idx.push(base[0], base[i], base[i + 1]);
  }
  mesh(): Mesh {
    return {
      pos: new Float32Array(this.pos),
      nrm: new Float32Array(this.nrm),
      col: new Float32Array(this.col),
      idx: new Uint32Array(this.idx),
    };
  }
}

/** Unit sphere (radius 1) centred at the origin. */
export function sphere(seg = 24, rings = 16): Mesh {
  const b = new Builder();
  for (let j = 0; j <= rings; j++) {
    const v = (j / rings) * Math.PI;
    for (let i = 0; i <= seg; i++) {
      const u = (i / seg) * 2 * Math.PI;
      const p: [number, number, number] = [Math.sin(v) * Math.cos(u), Math.sin(v) * Math.sin(u), Math.cos(v)];
      b.vert(p, p);
    }
  }
  for (let j = 0; j < rings; j++)
    for (let i = 0; i < seg; i++) {
      const a = j * (seg + 1) + i;
      const c = a + seg + 1;
      b.idx.push(a, c, a + 1, a + 1, c, c + 1);
    }
  return b.mesh();
}

/** Unit cylinder: radius 1, from z = 0 to z = 1, with a top cap. */
export function cylinder(seg = 16): Mesh {
  const b = new Builder();
  for (let i = 0; i <= seg; i++) {
    const u = (i / seg) * 2 * Math.PI;
    const c = Math.cos(u);
    const s = Math.sin(u);
    b.vert([c, s, 0], [c, s, 0]);
    b.vert([c, s, 1], [c, s, 0]);
  }
  for (let i = 0; i < seg; i++) {
    const a = i * 2;
    b.idx.push(a, a + 2, a + 1, a + 1, a + 2, a + 3);
  }
  const centre = b.vert([0, 0, 1], [0, 0, 1]);
  const ring: number[] = [];
  for (let i = 0; i <= seg; i++) {
    const u = (i / seg) * 2 * Math.PI;
    ring.push(b.vert([Math.cos(u), Math.sin(u), 1], [0, 0, 1]));
  }
  for (let i = 0; i < seg; i++) b.idx.push(centre, ring[i], ring[i + 1]);
  return b.mesh();
}

/** Unit box: x, y in [-0.5, 0.5], z in [0, 1]. */
export function box(): Mesh {
  const b = new Builder();
  const P = (x: number, y: number, z: number): [number, number, number] => [x - 0.5, y - 0.5, z];
  b.face([P(0, 0, 1), P(1, 0, 1), P(1, 1, 1), P(0, 1, 1)]);
  b.face([P(0, 0, 0), P(1, 0, 0), P(1, 0, 1), P(0, 0, 1)]);
  b.face([P(1, 0, 0), P(1, 1, 0), P(1, 1, 1), P(1, 0, 1)]);
  b.face([P(1, 1, 0), P(0, 1, 0), P(0, 1, 1), P(1, 1, 1)]);
  b.face([P(0, 1, 0), P(0, 0, 0), P(0, 0, 1), P(0, 1, 1)]);
  return b.mesh();
}

/**
 * Abstract rover marker: a low, rounded-nose wedge pointing along +x, length 1 (x in [-0.5, 0.5]),
 * width 0.72, height 0.22, with a lighter top plate. Deliberately not a model of the real rover
 * (its wheel count and dimensions aren't specified); it shows position and heading only.
 */
export function roverMarker(): Mesh {
  const b = new Builder();
  const body = [0.88, 0.48, 0.25, 1];
  const top = [1.0, 0.72, 0.5, 1];
  const h = 0.22;
  // Outline (CCW from above): flat back, sides tapering to a rounded nose.
  const outline: [number, number][] = [[-0.5, -0.36]];
  for (let i = 0; i <= 10; i++) {
    const a = -Math.PI / 2 + (i / 10) * Math.PI;
    outline.push([0.12 + 0.38 * Math.cos(a), 0.36 * Math.sin(a)]);
  }
  outline.push([-0.5, 0.36]);
  const inset = 0.78;
  // Sides.
  for (let i = 0; i < outline.length; i++) {
    const [x0, y0] = outline[i];
    const [x1, y1] = outline[(i + 1) % outline.length];
    b.face([[x0, y0, 0], [x1, y1, 0], [x1, y1, h], [x0, y0, h]], body);
  }
  // Top: bevelled ring from the outline to an inset plate, then the plate.
  const cx = -0.05;
  const inner = outline.map(([x, y]) => [cx + (x - cx) * inset, y * inset] as [number, number]);
  for (let i = 0; i < outline.length; i++) {
    const j = (i + 1) % outline.length;
    b.face([[outline[i][0], outline[i][1], h], [outline[j][0], outline[j][1], h], [inner[j][0], inner[j][1], h + 0.06], [inner[i][0], inner[i][1], h + 0.06]], body);
  }
  b.face(inner.map(([x, y]) => [x, y, h + 0.06] as [number, number, number]), top);
  return b.mesh();
}

// --- flat overlays (unlit, per-vertex colour) ----------------------------------------------------

export type RGBA = readonly [number, number, number, number];

export class OverlayBatch {
  pos: number[] = [];
  col: number[] = [];
  idx: number[] = [];
  constructor(
    private hf: Heightfield | undefined,
    private lift: number
  ) {}
  private v(x: number, y: number, c: RGBA): number {
    this.pos.push(x, y, heightAt(this.hf, x, y) + this.lift);
    this.col.push(c[0], c[1], c[2], c[3]);
    return this.pos.length / 3 - 1;
  }
  /** Straight band from a to b, `w` metres wide, ends extended by w/2 (square caps). */
  segment(ax: number, ay: number, bx: number, by: number, w: number, c: RGBA): void {
    const l = Math.hypot(bx - ax, by - ay);
    if (l < 1e-9) return;
    const ux = (bx - ax) / l;
    const uy = (by - ay) / l;
    const nx = (-uy * w) / 2;
    const ny = (ux * w) / 2;
    const ex = (ux * w) / 2;
    const ey = (uy * w) / 2;
    const i0 = this.v(ax - ex + nx, ay - ey + ny, c);
    const i1 = this.v(ax - ex - nx, ay - ey - ny, c);
    const i2 = this.v(bx + ex - nx, by + ey - ny, c);
    const i3 = this.v(bx + ex + nx, by + ey + ny, c);
    this.idx.push(i0, i1, i2, i0, i2, i3);
  }
  /** Ring band between radii r0 < r1; `dash` = [on, off] metres along the ring for a dashed ring. */
  ring(cx: number, cy: number, r0: number, r1: number, c: RGBA, dash?: [number, number]): void {
    const rm = (r0 + r1) / 2;
    const seg = Math.max(24, Math.min(256, Math.ceil(rm * 400)));
    const circ = 2 * Math.PI * rm;
    for (let i = 0; i < seg; i++) {
      const a0 = (i / seg) * 2 * Math.PI;
      const a1 = ((i + 1) / seg) * 2 * Math.PI;
      if (dash) {
        const s = (((a0 + a1) / 2) / (2 * Math.PI)) * circ;
        if (s % (dash[0] + dash[1]) > dash[0]) continue;
      }
      const p = [
        this.v(cx + r0 * Math.cos(a0), cy + r0 * Math.sin(a0), c),
        this.v(cx + r1 * Math.cos(a0), cy + r1 * Math.sin(a0), c),
        this.v(cx + r1 * Math.cos(a1), cy + r1 * Math.sin(a1), c),
        this.v(cx + r0 * Math.cos(a1), cy + r0 * Math.sin(a1), c),
      ];
      this.idx.push(p[0], p[1], p[2], p[0], p[2], p[3]);
    }
  }
  /** Filled disc; `edge` colour at the rim (e.g. transparent for a soft shadow). */
  disc(cx: number, cy: number, r: number, c: RGBA, edge: RGBA = c): void {
    const seg = Math.max(20, Math.min(96, Math.ceil(r * 600)));
    const centre = this.v(cx, cy, c);
    const first = this.pos.length / 3;
    for (let i = 0; i <= seg; i++) {
      const a = (i / seg) * 2 * Math.PI;
      this.v(cx + r * Math.cos(a), cy + r * Math.sin(a), edge);
    }
    for (let i = 0; i < seg; i++) this.idx.push(centre, first + i, first + i + 1);
  }
  get empty(): boolean {
    return this.idx.length === 0;
  }
  mesh(): Mesh {
    const n = this.pos.length / 3;
    const nrm = new Float32Array(n * 3);
    for (let i = 0; i < n; i++) nrm[i * 3 + 2] = 1;
    return { pos: new Float32Array(this.pos), nrm, col: new Float32Array(this.col), idx: new Uint32Array(this.idx) };
  }
}
