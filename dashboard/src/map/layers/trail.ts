// Pose trail and rover glyph (docs/06 §6): polyline coloured by pose source
// (dr gray, aruco green, fused white); triangle at the latest pose.
import type { Pose, RoverStatus } from '../../module_bindings/types';
import { type Camera, headingToCanvasRad, worldToScreen } from '../camera';

export const SOURCE_COLORS: Record<string, string> = {
  aruco: '#4caf50',
  fused: '#ffffff',
};
export const DR_COLOR = '#8a8580'; // dr, dr+gyro, vo and anything unknown

export const sourceColor = (source: string) => SOURCE_COLORS[source] ?? DR_COLOR;

export interface TrailRows {
  poses: readonly Pose[];
  /** Used for the glyph only when there are no poses yet. */
  status?: RoverStatus;
}

/** Where to draw the rover: the latest pose, else the status row, else nothing. */
export function roverPose(rows: TrailRows): { x: number; y: number; headingDeg: number } | undefined {
  let best: Pose | undefined;
  for (const p of rows.poses) if (!best || p.tUnix > best.tUnix) best = p;
  if (best) return best;
  return rows.status ? { x: rows.status.x, y: rows.status.y, headingDeg: rows.status.headingDeg } : undefined;
}

export function draw(ctx: CanvasRenderingContext2D, cam: Camera, rows: TrailRows): void {
  const poses = [...rows.poses].sort((a, b) => a.tUnix - b.tUnix);
  ctx.save();
  ctx.lineWidth = 2;
  ctx.lineCap = 'round';
  for (let i = 1; i < poses.length; i++) {
    const [ax, ay] = worldToScreen(cam, poses[i - 1].x, poses[i - 1].y);
    const [bx, by] = worldToScreen(cam, poses[i].x, poses[i].y);
    ctx.strokeStyle = sourceColor(poses[i].source);
    ctx.beginPath();
    ctx.moveTo(ax, ay);
    ctx.lineTo(bx, by);
    ctx.stroke();
  }
  ctx.restore();

  const rover = roverPose(rows);
  if (rover) drawRover(ctx, cam, rover.x, rover.y, rover.headingDeg);
}

/** Triangle pointing along the heading; size fixed on screen so it stays visible at any zoom. */
export function drawRover(ctx: CanvasRenderingContext2D, cam: Camera, x: number, y: number, headingDeg: number): void {
  const [sx, sy] = worldToScreen(cam, x, y);
  ctx.save();
  ctx.translate(sx, sy);
  ctx.rotate(headingToCanvasRad(headingDeg));
  ctx.beginPath();
  ctx.moveTo(0, -12); // nose = image up = heading direction
  ctx.lineTo(8, 9);
  ctx.lineTo(0, 5);
  ctx.lineTo(-8, 9);
  ctx.closePath();
  ctx.fillStyle = '#e07a3f';
  ctx.strokeStyle = '#12100f';
  ctx.lineWidth = 1.5;
  ctx.fill();
  ctx.stroke();
  ctx.restore();
}
