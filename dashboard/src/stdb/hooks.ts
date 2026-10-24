// Subscriptions (docs/02 §9). Global tables are always on; per-run tables re-subscribe
// when the selected run changes. Live and replay use the same subscriptions; replay
// filters client-side with useScrubbed.
import { useMemo } from 'react';
import { useTable } from 'spacetimedb/react';
import { tables } from '../module_bindings';
import { rowsAt, type Timed } from './scrub';

export function useGlobalTables() {
  const [runs, runsReady] = useTable(tables.run);
  const [statuses] = useTable(tables.roverStatus);
  return { runs, runsReady, roverStatus: statuses[0] };
}

export function useRunTables(runId: string | undefined) {
  const id = runId ?? '';
  const opts = { enabled: Boolean(runId) };
  const [missions] = useTable(tables.mission.where((r) => r.runId.eq(id)), opts);
  const [tasks] = useTable(tables.taskQueue.where((r) => r.runId.eq(id)), opts);
  const [poses] = useTable(tables.pose.where((r) => r.runId.eq(id)), opts);
  const [keyframes] = useTable(tables.keyframe.where((r) => r.runId.eq(id)), opts);
  const [localTiles] = useTable(tables.localMapTile.where((r) => r.runId.eq(id)), opts);
  const [globalTiles] = useTable(tables.globalTile.where((r) => r.runId.eq(id)), opts);
  const [candidates] = useTable(tables.candidate.where((r) => r.runId.eq(id)), opts);
  const [visits] = useTable(tables.visit.where((r) => r.runId.eq(id)), opts);
  const [analyses] = useTable(tables.analysisRecord.where((r) => r.runId.eq(id)), opts);
  const [telemetry] = useTable(tables.telemetry.where((r) => r.runId.eq(id)), opts);
  const [events] = useTable(tables.event.where((r) => r.runId.eq(id)), opts);
  return { missions, tasks, poses, keyframes, localTiles, globalTiles, candidates, visits, analyses, telemetry, events };
}

export type RunTables = ReturnType<typeof useRunTables>;

/** Rows visible at scrub time `scrubT` (replay); every row when scrubT is undefined (live). */
export function useScrubbed<R extends Timed>(rows: readonly R[], scrubT: number | undefined): R[] {
  return useMemo(() => rowsAt(rows, scrubT), [rows, scrubT]);
}

/** The row with the largest tUnix, or undefined. */
export function latest<R extends { tUnix: number }>(rows: readonly R[]): R | undefined {
  let best: R | undefined;
  for (const r of rows) if (!best || r.tUnix > best.tUnix) best = r;
  return best;
}
