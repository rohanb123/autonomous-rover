// Replay time window (docs/06 §2, D5). Live passes scrubT = undefined and keeps every row.

export type Timed = { tUnix: number };

/** Rows with tUnix ≤ scrubT. Undefined scrubT means live: nothing is hidden. */
export function rowsAt<R extends Timed>(rows: readonly R[], scrubT: number | undefined): R[] {
  if (scrubT === undefined) return [...rows];
  return rows.filter((r) => r.tUnix <= scrubT);
}

/** Scrubber range: run start through the later of the run end and any row time. */
export function runWindow(startedUnix: number, endedUnix: number, times: readonly number[]): { start: number; end: number } {
  const start = startedUnix > 0 ? startedUnix : 0;
  let end = endedUnix > start ? endedUnix : start;
  for (const t of times) if (t > end) end = t;
  return { start, end };
}

/** The latest row whose time was crossed while playing from `from` up to `to`. */
export function crossed<R extends Timed>(rows: readonly R[], from: number, to: number): R | undefined {
  let hit: R | undefined;
  for (const row of rows) {
    if (row.tUnix > from && row.tUnix <= to && (!hit || row.tUnix >= hit.tUnix)) hit = row;
  }
  return hit;
}
