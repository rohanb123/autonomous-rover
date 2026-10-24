// App shell: layout grid, run selection, live/replay mode, scrub time (docs/06 §2–§4).
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import MapView from './map/MapView';
import ConnectionBadge from './panels/ConnectionBadge';
import EStopButton from './panels/EStopButton';
import EventLog from './panels/EventLog';
import HealthPanel from './panels/HealthPanel';
import KeyframePanel from './panels/KeyframePanel';
import MissionPanel from './panels/MissionPanel';
import PinDetailDrawer from './panels/PinDetailDrawer';
import QuickBar from './panels/QuickBar';
import RunPicker from './panels/RunPicker';
import TaskQueuePanel from './panels/TaskQueuePanel';
import TimelineScrubber, { type PlaySpeed, type ScrubTick } from './panels/TimelineScrubber';
import { latest, useGlobalTables, useRunTables, useScrubbed } from './stdb/hooks';
import { crossed, runWindow } from './stdb/scrub';

type Mode = 'live' | 'replay';


export default function App() {
  const { runs, roverStatus } = useGlobalTables();
  const [runId, setRunId] = useState<string>();
  const [pinned, setPinned] = useState(false);
  const [mode, setMode] = useState<Mode>('live');
  const [scrub, setScrub] = useState<number>();
  const [playing, setPlaying] = useState(false);
  const [speed, setSpeed] = useState<PlaySpeed>(1);
  const [selectedId, setSelectedId] = useState<string>();
  const [autoplay, setAutoplay] = useState(false);
  const snapped = useRef<string>();
  const prevScrub = useRef<number | undefined>(undefined);

  useEffect(() => {
    if (pinned || runs.length === 0) return;
    const pick = [...runs].sort((a, b) => b.startedUnix - a.startedUnix)[0];
    setRunId(pick.runId);
    setMode(pick.status === 'active' ? 'live' : 'replay');
  }, [runs, pinned]);

  const run = runs.find((r) => r.runId === runId);
  const t = useRunTables(runId);
  const live = mode === 'live' && run?.status === 'active';

  const times = useMemo(
    () => [
      ...t.poses, ...t.events, ...t.analyses, ...t.candidates, ...t.visits, ...t.keyframes,
    ].map((row) => row.tUnix),
    [t.poses, t.events, t.analyses, t.candidates, t.visits, t.keyframes],
  );
  const bounds = runWindow(run?.startedUnix ?? 0, run?.endedUnix ?? -1, times);
  const scrubT = mode === 'replay' ? (scrub ?? bounds.end) : undefined;

  const poses = useScrubbed(t.poses, scrubT);
  const events = useScrubbed(t.events, scrubT);
  const analyses = useScrubbed(t.analyses, scrubT);
  const candidates = useScrubbed(t.candidates, scrubT);
  const visits = useScrubbed(t.visits, scrubT);
  const keyframes = useScrubbed(t.keyframes, scrubT);
  const telemetry = useScrubbed(t.telemetry, scrubT);
  const localTiles = useScrubbed(t.localTiles, scrubT);

  useEffect(() => {
    snapped.current = undefined;
    prevScrub.current = undefined;
    setSelectedId(undefined);
    setPlaying(false);
    setScrub(undefined);
  }, [runId]);

  useEffect(() => {
    if (mode !== 'replay' || !run) {
      setPlaying(false);
      return;
    }
    if (snapped.current !== run.runId) {
      snapped.current = run.runId;
      setScrub(bounds.end);
    }
  }, [mode, run, bounds.end]);

  useEffect(() => {
    if (mode !== 'replay' || !playing || scrubT === undefined) {
      prevScrub.current = scrubT;
      return;
    }
    const from = prevScrub.current ?? scrubT;
    const hit = crossed(analyses, from, scrubT);
    prevScrub.current = scrubT;
    if (hit) {
      setAutoplay(true);
      setSelectedId(hit.candId);
    }
  }, [mode, playing, scrubT, analyses]);

  const closeDrawer = useCallback(() => setSelectedId(undefined), []);
  const selectCand = useCallback((candId: string) => {
    setAutoplay(false);
    setSelectedId(candId);
  }, []);

  const selected = candidates.find((c) => c.candId === selectedId);
  const visit = visits.filter((v) => v.candId === selectedId).sort((a, b) => b.tUnix - a.tUnix)[0];
  const analysis = analyses.filter((a) => a.candId === selectedId).sort((a, b) => b.tUnix - a.tUnix)[0];
  const keyframe = latest(keyframes.filter((k) => k.stationId === selected?.stationId));
  const ticks: ScrubTick[] = [
    ...t.poses.map((p) => ({ t: p.tUnix, kind: 'pose' as const })),
    ...t.events.map((e) => ({ t: e.tUnix, kind: 'event' as const })),
    ...t.analyses.map((a) => ({ t: a.tUnix, kind: 'sample' as const })),
  ];

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
        {/* MapView (P3), docs/06 §4 MapViewProps */}
        <MapView
          mode={mode}
          poses={poses}
          localTiles={localTiles}
          globalTiles={mode === 'replay' && run?.fused ? [...t.globalTiles] : []}
          candidates={candidates}
          visits={visits}
          status={roverStatus?.runId === runId ? roverStatus : undefined}
          selectedCandId={selectedId}
          onSelectCand={selectCand}
        />
      </main>

      <aside className="side">
        <MissionPanel missions={t.missions} live={live} fsmState={roverStatus?.runId === runId ? roverStatus?.fsmState : undefined} />
        <TaskQueuePanel tasks={t.tasks} candidates={candidates} />
        <KeyframePanel keyframes={keyframes} />
      </aside>

      <footer className="bottom">
        {mode === 'replay' && run && (
          <TimelineScrubber
            start={bounds.start}
            end={bounds.end}
            scrubT={scrubT ?? bounds.end}
            ticks={ticks}
            playing={playing}
            speed={speed}
            onScrub={setScrub}
            onPlaying={setPlaying}
            onSpeed={setSpeed}
          />
        )}
        <QuickBar fsmState={roverStatus?.runId === runId ? roverStatus?.fsmState : undefined} live={live} />
        <HealthPanel telemetry={telemetry} roverStatus={roverStatus?.runId === runId ? roverStatus : undefined} poses={poses} />
        <EventLog events={events} />
      </footer>

      {selected && (
        <PinDetailDrawer
          candidate={selected}
          visit={visit}
          analysis={analysis}
          keyframe={keyframe}
          autoplay={autoplay}
          onClose={closeDrawer}
        />
      )}
    </div>
  );
}
