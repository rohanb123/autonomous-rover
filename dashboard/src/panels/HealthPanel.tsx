import type { Pose, RoverStatus, Telemetry } from '../module_bindings/types';
import { latest } from '../stdb/hooks';
import { fmtDeg, fmtNum, fmtTime } from './format';

// Latest telemetry + rover status + pose (docs/06 §5).
export default function HealthPanel({ telemetry, roverStatus, poses }:
  { telemetry: readonly Telemetry[]; roverStatus?: RoverStatus; poses: readonly Pose[] }) {
  const t = latest(telemetry);
  const p = latest(poses);
  return (
    <section className="panel health">
      <span>state <b>{roverStatus?.state ?? t?.state ?? '—'}</b></span>
      <span>pose {p ? `(${p.x.toFixed(2)}, ${p.y.toFixed(2)}) ${fmtDeg(p.headingDeg)} [${p.source}]` : '—'}</span>
      <span>battery {t && t.batteryV >= 0 ? `${fmtNum(t.batteryV)} V` : '—'}</span>
      <span>cpu {t ? `${t.cpuTempC.toFixed(0)} °C` : '—'}</span>
      <span>link {roverStatus ? (roverStatus.linkOk ? 'ok' : 'down') : '—'}</span>
      <span>uplink backlog {t?.uplinkBacklog ?? '—'}</span>
      <span>updated {roverStatus ? fmtTime(roverStatus.updatedUnix) : '—'}</span>
    </section>
  );
}
