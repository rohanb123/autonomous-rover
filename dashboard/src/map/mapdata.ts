// Shared MapView data helpers (P3): props contract, tile visibility, default bounds, next stop.
// Pure functions only, so both renderers (3D WebGL and the classic 2D canvas) and the tests use them.
import type { Candidate, GlobalTile, LocalMapTile, Pose, RoverStatus, Visit } from '../module_bindings/types';
import { type Bounds, boundsOf } from './camera';

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

export const EMPTY_BOUNDS: Bounds = { minX: -1, maxX: 1, minY: -1, maxY: 1 };

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

/** Where the rover is heading next, from what the map already receives (docs/00 §7):
 *  - RETURN: the home pad (0, 0).
 *  - SURVEY, IDLE, DONE, STOPPED, ESTOP: none (the next station isn't in the map's rows, and a
 *    stopped rover isn't heading anywhere).
 *  - otherwise (INVESTIGATE, PAUSED, PLAN, AWAIT_APPROVAL, or no status row): the candidate being
 *    approached (status "active", else the status row's activeCandId while it's still queued),
 *    else the top-ranked queued candidate. */
export type NextStop = { kind: 'home'; x: number; y: number } | { kind: 'cand'; x: number; y: number; cand: Candidate };

const NO_NEXT_STOP = new Set(['SURVEY', 'IDLE', 'DONE', 'STOPPED', 'ESTOP']);

export function nextStop(candidates: readonly Candidate[], status?: RoverStatus): NextStop | undefined {
  const fsm = status?.fsmState ?? '';
  if (status?.estop) return undefined;
  if (fsm === 'RETURN') return { kind: 'home', x: 0, y: 0 };
  if (NO_NEXT_STOP.has(fsm)) return undefined;
  const pick =
    candidates.find((c) => c.status === 'active') ??
    candidates.find((c) => c.candId === status?.activeCandId && c.status === 'queued') ??
    [...candidates].filter((c) => c.status === 'queued' && c.rank > 0).sort((a, b) => a.rank - b.rank)[0];
  return pick ? { kind: 'cand', x: pick.x, y: pick.y, cand: pick } : undefined;
}
