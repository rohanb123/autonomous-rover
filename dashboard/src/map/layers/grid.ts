// Background grid, home pad cross and scale bar (helps read distances; not in docs/06 §6 list).
import { type Camera, screenToWorld, worldToScreen } from '../camera';

const STEPS_M = [0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10];

/** Grid spacing (m) so that lines are at least `minPx` apart. */
export function gridStep(scale: number, minPx = 40): number {
  return STEPS_M.find((s) => s * scale >= minPx) ?? STEPS_M[STEPS_M.length - 1];
}

export function fmtMetres(m: number): string {
  return m < 1 ? `${Math.round(m * 100)} cm` : `${m} m`;
}

export function draw(ctx: CanvasRenderingContext2D, cam: Camera, width: number, height: number): void {
  const step = gridStep(cam.scale);
  // World extent of the canvas: screen corners map to x (vertical) and y (horizontal) ranges.
  const [xTop, yLeft] = screenToWorld(cam, 0, 0);
  const [xBottom, yRight] = screenToWorld(cam, width, height);
  ctx.save();
  ctx.lineWidth = 1;
  ctx.strokeStyle = 'rgba(232, 225, 217, 0.06)';
  ctx.beginPath();
  for (let x = Math.floor(xBottom / step) * step; x <= xTop; x += step) {
    const [, sy] = worldToScreen(cam, x, 0);
    ctx.moveTo(0, Math.round(sy) + 0.5);
    ctx.lineTo(width, Math.round(sy) + 0.5);
  }
  for (let y = Math.floor(yRight / step) * step; y <= yLeft; y += step) {
    const [sx] = worldToScreen(cam, 0, y);
    ctx.moveTo(Math.round(sx) + 0.5, 0);
    ctx.lineTo(Math.round(sx) + 0.5, height);
  }
  ctx.stroke();

  // Home pad: cross at the origin with an arrow along +x (the start heading).
  const [hx, hy] = worldToScreen(cam, 0, 0);
  ctx.strokeStyle = 'rgba(224, 122, 63, 0.8)';
  ctx.lineWidth = 2;
  ctx.beginPath();
  ctx.moveTo(hx - 8, hy);
  ctx.lineTo(hx + 8, hy);
  ctx.moveTo(hx, hy + 8);
  ctx.lineTo(hx, hy - 16);
  ctx.moveTo(hx - 4, hy - 12);
  ctx.lineTo(hx, hy - 16);
  ctx.lineTo(hx + 4, hy - 12);
  ctx.stroke();

  // Scale bar, bottom-left.
  const barPx = step * cam.scale;
  ctx.strokeStyle = '#e8e1d9';
  ctx.fillStyle = '#e8e1d9';
  ctx.lineWidth = 2;
  ctx.beginPath();
  ctx.moveTo(12, height - 14);
  ctx.lineTo(12 + barPx, height - 14);
  ctx.stroke();
  ctx.font = '12px ui-sans-serif, system-ui, sans-serif';
  ctx.textBaseline = 'bottom';
  ctx.fillText(fmtMetres(step), 12, height - 18);
  ctx.restore();
}
