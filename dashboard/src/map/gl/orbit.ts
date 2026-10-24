// Orbit camera for the 3D map. World = mission frame metres, z up (docs/00 §4).
// The default view looks from behind the start pose toward +x, so +x is "up/away" on screen and
// +y is to the left: the same orientation as the 2D map (docs/06 §6, D10).
import type { Bounds } from '../camera';
import { type Mat4, type Vec3, invert, lookAt, multiply, perspective, transform4 } from './mat';

export interface Orbit {
  /** Point the camera looks at (metres). */
  tx: number;
  ty: number;
  tz: number;
  /** Camera distance from the target (metres). */
  dist: number;
  /** Direction from the target to the camera, CCW from +x (radians). */
  az: number;
  /** Camera elevation above the ground plane (radians). */
  el: number;
}

export const FOV = (38 * Math.PI) / 180;
export const DEFAULT_AZ = Math.PI; // camera behind the start pose, looking along +x
export const DEFAULT_EL = (52 * Math.PI) / 180;
export const MIN_EL = (12 * Math.PI) / 180;
export const TOP_EL = (89.5 * Math.PI) / 180; // straight down, still +x up on screen
export const MIN_DIST = 0.15;
export const MAX_DIST = 80;

const clamp = (v: number, lo: number, hi: number) => Math.min(hi, Math.max(lo, v));

export function eye(o: Orbit): Vec3 {
  const ce = Math.cos(o.el);
  return [o.tx + o.dist * ce * Math.cos(o.az), o.ty + o.dist * ce * Math.sin(o.az), o.tz + o.dist * Math.sin(o.el)];
}

export interface View {
  vp: Mat4;
  inv: Mat4;
  eye: Vec3;
  w: number;
  h: number;
}

export function view(o: Orbit, w: number, h: number): View {
  const e = eye(o);
  const near = Math.max(0.005, o.dist * 0.02);
  const far = o.dist * 40 + 50;
  const proj = perspective(FOV, Math.max(1e-6, w / Math.max(1, h)), near, far);
  const vp = multiply(proj, lookAt(e, [o.tx, o.ty, o.tz], [0, 0, 1]));
  return { vp, inv: invert(vp) ?? vp, eye: e, w, h };
}

/** World point → screen CSS px; `front` is false when the point is behind the camera. */
export function project(v: View, p: Vec3): { x: number; y: number; front: boolean } {
  const [cx, cy, , cw] = transform4(v.vp, p);
  if (cw <= 1e-9) return { x: NaN, y: NaN, front: false };
  return { x: ((cx / cw + 1) / 2) * v.w, y: ((1 - cy / cw) / 2) * v.h, front: true };
}

/** Where the ray through screen point (sx, sy) meets the plane z = `z`, if it does (in front). */
export function groundAt(v: View, sx: number, sy: number, z = 0): [number, number] | undefined {
  const nx = (sx / v.w) * 2 - 1;
  const ny = 1 - (sy / v.h) * 2;
  const a = transform4(v.inv, [nx, ny, -1]);
  const b = transform4(v.inv, [nx, ny, 1]);
  const p0: Vec3 = [a[0] / a[3], a[1] / a[3], a[2] / a[3]];
  const p1: Vec3 = [b[0] / b[3], b[1] / b[3], b[2] / b[3]];
  const dz = p1[2] - p0[2];
  if (Math.abs(dz) < 1e-12) return undefined;
  const t = (z - p0[2]) / dz;
  if (t < 0) return undefined;
  return [p0[0] + (p1[0] - p0[0]) * t, p0[1] + (p1[1] - p0[1]) * t];
}

/** World metres per CSS pixel at distance `d` from the camera. */
export const metresPerPx = (d: number, h: number) => (2 * d * Math.tan(FOV / 2)) / Math.max(1, h);

/** Approximate pixels per metre at the target (for the grid spacing). */
export const pxPerMetre = (o: Orbit, h: number) => 1 / metresPerPx(o.dist, h);

/** Drag the ground point under (x0, y0) to (x1, y1). */
export function pan(o: Orbit, w: number, h: number, x0: number, y0: number, x1: number, y1: number): Orbit {
  const v = view(o, w, h);
  const a = groundAt(v, x0, y0, o.tz);
  const b = groundAt(v, x1, y1, o.tz);
  if (!a || !b) return o;
  let dx = a[0] - b[0];
  let dy = a[1] - b[1];
  const m = Math.hypot(dx, dy);
  const cap = o.dist * 0.5; // near the horizon one pixel can be metres; keep drags sane
  if (m > cap) {
    dx *= cap / m;
    dy *= cap / m;
  }
  return { ...o, tx: o.tx + dx, ty: o.ty + dy };
}

/** Zoom by `factor` (> 1 = closer) keeping the ground point under (sx, sy) fixed on screen. */
export function zoomAt(o: Orbit, w: number, h: number, sx: number, sy: number, factor: number): Orbit {
  const dist = clamp(o.dist / factor, MIN_DIST, MAX_DIST);
  const s = dist / o.dist;
  const p = groundAt(view(o, w, h), sx, sy, o.tz);
  if (!p || Math.hypot(p[0] - o.tx, p[1] - o.ty) > o.dist * 6) return { ...o, dist };
  // Scaling the whole camera rig about p keeps p at the same screen position.
  return { ...o, dist, tx: p[0] + (o.tx - p[0]) * s, ty: p[1] + (o.ty - p[1]) * s };
}

export function rotate(o: Orbit, dAz: number, dEl: number): Orbit {
  return { ...o, az: o.az + dAz, el: clamp(o.el + dEl, MIN_EL, TOP_EL) };
}

/** Camera at (az, el) that fits `b` plus `marginM` on every side into a w×h canvas. */
export function fit(b: Bounds, w: number, h: number, az: number, el: number, marginM = 0.5, tz = 0): Orbit {
  const minX = b.minX - marginM;
  const maxX = b.maxX + marginM;
  const minY = b.minY - marginM;
  const maxY = b.maxY + marginM;
  const corners: Vec3[] = [
    [minX, minY, tz],
    [minX, maxY, tz],
    [maxX, minY, tz],
    [maxX, maxY, tz],
  ];
  const span = Math.max(maxX - minX, (maxY - minY) * (h / Math.max(1, w)));
  let o: Orbit = {
    tx: (minX + maxX) / 2,
    ty: (minY + maxY) / 2,
    tz,
    az,
    el,
    dist: clamp(span / (2 * Math.tan(FOV / 2)), MIN_DIST, MAX_DIST),
  };
  for (let i = 0; i < 12; i++) {
    const v = view(o, w, h);
    let worst = 0;
    for (const c of corners) {
      const p = project(v, c);
      if (!p.front) {
        worst = 4;
        break;
      }
      worst = Math.max(worst, Math.abs(p.x / w - 0.5) * 2, Math.abs(p.y / h - 0.5) * 2);
    }
    const next = clamp(o.dist * (worst / 0.92), MIN_DIST, MAX_DIST);
    if (Math.abs(next - o.dist) < 1e-4 * o.dist) break;
    o = { ...o, dist: next };
  }
  return o;
}

const angleDiff = (a: number, b: number) => Math.atan2(Math.sin(b - a), Math.cos(b - a));

/** Smooth camera transition: position/angles eased, distance in log space, azimuth the short way. */
export function lerpOrbit(a: Orbit, b: Orbit, t: number): Orbit {
  const k = t * t * (3 - 2 * t);
  return {
    tx: a.tx + (b.tx - a.tx) * k,
    ty: a.ty + (b.ty - a.ty) * k,
    tz: a.tz + (b.tz - a.tz) * k,
    dist: Math.exp(Math.log(a.dist) + (Math.log(b.dist) - Math.log(a.dist)) * k),
    az: a.az + angleDiff(a.az, b.az) * k,
    el: a.el + (b.el - a.el) * k,
  };
}
