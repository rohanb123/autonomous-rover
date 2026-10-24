// Ground texture for the 3D map: every visible photo tile composited into one image in mission
// coordinates, then draped over the terrain. Placement reuses layers/tiles.ts (the same code and
// tests as the 2D map), so a tile lands in exactly the same place and orientation in both views.
import type { GlobalTile, LocalMapTile } from '../../module_bindings/types';
import type { Bounds, Camera } from '../camera';
import * as tiles from '../layers/tiles';
import type { Rect } from './geometry';

/** Largest texture we build (px per side); bigger areas get a coarser metres-per-pixel. */
export const MAX_TEX_PX = 4096;
/** Transparent border so clamped sampling outside the photos stays transparent. */
const BORDER_PX = 2;

/** Mission-frame corners of a local tile (rotated square centred on its station). */
export function localTileCorners(t: LocalMapTile): [number, number][] {
  const hx = (t.hPx * t.resMPx) / 2; // along rover +x (image up)
  const hy = (t.wPx * t.resMPx) / 2; // along rover +y (image left)
  const a = (t.headingDeg * Math.PI) / 180;
  const c = Math.cos(a);
  const s = Math.sin(a);
  return [
    [hx, hy],
    [hx, -hy],
    [-hx, -hy],
    [-hx, hy],
  ].map(([u, v]) => [t.x + u * c - v * s, t.y + u * s + v * c]);
}

/** Mission-frame extent of a global tile (origin = top-left pixel corner = max x, max y). */
export function globalTileRect(t: GlobalTile): Rect {
  return { maxX: t.originX, minX: t.originX - t.hPx * t.resMPx, maxY: t.originY, minY: t.originY - t.wPx * t.resMPx };
}

/** Area covered by the photo tiles that will be drawn, or undefined if none. */
export function photoExtent(local: readonly LocalMapTile[], global: readonly GlobalTile[]): Rect | undefined {
  let r: Rect | undefined;
  const add = (x: number, y: number) => {
    if (!Number.isFinite(x) || !Number.isFinite(y)) return;
    if (!r) r = { minX: x, maxX: x, minY: y, maxY: y };
    else {
      r.minX = Math.min(r.minX, x);
      r.maxX = Math.max(r.maxX, x);
      r.minY = Math.min(r.minY, y);
      r.maxY = Math.max(r.maxY, y);
    }
  };
  for (const t of local) for (const [x, y] of localTileCorners(t)) add(x, y);
  for (const t of global) {
    const g = globalTileRect(t);
    add(g.minX, g.minY);
    add(g.maxX, g.maxY);
  }
  return r;
}

export interface GroundLayout {
  /** World rect the texture covers (including the transparent border). */
  rect: Rect;
  /** Metres per texture pixel. */
  res: number;
  /** Texture size: width runs along −y (image left = +y), height along −x (image up = +x). */
  wPx: number;
  hPx: number;
}

/** Texture layout for `extent`: native tile resolution when it fits, else coarser. */
export function groundLayout(extent: Rect, nativeRes: number, maxPx = MAX_TEX_PX): GroundLayout {
  const spanX = extent.maxX - extent.minX;
  const spanY = extent.maxY - extent.minY;
  const res = Math.max(nativeRes, Math.max(spanX, spanY) / (maxPx - 2 * BORDER_PX));
  const pad = BORDER_PX * res;
  const rect = { minX: extent.minX - pad, maxX: extent.maxX + pad, minY: extent.minY - pad, maxY: extent.maxY + pad };
  return {
    rect,
    res,
    wPx: Math.max(1, Math.ceil((rect.maxY - rect.minY) / res)),
    hPx: Math.max(1, Math.ceil((rect.maxX - rect.minX) / res)),
  };
}

/** 2D-map camera that maps world → texture pixels for `layout` (top-left pixel = max x, max y). */
export function textureCamera(layout: GroundLayout): Camera {
  return { x0: layout.rect.maxX, y0: layout.rect.maxY, scale: 1 / layout.res, cx: 0, cy: 0 };
}

/** Texture coordinate (0..1) of a world point, matching textureCamera(). */
export function worldToUv(layout: GroundLayout, x: number, y: number): [number, number] {
  return [(layout.rect.maxY - y) / (layout.wPx * layout.res), (layout.rect.maxX - x) / (layout.hPx * layout.res)];
}

export interface GroundImage {
  canvas: HTMLCanvasElement;
  layout: GroundLayout;
}

/** Composite the visible tiles (same rules as the 2D map: global under local, newest on top). */
export function composeGround(rows: tiles.TileRows, images: tiles.ImageLookup, maxPx = MAX_TEX_PX): GroundImage | undefined {
  const local = rows.showLocal ? rows.local : [];
  const global = rows.showGlobal ? rows.global : [];
  const extent = photoExtent(local, global);
  if (!extent) return undefined;
  const resolutions = [...local, ...global].map((t) => t.resMPx).filter((r) => r > 0);
  const layout = groundLayout(extent, resolutions.length ? Math.min(...resolutions) : 0.005, maxPx);
  const canvas = document.createElement('canvas');
  canvas.width = layout.wPx;
  canvas.height = layout.hPx;
  const ctx = canvas.getContext('2d');
  if (!ctx) return undefined;
  ctx.imageSmoothingQuality = 'high';
  tiles.draw(ctx, textureCamera(layout), rows, images);
  return { canvas, layout };
}

/** Bounds → Rect helper (same shape). */
export const rectOf = (b: Bounds): Rect => ({ minX: b.minX, maxX: b.maxX, minY: b.minY, maxY: b.maxY });
