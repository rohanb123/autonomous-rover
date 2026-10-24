import type { Event } from '../module_bindings/types';
import { fmtTime } from './format';

// Last 50 events, newest first, level-colored (docs/06 §5).
export default function EventLog({ events }: { events: readonly Event[] }) {
  const last = [...events].sort((a, b) => b.tUnix - a.tUnix).slice(0, 50);
  return (
    <section className="panel eventlog">
      <h2>Events</h2>
      {last.length === 0 ? <p className="empty">No events.</p> : (
        <ul>
          {last.map((e) => (
            <li key={String(e.id)} className={`lvl-${e.level}`}>
              <span className="time">{fmtTime(e.tUnix)}</span> <span className="src">{e.src}</span> {e.msg}
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
