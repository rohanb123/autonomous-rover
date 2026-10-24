// Classic 2D canvas renderer for MapView (P3, docs/06 §6). Used when WebGL2 isn't available, when
// the 3D renderer fails, or when the user picks "2D". Same rows, same rules as before the 3D view.
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { type Camera, fitBounds, panBy, resize, zoomAt } from './camera';
import { useDecodedImages } from './images';
import * as exclusion from './layers/exclusion';
import * as grid from './layers/grid';
import * as pins from './layers/pins';
import * as stations from './layers/stations';
import * as tiles from './layers/tiles';
import * as trail from './layers/trail';
import { type MapViewProps, defaultBounds, tileVisibility } from './mapdata';

const CLICK_SLOP_PX = 4;

export default function MapView2D(props: MapViewProps) {
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
