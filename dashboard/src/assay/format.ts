// Bradford readout formatting and chart geometry (docs/06 §7).
// The browser never re-simulates: every number comes from the analysis row.

export const SIMULATED_BANNER = 'SIMULATED — proof of concept';
export const CUVETTE_CAPTION =
  'Coomassie G-250: 465/470 nm (red-brown) → 595 nm (blue) on protein binding';
export const CURVE_CITATION =
  'Parameters: 595 nm, BSA standards, linear range per Thermo Fisher MAN0011203 / Bio-Rad 4110065A';

export const DYE_BROWN = '#8B5A2B';
export const DYE_BLUE = '#1F3FBF';
export const X_MAX = 2000;

export const VIEW = { w: 320, h: 180, l: 36, r: 8, t: 14, b: 26 };

export type Standard = { c: number; a595: number[] };

export type Curve = {
  slope: number;
  intercept: number;
  r2: number;
  fitRange: [number, number];
  linearRange: [number, number];
  standards: Standard[];
};

type RawStandard = { c?: unknown; a595?: unknown };
type RawCurve = {
  linear_range?: unknown;
  fit?: { slope?: unknown; intercept?: unknown; r2?: unknown; fit_range?: unknown };
  standards?: RawStandard[];
};

function pair(value: unknown): [number, number] | undefined {
  if (!Array.isArray(value) || value.length < 2) return undefined;
  const a = value[0];
  const b = value[1];
  if (typeof a !== 'number' || typeof b !== 'number') return undefined;
  return [a, b];
}

export function parseCurve(json: string): Curve | undefined {
  let raw: RawCurve;
  try {
    raw = JSON.parse(json) as RawCurve;
  } catch {
    return undefined;
  }
  const slope = raw.fit?.slope;
  const intercept = raw.fit?.intercept;
  const r2 = raw.fit?.r2;
  if (typeof slope !== 'number' || typeof intercept !== 'number' || typeof r2 !== 'number') return undefined;
  const standards: Standard[] = [];
  for (const row of raw.standards ?? []) {
    if (typeof row.c !== 'number' || !Array.isArray(row.a595)) continue;
    const a595 = row.a595.filter((n): n is number => typeof n === 'number');
    if (a595.length > 0) standards.push({ c: row.c, a595 });
  }
  const fitRange = pair(raw.fit?.fit_range) ?? [125, 1000];
  const linearRange = pair(raw.linear_range) ?? fitRange;
  return { slope, intercept, r2, fitRange, linearRange, standards };
}

/** 0 decimals at or above 100 µg/mL, 1 decimal below (docs/06 §7). */
export function formatConcentration(concentration: number): string {
  return concentration >= 100 ? concentration.toFixed(0) : concentration.toFixed(1);
}

/** A595 to 4 decimals. */
export function formatA595(a595: number): string {
  return a595.toFixed(4);
}

export function formatVerdict(verdict: string): string {
  return verdict.replaceAll('_', ' ').toUpperCase();
}

export function formatSeed(seed: number | bigint): string {
  return seed.toString();
}

export function formatFit(curve: Curve): string {
  const sign = curve.intercept < 0 ? '−' : '+';
  const intercept = Math.abs(curve.intercept);
  return `y = ${curve.slope}·x ${sign} ${intercept}, R² = ${curve.r2}`;
}

function clamp01(t: number): number {
  return Math.min(1, Math.max(0, t));
}

function easeOut(t: number): number {
  const u = clamp01(t);
  return 1 - (1 - u) ** 3;
}

function hexToRgb(hex: string): [number, number, number] {
  const n = Number.parseInt(hex.slice(1), 16);
  return [(n >> 16) & 255, (n >> 8) & 255, n & 255];
}

export function lerpHex(from: string, to: string, t: number): string {
  const a = hexToRgb(from);
  const b = hexToRgb(to);
  const u = clamp01(t);
  const rgb = a.map((channel, i) => Math.round(channel + (b[i] - channel) * u));
  return `#${rgb.map((channel) => channel.toString(16).padStart(2, '0')).join('')}`;
}

/** Final liquid color. t = clamp(a595 / 1.2, 0, 1), then ease-out into the brown→blue mix. */
export function liquidColor(a595: number): string {
  return lerpHex(DYE_BROWN, DYE_BLUE, easeOut(clamp01(a595 / 1.2)));
}

export function plotYMax(standards: Standard[], a595: number, slope: number, intercept: number): number {
  const values = standards.flatMap((row) => row.a595);
  values.push(a595, slope * X_MAX + intercept, 0);
  const peak = Math.max(0.2, ...values);
  return peak * 1.1;
}

export function xOf(concentration: number): number {
  const plotW = VIEW.w - VIEW.l - VIEW.r;
  return VIEW.l + (concentration / X_MAX) * plotW;
}

export function yOf(a595: number, yMax: number): number {
  const plotH = VIEW.h - VIEW.t - VIEW.b;
  return VIEW.t + (1 - a595 / yMax) * plotH;
}

export function mean(values: number[]): number {
  return values.reduce((sum, value) => sum + value, 0) / values.length;
}
