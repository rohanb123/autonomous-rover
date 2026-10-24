import {
  CURVE_CITATION,
  SIMULATED_BANNER,
  VIEW,
  X_MAX,
  type Curve,
  formatFit,
  mean,
  plotYMax,
  xOf,
  yOf,
} from './format';

type Props = { curve: Curve; a595: number; concentration: number };

export default function StandardCurveChart({ curve, a595, concentration }: Props) {
  const yMax = plotYMax(curve.standards, a595, curve.slope, curve.intercept);
  const [fit0, fit1] = curve.fitRange;
  const [lin0, lin1] = curve.linearRange;
  const sample = { x: xOf(concentration), y: yOf(a595, yMax) };
  const axisY = yOf(0, yMax);
  const solid = segment(curve.slope, curve.intercept, fit0, fit1, yMax);
  const before = segment(curve.slope, curve.intercept, 0, fit0, yMax);
  const after = segment(curve.slope, curve.intercept, fit1, X_MAX, yMax);
  const yTicks = [0, yMax / 2, yMax];

  return (
    <figure className="curve">
      <figcaption>Standard curve · {SIMULATED_BANNER}</figcaption>
      <svg viewBox={`0 0 ${VIEW.w} ${VIEW.h}`} role="img" aria-label={`Standard curve ${SIMULATED_BANNER}`}>
        <title>Standard curve · {SIMULATED_BANNER}</title>
        <rect
          x={xOf(lin0)}
          y={VIEW.t}
          width={Math.max(0, xOf(lin1) - xOf(lin0))}
          height={VIEW.h - VIEW.t - VIEW.b}
          className="curve-band"
        />
        <line x1={VIEW.l} y1={axisY} x2={VIEW.w - VIEW.r} y2={axisY} className="curve-axis" />
        <line x1={VIEW.l} y1={VIEW.t} x2={VIEW.l} y2={axisY} className="curve-axis" />
        {yTicks.map((tick) => (
          <text key={tick} x={VIEW.l - 4} y={yOf(tick, yMax) + 3} className="curve-tick" textAnchor="end">
            {tick.toFixed(tick === 0 ? 0 : 2)}
          </text>
        ))}
        {[0, 1000, 2000].map((tick) => (
          <text key={tick} x={xOf(tick)} y={axisY + 14} className="curve-tick" textAnchor="middle">
            {tick}
          </text>
        ))}
        <path d={before} className="curve-ext" />
        <path d={after} className="curve-ext" />
        <path d={solid} className="curve-fit" />
        {curve.standards.map((row) => (
          <g key={row.c}>
            {row.a595.map((reading, i) => (
              <circle key={i} cx={xOf(row.c)} cy={yOf(reading, yMax)} r="1.6" className="curve-rep" />
            ))}
            <circle cx={xOf(row.c)} cy={yOf(mean(row.a595), yMax)} r="2.8" className="curve-mean" />
          </g>
        ))}
        <line x1={VIEW.l} y1={sample.y} x2={sample.x} y2={sample.y} className="curve-guide" />
        <line x1={sample.x} y1={sample.y} x2={sample.x} y2={axisY} className="curve-guide" />
        <circle cx={sample.x} cy={sample.y} r="3.5" className="curve-sample" />
      </svg>
      <p className="curve-fit-eq">{formatFit(curve)}</p>
      <p className="curve-cite">{CURVE_CITATION}</p>
    </figure>
  );
}

function segment(slope: number, intercept: number, x0: number, x1: number, yMax: number): string {
  const y0 = slope * x0 + intercept;
  const y1 = slope * x1 + intercept;
  return `M ${xOf(x0)} ${yOf(y0, yMax)} L ${xOf(x1)} ${yOf(y1, yMax)}`;
}
