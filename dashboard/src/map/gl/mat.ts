// Minimal 4×4 / 3-vector math for the 3D map (column-major, like WebGL).
export type Vec3 = [number, number, number];
export type Mat4 = Float32Array;

export const sub = (a: Vec3, b: Vec3): Vec3 => [a[0] - b[0], a[1] - b[1], a[2] - b[2]];
export const dot = (a: Vec3, b: Vec3) => a[0] * b[0] + a[1] * b[1] + a[2] * b[2];
export const cross = (a: Vec3, b: Vec3): Vec3 => [a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0]];
export const len = (a: Vec3) => Math.hypot(a[0], a[1], a[2]);
export function norm(a: Vec3): Vec3 {
  const l = len(a) || 1;
  return [a[0] / l, a[1] / l, a[2] / l];
}

export function perspective(fovy: number, aspect: number, near: number, far: number): Mat4 {
  const f = 1 / Math.tan(fovy / 2);
  const nf = 1 / (near - far);
  // prettier-ignore
  return new Float32Array([
    f / aspect, 0, 0, 0,
    0, f, 0, 0,
    0, 0, (far + near) * nf, -1,
    0, 0, 2 * far * near * nf, 0,
  ]);
}

export function lookAt(eye: Vec3, center: Vec3, up: Vec3): Mat4 {
  const z = norm(sub(eye, center));
  const x = norm(cross(up, z));
  const y = cross(z, x);
  // prettier-ignore
  return new Float32Array([
    x[0], y[0], z[0], 0,
    x[1], y[1], z[1], 0,
    x[2], y[2], z[2], 0,
    -dot(x, eye), -dot(y, eye), -dot(z, eye), 1,
  ]);
}

export function multiply(a: Mat4, b: Mat4): Mat4 {
  const out = new Float32Array(16);
  for (let c = 0; c < 4; c++)
    for (let r = 0; r < 4; r++) {
      let s = 0;
      for (let k = 0; k < 4; k++) s += a[k * 4 + r] * b[c * 4 + k];
      out[c * 4 + r] = s;
    }
  return out;
}

/** Inverse by Gauss-Jordan elimination (double precision), or undefined if singular. */
export function invert(m: Mat4): Mat4 | undefined {
  // Row-major working copy of the column-major input, augmented with the identity.
  const a: number[][] = [];
  for (let r = 0; r < 4; r++) {
    a.push([m[r], m[4 + r], m[8 + r], m[12 + r], r === 0 ? 1 : 0, r === 1 ? 1 : 0, r === 2 ? 1 : 0, r === 3 ? 1 : 0]);
  }
  for (let c = 0; c < 4; c++) {
    let piv = c;
    for (let r = c + 1; r < 4; r++) if (Math.abs(a[r][c]) > Math.abs(a[piv][c])) piv = r;
    if (Math.abs(a[piv][c]) < 1e-15) return undefined;
    [a[c], a[piv]] = [a[piv], a[c]];
    const p = a[c][c];
    for (let k = 0; k < 8; k++) a[c][k] /= p;
    for (let r = 0; r < 4; r++) {
      if (r === c) continue;
      const f = a[r][c];
      if (f) for (let k = 0; k < 8; k++) a[r][k] -= f * a[c][k];
    }
  }
  const out = new Float32Array(16);
  for (let r = 0; r < 4; r++) for (let c = 0; c < 4; c++) out[c * 4 + r] = a[r][4 + c];
  return out;
}

/** m · (x, y, z, 1) → [x, y, z, w] (not divided by w). */
export function transform4(m: Mat4, p: Vec3): [number, number, number, number] {
  const [x, y, z] = p;
  return [
    m[0] * x + m[4] * y + m[8] * z + m[12],
    m[1] * x + m[5] * y + m[9] * z + m[13],
    m[2] * x + m[6] * y + m[10] * z + m[14],
    m[3] * x + m[7] * y + m[11] * z + m[15],
  ];
}

/** Model matrix: scale (sx, sy, sz), then rotate by `rotZ` radians about +z, then translate. */
export function model(tx: number, ty: number, tz: number, rotZ: number, sx: number, sy: number, sz: number): Mat4 {
  const c = Math.cos(rotZ);
  const s = Math.sin(rotZ);
  // prettier-ignore
  return new Float32Array([
    c * sx, s * sx, 0, 0,
    -s * sy, c * sy, 0, 0,
    0, 0, sz, 0,
    tx, ty, tz, 1,
  ]);
}

/** Normal matrix (inverse-transpose of the model's 3×3) for `model(…, rotZ, sx, sy, sz)`. */
export function normalMatrix(rotZ: number, sx: number, sy: number, sz: number): Float32Array {
  const c = Math.cos(rotZ);
  const s = Math.sin(rotZ);
  // prettier-ignore
  return new Float32Array([
    c / sx, s / sx, 0,
    -s / sy, c / sy, 0,
    0, 0, 1 / sz,
  ]);
}
