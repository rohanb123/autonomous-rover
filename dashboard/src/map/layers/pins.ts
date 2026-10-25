// Candidate pins (docs/06 §6): coloured by status, rank number inside top-k pins, size ∝ score,
// click hit-test within 12 px.
import type { Candidate } from '../../module_bindings/types';
import { type Camera, worldToScreen } from '../camera';

export const HIT_RADIUS_PX = 12;

export const STATUS_COLORS: Record<string, string> = {
  new: '#8a8a8a', // grey
  queued: '#ffffff', // white: in the top-k
  active: '#cfe6ff', // cold white-blue: being investigated
  visited: '#4a4a4a', // dim: done
  failed: '#c8693a', // muted Mars orange
};
const DIM = 'rgba(138, 138, 138, 0.3)'; // skipped, excluded, merged

export const statusColor = (status: string) => STATUS_COLORS[status] ?? DIM;
export const isDim = (status: string) => !(status in STATUS_COLORS);

/** Pin radius in CSS px: 5 px at score 0 up to 13 px at score 1. */
export const pinRadius = (score: number) => 5 + 8 * Math.min(1, Math.max(0, score));

export interface PinRows {
  candidates: readonly Candidate[];
  selectedCandId?: string;
}

export function draw(ctx: CanvasRenderingContext2D, cam: Camera, rows: PinRows): void {
  // Dim pins first, then by rank so ranked pins sit on top; the selected pin last.
  const order = [...rows.candidates].sort((a, b) => pinOrder(a, rows.selectedCandId) - pinOrder(b, rows.selectedCandId));
  for (const c of order) {
    const [sx, sy] = worldToScreen(cam, c.x, c.y);
    const r = pinRadius(c.score);
    ctx.save();
    ctx.beginPath();
    ctx.arc(sx, sy, r, 0, 2 * Math.PI);
    ctx.fillStyle = statusColor(c.status);
    ctx.fill();
    ctx.lineWidth = c.candId === rows.selectedCandId ? 3 : 1;
    ctx.strokeStyle = c.candId === rows.selectedCandId ? '#ffffff' : '#000000';
    ctx.stroke();
    if (c.rank > 0) {
      ctx.fillStyle = '#000000';
      ctx.font = `bold ${Math.max(9, Math.round(r))}px ui-sans-serif, system-ui, sans-serif`;
      ctx.textAlign = 'center';
      ctx.textBaseline = 'middle';
      ctx.fillText(String(c.rank), sx, sy + 0.5);
    }
    ctx.restore();
  }
}

function pinOrder(c: Candidate, selected?: string): number {
  if (c.candId === selected) return 1e6;
  if (isDim(c.status)) return 0;
  return c.rank > 0 ? 1000 - c.rank : 1;
}

/** candId of the nearest pin within HIT_RADIUS_PX (or the pin's own radius if larger). */
export function hitTest(cam: Camera, candidates: readonly Candidate[], sx: number, sy: number): string | undefined {
  let best: string | undefined;
  let bestD = Infinity;
  for (const c of candidates) {
    const [px, py] = worldToScreen(cam, c.x, c.y);
    const d = Math.hypot(px - sx, py - sy);
    if (d <= Math.max(HIT_RADIUS_PX, pinRadius(c.score)) && d < bestD) {
      best = c.candId;
      bestD = d;
    }
  }
  return best;
}
