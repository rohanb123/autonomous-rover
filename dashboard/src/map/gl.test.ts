import { describe, expect, it } from 'vitest';
import * as tiles from './layers/tiles';
import { nextStop } from './mapdata';
import { heightAt, terrainMesh, type Heightfield } from './gl/geometry';
import { globalTileRect, groundLayout, localTileCorners, photoExtent, textureCamera, worldToUv } from './gl/ground';
import { invert, multiply } from './gl/mat';
import { DEFAULT_AZ, DEFAULT_EL, type Orbit, TOP_EL, fit, groundAt, pan, project, view, zoomAt } from './gl/orbit';
import { buildScene, hitTest } from './gl/scene';
import { candidate, globalTile, localTile, pose, roverStatus, visit } from './testdata';

const W = 800;
const H = 600;
const orbitAt = (el: number): Orbit => ({ tx: 1, ty: 0.5, tz: 0, dist: 4, az: DEFAULT_AZ, el });

describe('mat', () => {
  it('invert(vp) · vp = identity', () => {
    const v = view(orbitAt(DEFAULT_EL), W, H);
    const id = multiply(invert(v.vp)!, v.vp);
    for (let r = 0; r < 4; r++) for (let c = 0; c < 4; c++) expect(id[c * 4 + r]).toBeCloseTo(r === c ? 1 : 0, 4);
  });
});

describe('orbit camera', () => {
  for (const [name, el] of [
    ['3D', DEFAULT_EL],
    ['top', TOP_EL],
  ] as const) {
    it(`${name}: default view has +x up and +y left on screen (same as the 2D map, docs/06 §6 / D10)`, () => {
      const v = view(orbitAt(el), W, H);
      const c = project(v, [1, 0.5, 0]);
      const px = project(v, [1.5, 0.5, 0]);
      const py = project(v, [1, 1.0, 0]);
      expect(c.x).toBeCloseTo(W / 2, 3);
      expect(c.y).toBeCloseTo(H / 2, 3);
      expect(px.y).toBeLessThan(c.y - 10); // +x = up
      expect(Math.abs(px.x - c.x)).toBeLessThan(1);
      expect(py.x).toBeLessThan(c.x - 10); // +y = left
    });
  }

  it('groundAt inverts project on the ground plane', () => {
    const v = view(orbitAt(DEFAULT_EL), W, H);
    for (const p of [
      [0, 0],
      [2, -1],
      [1.3, 0.9],
    ] as const) {
      const s = project(v, [p[0], p[1], 0]);
      const g = groundAt(v, s.x, s.y)!;
      expect(g[0]).toBeCloseTo(p[0], 4);
      expect(g[1]).toBeCloseTo(p[1], 4);
    }
  });

  it('zoomAt keeps the ground point under the cursor fixed', () => {
    const o = orbitAt(DEFAULT_EL);
    const before = groundAt(view(o, W, H), 600, 200)!;
    const z = zoomAt(o, W, H, 600, 200, 1.8);
    expect(z.dist).toBeCloseTo(o.dist / 1.8, 6);
    const after = groundAt(view(z, W, H), 600, 200)!;
    expect(after[0]).toBeCloseTo(before[0], 4);
    expect(after[1]).toBeCloseTo(before[1], 4);
  });

  it('pan drags the ground point under the cursor', () => {
    const o = orbitAt(DEFAULT_EL);
    const g = groundAt(view(o, W, H), 300, 350)!;
    const p = pan(o, W, H, 300, 350, 420, 300);
    const s = project(view(p, W, H), [g[0], g[1], 0]);
    expect(s.x).toBeCloseTo(420, 2);
    expect(s.y).toBeCloseTo(300, 2);
  });

  it('fit shows every corner of the bounds (plus margin) on screen, tilted or top-down', () => {
    const b = { minX: -0.2, maxX: 1.6, minY: -0.5, maxY: 1.2 };
    for (const el of [DEFAULT_EL, TOP_EL]) {
      const o = fit(b, W, H, DEFAULT_AZ, el);
      const v = view(o, W, H);
      for (const [x, y] of [
        [b.minX - 0.5, b.minY - 0.5],
        [b.maxX + 0.5, b.maxY + 0.5],
        [b.minX - 0.5, b.maxY + 0.5],
        [b.maxX + 0.5, b.minY - 0.5],
      ]) {
        const s = project(v, [x, y, 0]);
        expect(s.x).toBeGreaterThanOrEqual(-1);
        expect(s.x).toBeLessThanOrEqual(W + 1);
        expect(s.y).toBeGreaterThanOrEqual(-1);
        expect(s.y).toBeLessThanOrEqual(H + 1);
      }
    }
  });
});

describe('ground texture placement matches the 2D tile layer', () => {
  const lt = [localTile('s_0001', 0.8, 0, 0, 3), localTile('s_0002', 0.8, 0.8, 90, 6), localTile('s_0003', 0.1, 0.8, 200, 8)];
  const gt = [globalTile(0, 0, 1.92, 1.68)];
  const extent = photoExtent(lt, gt)!;
  const layout = groundLayout(extent, 0.005);
  const cam = textureCamera(layout);

  it('a local tile centre lands at the same texture pixel as tiles.localTilePlacement', () => {
    for (const t of lt) {
      const p = tiles.localTilePlacement(cam, t);
      const [u, v] = worldToUv(layout, t.x, t.y);
      expect(u * layout.wPx).toBeCloseTo(p.sx, 6);
      expect(v * layout.hPx).toBeCloseTo(p.sy, 6);
    }
  });

  it('a global tile origin (top-left pixel corner) lands where tiles.globalTilePlacement draws it', () => {
    const p = tiles.globalTilePlacement(cam, gt[0]);
    const [u, v] = worldToUv(layout, gt[0].originX, gt[0].originY);
    expect(u * layout.wPx).toBeCloseTo(p.sx, 6);
    expect(v * layout.hPx).toBeCloseTo(p.sy, 6);
  });

  it('heading-90 tile: its forward edge points to mission +y (D10)', () => {
    const t = lt[1];
    const c = localTileCorners(t);
    const fwd = [(c[0][0] + c[1][0]) / 2 - t.x, (c[0][1] + c[1][1]) / 2 - t.y];
    expect(fwd[0]).toBeCloseTo(0, 9);
    expect(fwd[1]).toBeCloseTo(0.9, 9); // half of 360 px · 0.005 m
  });

  it('extent covers every tile corner, and the layout respects the size cap', () => {
    for (const t of lt)
      for (const [x, y] of localTileCorners(t)) {
        expect(x).toBeGreaterThanOrEqual(extent.minX - 1e-9);
        expect(x).toBeLessThanOrEqual(extent.maxX + 1e-9);
        expect(y).toBeGreaterThanOrEqual(extent.minY - 1e-9);
        expect(y).toBeLessThanOrEqual(extent.maxY + 1e-9);
      }
    const g = globalTileRect(gt[0]);
    expect(extent.maxX).toBeGreaterThanOrEqual(g.maxX);
    const small = groundLayout(extent, 0.005, 256);
    expect(Math.max(small.wPx, small.hPx)).toBeLessThanOrEqual(256);
    expect(small.res).toBeGreaterThan(0.005);
  });
});

describe('terrain geometry', () => {
  it('no heightfield = flat ground with straight-up normals', () => {
    expect(heightAt(undefined, 3, 4)).toBe(0);
    const m = terrainMesh({ minX: 0, maxX: 1, minY: 0, maxY: 1 }, 4);
    for (let i = 0; i < m.pos.length; i += 3) {
      expect(m.pos[i + 2]).toBe(0);
      expect(m.nrm[i + 2]).toBeCloseTo(1, 9);
    }
  });

  it('heights interpolate bilinearly, 0 outside, and slopes tilt the normals', () => {
    // z = 0.5·x on a 3×3 grid at 1 m spacing.
    const hf: Heightfield = { minX: 0, minY: 0, res: 1, nx: 3, ny: 3, z: new Float32Array([0, 0.5, 1, 0, 0.5, 1, 0, 0.5, 1]) };
    expect(heightAt(hf, 1.5, 0.7)).toBeCloseTo(0.75, 6);
    expect(heightAt(hf, -1, 1)).toBe(0);
    const m = terrainMesh({ minX: 0.5, maxX: 1.5, minY: 0.5, maxY: 1.5 }, 2, hf);
    const k = 4 * 3; // centre vertex
    expect(m.pos[k + 2]).toBeCloseTo(0.5, 6);
    const l = Math.hypot(0.5, 1);
    expect(m.nrm[k]).toBeCloseTo(-0.5 / l, 4);
    expect(m.nrm[k + 2]).toBeCloseTo(1 / l, 4);
  });
});

describe('nextStop', () => {
  const cands = [candidate('c_1', 1, 0, 0.5, 'queued', 2), candidate('c_2', 2, 0, 0.6, 'queued', 1), candidate('c_3', 3, 0, 0.7, 'new')];
  const st = (fsmState: string, activeCandId = '') => ({ ...roverStatus(0, 0, 0), fsmState, activeCandId });

  it('prefers the active candidate, then activeCandId, then rank 1', () => {
    expect(nextStop([...cands, candidate('c_4', 4, 0, 0.2, 'active', 3)], st('INVESTIGATE'))).toMatchObject({ kind: 'cand', cand: { candId: 'c_4' } });
    expect(nextStop(cands, st('INVESTIGATE', 'c_1'))).toMatchObject({ kind: 'cand', cand: { candId: 'c_1' } });
    expect(nextStop(cands, st('INVESTIGATE'))).toMatchObject({ kind: 'cand', cand: { candId: 'c_2' } });
    expect(nextStop(cands)).toMatchObject({ kind: 'cand', cand: { candId: 'c_2' } });
  });

  it('home while returning; nothing while surveying, stopped, done or E-stopped', () => {
    expect(nextStop(cands, st('RETURN'))).toEqual({ kind: 'home', x: 0, y: 0 });
    for (const s of ['SURVEY', 'IDLE', 'DONE', 'STOPPED', 'ESTOP']) expect(nextStop(cands, st(s))).toBeUndefined();
    expect(nextStop(cands, { ...st('INVESTIGATE'), estop: true })).toBeUndefined();
    expect(nextStop([candidate('c_9', 1, 1, 0.3, 'visited')], st('INVESTIGATE'))).toBeUndefined();
  });
});

describe('buildScene', () => {
  const rows = {
    poses: [pose(0, 0, 0, 1), pose(0.8, 0, 90, 2, 'aruco')],
    candidates: [candidate('c_1', 1.1, 1.0, 0.7, 'active', 1), candidate('c_2', 0.4, 1.2, 0.6, 'queued', 2), candidate('c_3', 1.5, -0.3, 0.3, 'skipped')],
    visits: [visit('v_1', 'c_3', 1.5, -0.3, 2)],
    stations: [localTile('s_0001', 0.8, 0, 0, 2)],
    status: { ...roverStatus(0.8, 0, 90), fsmState: 'INVESTIGATE' },
  };
  const o = fit({ minX: -0.5, maxX: 2, minY: -0.5, maxY: 1.5 }, W, H, DEFAULT_AZ, DEFAULT_EL);
  const s = buildScene(rows, o, W, H, 0);

  it('draws the rover, a stem + head per ranked/new pin, a box per station; dim pins stay flat', () => {
    expect(s.objects.filter((x) => x.shape === 'rover')).toHaveLength(1);
    expect(s.objects.filter((x) => x.shape === 'sphere')).toHaveLength(2); // c_3 is skipped → flat dot
    expect(s.objects.filter((x) => x.shape === 'cylinder')).toHaveLength(2);
    expect(s.objects.filter((x) => x.shape === 'box')).toHaveLength(1);
    expect(s.overlay).toBeDefined();
  });

  it('labels ranks and the next stop; clicking a pin head hits that pin', () => {
    expect(s.labels.filter((l) => l.kind === 'rank').map((l) => l.text).sort()).toEqual(['1', '2']);
    const next = s.labels.find((l) => l.kind === 'next');
    expect(next?.text).toBe('Next stop');
    const head = s.labels.find((l) => l.key === 'rank:c_2')!;
    expect(hitTest(s.hits, head.x + 3, head.y - 2)).toBe('c_2');
    expect(hitTest(s.hits, 5, 5)).toBeUndefined();
  });

  it('the dim pin is hit at its ground position', () => {
    const g = project(s.view, [1.5, -0.3, 0]);
    expect(hitTest(s.hits, g.x, g.y)).toBe('c_3');
  });

  it('selected pin gets an outline; returning rover labels home', () => {
    const sel = buildScene({ ...rows, selectedCandId: 'c_2' }, o, W, H, 0);
    expect(sel.objects.filter((x) => x.outline)).toHaveLength(1);
    const ret = buildScene({ ...rows, status: { ...rows.status, fsmState: 'RETURN' } }, o, W, H, 0);
    expect(ret.labels.find((l) => l.kind === 'next')?.text).toBe('Next stop · home');
  });
});
