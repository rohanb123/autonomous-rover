// World (mission frame, metres) <-> screen (CSS pixels) for MapView (docs/06 §6).
// Mission frame: +x forward at start, +y left, heading CCW in degrees (docs/00 §4).
// On screen: +x points UP and +y points LEFT, so the start heading is "up".

export interface Camera {
  /** World point shown at the screen centre (metres). */
  x0: number;
  y0: number;
  /** Pixels per metre. */
  scale: number;
  /** Screen centre (CSS px). */
  cx: number;
  cy: number;
}

export interface Bounds {
  minX: number;
  maxX: number;
  minY: number;
  maxY: number;
}

export const MIN_SCALE = 20; // px/m: ~ 40 m across a 800 px canvas
export const MAX_SCALE = 4000; // px/m: 0.25 mm per px

export function worldToScreen(cam: Camera, x: number, y: number): [number, number] {
  return [cam.cx - (y - cam.y0) * cam.scale, cam.cy - (x - cam.x0) * cam.scale];
}

export function screenToWorld(cam: Camera, sx: number, sy: number): [number, number] {
  return [cam.x0 - (sy - cam.cy) / cam.scale, cam.y0 - (sx - cam.cx) / cam.scale];
}

/** Canvas rotation (radians, clockwise-positive) that turns image-up into a world heading. */
export function headingToCanvasRad(headingDeg: number): number {
  return (-headingDeg * Math.PI) / 180;
}

const clampScale = (s: number) => Math.min(MAX_SCALE, Math.max(MIN_SCALE, s));

/** Camera that fits `b` (plus `marginM` on every side) into a width×height canvas. */
export function fitBounds(b: Bounds, width: number, height: number, marginM = 0.5): Camera {
  const spanX = b.maxX - b.minX + 2 * marginM; // vertical on screen
  const spanY = b.maxY - b.minY + 2 * marginM; // horizontal on screen
  const scale = clampScale(Math.min(height / spanX, width / spanY));
  return {
    x0: (b.minX + b.maxX) / 2,
    y0: (b.minY + b.maxY) / 2,
    scale,
    cx: width / 2,
    cy: height / 2,
  };
}

/** Bounding box of points, or undefined for none. */
export function boundsOf(points: Iterable<{ x: number; y: number }>): Bounds | undefined {
  let b: Bounds | undefined;
  for (const p of points) {
    if (!Number.isFinite(p.x) || !Number.isFinite(p.y)) continue;
    if (!b) b = { minX: p.x, maxX: p.x, minY: p.y, maxY: p.y };
    else {
      b.minX = Math.min(b.minX, p.x);
      b.maxX = Math.max(b.maxX, p.x);
      b.minY = Math.min(b.minY, p.y);
      b.maxY = Math.max(b.maxY, p.y);
    }
  }
  return b;
}

/** Zoom by `factor` keeping the world point under (sx, sy) fixed. */
export function zoomAt(cam: Camera, sx: number, sy: number, factor: number): Camera {
  const [wx, wy] = screenToWorld(cam, sx, sy);
  const scale = clampScale(cam.scale * factor);
  // Solve worldToScreen(next, wx, wy) == (sx, sy) for the new centre.
  return { ...cam, scale, x0: wx + (sy - cam.cy) / scale, y0: wy + (sx - cam.cx) / scale };
}

/** Pan by a screen-space drag of (dx, dy) CSS px. */
export function panBy(cam: Camera, dx: number, dy: number): Camera {
  return { ...cam, x0: cam.x0 + dy / cam.scale, y0: cam.y0 + dx / cam.scale };
}

/** Keep the same world centre and scale when the canvas is resized. */
export function resize(cam: Camera, width: number, height: number): Camera {
  return { ...cam, cx: width / 2, cy: height / 2 };
}
