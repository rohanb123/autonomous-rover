import type { Candidate, TaskQueue } from '../module_bindings/types';
import CountUp from './CountUp';

// Top-k (rank ≥ 0) with score, category and reason, then the task queue (docs/06 §5).
export default function TaskQueuePanel({ tasks, candidates }: { tasks: readonly TaskQueue[]; candidates: readonly Candidate[] }) {
  const topk = candidates.filter((c) => c.rank >= 0).sort((a, b) => a.rank - b.rank);
  const queue = [...tasks].sort((a, b) => a.seq - b.seq);
  return (
    <section className="panel">
      <h2>Top-k</h2>
      {topk.length === 0 ? <p className="empty">No ranked candidates.</p> : (
        <ol className="topk">
          {topk.map((c) => (
            <li key={c.candId}>
              <span className="rank">#{c.rank}</span> <b>{c.candId}</b> <span className="score"><CountUp value={c.score} /></span> {c.category}{' '}
              <span className={`status s-${c.status}`}>{c.status}</span>
              <div className="reason">{c.reason}</div>
            </li>
          ))}
        </ol>
      )}
      <h2>Queue</h2>
      {queue.length === 0 ? <p className="empty">No tasks.</p> : (
        <ul className="queue">
          {queue.map((t) => (
            <li key={t.taskId}>
              {t.seq}. {t.kind} {t.ref} <span className={`status t-${t.status}`}>{t.status}</span>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
