// Score breakdown shown in the pin drawer (docs/00 §8, docs/06 §3).

export type ScoreComponents = {
  novelty?: number;
  visual?: number;
  pickable?: number;
  category_score?: number;
  dist_norm?: number;
  distance_m?: number;
  size_m?: number;
};

/** Weights from docs/00 §8. dist_norm is stored positive; its weight is the penalty. */
export const SCORE_TERMS = [
  { key: 'novelty', weight: 0.35, label: 'nov' },
  { key: 'visual', weight: 0.25, label: 'vis' },
  { key: 'pickable', weight: 0.15, label: 'pick' },
  { key: 'category_score', weight: 0.15, label: 'cat' },
  { key: 'dist_norm', weight: -0.1, label: 'dist' },
] as const;

export function parseComponents(json: string): ScoreComponents | undefined {
  try {
    const value = JSON.parse(json) as unknown;
    if (!value || typeof value !== 'object') return undefined;
    return value as ScoreComponents;
  } catch {
    return undefined;
  }
}

/** "nov 0.64×0.35 + vis 0.71×0.25 − dist 0.40×0.10" */
export function scoreBreakdown(components: ScoreComponents | undefined): string {
  if (!components) return '';
  const parts: string[] = [];
  for (const term of SCORE_TERMS) {
    const value = components[term.key];
    if (typeof value !== 'number' || !Number.isFinite(value)) continue;
    const piece = `${term.label} ${value.toFixed(2)}×${Math.abs(term.weight).toFixed(2)}`;
    if (parts.length === 0) parts.push(piece);
    else parts.push(`${term.weight < 0 ? '−' : '+'} ${piece}`);
  }
  return parts.join(' ');
}
