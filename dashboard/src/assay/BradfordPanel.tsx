import type { AnalysisRecord } from '../module_bindings/types';
import Cuvette from './Cuvette';
import StandardCurveChart from './StandardCurveChart';
import './assay.css';
import {
  SIMULATED_BANNER,
  X_MAX,
  formatA595,
  formatConcentration,
  formatSeed,
  formatVerdict,
  parseCurve,
} from './format';

export interface BradfordPanelProps {
  record: AnalysisRecord;
  autoplay: boolean;
}

const ZONES = [
  { label: 'below detection', cls: 'zone-below', width: (125 / X_MAX) * 100 },
  { label: 'low', cls: 'zone-low', width: ((500 - 125) / X_MAX) * 100 },
  { label: 'elevated', cls: 'zone-elevated', width: ((X_MAX - 500) / X_MAX) * 100 },
] as const;

// Simulated Bradford panel (docs/06 §7). Draws the analysis row; it does not recompute the assay.
export default function BradfordPanel({ record, autoplay }: BradfordPanelProps) {
  const curve = parseCurve(record.standardCurveParams);
  const marker = Math.min(100, Math.max(0, (record.concentration / X_MAX) * 100));

  return (
    <section className="bradford" aria-label="Bradford assay">
      <p className="bradford-banner">{SIMULATED_BANNER}</p>
      <div className="bradford-top">
        <Cuvette a595={record.a595} autoplay={autoplay} />
        <div className="bradford-readout">
          <p className="bradford-conc">
            {formatConcentration(record.concentration)} {record.units}
          </p>
          <p>A595 = {formatA595(record.a595)}</p>
          <p className="meta">seed {formatSeed(record.seed)}</p>
          <p className="bradford-verdict">{formatVerdict(record.verdict)}</p>
        </div>
      </div>
      {curve && (
        <StandardCurveChart curve={curve} a595={record.a595} concentration={record.concentration} />
      )}
      <div className="verdict-band" aria-label={`verdict ${formatVerdict(record.verdict)}`}>
        <div className="verdict-track">
          {ZONES.map((zone) => (
            <span key={zone.label} className={`zone ${zone.cls}`} style={{ width: `${zone.width}%` }} />
          ))}
          <span className="verdict-marker" style={{ left: `${marker}%` }} />
        </div>
        <div className="verdict-labels">
          {ZONES.map((zone) => (
            <span key={zone.label}>{zone.label}</span>
          ))}
        </div>
      </div>
      {!record.inLinearRange && (
        <p className="meta">outside linear range: a real lab would dilute and re-assay</p>
      )}
    </section>
  );
}
