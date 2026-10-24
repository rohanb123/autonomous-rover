import { describe, expect, it } from 'vitest';
import { crossed, rowsAt, runWindow } from '../stdb/scrub';
import { parseComponents, scoreBreakdown } from './replay';

describe('rowsAt', () => {
  const rows = [{ id: 'a', tUnix: 1 }, { id: 'b', tUnix: 2 }, { id: 'c', tUnix: 3 }];

  it('keeps every row in live mode', () => {
    expect(rowsAt(rows, undefined).map((r) => r.id)).toEqual(['a', 'b', 'c']);
  });

  it('hides rows after the scrub time', () => {
    expect(rowsAt(rows, 2).map((r) => r.id)).toEqual(['a', 'b']);
    expect(rowsAt(rows, 0)).toEqual([]);
  });
});

describe('runWindow', () => {
  it('ends at the run end when that is later than the rows', () => {
    expect(runWindow(10, 40, [12, 18])).toEqual({ start: 10, end: 40 });
  });

  it('extends through a row written after endedUnix', () => {
    expect(runWindow(10, 20, [15, 25])).toEqual({ start: 10, end: 25 });
  });

  it('treats an open run (endedUnix < 0) as ending at the latest row', () => {
    expect(runWindow(10, -1, [12, 16])).toEqual({ start: 10, end: 16 });
  });
});

describe('crossed', () => {
  const samples = [{ candId: 'c_0001', tUnix: 5 }, { candId: 'c_0002', tUnix: 8 }];

  it('returns the latest sample entered while playing forward', () => {
    expect(crossed(samples, 4, 9)?.candId).toBe('c_0002');
    expect(crossed(samples, 5, 7)?.candId).toBeUndefined();
  });

  it('does not fire when scrubbing backward', () => {
    expect(crossed(samples, 9, 4)).toBeUndefined();
  });
});

describe('scoreBreakdown', () => {
  it('formats the doc 00 weights and skips a bad blob', () => {
    const text = scoreBreakdown(parseComponents(
      '{"novelty":0.64,"visual":0.71,"pickable":0.9,"category_score":0.55,"dist_norm":0.4}',
    ));
    expect(text).toBe('nov 0.64×0.35 + vis 0.71×0.25 + pick 0.90×0.15 + cat 0.55×0.15 − dist 0.40×0.10');
    expect(scoreBreakdown(parseComponents('nope'))).toBe('');
  });
});
