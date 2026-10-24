// App shell: layout grid, run selection and live/replay mode (docs/06 §2–§4).
import { useEffect, useState } from 'react';
import ConnectionBadge from './panels/ConnectionBadge';
import EStopButton from './panels/EStopButton';
import EventLog from './panels/EventLog';
import HealthPanel from './panels/HealthPanel';
import KeyframePanel from './panels/KeyframePanel';
import MissionPanel from './panels/MissionPanel';
import QuickBar from './panels/QuickBar';
import RunPicker from './panels/RunPicker';
import TaskQueuePanel from './panels/TaskQueuePanel';
import { latest, useGlobalTables, useRunTables } from './stdb/hooks';

type Mode = 'live' | 'replay';

export default function App() {
  const { runs, roverStatus } = useGlobalTables();
  const [runId, setRunId] = useState<string>();
  const [pinned, setPinned] = useState(false); // true once the user picks a run by hand
  const [mode, setMode] = useState<Mode>('live');

  // Follow the newest run (active first) until the user picks one.
  useEffect(() => {
    if (pinned || runs.length === 0) return;
    const newest = [...runs].sort((a, b) => b.startedUnix - a.startedUnix);
    const pick = newest.find((r) => r.status === 'active') ?? newest[0];
    setRunId(pick.runId);
    setMode(pick.status === 'active' ? 'live' : 'replay');
  }, [runs, pinned]);

  const run = runs.find((r) => r.runId === runId);
  const t = useRunTables(runId);
  const live = mode === 'live' && run?.status === 'active';
  const pose = latest(t.poses);

  return (
    <div className="app">
      <header className="topbar">
        <span className="brand">ARES SCOUT</span>
        <RunPicker runs={runs} selected={runId} onSelect={(id) => { setPinned(true); setRunId(id); }} />
        <div className="mode">
          {(['live', 'replay'] as const).map((m) => (
            <button key={m} className={mode === m ? 'on' : ''} onClick={() => setMode(m)}>{m.toUpperCase()}</button>
          ))}
        </div>
        <ConnectionBadge roverStatus={roverStatus} />
        <EStopButton roverStatus={roverStatus} live={live} />
      </header>

      <main className="map-area">
        {/* MapView (P3, dashboard/src/map/) mounts here with MapViewProps (docs/06 §4). */}
        <div className="map-placeholder">
          <p><b>MapView (P3)</b> mounts here.</p>
          <p className="meta">
            {run ? `${run.runId} · ${run.status}${run.fused ? ' · fused' : ''}` : 'no run selected'}
            {pose && ` · rover (${pose.x.toFixed(2)}, ${pose.y.toFixed(2)}) ${pose.headingDeg.toFixed(0)}°`}
          </p>
          <p className="meta">
            {t.poses.length} poses · {t.localTiles.length} local tiles · {t.globalTiles.length} global tiles ·{' '}
            {t.candidates.length} candidates · {t.visits.length} visits · {t.analyses.length} analyses
          </p>
        </div>
      </main>

      <aside className="side">
        <MissionPanel missions={t.missions} live={live} />
        <TaskQueuePanel tasks={t.tasks} candidates={t.candidates} />
        <KeyframePanel keyframes={t.keyframes} />
      </aside>

      <footer className="bottom">
        <QuickBar fsmState={roverStatus?.runId === runId ? roverStatus?.fsmState : undefined} live={live} />
        <HealthPanel telemetry={t.telemetry} roverStatus={roverStatus?.runId === runId ? roverStatus : undefined} poses={t.poses} />
        <EventLog events={t.events} />
      </footer>
    </div>
  );
}
