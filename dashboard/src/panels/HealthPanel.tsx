import type { Pose, RoverStatus, Telemetry } from '../module_bindings/types';
import { latest } from '../stdb/hooks';
import CountUp from './CountUp';
import { fmtTime } from './format';

// Latest telemetry + rover status + pose (docs/06 §5), as a flight-style readout.
export default function HealthPanel({ telemetry, roverStatus, poses }:
  { telemetry: readonly Telemetry[]; roverStatus?: RoverStatus; poses: readonly Pose[] }) {
  const t = latest(telemetry);
  const p = latest(poses);
  const item = (k: string, v: React.ReactNode) => (
    <span key={k}><span className="k">{k}</span><span className="v">{v}</span></span>
  );
  return (
    <section className="panel health">
      {item('State', roverStatus?.state ?? t?.state ?? '—')}
      {item('X m', p ? <CountUp value={p.x} /> : '—')}
      {item('Y m', p ? <CountUp value={p.y} /> : '—')}
      {item('Heading', p ? <><CountUp value={p.headingDeg} digits={0} />°</> : '—')}
      {item('Pose src', p?.source ?? '—')}
      {item('Battery', t && t.batteryV >= 0 ? <><CountUp value={t.batteryV} /> V</> : '—')}
      {item('CPU', t ? <><CountUp value={t.cpuTempC} digits={0} /> °C</> : '—')}
      {item('Link', roverStatus ? (roverStatus.linkOk ? 'OK' : 'DOWN') : '—')}
      {item('Backlog', t?.uplinkBacklog ?? '—')}
      {item('Updated', roverStatus ? fmtTime(roverStatus.updatedUnix) : '—')}
    </section>
  );
}
