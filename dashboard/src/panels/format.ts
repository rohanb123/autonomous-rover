// Small display helpers shared by the P4 panels.
export const fmtTime = (tUnix: number) =>
  tUnix > 0 ? new Date(tUnix * 1000).toLocaleTimeString([], { hour12: false }) : '—';
export const fmtNum = (v: number, digits = 2) => (Number.isFinite(v) && v >= 0 ? v.toFixed(digits) : '—');
export const fmtDeg = (v: number) => `${v.toFixed(0)}°`;
