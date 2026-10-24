// Standalone MapView preview with sample rows (no STDB). Dev only:
//   cd dashboard && npm run dev  ->  http://localhost:5173/src/map/preview.html (?mode=replay&fused=1)
// Checks by eye: docs/06 D10 (heading-90 tile's arrow points LEFT), trail colours, pin colours
// and ranks, dashed exclusion rings, station squares, replay "fused map".
import { StrictMode, useMemo, useState } from 'react';
import { createRoot } from 'react-dom/client';
import '../styles.css';
import MapView from './MapView';
import { RUN, candidate, globalTile, localTile, pose, visit } from './testdata';

/** A BEV-like tile: brown ground, grid, white arrow pointing image-up (= rover forward). */
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
  return c.toDataURL('image/png').split(',')[1];
}

function Preview() {
  const params = new URLSearchParams(window.location.search);
  const [mode, setMode] = useState<'live' | 'replay'>(params.get('mode') === 'replay' ? 'replay' : 'live');
  const [fused, setFused] = useState(params.get('fused') === '1');
  const [selected, setSelected] = useState<string>();
  const rows = useMemo(() => {
    const poses = [
      pose(0, 0, 0, 1, 'dr'), pose(0.4, 0, 0, 2, 'dr'), pose(0.8, 0, 0, 3, 'dr+gyro'),
      pose(0.8, 0, 90, 4, 'aruco'), pose(0.8, 0.5, 90, 5, 'aruco'), pose(0.8, 0.8, 90, 6, 'fused'),
      pose(0.5, 0.9, 200, 7, 'dr'), pose(0.1, 0.8, 200, 8, 'dr'),
    ];
    const localTiles = [
      localTile('s_0001', 0.8, 0, 0, 3, arrowTilePng(360, 's_0001 hdg 0')),
      localTile('s_0002', 0.8, 0.8, 90, 6, arrowTilePng(360, 's_0002 hdg 90 (D10: arrow LEFT)')),
      localTile('s_0003', 0.1, 0.8, 200, 8, arrowTilePng(360, 's_0003 hdg 200')),
    ];
    const candidates = [
      candidate('c_0001', 1.3, 0.2, 0.82, 'visited'),
      candidate('c_0002', 1.1, 1.0, 0.71, 'active', 1),
      candidate('c_0003', 0.4, 1.2, 0.64, 'queued', 2),
      candidate('c_0004', 0.2, 0.4, 0.4, 'new'),
      candidate('c_0005', 1.5, -0.3, 0.3, 'skipped'),
      candidate('c_0006', 0.6, -0.4, 0.55, 'queued', 3),
    ];
    const visits = [visit('v_0001', 'c_0001', 1.3, 0.2, 5), visit('v_0002', 'c_0005', 1.5, -0.3, 6)];
    // One fused tile centred on (0.64, 0.4): origin = its top-left corner (max x, max y).
    const globalTiles = [globalTile(0, 0, 0.64 + 1.28, 0.4 + 1.28, arrowTilePng(512, 'global (fused)', '#5c4a3a'))];
    return { poses, localTiles, candidates, visits, globalTiles };
  }, []);

  return (
    <div style={{ display: 'grid', gridTemplateRows: 'auto 1fr', height: '100vh', gap: 8, padding: 8 }}>
      <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
        <b>MapView preview · {RUN}</b>
        <button onClick={() => setMode(mode === 'live' ? 'replay' : 'live')}>mode: {mode}</button>
        <button onClick={() => setFused(!fused)}>fused: {String(fused)}</button>
        <span className="meta">selected: {selected ?? '—'}</span>
      </div>
      <div style={{ minHeight: 0, border: '1px solid var(--border)', borderRadius: 6 }}>
        <MapView
          mode={mode}
          poses={rows.poses}
          localTiles={rows.localTiles}
          globalTiles={fused ? rows.globalTiles : []}
          candidates={rows.candidates}
          visits={rows.visits}
          selectedCandId={selected}
          onSelectCand={setSelected}
        />
      </div>
    </div>
  );
}

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <Preview />
  </StrictMode>
);
