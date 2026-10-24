// 3D map renderer for MapView (P3). Same rows and rules as the 2D map (docs/06 §6), drawn with
// WebGL2: the ground photos are draped over a terrain mesh, the rover, pins and stations are lit
// 3D markers, and the camera orbits. Rows come in via props; nothing here talks to STDB.
//
// Controls: drag = pan, right-drag (or Shift/Ctrl-drag) = rotate/tilt, wheel/pinch = zoom,
// click = select a pin, double-click = reset the view.
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import type { Candidate } from '../module_bindings/types';
import { useDecodedImages } from './images';
import { fmtMetres } from './layers/grid';
import { STATUS_COLORS } from './layers/pins';
import { SOURCE_COLORS, DR_COLOR } from './layers/trail';
import { type MapViewProps, defaultBounds, tileVisibility } from './mapdata';
import type { Heightfield, Rect } from './gl/geometry';
import { MAX_TEX_PX, composeGround, rectOf } from './gl/ground';
import { DEFAULT_AZ, DEFAULT_EL, type Orbit, TOP_EL, fit, lerpOrbit, metresPerPx, pan, project, rotate, zoomAt } from './gl/orbit';
import { MapRenderer } from './gl/renderer';
import { type Scene, buildScene, hitTest } from './gl/scene';
import './map.css';

export interface MapView3DProps extends MapViewProps {
  /** Look straight down (+x up, +y left, like the 2D map) instead of the tilted view. */
  topDown: boolean;
  /** Ground heights; omit for flat ground (nothing in the data contract provides heights yet). */
  heightfield?: Heightfield;
  /** WebGL2 is missing or the context was lost: the parent falls back to the 2D map. */
  onFail(): void;
}

const CLICK_SLOP_PX = 4;
const FLY_MS = 650;
const MAX_DPR = 2;

interface Anim {
  from: Orbit;
  to: Orbit;
  start: number;
  dur: number;
}

export default function MapView3D(props: MapView3DProps) {
  const { mode, poses, localTiles, globalTiles, candidates, visits, status, selectedCandId, onSelectCand, topDown, heightfield, onFail } = props;
  const wrapRef = useRef<HTMLDivElement>(null);
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const labelsRef = useRef<HTMLDivElement>(null);
  const gizmoX = useRef<SVGGElement>(null);
  const gizmoY = useRef<SVGGElement>(null);
  const gridRef = useRef<HTMLSpanElement>(null);
  const renderer = useRef<MapRenderer>();
  const orbit = useRef<Orbit>();
  const anim = useRef<Anim | null>(null);
  const dirty = useRef(true);
  const scene = useRef<Scene>();
  const userMovedRef = useRef(false);
  const [userMoved, setUserMovedState] = useState(false);
  const [size, setSize] = useState({ w: 0, h: 0 });
  const [hover, setHover] = useState<{ candId: string; x: number; y: number }>();
  // Legend starts open on tall maps, collapsed on short ones, until the user toggles it.
  const [legendChoice, setLegendChoice] = useState<boolean>();
  const legendOpen = legendChoice ?? size.h >= 760;
  const images = useDecodedImages(localTiles, globalTiles);
  const vis = tileVisibility(mode, globalTiles.length);
  const failRef = useRef(onFail);
  failRef.current = onFail;

  const setUserMoved = useCallback((v: boolean) => {
    userMovedRef.current = v;
    setUserMovedState(v);
  }, []);

  // Rows the render loop reads (kept in a ref so drags don't re-render React).
  const rows = useRef({ poses, candidates, visits, stations: localTiles, status, selectedCandId, hoveredCandId: hover?.candId, heightfield });
  rows.current = { poses, candidates, visits, stations: localTiles, status, selectedCandId, hoveredCandId: hover?.candId, heightfield };
  useEffect(() => {
    dirty.current = true;
  });

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

  // WebGL renderer for the life of the component; any failure falls back to the 2D map.
  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    let r: MapRenderer;
    try {
      r = new MapRenderer(canvas);
    } catch (e) {
      console.warn('3D map unavailable, using the 2D map:', e);
      failRef.current();
      return;
    }
    renderer.current = r;
    dirty.current = true;
    const lost = (e: Event) => {
      e.preventDefault();
      failRef.current();
    };
    canvas.addEventListener('webglcontextlost', lost);
    return () => {
      canvas.removeEventListener('webglcontextlost', lost);
      r.dispose();
      renderer.current = undefined;
    };
  }, []);

  const bounds = useMemo(
    () => defaultBounds({ poses, localTiles, candidates, visits, status }),
    [poses, localTiles, candidates, visits, status]
  );

  // Ground photos: recomposited only when the tiles, their decoded images or visibility change.
  const [photoRect, setPhotoRect] = useState<Rect>();
  useEffect(() => {
    const r = renderer.current;
    if (!r) return;
    const ground = composeGround({ local: localTiles, global: globalTiles, ...vis }, images.get, Math.min(MAX_TEX_PX, r.maxTextureSize));
    r.setGround(ground);
    setPhotoRect(ground?.layout.rect);
    dirty.current = true;
  }, [localTiles, globalTiles, vis.showGlobal, vis.showLocal, images]);

  // Terrain extent: rounded out to 0.5 m so new poses don't rebuild it every update.
  const content = useMemo(() => snapOut(union(rectOf(bounds), photoRect), 0.5), [bounds, photoRect]);
  const contentKey = `${content.minX},${content.maxX},${content.minY},${content.maxY}`;
  useEffect(() => {
    const r = renderer.current;
    if (!r) return;
    const span = Math.max(content.maxX - content.minX, content.maxY - content.minY);
    const pad = Math.max(6, span * 2);
    r.setTerrain({ minX: content.minX - pad, maxX: content.maxX + pad, minY: content.minY - pad, maxY: content.maxY + pad }, content, heightfield);
    dirty.current = true;
  }, [contentKey, heightfield]);

  const flyTo = useCallback((to: Orbit, ms = FLY_MS) => {
    const from = orbit.current;
    if (!from || ms <= 0) {
      orbit.current = to;
      anim.current = null;
    } else {
      anim.current = { from, to, start: performance.now(), dur: ms };
    }
    dirty.current = true;
  }, []);

  const fitView = useCallback(
    (ms?: number) => {
      if (size.w === 0 || size.h === 0) return;
      flyTo(fit(bounds, size.w, size.h, DEFAULT_AZ, topDown ? TOP_EL : DEFAULT_EL), ms);
    },
    [bounds, size, topDown, flyTo]
  );

  // Auto-fit until the user moves the camera (like the 2D map); animate later refits.
  const firstFit = useRef(true);
  useEffect(() => {
    if (size.w === 0 || size.h === 0) return;
    if (!userMovedRef.current) {
      fitView(firstFit.current ? 0 : FLY_MS);
      firstFit.current = false;
    }
  }, [bounds, size, fitView]);

  // Top-down / 3D switch keeps the user's framing when they have moved the camera.
  const prevTop = useRef(topDown);
  useEffect(() => {
    if (prevTop.current === topDown) return;
    prevTop.current = topDown;
    const o = orbit.current;
    if (o && userMovedRef.current) flyTo({ ...o, az: topDown ? DEFAULT_AZ : o.az, el: topDown ? TOP_EL : DEFAULT_EL });
  }, [topDown, flyTo]);

  // Render loop: only draws when something changed, or while something animates.
  useEffect(() => {
    dirty.current = true;
    let raf = 0;
    const labelEls = new Map<string, HTMLDivElement>();
    let lastGrid = -1;
    const frame = (now: number) => {
      raf = requestAnimationFrame(frame);
      const r = renderer.current;
      const canvas = canvasRef.current;
      if (!r || !canvas || size.w === 0 || size.h === 0 || !orbit.current) return;
      const a = anim.current;
      if (a) {
        const t = Math.min(1, (now - a.start) / a.dur);
        orbit.current = lerpOrbit(a.from, a.to, t);
        if (t >= 1) anim.current = null;
        dirty.current = true;
      }
      if (!dirty.current && !scene.current?.animating) return;
      dirty.current = false;
      const dpr = Math.min(MAX_DPR, window.devicePixelRatio || 1);
      const pw = Math.round(size.w * dpr);
      const ph = Math.round(size.h * dpr);
      if (canvas.width !== pw || canvas.height !== ph) {
        canvas.width = pw;
        canvas.height = ph;
      }
      const s = buildScene(rows.current, orbit.current, size.w, size.h, now / 1000);
      scene.current = s;
      r.render(s, pw, ph);

      // HTML labels (crisp text) at the projected positions.
      const box = labelsRef.current;
      if (box) {
        const seen = new Set<string>();
        for (const l of s.labels) {
          seen.add(l.key);
          let el = labelEls.get(l.key);
          if (!el) {
            el = document.createElement('div');
            el.className = `map3d-label map3d-label-${l.kind}`;
            box.appendChild(el);
            labelEls.set(l.key, el);
          }
          if (el.textContent !== l.text) el.textContent = l.text;
          el.style.transform = `translate(${l.x.toFixed(1)}px, ${l.y.toFixed(1)}px)`;
        }
        for (const [k, el] of labelEls)
          if (!seen.has(k)) {
            el.remove();
            labelEls.delete(k);
          }
      }

      // Orientation dial: screen directions of mission +x and +y at the target.
      const o = orbit.current;
      const c = project(s.view, [o.tx, o.ty, o.tz]);
      const step = o.dist * 0.2;
      for (const [ref, dx, dy] of [
        [gizmoX, step, 0],
        [gizmoY, 0, step],
      ] as const) {
        const p = project(s.view, [o.tx + dx, o.ty + dy, o.tz]);
        const ang = Math.atan2(p.y - c.y, p.x - c.x);
        // 1 = the axis lies in the screen plane; smaller = it points into/out of the screen.
        const fore = Math.min(1, Math.hypot(p.x - c.x, p.y - c.y) / (step / metresPerPx(o.dist, size.h)));
        ref.current?.setAttribute('transform', `rotate(${((ang * 180) / Math.PI).toFixed(1)}) scale(${(0.35 + 0.65 * fore).toFixed(3)} 1)`);
      }
      if (gridRef.current && s.gridStepM !== lastGrid) {
        lastGrid = s.gridStepM;
        gridRef.current.textContent = `grid ${fmtMetres(s.gridStepM)}`;
      }
    };
    raf = requestAnimationFrame(frame);
    return () => {
      cancelAnimationFrame(raf);
      for (const el of labelEls.values()) el.remove();
    };
  }, [size]);

  // --- input -------------------------------------------------------------------------------------

  const moveCamera = useCallback(
    (next: (o: Orbit) => Orbit) => {
      if (!orbit.current) return;
      anim.current = null;
      orbit.current = next(orbit.current);
      dirty.current = true;
      if (!userMovedRef.current) setUserMoved(true);
    },
    [setUserMoved]
  );

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const onWheel = (e: WheelEvent) => {
      e.preventDefault();
      const r = canvas.getBoundingClientRect();
      const f = Math.exp(-e.deltaY * (e.deltaMode === 1 ? 0.05 : 0.0015));
      moveCamera((o) => zoomAt(o, r.width, r.height, e.clientX - r.left, e.clientY - r.top, f));
    };
    canvas.addEventListener('wheel', onWheel, { passive: false });
    return () => canvas.removeEventListener('wheel', onWheel);
  }, [moveCamera]);

  const pointers = useRef(new Map<number, { x: number; y: number }>());
  const drag = useRef<{ moved: number; orbiting: boolean } | null>(null);
  const local = (e: React.PointerEvent) => {
    const r = e.currentTarget.getBoundingClientRect();
    return { x: e.clientX - r.left, y: e.clientY - r.top };
  };

  const onPointerDown = (e: React.PointerEvent<HTMLCanvasElement>) => {
    e.currentTarget.setPointerCapture(e.pointerId);
    pointers.current.set(e.pointerId, local(e));
    drag.current = { moved: 0, orbiting: e.button === 2 || e.shiftKey || e.ctrlKey || e.metaKey };
    setHover(undefined);
  };

  const onPointerMove = (e: React.PointerEvent<HTMLCanvasElement>) => {
    const p = local(e);
    const ps = pointers.current;
    const prev = ps.get(e.pointerId);
    if (!prev || !drag.current) {
      // Hover: tooltip for the pin under the cursor.
      const id = scene.current ? hitTest(scene.current.hits, p.x, p.y) : undefined;
      setHover((h) => (id ? { candId: id, x: p.x, y: p.y } : h ? undefined : h));
      return;
    }
    const d = drag.current;
    const w = size.w;
    const h = size.h;
    const other = ps.size >= 2 ? [...ps.entries()].find(([id]) => id !== e.pointerId)?.[1] : undefined;
    if (other) {
      // Pinch: zoom about the midpoint and pan with it.
      const before = Math.hypot(prev.x - other.x, prev.y - other.y);
      const after = Math.hypot(p.x - other.x, p.y - other.y);
      const mid = { x: (p.x + other.x) / 2, y: (p.y + other.y) / 2 };
      const midPrev = { x: (prev.x + other.x) / 2, y: (prev.y + other.y) / 2 };
      d.moved += CLICK_SLOP_PX + 1;
      moveCamera((o) => {
        const z = before > 0 ? zoomAt(o, w, h, mid.x, mid.y, after / before) : o;
        return pan(z, w, h, midPrev.x, midPrev.y, mid.x, mid.y);
      });
    } else {
      const dx = p.x - prev.x;
      const dy = p.y - prev.y;
      d.moved += Math.abs(dx) + Math.abs(dy);
      if (d.moved > CLICK_SLOP_PX) {
        if (d.orbiting) moveCamera((o) => rotate(o, -dx * 0.006, dy * 0.005));
        else moveCamera((o) => pan(o, w, h, prev.x, prev.y, p.x, p.y));
      }
    }
    ps.set(e.pointerId, p);
  };

  const onPointerUp = (e: React.PointerEvent<HTMLCanvasElement>) => {
    const ps = pointers.current;
    const wasSingle = ps.size === 1;
    ps.delete(e.pointerId);
    const d = drag.current;
    if (ps.size === 0) drag.current = null;
    if (!d || !wasSingle || d.moved > CLICK_SLOP_PX || !scene.current) return;
    const p = local(e);
    const hit = hitTest(scene.current.hits, p.x, p.y);
    if (hit) onSelectCand(hit);
  };

  const resetView = () => {
    setUserMoved(false);
    fitView();
  };

  const zoomButton = (f: number) => moveCamera((o) => zoomAt(o, size.w, size.h, size.w / 2, size.h / 2, f));
  const northUp = () => {
    const o = orbit.current;
    if (o) flyTo({ ...o, az: DEFAULT_AZ });
  };

  const hovered = hover ? candidates.find((c) => c.candId === hover.candId) : undefined;

  return (
    <div className="mapview map3d" ref={wrapRef}>
      <canvas
        ref={canvasRef}
        className={`mapview-canvas${hovered ? ' map3d-pointer' : ''}`}
        style={{ width: size.w, height: size.h }}
        onPointerDown={onPointerDown}
        onPointerMove={onPointerMove}
        onPointerUp={onPointerUp}
        onPointerCancel={(e) => {
          pointers.current.delete(e.pointerId);
          drag.current = null;
        }}
        onPointerLeave={() => setHover(undefined)}
        onDoubleClick={resetView}
        onContextMenu={(e) => e.preventDefault()}
        aria-label="Mission map (3D)"
        role="img"
      />
      <div className="map3d-labels" ref={labelsRef} aria-hidden="true" />
      {hovered && hover && <PinTooltip c={hovered} x={hover.x} y={hover.y} w={size.w} />}

      <div className="map3d-controls">
        {mode === 'replay' && vis.showGlobal && <span className="mapview-chip">fused map</span>}
        <div className="map3d-btngroup">
          <button onClick={() => zoomButton(1.4)} title="Zoom in" aria-label="Zoom in">+</button>
          <button onClick={() => zoomButton(1 / 1.4)} title="Zoom out" aria-label="Zoom out">−</button>
        </div>
        <button onClick={resetView} disabled={!userMoved} title="Fit all poses (double-click the map)">
          Fit
        </button>
      </div>

      <button className="map3d-dial" onClick={northUp} title="Mission frame axes. Click to face +x (start heading).">
        <svg viewBox="-24 -24 48 48" width="48" height="48" aria-hidden="true">
          <circle r="22" className="map3d-dial-ring" />
          <g ref={gizmoX}>
            <line x1="0" y1="0" x2="15" y2="0" className="map3d-dial-x" />
            <circle cx="17" cy="0" r="2.4" className="map3d-dial-xdot" />
          </g>
          <g ref={gizmoY}>
            <line x1="0" y1="0" x2="15" y2="0" className="map3d-dial-y" />
            <circle cx="17" cy="0" r="2.4" className="map3d-dial-ydot" />
          </g>
          <circle r="2" className="map3d-dial-hub" />
        </svg>
        <span className="map3d-dial-key">
          <i className="kx" /> +x <i className="ky" /> +y
        </span>
      </button>

      <span className="map3d-grid" ref={gridRef} />

      <div className={`map3d-legend${legendOpen ? '' : ' closed'}`}>
        <button className="map3d-legend-toggle" onClick={() => setLegendChoice(!legendOpen)} aria-expanded={legendOpen}>
          Legend
        </button>
        {legendOpen && (
          <ul>
            <li><i className="sw rover" /> Rover (now)</li>
            <li><i className="sw pulse" /> Next stop</li>
            <li><i className="sw dot" style={{ background: STATUS_COLORS.queued }} /> Queued rock (rank #)</li>
            <li><i className="sw dot" style={{ background: STATUS_COLORS.active }} /> Being approached</li>
            <li><i className="sw dot" style={{ background: STATUS_COLORS.visited }} /> Visited</li>
            <li><i className="sw dot" style={{ background: STATUS_COLORS.new }} /> Candidate</li>
            <li><i className="sw dot" style={{ background: STATUS_COLORS.failed }} /> Failed</li>
            <li><i className="sw dot dim" /> Skipped / excluded</li>
            <li><i className="sw station" /> Survey station</li>
            <li><i className="sw ring" /> Exclusion zone</li>
            <li>
              <i className="sw line" style={{ background: DR_COLOR }} />
              <i className="sw line" style={{ background: SOURCE_COLORS.aruco }} />
              <i className="sw line" style={{ background: SOURCE_COLORS.fused }} /> Trail: DR · ArUco · fused
            </li>
            <li className="hint">Drag to pan · right-drag to rotate · scroll to zoom</li>
          </ul>
        )}
      </div>
    </div>
  );
}

function PinTooltip({ c, x, y, w }: { c: Candidate; x: number; y: number; w: number }) {
  const left = x > w - 220;
  return (
    <div className="map3d-tooltip" style={{ transform: `translate(${left ? x - 14 : x + 14}px, ${y + 12}px) translateX(${left ? '-100%' : '0'})` }}>
      <div className="t-head">
        <b>{c.candId}</b>
        <span className="t-status" style={{ color: STATUS_COLORS[c.status] ?? undefined }}>
          {c.status}
          {c.rank > 0 ? ` · #${c.rank}` : ''}
        </span>
      </div>
      <div className="t-row">
        {c.category.replace(/_/g, ' ')} · score {c.score.toFixed(2)} · {(c.sizeM * 100).toFixed(1)} cm
      </div>
    </div>
  );
}

function snapOut(r: Rect, step: number): Rect {
  return {
    minX: Math.floor(r.minX / step) * step,
    maxX: Math.ceil(r.maxX / step) * step,
    minY: Math.floor(r.minY / step) * step,
    maxY: Math.ceil(r.maxY / step) * step,
  };
}

function union(a: Rect, b?: Rect): Rect {
  if (!b) return a;
  return { minX: Math.min(a.minX, b.minX), maxX: Math.max(a.maxX, b.maxX), minY: Math.min(a.minY, b.minY), maxY: Math.max(a.maxY, b.maxY) };
}
