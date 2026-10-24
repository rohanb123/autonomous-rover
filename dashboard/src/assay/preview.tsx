// Drawer + Bradford panel with the golden record (docs/00 §6.7). No STDB.
// cd dashboard && npm run dev, then open /src/assay/preview.html
import { StrictMode, useState } from 'react';
import { createRoot } from 'react-dom/client';
import type { AnalysisRecord, Candidate, Visit } from '../module_bindings/types';
import PinDetailDrawer from '../panels/PinDetailDrawer';
import '../styles.css';

const CURVE = {
  standard: 'BSA',
  wavelength_nm: 595,
  linear_range: [125.0, 1000.0],
  true_model: { type: 'hyperbolic', amax: 3.0, k: 3000.0, noise_sd: 0.01 },
  fit: { type: 'linear', slope: 0.0007188, intercept: 0.0557, r2: 0.9947, fit_range: [125.0, 1000.0] },
  blank_a595: -0.0068,
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
};

const candidate: Candidate = {
  key: 'r_20261024_101500|c_0007',
  candId: 'c_0007',
  runId: 'r_20261024_101500',
  stationId: 's_0002',
  x: 1.12,
  y: 0.21,
  score: 0.63,
  componentsJson: JSON.stringify({
    novelty: 0.64, visual: 0.71, pickable: 0.9, category_score: 0.55, dist_norm: 0.4, distance_m: 1.14,
  }),
  category: 'float_rock',
  sizeM: 0.028,
  status: 'visited',
  rank: 2,
  reason: "Novel texture (0.64), pickable size 2.8 cm, matches requested 'float_rock'; 1.1 m away.",
  thumbUrl: '',
  tUnix: 0,
};

const visit: Visit = {
  key: 'r_20261024_101500|v_0001',
  visitId: 'v_0001',
  runId: candidate.runId,
  candId: candidate.candId,
  x: 1.12,
  y: 0.21,
  radiusM: 0.4,
  outcome: 'sampled',
  tUnix: 1,
};

const analysis: AnalysisRecord = {
  id: 1n,
  runId: candidate.runId,
  key: 'r_20261024_101500|a_0001',
  sampleId: 'a_0001',
  candId: candidate.candId,
  visitId: visit.visitId,
  tUnix: 1,
  imageUrl: '',
  label: 'float_rock',
  confidence: 0.62,
  caption: 'A small angular dark rock fragment with a smooth face.',
  scoopHasItem: true,
  assayType: 'bradford_sim',
  a595: 0.2585,
  concentration: 282.1,
  units: 'µg/mL',
  verdict: 'low',
  inLinearRange: true,
  standardCurveParams: JSON.stringify(CURVE),
  simulated: true,
  seed: 3896889563,
  modelVersion: 'bradford_sim-1',
};

function Preview() {
  const [open, setOpen] = useState(true);
  const [autoplay, setAutoplay] = useState(true);
  return (
    <main style={{ padding: 16 }}>
      <p>Golden Bradford record (docs/00 §6.7).</p>
      <button type="button" onClick={() => { setAutoplay(true); setOpen(true); }}>Open with autoplay</button>
      {open && (
        <PinDetailDrawer
          candidate={candidate}
          visit={visit}
          analysis={analysis}
          autoplay={autoplay}
          onClose={() => setOpen(false)}
        />
      )}
    </main>
  );
}

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <Preview />
  </StrictMode>,
);
