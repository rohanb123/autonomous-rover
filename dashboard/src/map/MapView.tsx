// MapView (P3): canvas map of one run (docs/06 §4, §6). Rows come in via props from the P4
// shell; this component never opens its own STDB connection and never writes anything.
// Replay filtering by scrub time is done by the shell (useScrubbed) before rows reach here.
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import type { Candidate, GlobalTile, LocalMapTile, Pose, RoverStatus, Visit } from '../module_bindings/types';
import { type Bounds, type Camera, boundsOf, fitBounds, panBy, resize, zoomAt } from './camera';
import * as exclusion from './layers/exclusion';
import * as grid from './layers/grid';
import * as pins from './layers/pins';
import * as stations from './layers/stations';
import * as tiles from './layers/tiles';
import * as trail from './layers/trail';
import './map.css';

export interface MapViewProps {
  mode: 'live' | 'replay';
  poses: readonly Pose[];
  localTiles: readonly LocalMapTile[];
  globalTiles: readonly GlobalTile[];
  candidates: readonly Candidate[];
  visits: readonly Visit[];
  status?: RoverStatus;
  selectedCandId?: string;
  onSelectCand(candId: string): void;
}

const CLICK_SLOP_PX = 4;
const EMPTY_BOUNDS: Bounds = { minX: -1, maxX: 1, minY: -1, maxY: 1 };

/** Which tile sets to draw. Global tiles only exist after /fuse (set_run_fused follows them), so
 *  their presence stands in for run.fused: replay shows global instead of local once fused;
 *  live shows local tiles, plus global ones if the run is already fused (docs/06 §2, §6). */
export function tileVisibility(mode: 'live' | 'replay', globalCount: number) {
  const fused = globalCount > 0;
  return { showGlobal: fused, showLocal: !(mode === 'replay' && fused) };
}

/** What the default view fits: all poses (docs/06 §6), else whatever else there is. */
export function defaultBounds(p: Pick<MapViewProps, 'poses' | 'localTiles' | 'candidates' | 'visits' | 'status'>): Bounds {
  return (
    boundsOf(p.poses) ??
    boundsOf([...p.localTiles, ...p.candidates, ...p.visits, ...(p.status ? [p.status] : [])]) ??
    EMPTY_BOUNDS
  );
}

export default function MapView(props: MapViewProps) {
  const { mode, poses, localTiles, globalTiles, candidates, visits, status, selectedCandId, onSelectCand } = props;
  const wrapRef = useRef<HTMLDivElement>(null);
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const [size, setSize] = useState({ w: 0, h: 0 });
  const [cam, setCam] = useState<Camera>();
  const [userMoved, setUserMoved] = useState(false);
  const images = useDecodedImages(localTiles, globalTiles);
  const vis = tileVisibility(mode, globalTiles.length);

  // Track the container size (CSS px).
  useEffect(() => {
    const el = wrapRef.current;
    if (!el) return;
    const ro = new ResizeObserver(([entry]) => {
      const { width, height } = entry.contentRect;
      setSize({ w: Math.floor(width), h: Math.floor(height) });
    });
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  // Auto-fit until the user pans/zooms; after that only follow resizes.
  const bounds = useMemo(
    () => defaultBounds({ poses, localTiles, candidates, visits, status }),
    [poses, localTiles, candidates, visits, status]
  );
  useEffect(() => {
    if (size.w === 0 || size.h === 0) return;
    setCam((prev) => (userMoved && prev ? resize(prev, size.w, size.h) : fitBounds(bounds, size.w, size.h)));
  }, [bounds, size, userMoved]);

  // Draw on the next animation frame whenever anything visible changes.
  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas || !cam || size.w === 0) return;
    const id = requestAnimationFrame(() => {
      const dpr = window.devicePixelRatio || 1;
      if (canvas.width !== size.w * dpr || canvas.height !== size.h * dpr) {
        canvas.width = size.w * dpr;
        canvas.height = size.h * dpr;
      }
      const ctx = canvas.getContext('2d');
      if (!ctx) return;
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0); // draw in CSS px, sharp on HiDPI screens
      ctx.clearRect(0, 0, size.w, size.h);
      grid.draw(ctx, cam, size.w, size.h);
      tiles.draw(ctx, cam, { local: localTiles, global: globalTiles, ...vis }, images.get);
      stations.draw(ctx, cam, localTiles);
      exclusion.draw(ctx, cam, visits);
      trail.draw(ctx, cam, { poses, status });
      pins.draw(ctx, cam, { candidates, selectedCandId });
    });
    return () => cancelAnimationFrame(id);
  }, [cam, size, localTiles, globalTiles, vis.showGlobal, vis.showLocal, visits, poses, status, candidates, selectedCandId, images]);

  // Wheel zoom needs a non-passive listener to stop the page from scrolling.
  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const onWheel = (e: WheelEvent) => {
      e.preventDefault();
      const r = canvas.getBoundingClientRect();
      setUserMoved(true);
      setCam((c) => (c ? zoomAt(c, e.clientX - r.left, e.clientY - r.top, Math.exp(-e.deltaY * 0.0015)) : c));
    };
    canvas.addEventListener('wheel', onWheel, { passive: false });
    return () => canvas.removeEventListener('wheel', onWheel);
  }, []);

  // Drag to pan; a press that barely moves is a click (pin selection).
  const drag = useRef<{ x: number; y: number; moved: number; id: number } | null>(null);
  const onPointerDown = useCallback((e: React.PointerEvent<HTMLCanvasElement>) => {
    e.currentTarget.setPointerCapture(e.pointerId);
    drag.current = { x: e.clientX, y: e.clientY, moved: 0, id: e.pointerId };
  }, []);
  const onPointerMove = useCallback((e: React.PointerEvent<HTMLCanvasElement>) => {
    const d = drag.current;
    if (!d || d.id !== e.pointerId) return;
    const dx = e.clientX - d.x;
    const dy = e.clientY - d.y;
    d.moved += Math.abs(dx) + Math.abs(dy);
    d.x = e.clientX;
    d.y = e.clientY;
    if (d.moved > CLICK_SLOP_PX) {
      setUserMoved(true);
      setCam((c) => (c ? panBy(c, dx, dy) : c));
    }
  }, []);
  const onPointerUp = useCallback(
    (e: React.PointerEvent<HTMLCanvasElement>) => {
      const d = drag.current;
      drag.current = null;
      if (!d || d.moved > CLICK_SLOP_PX || !cam) return;
      const r = e.currentTarget.getBoundingClientRect();
      const hit = pins.hitTest(cam, candidates, e.clientX - r.left, e.clientY - r.top);
      if (hit) onSelectCand(hit);
    },
    [cam, candidates, onSelectCand]
  );

  return (
    <div className="mapview" ref={wrapRef}>
      <canvas
        ref={canvasRef}
        className="mapview-canvas"
        style={{ width: size.w, height: size.h }}
        onPointerDown={onPointerDown}
        onPointerMove={onPointerMove}
        onPointerUp={onPointerUp}
        onPointerCancel={() => (drag.current = null)}
        onDoubleClick={() => setUserMoved(false)}
        aria-label="Mission map"
        role="img"
      />
      <div className="mapview-overlay">
        <span className="mapview-axis" title="Mission frame: +x up (start heading), +y left">+x ↑ · +y ←</span>
        {mode === 'replay' && vis.showGlobal && <span className="mapview-chip">fused map</span>}
        <button className="mapview-fit" onClick={() => setUserMoved(false)} disabled={!userMoved} title="Fit all poses (double-click the map)">
          Fit
        </button>
      </div>
    </div>
  );
}

// --- base64 tiles -> ImageBitmap, decoded once per row version (docs/06 §9 pitfalls) ---------

interface ImageCache {
  get: tiles.ImageLookup;
}

function useDecodedImages(localTiles: readonly LocalMapTile[], globalTiles: readonly GlobalTile[]): ImageCache {
  const cache = useRef(new Map<string, ImageBitmap | 'pending' | 'failed'>());
  const [tick, setTick] = useState(0);

  useEffect(() => {
    const wanted = new Map<string, string>();
    for (const t of localTiles) if (t.pngB64) wanted.set(tiles.localKey(t), t.pngB64);
    for (const t of globalTiles) if (t.pngB64) wanted.set(tiles.globalKey(t), t.pngB64);
    const map = cache.current;
    let alive = true;
    for (const [key, entry] of map) {
      if (!wanted.has(key)) {
        if (entry instanceof ImageBitmap) entry.close();
        map.delete(key);
      }
    }
    for (const [key, b64] of wanted) {
      if (map.has(key)) continue;
      map.set(key, 'pending');
      decodePng(b64)
        .then((bmp) => {
          if (map.get(key) === 'pending') map.set(key, bmp);
          else bmp.close();
          if (alive) setTick((n) => n + 1);
        })
        .catch(() => map.set(key, 'failed'));
    }
    return () => {
      alive = false;
    };
  }, [localTiles, globalTiles]);

  // A new object per decode tick so the draw effect re-runs.
  return useMemo(
    () => ({
      get: (key: string) => {
        const e = cache.current.get(key);
        return e instanceof ImageBitmap ? e : undefined;
      },
    }),
    [tick]
  );
}

async function decodePng(b64: string): Promise<ImageBitmap> {
  const bin = atob(b64);
  const bytes = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
  return createImageBitmap(new Blob([bytes], { type: 'image/png' }));
}
