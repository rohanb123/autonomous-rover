import { useEffect, useRef } from 'react';

export type ScrubTick = { t: number; kind: 'pose' | 'event' | 'sample' };

const SPEEDS = [1, 4, 16] as const;
export type PlaySpeed = (typeof SPEEDS)[number];

type Props = {
  start: number;
  end: number;
  scrubT: number;
  ticks: readonly ScrubTick[];
  playing: boolean;
  speed: PlaySpeed;
  onScrub: (t: number) => void;
  onPlaying: (playing: boolean) => void;
  onSpeed: (speed: PlaySpeed) => void;
};

function clock(tUnix: number): string {
  return new Date(tUnix * 1000).toLocaleTimeString([], {
    hour12: false, hour: '2-digit', minute: '2-digit', second: '2-digit',
  });
}

// Replay timeline (docs/06 §5). Play advances scrubT at 1×/4×/16×; dragging seeks.
export default function TimelineScrubber({ start, end, scrubT, ticks, playing, speed, onScrub, onPlaying, onSpeed }: Props) {
  const scrubRef = useRef(scrubT);
  scrubRef.current = scrubT;
  const span = Math.max(end - start, 0);

  useEffect(() => {
    if (!playing) return;
    let last = performance.now();
    let frame = 0;
    const loop = (now: number) => {
      const next = Math.min(end, scrubRef.current + ((now - last) / 1000) * speed);
      last = now;
      onScrub(next);
      if (next >= end) {
        onPlaying(false);
        return;
      }
      frame = requestAnimationFrame(loop);
    };
    frame = requestAnimationFrame(loop);
    return () => cancelAnimationFrame(frame);
  }, [playing, speed, end, onScrub, onPlaying]);

  const pct = (t: number) => (span === 0 ? 0 : ((t - start) / span) * 100);

  return (
    <section className="panel scrubber">
      <div className="scrub-track" aria-hidden="true">
        {ticks.map((tick, i) => (
          <span key={`${tick.kind}-${i}`} className={`tick tick-${tick.kind}`} style={{ left: `${pct(tick.t)}%` }} />
        ))}
      </div>
      <input
        type="range"
        min={start}
        max={end}
        step={span > 0 ? Math.min(0.05, span / 200) : 1}
        value={Math.min(Math.max(scrubT, start), end)}
        aria-label="Replay time"
        disabled={span === 0}
        onChange={(e) => {
          onPlaying(false);
          onScrub(Number(e.target.value));
        }}
      />
      <div className="scrub-controls">
        <button type="button" onClick={() => onPlaying(!playing)} disabled={span === 0} aria-label={playing ? 'Pause' : 'Play'}>
          {playing ? 'Pause' : 'Play'}
        </button>
        {SPEEDS.map((n) => (
          <button key={n} type="button" className={speed === n ? 'on' : ''} onClick={() => onSpeed(n)}>{n}×</button>
        ))}
        <span className="scrub-time">{clock(scrubT)}</span>
      </div>
    </section>
  );
}
