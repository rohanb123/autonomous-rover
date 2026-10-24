import { useEffect, useState } from 'react';
import { brain } from '../api/brain';
import type { RoverStatus } from '../module_bindings/types';

// Always enabled in live mode; goes straight to the brain's estop path. Space bar fires it;
// clearing the latch needs a confirm (docs/06 §5).
export default function EStopButton({ roverStatus, live }: { roverStatus?: RoverStatus; live: boolean }) {
  const latched = Boolean(roverStatus?.estop);
  const [confirming, setConfirming] = useState(false);
  const [err, setErr] = useState('');

  const fire = () => brain.estop().then(() => setErr(''), (e) => setErr(String(e.message ?? e)));

  useEffect(() => {
    if (!live) return;
    const onKey = (e: KeyboardEvent) => {
      const tag = (e.target as HTMLElement | null)?.tagName;
      if (e.code === 'Space' && tag !== 'INPUT' && tag !== 'TEXTAREA') {
        e.preventDefault();
        fire();
      }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [live]);

  if (latched) {
    return confirming ? (
      <button className="estop clear" onClick={() => brain.estopClear().then(() => setConfirming(false), (e) => setErr(String(e.message ?? e)))}>
        Confirm clear?
      </button>
    ) : (
      <button className="estop latched" onClick={() => setConfirming(true)} title={err}>E-STOP LATCHED · clear</button>
    );
  }
  return (
    <button className="estop" disabled={!live} onClick={fire} title={err || 'Space bar'}>
      E-STOP{err ? ' ⚠' : ''}
    </button>
  );
}
