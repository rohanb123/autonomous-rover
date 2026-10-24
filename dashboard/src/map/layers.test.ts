import { describe, expect, it } from 'vitest';
import { type Camera, worldToScreen } from './camera';
import * as exclusion from './layers/exclusion';
import { fmtMetres, gridStep } from './layers/grid';
import * as pins from './layers/pins';
import * as stations from './layers/stations';
import * as tiles from './layers/tiles';
import * as trail from './layers/trail';
import { defaultBounds, tileVisibility } from './MapView';
import { candidate, globalTile, localTile, pose, roverStatus, visit } from './testdata';

const cam: Camera = { x0: 0, y0: 0, scale: 100, cx: 400, cy: 300 };

/** Fake 2D context that records every call and property write. */
function recorder() {
  const calls: { op: string; args: unknown[] }[] = [];
  const ctx = new Proxy({} as Record<string, unknown>, {
    get: (_t, op: string) => (...args: unknown[]) => void calls.push({ op, args }),
    set: (_t, op: string, value) => {
      calls.push({ op: `set:${op}`, args: [value] });
      return true;
    },
  }) as unknown as CanvasRenderingContext2D;
  const ops = (op: string) => calls.filter((c) => c.op === op).map((c) => c.args);
  return { ctx, calls, ops };
}

describe('local tiles (docs/06 §6, D10)', () => {
  it('a heading-90 tile is rotated so rover-forward points screen-left', () => {
    const p = tiles.localTilePlacement(cam, localTile('s_0001', 0, 0, 90, 1));
    expect(p.rot).toBeCloseTo(-Math.PI / 2);
    // image-up (0,-1) under canvas rotation p.rot
    const ux = Math.sin(p.rot);
    const uy = -Math.cos(p.rot);
    expect(ux).toBeCloseTo(-1);
    expect(uy).toBeCloseTo(0);
  });
  it('is centred on the station pose and sized wPx·resMPx metres', () => {
    const p = tiles.localTilePlacement(cam, localTile('s_0001', 1, 0.5, 0, 1));
    expect([p.sx, p.sy]).toEqual(worldToScreen(cam, 1, 0.5));
    expect(p.w).toBeCloseTo(360 * 0.005 * 100);
  });
  it('draws newest on top at 0.85 opacity, rotated about the centre', () => {
    const { ctx, ops, calls } = recorder();
    const img = {} as CanvasImageSource;
    const old = localTile('s_0001', 0, 0, 0, 1, 'AAA');
    const neu = localTile('s_0002', 1, 0, 90, 2, 'BBB');
    tiles.draw(ctx, cam, { local: [neu, old], global: [], showLocal: true, showGlobal: false }, () => img);
    const translates = ops('translate');
    expect(translates[0]).toEqual(worldToScreen(cam, 0, 0)); // older first
    expect(translates[1]).toEqual(worldToScreen(cam, 1, 0));
    expect(ops('rotate')[1][0]).toBeCloseTo(-Math.PI / 2);
    expect(calls.filter((c) => c.op === 'set:globalAlpha').map((c) => c.args[0])).toEqual([0.85, 0.85]);
    const [, dx, dy, w, h] = ops('drawImage')[0] as number[];
    expect([dx, dy]).toEqual([-90, -90]);
    expect([w, h]).toEqual([180, 180]);
  });
  it('draws a placeholder outline while the image is decoding', () => {
    const { ctx, ops } = recorder();
    tiles.draw(ctx, cam, { local: [localTile('s_0001', 0, 0, 0, 1, 'A')], global: [], showLocal: true, showGlobal: false }, () => undefined);
    expect(ops('drawImage')).toHaveLength(0);
    expect(ops('strokeRect')).toHaveLength(1);
  });
});

describe('global tiles (docs/06 §6 convention)', () => {
  it('top-left pixel corner sits at (originX, originY); +col runs right, +row runs down', () => {
    const t = globalTile(0, 0, 1.28, 1.28);
    const p = tiles.globalTilePlacement(cam, t);
    expect([p.sx, p.sy]).toEqual(worldToScreen(cam, 1.28, 1.28));
    expect(p.w).toBeCloseTo(512 * 0.005 * 100);
    // Bottom-right corner: x = originX − hPx·res, y = originY − wPx·res
    const [brx, bry] = worldToScreen(cam, 1.28 - 512 * 0.005, 1.28 - 512 * 0.005);
    expect(p.sx + p.w).toBeCloseTo(brx);
    expect(p.sy + p.h).toBeCloseTo(bry);
  });
  it('is drawn unrotated', () => {
    const { ctx, ops } = recorder();
    tiles.draw(ctx, cam, { local: [], global: [globalTile(0, 0, 1, 1, 'A')], showLocal: false, showGlobal: true }, () => ({}) as CanvasImageSource);
    expect(ops('rotate')).toHaveLength(0);
    expect(ops('drawImage')).toHaveLength(1);
  });
});

describe('tile visibility (docs/06 §2)', () => {
  it('live: local tiles, plus global once fused', () => {
    expect(tileVisibility('live', 0)).toEqual({ showGlobal: false, showLocal: true });
    expect(tileVisibility('live', 4)).toEqual({ showGlobal: true, showLocal: true });
  });
  it('replay: global tiles if fused, else local', () => {
    expect(tileVisibility('replay', 0)).toEqual({ showGlobal: false, showLocal: true });
    expect(tileVisibility('replay', 4)).toEqual({ showGlobal: true, showLocal: false });
  });
});

describe('trail and rover glyph', () => {
  it('colours segments by pose source: dr gray, aruco green, fused white', () => {
    expect(trail.sourceColor('dr')).toBe(trail.DR_COLOR);
    expect(trail.sourceColor('dr+gyro')).toBe(trail.DR_COLOR);
    expect(trail.sourceColor('aruco')).toBe('#4caf50');
    expect(trail.sourceColor('fused')).toBe('#ffffff');
    const { ctx, calls } = recorder();
    trail.draw(ctx, cam, { poses: [pose(1, 0, 0, 2, 'aruco'), pose(0, 0, 0, 1, 'dr')] });
    const strokes = calls.filter((c) => c.op === 'set:strokeStyle').map((c) => c.args[0]);
    expect(strokes[0]).toBe('#4caf50'); // the one segment, coloured by its later pose
  });
  it('glyph uses the latest pose, else the status row', () => {
    expect(trail.roverPose({ poses: [pose(0, 0, 0, 1), pose(2, 1, 45, 3), pose(1, 1, 0, 2)] })).toMatchObject({ x: 2, y: 1, headingDeg: 45 });
    expect(trail.roverPose({ poses: [], status: roverStatus(0.5, -0.5, 10) })).toEqual({ x: 0.5, y: -0.5, headingDeg: 10 });
    expect(trail.roverPose({ poses: [] })).toBeUndefined();
  });
});

describe('pins (docs/06 §6)', () => {
  it('colours by status and dims skipped/excluded/merged', () => {
    expect(pins.statusColor('new')).toBe('#9a8f85');
    expect(pins.statusColor('queued')).toBe('#f0b429');
    expect(pins.statusColor('active')).toBe('#e07a3f');
    expect(pins.statusColor('visited')).toBe('#4caf50');
    for (const s of ['skipped', 'excluded', 'merged']) expect(pins.isDim(s)).toBe(true);
  });
  it('size grows with score', () => {
    expect(pins.pinRadius(0)).toBe(5);
    expect(pins.pinRadius(1)).toBe(13);
    expect(pins.pinRadius(2)).toBe(13);
  });
  it('writes the rank number inside top-k pins only', () => {
    const { ctx, ops } = recorder();
    pins.draw(ctx, cam, { candidates: [candidate('c_0001', 1, 0, 0.8, 'queued', 1), candidate('c_0002', 0, 1, 0.3, 'new', -1)] });
    expect(ops('fillText').map((a) => a[0])).toEqual(['1']);
  });
  it('hit-tests within 12 px, nearest wins', () => {
    const cs = [candidate('c_0001', 0, 0, 0, 'new'), candidate('c_0002', 0.05, 0, 0, 'new')];
    const [sx, sy] = worldToScreen(cam, 0, 0);
    expect(pins.hitTest(cam, cs, sx + 3, sy)).toBe('c_0001');
    expect(pins.hitTest(cam, cs, sx, sy - 5)).toBe('c_0002'); // 0.05 m = 5 px up
    expect(pins.hitTest(cam, cs, sx + 13, sy)).toBeUndefined();
  });
});

describe('exclusion rings and stations', () => {
  it('rings are dashed circles of radiusM', () => {
    const { ctx, ops } = recorder();
    exclusion.draw(ctx, cam, [visit('v_0001', 'c_0001', 1, 0, 5)]);
    expect(ops('setLineDash')[0]).toEqual([[6, 4]]);
    const [sx, sy, r] = ops('arc')[0] as number[];
    expect([sx, sy]).toEqual(worldToScreen(cam, 1, 0));
    expect(r).toBeCloseTo(20); // 0.2 m × 100 px/m
  });
  it('stations are squares centred on each local tile', () => {
    const { ctx, ops } = recorder();
    stations.draw(ctx, cam, [localTile('s_0001', 0, 0, 0, 1)]);
    const [x, y, w] = ops('fillRect')[0] as number[];
    expect([x + w / 2, y + w / 2]).toEqual([400, 300]);
  });
});

describe('default view and grid', () => {
  it('fits poses first, then other rows, else a 2 m box at home', () => {
    const empty = { poses: [], localTiles: [], candidates: [], visits: [] };
    expect(defaultBounds({ ...empty, poses: [pose(1, 2, 0, 1)], candidates: [candidate('c', 9, 9, 0, 'new')] })).toEqual({ minX: 1, maxX: 1, minY: 2, maxY: 2 });
    expect(defaultBounds({ ...empty, candidates: [candidate('c', 3, -1, 0, 'new')] })).toEqual({ minX: 3, maxX: 3, minY: -1, maxY: -1 });
    expect(defaultBounds(empty)).toEqual({ minX: -1, maxX: 1, minY: -1, maxY: 1 });
  });
  it('picks a readable grid step', () => {
    expect(gridStep(100)).toBe(0.5); // 50 px
    expect(gridStep(1000)).toBe(0.05);
    expect(fmtMetres(0.25)).toBe('25 cm');
    expect(fmtMetres(2)).toBe('2 m');
  });
});
