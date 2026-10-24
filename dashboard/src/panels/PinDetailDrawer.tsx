import { useEffect, useState } from 'react';
import BradfordPanel from '../assay/BradfordPanel';
import type { AnalysisRecord, Candidate, Keyframe, Visit } from '../module_bindings/types';
import { parseComponents, scoreBreakdown } from './replay';

type Props = {
  candidate: Candidate;
  visit?: Visit;
  analysis?: AnalysisRecord;
  keyframe?: Keyframe;
  autoplay: boolean;
  onClose: () => void;
};

function Photo({ src, label }: { src: string; label: string }) {
  const [failed, setFailed] = useState(false);
  if (!src || failed) return <p className="empty">{label} unavailable</p>;
  return <img src={src} alt={label} onError={() => setFailed(true)} />;
}

// Pin detail (docs/06 §3, D6). Hosts BradfordPanel for a bradford_sim analysis.
export default function PinDetailDrawer({ candidate, visit, analysis, keyframe, autoplay, onClose }: Props) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [onClose]);

  const components = parseComponents(candidate.componentsJson);
  const formula = scoreBreakdown(components);
  const showAssay = analysis?.assayType === 'bradford_sim';

  return (
    <div className="drawer-backdrop" onClick={onClose}>
      <aside className="drawer" role="dialog" aria-label={`${candidate.candId} detail`} onClick={(e) => e.stopPropagation()}>
        <header className="drawer-head">
          <h2>
            {candidate.candId} · {candidate.category} · {candidate.status}
            {visit ? ` (${visit.outcome})` : ''}
          </h2>
          <button type="button" onClick={onClose} aria-label="Close">Close</button>
        </header>
        {analysis?.simulated && <p className="sim-chip">analysis: SIMULATED — proof of concept</p>}

        <div className="photos">
          <figure>
            <Photo src={candidate.thumbUrl} label="Crop photo" />
            <figcaption>Crop</figcaption>
          </figure>
          <figure>
            <Photo src={analysis?.imageUrl ?? ''} label="Presented sample" />
            <figcaption>Presented sample</figcaption>
          </figure>
        </div>
        {keyframe && (
          <figure className="keyframe">
            <img src={`data:image/jpeg;base64,${keyframe.thumbB64}`} alt={`keyframe ${keyframe.kfId}`} />
            <figcaption>Station keyframe {keyframe.kfId}</figcaption>
          </figure>
        )}

        <p className="score">
          score {candidate.score.toFixed(2)}
          {formula && <span className="breakdown"> = {formula}</span>}
        </p>
        {components?.distance_m !== undefined && (
          <p className="meta">{components.distance_m.toFixed(1)} m away · size {(candidate.sizeM * 100).toFixed(1)} cm</p>
        )}
        <p className="reason">{candidate.reason}</p>

        {analysis ? (
          <section>
            <h3>Analysis</h3>
            <p>
              {analysis.label || '—'}
              {analysis.scoopHasItem ? ` (${analysis.confidence.toFixed(2)})` : ' · empty scoop'}
            </p>
            {analysis.caption && <p className="meta">{analysis.caption}</p>}
          </section>
        ) : <p className="empty">No analysis for this rock.</p>}

        {showAssay && analysis && <BradfordPanel record={analysis} autoplay={autoplay} />}
      </aside>
    </div>
  );
}
