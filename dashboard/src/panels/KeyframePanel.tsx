import type { Keyframe } from '../module_bindings/types';
import { latest } from '../stdb/hooks';
import { fmtDeg, fmtTime } from './format';

// Latest keyframe thumbnail; click opens the full-res url when the mapper has one (docs/06 §5).
export default function KeyframePanel({ keyframes }: { keyframes: readonly Keyframe[] }) {
  const k = latest(keyframes);
  return (
    <section className="panel">
      <h2>Keyframe</h2>
      {k ? (
        <figure className="keyframe">
          <img src={`data:image/jpeg;base64,${k.thumbB64}`} alt={`keyframe ${k.kfId}`}
            onClick={() => k.url && window.open(k.url, '_blank')} style={{ cursor: k.url ? 'pointer' : 'default' }} />
          <figcaption>{k.kfId} · {k.stationId} · heading {fmtDeg(k.headingDeg)} · {fmtTime(k.tUnix)}</figcaption>
        </figure>
      ) : <p className="empty">No keyframes yet.</p>}
    </section>
  );
}
