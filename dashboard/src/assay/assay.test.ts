import { describe, expect, it } from 'vitest';
import {
  formatA595,
  formatConcentration,
  formatFit,
  formatVerdict,
  liquidColor,
  parseCurve,
  xOf,
} from './format';

// Golden vector: docs/00 §6.7, the real output of bradford_sim for
// run_id=r_20261024_101500, sample_id=a_0001, interest_score=0.63.
const GOLDEN_CURVE = JSON.stringify({
  standard: 'BSA',
  wavelength_nm: 595,
  linear_range: [125.0, 1000.0],
  fit: { type: 'linear', slope: 0.0007188, intercept: 0.0557, r2: 0.9947, fit_range: [125.0, 1000.0] },
  standards: [
    { c: 0, a595: [0.0073, -0.0063, -0.001] },
    { c: 25, a595: [0.0368, 0.0404, 0.0364] },
    { c: 125, a595: [0.1281, 0.1487, 0.134] },
    { c: 250, a595: [0.2412, 0.2217, 0.2208] },
    { c: 500, a595: [0.433, 0.4438, 0.4232] },
    { c: 750, a595: [0.6016, 0.6225, 0.6089] },
    { c: 1000, a595: [0.7483, 0.7649, 0.7555] },
    { c: 1500, a595: [1.0033, 1.0164, 1.0021] },
    { c: 2000, a595: [1.1919, 1.2138, 1.2149] },
  ],
});

describe('Bradford readout (docs/06 §7, D7)', () => {
  it('formats the golden concentration, A595 and verdict', () => {
    expect(formatConcentration(282.1)).toBe('282');
    expect(formatA595(0.2585)).toBe('0.2585');
    expect(formatVerdict('low')).toBe('LOW');
  });

  it('keeps one decimal below 100 µg/mL', () => {
    expect(formatConcentration(60.4)).toBe('60.4');
  });

  it('puts the sample dot on the fitted line between the 250 and 500 standards', () => {
    const curve = parseCurve(GOLDEN_CURVE);
    expect(curve).toBeDefined();
    if (!curve) return;
    const a595 = 0.2585;
    const concentration = 282.1;
    expect(curve.slope * concentration + curve.intercept).toBeCloseTo(a595, 3);
    const x = xOf(concentration);
    expect(x).toBeGreaterThan(xOf(250));
    expect(x).toBeLessThan(xOf(500));
    expect(formatFit(curve)).toBe('y = 0.0007188·x + 0.0557, R² = 0.9947');
  });

  it('is deterministic for the same absorbance', () => {
    expect(liquidColor(0.2585)).toBe(liquidColor(0.2585));
    expect(liquidColor(0)).toBe('#8b5a2b');
  });

  it('rejects a curve that is not JSON', () => {
    expect(parseCurve('not json')).toBeUndefined();
  });
});
