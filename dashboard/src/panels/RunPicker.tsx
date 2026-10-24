import { useState } from 'react';
import { brain } from '../api/brain';
import type { Run } from '../module_bindings/types';

interface Props { runs: readonly Run[]; selected?: string; onSelect(runId: string): void; }

// Lists runs newest first (docs/06 §5).
export default function RunPicker({ runs, selected, onSelect }: Props) {
  const sorted = [...runs].sort((a, b) => b.startedUnix - a.startedUnix);
  const [err, setErr] = useState('');
  const newRun = () => brain.runStart().then(() => setErr(''), (e) => setErr(String(e.message ?? e)));
  return (
    <>
    <select className="run-picker" value={selected ?? ''} onChange={(e) => onSelect(e.target.value)}>
      {sorted.length === 0 && <option value="">no runs yet</option>}
      {sorted.map((r) => (
        <option key={r.runId} value={r.runId}>
          {r.runId} · {r.status}{r.fused ? ' · fused' : ''}
        </option>
      ))}
    </select>
    <button onClick={newRun} title={err}>New run{err ? ' ⚠' : ''}</button>
    </>
  );
}
