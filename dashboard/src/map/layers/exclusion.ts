// Exclusion rings (docs/06 §6): dashed circle of radiusM at each visit.
import type { Visit } from '../../module_bindings/types';
import { type Camera, worldToScreen } from '../camera';

export const RING_COLOR = 'rgba(232, 225, 217, 0.7)';

export function draw(ctx: CanvasRenderingContext2D, cam: Camera, visits: readonly Visit[]): void {
  ctx.save();
  ctx.strokeStyle = RING_COLOR;
  ctx.lineWidth = 1.5;
  ctx.setLineDash([6, 4]);
  for (const v of visits) {
    const [sx, sy] = worldToScreen(cam, v.x, v.y);
    ctx.beginPath();
    ctx.arc(sx, sy, Math.max(0, v.radiusM) * cam.scale, 0, 2 * Math.PI);
    ctx.stroke();
  }
  ctx.restore();
}
