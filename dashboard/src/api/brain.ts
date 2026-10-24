// Typed REST client for the brain (doc 00 §6.11). The dashboard never writes to STDB;
// every command goes through here.
export const BRAIN_URL = import.meta.env.VITE_BRAIN_URL ?? 'http://192.168.50.10:8000';

export type Health = 'ok' | 'down' | 'degraded';
export interface HealthResp {
  brain: Health; rover_link: Health; vision: Health; mapper: Health; stdb: Health; overhead: Health;
}
export interface CommandAccepted { accepted: boolean; cmd_uid: string; }
export type QuickCmd = 'pause' | 'resume' | 'stop' | 'skip' | 'status' | 'come_back';

export class BrainError extends Error {}

async function call<T>(path: string, init: RequestInit = {}, timeoutMs = 3000): Promise<T> {
  const ctl = new AbortController();
  const timer = setTimeout(() => ctl.abort(), timeoutMs);
  try {
    const resp = await fetch(BRAIN_URL + path, { ...init, signal: ctl.signal });
    if (!resp.ok) throw new BrainError(`${path}: HTTP ${resp.status} ${await resp.text()}`);
    return (await resp.json()) as T;
  } catch (e) {
    throw e instanceof BrainError ? e : new BrainError(`${path}: brain unreachable (${String(e)})`);
  } finally {
    clearTimeout(timer);
  }
}

const post = <T = CommandAccepted>(path: string, body: unknown = {}) =>
  call<T>(path, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });

export const brain = {
  health: () => call<HealthResp>('/api/health', {}, 1500),
  runStart: (notes?: string) => post('/api/run/start', notes ? { notes } : {}),
  runEnd: () => post('/api/run/end'),
  missionText: (text: string) => post('/api/mission/text', { text }),
  missionVoice: (audio: Blob) =>
    call<CommandAccepted>('/api/mission/voice', { method: 'POST', headers: { 'Content-Type': 'audio/webm' }, body: audio }, 15000),
  approve: (mission_id: string, approve: boolean) => post('/api/approve', { mission_id, approve }),
  quick: (cmd: QuickCmd) => post('/api/quick', { cmd }),
  estop: () => post('/api/estop', {}),
  estopClear: () => post('/api/estop/clear', {}),
};
