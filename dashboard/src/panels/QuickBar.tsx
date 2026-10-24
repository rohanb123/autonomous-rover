import { useState } from 'react';
import { brain, type QuickCmd } from '../api/brain';

const CMDS: [QuickCmd, string][] = [
  ['pause', 'pause'], ['resume', 'resume'], ['stop', 'stop'], ['skip', 'skip'], ['status', 'status'], ['come_back', 'come back'],
];

// Quick commands to the brain; disabled in replay (docs/06 §5).
export default function QuickBar({ fsmState, live }: { fsmState?: string; live: boolean }) {
  const [msg, setMsg] = useState('');
  const send = (cmd: QuickCmd) =>
    brain.quick(cmd).then((r) => setMsg(`${cmd}: accepted (${r.cmd_uid})`), (e) => setMsg(String(e.message ?? e)));
  return (
    <div className="quickbar">
      {CMDS.map(([cmd, label]) => (
        <button key={cmd} disabled={!live} onClick={() => send(cmd)}>{label}</button>
      ))}
      <span className="meta">FSM: <b>{fsmState ?? '—'}</b></span>
      {msg && <span className="meta">{msg}</span>}
    </div>
  );
}
