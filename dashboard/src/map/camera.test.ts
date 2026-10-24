import { describe, expect, it } from 'vitest';
import {
  type Camera,
  MAX_SCALE,
  MIN_SCALE,
  boundsOf,
  fitBounds,
  headingToCanvasRad,
  panBy,
  resize,
  screenToWorld,
  worldToScreen,
  zoomAt,
} from './camera';

const cam: Camera = { x0: 0, y0: 0, scale: 100, cx: 400, cy: 300 };

describe('worldToScreen (docs/06 §6: +x up, +y left)', () => {
  it('puts the camera centre at the screen centre', () => {
    expect(worldToScreen(cam, 0, 0)).toEqual([400, 300]);
  });
  it('+x is up on screen', () => {
    expect(worldToScreen(cam, 1, 0)).toEqual([400, 200]);
  });
  it('+y is left on screen', () => {
    expect(worldToScreen(cam, 0, 1)).toEqual([300, 300]);
  });
  it('round-trips with screenToWorld', () => {
    const c: Camera = { x0: 1.3, y0: -0.7, scale: 237, cx: 512, cy: 384 };
    for (const [x, y] of [[0, 0], [1.5, -2], [-3.25, 0.4]]) {
      const [sx, sy] = worldToScreen(c, x, y);
      const [wx, wy] = screenToWorld(c, sx, sy);
      expect(wx).toBeCloseTo(x, 9);
      expect(wy).toBeCloseTo(y, 9);
    }
  });
});

describe('headingToCanvasRad', () => {
  it('heading 90° (facing +y) rotates image-up to screen-left (docs/06 D10)', () => {
    const a = headingToCanvasRad(90);
    // Canvas rotation is clockwise-positive in a y-down space: up (0,-1) -> (x', y').
    const ux = 0 * Math.cos(a) - -1 * Math.sin(a);
    const uy = 0 * Math.sin(a) + -1 * Math.cos(a);
    expect(ux).toBeCloseTo(-1, 9); // left
    expect(uy).toBeCloseTo(0, 9);
  });
  it('heading 0° leaves image-up pointing up', () => {
    expect(headingToCanvasRad(0)).toBeCloseTo(0, 12);
  });
});

describe('fitBounds', () => {
  it('fits the bounds plus 0.5 m margin and centres them', () => {
    const c = fitBounds({ minX: 0, maxX: 2, minY: -1, maxY: 1 }, 800, 600);
    // spans incl. margin: x 3 m (vertical, 600 px), y 3 m (horizontal, 800 px) -> 200 px/m
    expect(c.scale).toBeCloseTo(200);
    expect([c.x0, c.y0]).toEqual([1, 0]);
    const [sxTop, syTop] = worldToScreen(c, 2.5, 0);
    expect(syTop).toBeCloseTo(0);
    expect(sxTop).toBeCloseTo(400);
  });
  it('clamps the scale for a single point', () => {
    expect(fitBounds({ minX: 1, maxX: 1, minY: 1, maxY: 1 }, 100000, 100000, 0).scale).toBe(MAX_SCALE);
    expect(fitBounds({ minX: -1e3, maxX: 1e3, minY: 0, maxY: 0 }, 100, 100).scale).toBe(MIN_SCALE);
  });
});

describe('boundsOf', () => {
  it('returns undefined for no points and skips non-finite ones', () => {
    expect(boundsOf([])).toBeUndefined();
    expect(boundsOf([{ x: NaN, y: 0 }])).toBeUndefined();
    expect(boundsOf([{ x: 1, y: -2 }, { x: -3, y: 4 }])).toEqual({ minX: -3, maxX: 1, minY: -2, maxY: 4 });
  });
});

describe('zoom, pan, resize', () => {
  it('zoomAt keeps the world point under the cursor fixed', () => {
    const before = screenToWorld(cam, 123, 456);
    const z = zoomAt(cam, 123, 456, 2.5);
    expect(z.scale).toBe(250);
    const after = screenToWorld(z, 123, 456);
    expect(after[0]).toBeCloseTo(before[0], 9);
    expect(after[1]).toBeCloseTo(before[1], 9);
  });
  it('panBy moves the map with the drag', () => {
    const p = panBy(cam, 50, -20);
    // The world point that was at the centre now sits 50 px right, 20 px up.
    expect(worldToScreen(p, 0, 0)).toEqual([450, 280]);
  });
  it('resize keeps centre and scale', () => {
    expect(resize(cam, 1000, 500)).toEqual({ ...cam, cx: 500, cy: 250 });
  });
});
