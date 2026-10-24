// Map tiles (docs/06 §6).
// Local tile: BEV in the rover frame at capture, image up = rover +x, image left = rover +y
//   (docs/00 §4). Drawn centred at the station (x, y), rotated by headingDeg, size wPx·resMPx m,
//   newest on top, opacity 0.85.
// Global tile: mission frame, axis-aligned, image up = +x, image left = +y; (originX, originY) is
//   the mission position of the top-left pixel corner (docs/06 §6 convention).
import type { GlobalTile, LocalMapTile } from '../../module_bindings/types';
import { type Camera, headingToCanvasRad, worldToScreen } from '../camera';

export const LOCAL_OPACITY = 0.85;

/** Decoded image for a cache key, or undefined while it's still decoding. */
export type ImageLookup = (key: string) => CanvasImageSource | undefined;

// Keys change whenever the pixels can change (re-upload of a station, re-fuse bumps version).
export const localKey = (t: LocalMapTile) => `L:${t.stationId}:${t.tUnix}:${t.pngB64.length}`;
export const globalKey = (t: GlobalTile) => `G:${t.tileId}:${t.version}`;

export interface LocalPlacement {
  /** Screen position of the tile centre. */
  sx: number;
  sy: number;
  /** Canvas rotation (radians, clockwise-positive). */
  rot: number;
  /** On-screen size (CSS px). */
  w: number;
  h: number;
}

export function localTilePlacement(cam: Camera, t: LocalMapTile): LocalPlacement {
  const [sx, sy] = worldToScreen(cam, t.x, t.y);
  return {
    sx,
    sy,
    rot: headingToCanvasRad(t.headingDeg),
    w: t.wPx * t.resMPx * cam.scale,
    h: t.hPx * t.resMPx * cam.scale,
  };
}

export interface GlobalPlacement {
  /** Screen position of the top-left pixel corner. */
  sx: number;
  sy: number;
  w: number;
  h: number;
}

export function globalTilePlacement(cam: Camera, t: GlobalTile): GlobalPlacement {
  // Pixel (col, row) -> x = originX − row·res, y = originY − col·res. On screen +x is up and +y
  // is left, so columns run right and rows run down: the image is drawn unrotated.
  const [sx, sy] = worldToScreen(cam, t.originX, t.originY);
  return { sx, sy, w: t.wPx * t.resMPx * cam.scale, h: t.hPx * t.resMPx * cam.scale };
}

export interface TileRows {
  local: readonly LocalMapTile[];
  global: readonly GlobalTile[];
  showLocal: boolean;
  showGlobal: boolean;
}

export function draw(ctx: CanvasRenderingContext2D, cam: Camera, rows: TileRows, images: ImageLookup): void {
  if (rows.showGlobal) {
    for (const t of rows.global) {
      const p = globalTilePlacement(cam, t);
      const img = images(globalKey(t));
      if (img) ctx.drawImage(img, p.sx, p.sy, p.w, p.h);
      else placeholder(ctx, p.sx, p.sy, p.w, p.h);
    }
  }
  if (rows.showLocal) {
    const ordered = [...rows.local].sort((a, b) => a.tUnix - b.tUnix); // newest drawn last = on top
    for (const t of ordered) {
      const p = localTilePlacement(cam, t);
      ctx.save();
      ctx.translate(p.sx, p.sy);
      ctx.rotate(p.rot);
      const img = images(localKey(t));
      if (img) {
        ctx.globalAlpha = LOCAL_OPACITY;
        ctx.drawImage(img, -p.w / 2, -p.h / 2, p.w, p.h);
      } else {
        placeholder(ctx, -p.w / 2, -p.h / 2, p.w, p.h);
      }
      ctx.restore();
    }
  }
}

function placeholder(ctx: CanvasRenderingContext2D, x: number, y: number, w: number, h: number): void {
  ctx.save();
  ctx.strokeStyle = 'rgba(255, 255, 255, 0.18)';
  ctx.setLineDash([4, 4]);
  ctx.strokeRect(x, y, w, h);
  ctx.restore();
}
