// Survey stations (docs/06 §6): small square at each local_map_tile (x, y).
import type { LocalMapTile } from '../../module_bindings/types';
import { type Camera, worldToScreen } from '../camera';

export const STATION_PX = 7;

export function draw(ctx: CanvasRenderingContext2D, cam: Camera, tiles: readonly LocalMapTile[]): void {
  ctx.save();
  ctx.fillStyle = '#e8e1d9';
  ctx.strokeStyle = '#12100f';
  ctx.lineWidth = 1;
  const h = STATION_PX / 2;
  for (const t of tiles) {
    const [sx, sy] = worldToScreen(cam, t.x, t.y);
    ctx.fillRect(sx - h, sy - h, STATION_PX, STATION_PX);
    ctx.strokeRect(sx - h, sy - h, STATION_PX, STATION_PX);
  }
  ctx.restore();
}
