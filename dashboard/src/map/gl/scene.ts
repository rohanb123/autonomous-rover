// Builds one frame of the 3D map from the rows (pure: no WebGL), so it can be unit-tested.
// Same rules as docs/06 §6: trail coloured by pose source, pins coloured by status with rank
// numbers and size ∝ score, dashed exclusion rings of radiusM, a marker per station, the rover
// at the latest pose. Markers keep a constant on-screen size (like the 2D map); rings, tiles and
// the trail sit at their true mission-frame positions.
import type { Candidate, LocalMapTile, Pose, RoverStatus, Visit } from '../../module_bindings/types';
import { gridStep } from '../layers/grid';
import { HIT_RADIUS_PX, isDim, pinRadius, statusColor } from '../layers/pins';
import { roverPose, sourceColor } from '../layers/trail';
import { type NextStop, nextStop } from '../mapdata';
import { type Heightfield, type Mesh, OverlayBatch, type RGBA, heightAt } from './geometry';
import { type Mat4, type Vec3, len, model, normalMatrix, sub } from './mat';
import { type Orbit, type View, metresPerPx, project, pxPerMetre, view } from './orbit';

export type ShapeName = 'sphere' | 'cylinder' | 'box' | 'rover';

export interface SceneObject {
  shape: ShapeName;
  model: Mat4;
  nmat: Float32Array;
  color: RGBA;
  /** Draw a white back-face outline around it (selected pin). */
  outline?: boolean;
}

export interface Label {
  key: string;
  kind: 'rank' | 'next';
  text: string;
  x: number;
  y: number;
}

export interface Hit {
  candId: string;
  x: number;
  y: number;
  r: number;
}

export interface Scene {
  view: View;
  overlay?: Mesh;
  objects: SceneObject[];
  labels: Label[];
  hits: Hit[];
  gridStepM: number;
  next?: NextStop;
  /** Something animates (pulses), so keep rendering frames. */
  animating: boolean;
}

export interface SceneRows {
  poses: readonly Pose[];
  candidates: readonly Candidate[];
  visits: readonly Visit[];
  stations: readonly LocalMapTile[];
  status?: RoverStatus;
  selectedCandId?: string;
  hoveredCandId?: string;
  heightfield?: Heightfield;
}

// On-screen sizes (CSS px).
export const ROVER_PX = 34;
export const STATION_PX = 8;
export const TRAIL_PX = 3;
export const RING_PX = 1.5;
export const STEM_PX = 20;

export function hexRgba(hex: string, a = 1): RGBA {
  const m = /^#?([0-9a-f]{2})([0-9a-f]{2})([0-9a-f]{2})$/i.exec(hex);
  if (!m) {
    const r = /rgba?\(([^)]+)\)/.exec(hex);
    if (r) {
      const [cr, cg, cb, ca] = r[1].split(',').map((s) => parseFloat(s));
      return [cr / 255, cg / 255, cb / 255, (ca ?? 1) * a];
    }
    return [0.6, 0.56, 0.52, a];
  }
  return [parseInt(m[1], 16) / 255, parseInt(m[2], 16) / 255, parseInt(m[3], 16) / 255, a];
}

const shade = (c: RGBA, k: number, a = c[3]): RGBA => [c[0] * k, c[1] * k, c[2] * k, a];
const WHITE: RGBA = [1, 1, 1, 1];
const SHADOW: RGBA = [0, 0, 0, 0.32];
const CLEAR: RGBA = [0, 0, 0, 0];
const HOME: RGBA = [0.88, 0.48, 0.25, 0.85];
const RING: RGBA = hexRgba('rgba(255, 255, 255, 0.5)');
const ZONE: RGBA = [0.91, 0.88, 0.85, 0.06];
const PULSE: RGBA = [1, 0.8, 0.5, 1];

export function buildScene(rows: SceneRows, orbit: Orbit, w: number, h: number, timeS: number): Scene {
  const v = view(orbit, w, h);
  const hf = rows.heightfield;
  const ground = (x: number, y: number): Vec3 => [x, y, heightAt(hf, x, y)];
  /** Metres per CSS px at world point p. */
  const mpp = (p: Vec3) => metresPerPx(Math.max(1e-3, len(sub(v.eye, p))), h);
  const lift = orbit.dist * 0.0006;
  const ov = new OverlayBatch(hf, lift);
  const objects: SceneObject[] = [];
  const labels: Label[] = [];
  const hits: Hit[] = [];
  let animating = false;

  // Exclusion zones: faint fill + dashed ring at the true radius.
  for (const vis of rows.visits) {
    const r = Math.max(0, vis.radiusM);
    if (r <= 0) continue;
    const m = mpp(ground(vis.x, vis.y));
    ov.disc(vis.x, vis.y, r, ZONE);
    ov.ring(vis.x, vis.y, Math.max(0, r - (RING_PX * m) / 2), r + (RING_PX * m) / 2, RING, [6 * m, 4 * m]);
  }

  // Home pad: tape cross + circle at the origin (no heading arrow).
  {
    const m = mpp(ground(0, 0));
    ov.segment(-9 * m, 0, 9 * m, 0, 2 * m, HOME);
    ov.segment(0, -9 * m, 0, 9 * m, 2 * m, HOME);
    ov.ring(0, 0, 12 * m, 13.5 * m, HOME);
  }

  // Trail: dark under-stroke, then segments coloured by the newer pose's source.
  const poses = [...rows.poses].sort((a, b) => a.tUnix - b.tUnix);
  for (const pass of [0, 1]) {
    for (let i = 1; i < poses.length; i++) {
      const a = poses[i - 1];
      const b = poses[i];
      const m = mpp(ground((a.x + b.x) / 2, (a.y + b.y) / 2));
      if (pass === 0) ov.segment(a.x, a.y, b.x, b.y, (TRAIL_PX + 1.6) * m, [0.05, 0.04, 0.035, 0.55]);
      else ov.segment(a.x, a.y, b.x, b.y, TRAIL_PX * m, hexRgba(sourceColor(b.source)));
    }
  }

  // Stations: small raised squares.
  for (const t of rows.stations) {
    const p = ground(t.x, t.y);
    const m = mpp(p);
    const s = STATION_PX * m;
    ov.disc(t.x, t.y, s * 0.9, SHADOW, CLEAR);
    objects.push({ shape: 'box', model: model(p[0], p[1], p[2], (t.headingDeg * Math.PI) / 180, s, s, s * 0.4), nmat: normalMatrix(0, s, s, s * 0.4), color: [0.91, 0.88, 0.85, 1] });
  }

  // Candidate pins.
  const next = nextStop(rows.candidates, rows.status);
  const order = [...rows.candidates].sort((a, b) => pinOrder(a, rows.selectedCandId) - pinOrder(b, rows.selectedCandId));
  for (const c of order) {
    const base = ground(c.x, c.y);
    const m = mpp(base);
    const color = hexRgba(statusColor(c.status));
    const selected = c.candId === rows.selectedCandId;
    const hovered = c.candId === rows.hoveredCandId;
    const bp = project(v, base);
    if (isDim(c.status)) {
      // Skipped / merged / excluded: a quiet dot on the ground.
      const r = (selected ? 6 : 4) * m;
      ov.disc(c.x, c.y, r + 1.2 * m, [0.07, 0.06, 0.06, 0.5]);
      ov.disc(c.x, c.y, r, color);
      if (selected) ov.ring(c.x, c.y, r + 2 * m, r + 3.5 * m, WHITE);
      if (bp.front) hits.push({ candId: c.candId, x: bp.x, y: bp.y, r: HIT_RADIUS_PX });
      continue;
    }
    const rPx = pinRadius(c.score) * (selected ? 1.2 : hovered ? 1.12 : 1);
    const r = rPx * m;
    const stem = (STEM_PX + rPx) * m;
    const head: Vec3 = [base[0], base[1], base[2] + stem + r];
    ov.disc(c.x, c.y, r * 1.3, SHADOW, CLEAR);
    ov.disc(c.x, c.y, 3 * m, shade(color, 0.9));
    if (selected) ov.ring(c.x, c.y, (rPx * 0.9 + 3) * m, (rPx * 0.9 + 5) * m, WHITE);
    const sw = 1.1 * m;
    objects.push({ shape: 'cylinder', model: model(base[0], base[1], base[2], 0, sw, sw, stem), nmat: normalMatrix(0, sw, sw, stem), color: shade(color, 0.75, 1) });
    objects.push({ shape: 'sphere', model: model(head[0], head[1], head[2], 0, r, r, r), nmat: normalMatrix(0, r, r, r), color, outline: selected });
    const hp = project(v, head);
    if (hp.front) {
      hits.push({ candId: c.candId, x: hp.x, y: hp.y, r: Math.max(HIT_RADIUS_PX, rPx) });
      if (c.rank > 0) labels.push({ key: `rank:${c.candId}`, kind: 'rank', text: String(c.rank), x: hp.x, y: hp.y });
    }
    if (bp.front) hits.push({ candId: c.candId, x: bp.x, y: bp.y, r: HIT_RADIUS_PX * 0.75 });
  }

  // Next stop: pulsing rings on the ground + a label above it.
  if (next) {
    const p = ground(next.x, next.y);
    const m = mpp(p);
    for (const phase of [0, 0.5]) {
      const t = (timeS / 1.8 + phase) % 1;
      const rr = (10 + 26 * t) * m;
      ov.ring(next.x, next.y, rr, rr + 2.2 * m, [PULSE[0], PULSE[1], PULSE[2], 0.75 * (1 - t)]);
    }
    animating = true;
    const top: Vec3 =
      next.kind === 'cand' && !isDim(next.cand.status)
        ? [p[0], p[1], p[2] + (STEM_PX + 2 * pinRadius(next.cand.score)) * m]
        : [p[0], p[1], p[2]];
    const lp = project(v, top);
    if (lp.front) labels.push({ key: 'next', kind: 'next', text: next.kind === 'home' ? 'Next stop · home' : 'Next stop', x: lp.x, y: lp.y });
  }

  // Rover: abstract marker at the latest pose, with a soft halo.
  const rover = roverPose({ poses: rows.poses, status: rows.status });
  if (rover) {
    const p = ground(rover.x, rover.y);
    const m = mpp(p);
    const L = ROVER_PX * m;
    ov.disc(rover.x, rover.y, L * 0.75, [0, 0, 0, 0.38], CLEAR);
    const t = (timeS / 2.2) % 1;
    ov.ring(rover.x, rover.y, L * (0.55 + 0.5 * t), L * (0.55 + 0.5 * t) + 1.8 * m, [0.88, 0.48, 0.25, 0.6 * (1 - t)]);
    animating = true;
    const a = (rover.headingDeg * Math.PI) / 180;
    objects.push({ shape: 'rover', model: model(p[0], p[1], p[2], a, L, L, L), nmat: normalMatrix(a, L, L, L), color: WHITE });
  }

  return {
    view: v,
    overlay: ov.empty ? undefined : ov.mesh(),
    objects,
    labels,
    hits,
    gridStepM: gridStep(pxPerMetre(orbit, h), 48),
    next,
    animating,
  };
}

function pinOrder(c: Candidate, selected?: string): number {
  if (c.candId === selected) return 1e6;
  if (isDim(c.status)) return 0;
  return c.rank > 0 ? 1000 - c.rank : 1;
}

/** candId of the nearest hit target containing (sx, sy). */
export function hitTest(hits: readonly Hit[], sx: number, sy: number): string | undefined {
  let best: string | undefined;
  let bestD = Infinity;
  for (const h of hits) {
    const d = Math.hypot(h.x - sx, h.y - sy);
    if (d <= h.r && d < bestD) {
      best = h.candId;
      bestD = d;
    }
  }
  return best;
}
