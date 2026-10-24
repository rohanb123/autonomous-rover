import { useEffect, useState } from 'react';
import { useSpacetimeDB } from 'spacetimedb/react';
import { brain } from '../api/brain';
import type { RoverStatus } from '../module_bindings/types';

const STALE_S = 2;

// STDB from the connection state, Brain from GET /api/health every 2 s, Rover from
// rover_status freshness (docs/06 §5).
export default function ConnectionBadge({ roverStatus }: { roverStatus?: RoverStatus }) {
  const { isActive } = useSpacetimeDB();
  const [brainOk, setBrainOk] = useState(false);
  const [now, setNow] = useState(Date.now() / 1000);

  useEffect(() => {
    let alive = true;
    const poll = () => brain.health().then(() => alive && setBrainOk(true), () => alive && setBrainOk(false));
    poll();
    const h = setInterval(poll, 2000);
    const n = setInterval(() => setNow(Date.now() / 1000), 500);
    return () => { alive = false; clearInterval(h); clearInterval(n); };
  }, []);

  const roverOk = Boolean(roverStatus && now - roverStatus.updatedUnix <= STALE_S && roverStatus.linkOk);
  return (
    <div className="badge">
      <Dot ok={isActive} label="STDB" />
      <Dot ok={brainOk} label="Brain" />
      <Dot ok={roverOk} label="Rover" />
    </div>
  );
}

function Dot({ ok, label }: { ok: boolean; label: string }) {
  return <span className={`dot ${ok ? 'ok' : 'bad'}`} title={ok ? 'ok' : 'down'}>{label}</span>;
}
