// Standalone MapView preview with sample rows (no STDB). Dev only:
//   cd dashboard && npm run dev  ->  http://localhost:5173/src/map/preview.html
// URL options: ?mode=replay  ?fused=1  ?check=d10 (orientation test tiles)  ?view=3d|top|2d
//
// The sample ground photos are rendered from ONE simulated scene (sand, pebbles, and a rock under
// every candidate) through each tile's own placement, exactly as the mapper's BEV/global tiles are
// defined (docs/06 §6). So if placement were wrong anywhere, rocks would not line up across
// overlapping tiles or under their pins. Nothing here is real rover data.
//
// ?check=d10 swaps in arrow tiles for docs/06 D10: the heading-90 tile's arrow must point LEFT
// (+y) in the 2D and Top views.
import { StrictMode, useMemo, useState } from 'react';
import { createRoot } from 'react-dom/client';
import type { Candidate, GlobalTile, LocalMapTile } from '../module_bindings/types';
import '../styles.css';
import MapView from './MapView';
import MapView3D from './MapView3D';
import type { Heightfield } from './gl/geometry';
import { RUN, candidate, globalTile, localTile, pose, roverStatus, visit } from './testdata';

// --- simulated scene -------------------------------------------------------------------------

function hash(ix: number, iy: number, seed: number): number {
  let h = Math.imul(ix, 374761393) ^ Math.imul(iy, 668265263) ^ Math.imul(seed, 2147483647);
  h = Math.imul(h ^ (h >>> 13), 1274126177);
  return ((h ^ (h >>> 16)) >>> 0) / 4294967296;
}

function valueNoise(x: number, y: number, seed: number): number {
  const ix = Math.floor(x);
  const iy = Math.floor(y);
  const fx = x - ix;
  const fy = y - iy;
  const sx = fx * fx * (3 - 2 * fx);
  const sy = fy * fy * (3 - 2 * fy);
  const a = hash(ix, iy, seed) * (1 - sx) + hash(ix + 1, iy, seed) * sx;
  const b = hash(ix, iy + 1, seed) * (1 - sx) + hash(ix + 1, iy + 1, seed) * sx;
  return a * (1 - sy) + b * sy;
}

interface Rock {
  x: number;
  y: number;
  r: number;
  tone: [number, number, number];
}

/** Colour of the simulated ground at mission point (x, y), metres. */
function groundRgb(x: number, y: number, rocks: readonly Rock[]): [number, number, number] {
  const broad = valueNoise(x * 1.3, y * 1.3, 1) * 0.6 + valueNoise(x * 4, y * 4, 2) * 0.4;
  const grain = valueNoise(x * 140, y * 140, 3);
  let r = 150 + 40 * broad + 18 * (grain - 0.5);
  let g = 108 + 30 * broad + 14 * (grain - 0.5);
  let b = 76 + 22 * broad + 10 * (grain - 0.5);
  // Pebbles: one per 3 cm cell at most.
  const cell = 0.03;
  const cx = Math.floor(x / cell);
  const cy = Math.floor(y / cell);
  for (let j = -1; j <= 1; j++)
    for (let i = -1; i <= 1; i++) {
      const h = hash(cx + i, cy + j, 7);
      if (h > 0.12) continue;
      const px = (cx + i + hash(cx + i, cy + j, 8)) * cell;
      const py = (cy + j + hash(cx + i, cy + j, 9)) * cell;
      const pr = 0.002 + 0.005 * hash(cx + i, cy + j, 10);
      const d = Math.hypot(x - px, y - py);
      if (d < pr) {
        const lit = 0.75 + 0.5 * ((px - x + (py - y)) / pr) * 0.5;
        const t = 105 + 70 * hash(cx + i, cy + j, 11);
        r = t * lit;
        g = t * 0.86 * lit;
        b = t * 0.74 * lit;
      } else if (d < pr * 1.6 && x < px) {
        r *= 0.9;
        g *= 0.9;
        b *= 0.9;
      }
    }
  for (const k of rocks) {
    const dx = x - k.x;
    const dy = y - k.y;
    const d = Math.hypot(dx, dy);
    if (d < k.r) {
      // Lit from the -x, +y side: brighter toward it, darker away.
      const shade = 0.78 + 0.35 * ((-dx + dy) / k.r) * 0.5 + 0.12 * valueNoise(x * 300, y * 300, 12);
      return [k.tone[0] * shade, k.tone[1] * shade, k.tone[2] * shade];
    }
    if (d < k.r * 1.5 && dx > 0) {
      r *= 0.6 + 0.4 * ((d - k.r) / (k.r * 0.5));
      g *= 0.6 + 0.4 * ((d - k.r) / (k.r * 0.5));
      b *= 0.6 + 0.4 * ((d - k.r) / (k.r * 0.5));
    }
  }
  return [r, g, b];
}

function toPngB64(c: HTMLCanvasElement): string {
  return c.toDataURL('image/png').split(',')[1];
}

/** BEV tile of the scene: image up = rover +x, image left = rover +y (docs/00 §4); round
 *  footprint of the 8-photo ring with a soft edge, transparent outside. */
function sceneLocalPng(t: LocalMapTile, rocks: readonly Rock[]): string {
  const c = document.createElement('canvas');
  c.width = t.wPx;
  c.height = t.hPx;
  const g = c.getContext('2d')!;
  const img = g.createImageData(t.wPx, t.hPx);
  const a = (t.headingDeg * Math.PI) / 180;
  const cos = Math.cos(a);
  const sin = Math.sin(a);
  const R = (Math.min(t.wPx, t.hPx) / 2) * t.resMPx;
  for (let row = 0; row < t.hPx; row++)
    for (let col = 0; col < t.wPx; col++) {
      const u = (t.hPx / 2 - row - 0.5) * t.resMPx; // rover +x
      const v = (t.wPx / 2 - col - 0.5) * t.resMPx; // rover +y
      const rr = Math.hypot(u, v);
      const alpha = Math.max(0, Math.min(1, (R - rr) / (R * 0.12)));
      if (alpha <= 0) continue;
      const [r, gg, b] = groundRgb(t.x + u * cos - v * sin, t.y + u * sin + v * cos, rocks);
      const k = (row * t.wPx + col) * 4;
      img.data[k] = r;
      img.data[k + 1] = gg;
      img.data[k + 2] = b;
      img.data[k + 3] = alpha * 255;
    }
  g.putImageData(img, 0, 0);
  return toPngB64(c);
}

/** Global (fused) tile of the scene: pixel (col, row) → x = originX − row·res, y = originY − col·res. */
function sceneGlobalPng(t: GlobalTile, rocks: readonly Rock[], covered: (x: number, y: number) => boolean): string {
  const c = document.createElement('canvas');
  c.width = t.wPx;
  c.height = t.hPx;
  const g = c.getContext('2d')!;
  const img = g.createImageData(t.wPx, t.hPx);
  for (let row = 0; row < t.hPx; row++)
    for (let col = 0; col < t.wPx; col++) {
      const x = t.originX - (row + 0.5) * t.resMPx;
      const y = t.originY - (col + 0.5) * t.resMPx;
      if (!covered(x, y)) continue;
      const [r, gg, b] = groundRgb(x, y, rocks);
      const k = (row * t.wPx + col) * 4;
      img.data.set([r, gg, b, 255], k);
    }
  g.putImageData(img, 0, 0);
  return toPngB64(c);
}

/** D10 test tile: brown ground, grid, white arrow pointing image-up (= rover forward). */
function arrowTilePng(px: number, label: string, ground = '#7a5234'): string {
  const c = document.createElement('canvas');
  c.width = c.height = px;
  const g = c.getContext('2d')!;
  g.fillStyle = ground;
  g.fillRect(0, 0, px, px);
  g.strokeStyle = 'rgba(255,255,255,0.15)';
  for (let i = 0; i <= px; i += px / 8) {
    g.beginPath(); g.moveTo(i, 0); g.lineTo(i, px); g.moveTo(0, i); g.lineTo(px, i); g.stroke();
  }
  g.fillStyle = '#f5f5f5';
  g.beginPath();
  g.moveTo(px / 2, px * 0.08);
  g.lineTo(px * 0.62, px * 0.3);
  g.lineTo(px * 0.54, px * 0.3);
  g.lineTo(px * 0.54, px * 0.7);
  g.lineTo(px * 0.46, px * 0.7);
  g.lineTo(px * 0.46, px * 0.3);
  g.lineTo(px * 0.38, px * 0.3);
  g.closePath();
  g.fill();
  g.font = `${Math.round(px / 12)}px sans-serif`;
  g.fillText(label, 8, px - 10);
  return toPngB64(c);
}

/** Synthetic bumps for the "demo heights" toggle ONLY (preview, clearly labelled). */
function demoHeightfield(): Heightfield {
  const res = 0.02;
  const minX = -1.5;
  const minY = -1.5;
  const nx = 200;
  const ny = 200;
  const z = new Float32Array(nx * ny);
  const bumps = [
    [1.7, -0.7, 0.35, 0.22],
    [0.3, 1.7, 0.45, 0.28],
    [-0.5, -0.3, 0.35, -0.12],
    [1.3, 1.7, 0.25, 0.15],
  ];
  for (let j = 0; j < ny; j++)
    for (let i = 0; i < nx; i++) {
      const x = minX + i * res;
      const y = minY + j * res;
      let h = 0;
      for (const [bx, by, s, a] of bumps) h += a * Math.exp(-((x - bx) ** 2 + (y - by) ** 2) / (2 * s * s));
      z[j * nx + i] = h;
    }
  return { minX, minY, res, nx, ny, z };
}

// --- page --------------------------------------------------------------------------------------

function Preview() {
  const params = new URLSearchParams(window.location.search);
  const d10 = params.get('check') === 'd10';
  const [mode, setMode] = useState<'live' | 'replay'>(params.get('mode') === 'replay' ? 'replay' : 'live');
  const [fused, setFused] = useState(params.get('fused') === '1');
  const [demoHills, setDemoHills] = useState(false);
  const [selected, setSelected] = useState<string>();
  const rows = useMemo(() => {
    const poses = [
      pose(0, 0, 0, 1, 'dr'), pose(0.4, 0, 0, 2, 'dr'), pose(0.8, 0, 0, 3, 'dr+gyro'),
      pose(0.8, 0, 90, 4, 'aruco'), pose(0.8, 0.5, 90, 5, 'aruco'), pose(0.8, 0.8, 90, 6, 'fused'),
      pose(0.5, 0.9, 200, 7, 'dr'), pose(0.1, 0.8, 200, 8, 'dr'),
    ];
    const sized = (c: Candidate, sizeM: number, category: string): Candidate => ({ ...c, sizeM, category });
    const candidates = [
      sized(candidate('c_0001', 1.3, 0.2, 0.82, 'visited'), 0.028, 'float_rock'),
      sized(candidate('c_0002', 1.1, 1.0, 0.71, 'active', 1), 0.034, 'veined'),
      sized(candidate('c_0003', 0.4, 1.2, 0.64, 'queued', 2), 0.022, 'layered'),
      sized(candidate('c_0004', 0.2, 0.4, 0.4, 'new'), 0.018, 'pebble'),
      sized(candidate('c_0005', 1.5, -0.3, 0.3, 'skipped'), 0.045, 'bedrock'),
      sized(candidate('c_0006', 0.6, -0.4, 0.55, 'queued', 3), 0.026, 'float_rock'),
    ];
    const tones: [number, number, number][] = [[96, 84, 78], [120, 110, 104], [88, 70, 58], [140, 120, 96], [70, 64, 62], [110, 92, 80]];
    const rocks: Rock[] = candidates.map((c, i) => ({ x: c.x, y: c.y, r: c.sizeM / 2, tone: tones[i % tones.length] }));
    const stations = [
      localTile('s_0001', 0.8, 0, 0, 3),
      localTile('s_0002', 0.8, 0.8, 90, 6),
      localTile('s_0003', 0.1, 0.8, 200, 8),
    ];
    const localTiles = stations.map((t, i) => ({
      ...t,
      pngB64: d10 ? arrowTilePng(360, `${t.stationId} hdg ${t.headingDeg}${i === 1 ? ' (D10: arrow LEFT)' : ''}`) : sceneLocalPng(t, rocks),
    }));
    const visits = [visit('v_0001', 'c_0001', 1.3, 0.2, 5), visit('v_0002', 'c_0005', 1.5, -0.3, 6)];
    // One fused tile centred on (0.64, 0.4): origin = its top-left corner (max x, max y).
    const g = globalTile(0, 0, 0.64 + 1.28, 0.4 + 1.28);
    const covered = (x: number, y: number) => stations.some((s) => Math.hypot(x - s.x, y - s.y) < 0.9);
    const globalTiles = [{ ...g, pngB64: d10 ? arrowTilePng(512, 'global (fused)', '#5c4a3a') : sceneGlobalPng(g, rocks, covered) }];
    const status = { ...roverStatus(0.1, 0.8, 200), fsmState: 'INVESTIGATE', activeCandId: 'c_0002' };
    return { poses, localTiles, candidates, visits, globalTiles, status };
  }, [d10]);
  const hills = useMemo(() => (demoHills ? demoHeightfield() : undefined), [demoHills]);

  const common = {
    mode,
    poses: rows.poses,
    localTiles: rows.localTiles,
    globalTiles: fused ? rows.globalTiles : [],
    candidates: rows.candidates,
    visits: rows.visits,
    status: rows.status,
    selectedCandId: selected,
    onSelectCand: setSelected,
  };

  return (
    <div style={{ display: 'grid', gridTemplateRows: 'auto 1fr', height: '100vh', gap: 8, padding: 8, boxSizing: 'border-box' }}>
      <div style={{ display: 'flex', gap: 8, alignItems: 'center', flexWrap: 'wrap' }}>
        <b>MapView preview · {RUN}</b>
        <button onClick={() => setMode(mode === 'live' ? 'replay' : 'live')}>mode: {mode}</button>
        <button onClick={() => setFused(!fused)}>fused: {String(fused)}</button>
        <button onClick={() => setDemoHills(!demoHills)} title="Synthetic heights to show how real elevation data would render">
          demo hills: {demoHills ? 'on (synthetic, not rover data)' : 'off'}
        </button>
        <span className="meta">selected: {selected ?? '—'}</span>
        <span className="meta">sample data, not a real run{d10 ? ' · D10 arrow tiles' : ''}</span>
      </div>
      <div style={{ minHeight: 0, border: '1px solid var(--border)', borderRadius: 6 }}>
        {demoHills ? <MapView3D {...common} topDown={false} heightfield={hills} onFail={() => setDemoHills(false)} /> : <MapView {...common} />}
      </div>
    </div>
  );
}

const view = new URLSearchParams(window.location.search).get('view');
if (view === '3d' || view === 'top' || view === '2d') {
  try {
    window.localStorage.setItem('marsrover.mapview.view', view);
  } catch {
    // ignore
  }
}

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <Preview />
  </StrictMode>
);
