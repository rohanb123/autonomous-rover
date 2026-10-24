import { useState } from 'react';
import { brain } from '../api/brain';
import type { Mission } from '../module_bindings/types';

interface Params { radius_m?: number; k?: number; categories?: string[]; mode?: string; }

function parse<T>(json: string, fallback: T): T {
  try { return JSON.parse(json) as T; } catch { return fallback; }
}

// Latest mission with parsed params and assumptions; text box sends a mission (docs/06 §5).
// VoiceButton (push-to-talk) mounts next to the text box later.
export default function MissionPanel({ missions, live }: { missions: readonly Mission[]; live: boolean }) {
  const m = [...missions].sort((a, b) => b.missionId.localeCompare(a.missionId))[0]; // m_0002 > m_0001
  const p = m ? parse<Params>(m.paramsJson, {}) : {};
  const assumptions = m ? parse<string[]>(m.assumptionsJson, []) : [];
  const [text, setText] = useState('');
  const [err, setErr] = useState('');

  const send = () => {
    if (!text.trim()) return;
    brain.missionText(text.trim()).then(() => { setText(''); setErr(''); }, (e) => setErr(String(e.message ?? e)));
  };

  return (
    <section className="panel">
      <h2>Mission</h2>
      {m ? (
        <>
          <p className="mission-text">“{m.text}”</p>
          <p className="meta">
            {m.missionId} · <b>{m.status}</b> · radius {p.radius_m ?? '—'} m · k {p.k ?? '—'} ·{' '}
            {(p.categories ?? []).join(', ') || '—'} · {p.mode ?? '—'}
          </p>
          {assumptions.length > 0 && <p className="meta">assumptions: {assumptions.join('; ')}</p>}
        </>
      ) : (
        <p className="empty">No mission in this run.</p>
      )}
      <div className="row">
        <input value={text} disabled={!live} placeholder="Type a mission…" onChange={(e) => setText(e.target.value)}
          onKeyDown={(e) => e.key === 'Enter' && send()} />
        <button disabled={!live || !text.trim()} onClick={send}>Send</button>
      </div>
      {err && <p className="error">{err}</p>}
    </section>
  );
}
