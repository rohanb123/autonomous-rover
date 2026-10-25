// Reducers for the "marsrover" module: docs/02-spacetimedb.md §6.
// Export names are the HTTP reducer names. Param key order IS the HTTP positional arg
// order, so never reorder keys without a contract change (AGENTS.md §3).
// No Date.now() / Math.random(): reducers must be deterministic; use ctx.timestamp.
import { SenderError, t, type InferSchema, type ReducerCtx } from 'spacetimedb/server';
import spacetimedb from './schema';

export { default } from './schema';

type Ctx = ReducerCtx<InferSchema<typeof spacetimedb>>;

const MAX_THUMB_B64 = 150_000;
const MAX_LOCAL_TILE_B64 = 700_000;
const MAX_EVENT_MSG = 500;

// Ids like c_0001 restart every run, so rows are keyed by run + id (contract 1.4.0).
function runKey(runId: string, id: string): string {
  return `${runId}|${id}`;
}

function nowUnix(ctx: Ctx): number {
  return Number(ctx.timestamp.microsSinceUnixEpoch) / 1e6;
}

function requireWriter(ctx: Ctx): void {
  if (!ctx.db.writer.identityHex.find(ctx.sender.toHexString())) {
    throw new SenderError('not a writer');
  }
}

function oneOf(field: string, value: string, allowed: readonly string[]): void {
  if (!allowed.includes(value)) {
    throw new SenderError(`bad ${field} '${value}' (want ${allowed.join('|')})`);
  }
}

// --- writers --------------------------------------------------------------------

export const claim_writer = spacetimedb.reducer({ label: t.string() }, (ctx, { label }) => {
  for (const _ of ctx.db.writer.iter()) throw new SenderError('writer already claimed');
  ctx.db.writer.insert({ identityHex: ctx.sender.toHexString(), addedUnix: nowUnix(ctx), label });
});

export const add_writer = spacetimedb.reducer(
  { identityHex: t.string(), label: t.string() },
  (ctx, { identityHex, label }) => {
    requireWriter(ctx);
    if (!/^[0-9a-f]{64}$/.test(identityHex)) throw new SenderError('identityHex must be 64 hex chars');
    const row = { identityHex, addedUnix: nowUnix(ctx), label };
    if (ctx.db.writer.identityHex.find(identityHex)) ctx.db.writer.identityHex.update(row);
    else ctx.db.writer.insert(row);
  }
);

// --- runs and missions -------------------------------------------------------------

export const start_run = spacetimedb.reducer(
  { runId: t.string(), notes: t.string(), tUnix: t.f64() },
  (ctx, { runId, notes, tUnix }) => {
    requireWriter(ctx);
    if (ctx.db.run.runId.find(runId)) throw new SenderError(`run ${runId} already exists`);
    ctx.db.run.insert({
      runId, status: 'active', startedUnix: tUnix, endedUnix: -1, notes, fused: false, at: ctx.timestamp,
    });
  }
);

export const end_run = spacetimedb.reducer(
  { runId: t.string(), status: t.string(), tUnix: t.f64() },
  (ctx, { runId, status, tUnix }) => {
    requireWriter(ctx);
    oneOf('status', status, ['done', 'aborted']);
    const row = ctx.db.run.runId.find(runId);
    if (!row) throw new SenderError(`no run ${runId}`);
    ctx.db.run.runId.update({ ...row, status, endedUnix: tUnix });
  }
);

export const set_run_fused = spacetimedb.reducer(
  { runId: t.string(), fused: t.bool() },
  (ctx, { runId, fused }) => {
    requireWriter(ctx);
    const row = ctx.db.run.runId.find(runId);
    if (!row) throw new SenderError(`no run ${runId}`);
    ctx.db.run.runId.update({ ...row, fused });
  }
);

export const upsert_mission = spacetimedb.reducer(
  {
    missionId: t.string(), runId: t.string(), text: t.string(), paramsJson: t.string(),
    assumptionsJson: t.string(), status: t.string(),
  },
  (ctx, a) => {
    requireWriter(ctx);
    oneOf('status', a.status, ['parsed', 'active', 'done', 'rejected']);
    const row = { key: runKey(a.runId, a.missionId), ...a };
    if (ctx.db.mission.key.find(row.key)) ctx.db.mission.key.update(row);
    else ctx.db.mission.insert(row);
  }
);

export const upsert_task = spacetimedb.reducer(
  {
    taskId: t.string(), runId: t.string(), missionId: t.string(), kind: t.string(), ref: t.string(),
    seq: t.u32(), status: t.string(), note: t.string(),
  },
  (ctx, a) => {
    requireWriter(ctx);
    oneOf('kind', a.kind, ['station', 'visit', 'return']);
    oneOf('status', a.status, ['pending', 'active', 'done', 'failed', 'skipped']);
    const row = { key: runKey(a.runId, a.taskId), ...a };
    if (ctx.db.taskQueue.key.find(row.key)) ctx.db.taskQueue.key.update(row);
    else ctx.db.taskQueue.insert(row);
  }
);

// --- high-rate streams (brain rate-limits these) -------------------------------------

export const insert_pose = spacetimedb.reducer(
  {
    runId: t.string(), tUnix: t.f64(), x: t.f64(), y: t.f64(), headingDeg: t.f64(),
    source: t.string(), sigmaXy: t.f64(),
  },
  (ctx, a) => {
    requireWriter(ctx);
    ctx.db.pose.insert({ id: 0n, ...a });
  }
);

export const insert_telemetry = spacetimedb.reducer(
  {
    runId: t.string(), tUnix: t.f64(), state: t.string(), batteryV: t.f64(), cpuTempC: t.f64(),
    imuJson: t.string(), rangeM: t.f64(), uplinkBacklog: t.u32(),
  },
  (ctx, a) => {
    requireWriter(ctx);
    ctx.db.telemetry.insert({ id: 0n, ...a });
  }
);

export const upsert_rover_status = spacetimedb.reducer(
  {
    runId: t.string(), tUnix: t.f64(), state: t.string(), estop: t.bool(), x: t.f64(), y: t.f64(),
    headingDeg: t.f64(), fsmState: t.string(), activeCandId: t.string(), linkOk: t.bool(),
  },
  (ctx, a) => {
    requireWriter(ctx);
    const row = { key: 'rover', ...a, updatedUnix: a.tUnix };
    if (ctx.db.roverStatus.key.find('rover')) ctx.db.roverStatus.key.update(row);
    else ctx.db.roverStatus.insert(row);
  }
);

// --- map data (mapper) ------------------------------------------------------------------

export const upsert_keyframe = spacetimedb.reducer(
  {
    kfId: t.string(), runId: t.string(), stationId: t.string(), tUnix: t.f64(), x: t.f64(), y: t.f64(),
    headingDeg: t.f64(), thumbB64: t.string(), url: t.string(),
  },
  (ctx, a) => {
    requireWriter(ctx);
    if (a.thumbB64.length > MAX_THUMB_B64) throw new SenderError(`thumbB64 > ${MAX_THUMB_B64} chars`);
    const row = { key: runKey(a.runId, a.kfId), ...a };
    if (ctx.db.keyframe.key.find(row.key)) ctx.db.keyframe.key.update(row);
    else ctx.db.keyframe.insert(row);
  }
);

export const upsert_local_tile = spacetimedb.reducer(
  {
    stationId: t.string(), runId: t.string(), tUnix: t.f64(), x: t.f64(), y: t.f64(), headingDeg: t.f64(),
    resMPx: t.f64(), wPx: t.u32(), hPx: t.u32(), pngB64: t.string(), url: t.string(),
  },
  (ctx, a) => {
    requireWriter(ctx);
    if (a.pngB64.length > MAX_LOCAL_TILE_B64) throw new SenderError(`pngB64 > ${MAX_LOCAL_TILE_B64} chars`);
    const row = { key: runKey(a.runId, a.stationId), ...a };
    if (ctx.db.localMapTile.key.find(row.key)) ctx.db.localMapTile.key.update(row);
    else ctx.db.localMapTile.insert(row);
  }
);

export const upsert_global_tile = spacetimedb.reducer(
  {
    tileId: t.string(), runId: t.string(), ix: t.i32(), iy: t.i32(), originX: t.f64(), originY: t.f64(),
    resMPx: t.f64(), wPx: t.u32(), hPx: t.u32(), pngB64: t.string(), version: t.u32(),
  },
  (ctx, a) => {
    requireWriter(ctx);
    const existing = ctx.db.globalTile.tileId.find(a.tileId);
    if (!existing) ctx.db.globalTile.insert(a);
    else if (a.version >= existing.version) ctx.db.globalTile.tileId.update(a);
    // An older version is ignored: a late re-fuse must not overwrite a newer tile.
  }
);

// --- science loop (brain) ---------------------------------------------------------------

export const upsert_candidate = spacetimedb.reducer(
  {
    candId: t.string(), runId: t.string(), stationId: t.string(), x: t.f64(), y: t.f64(), score: t.f64(),
    componentsJson: t.string(), category: t.string(), sizeM: t.f64(), status: t.string(), rank: t.i32(),
    reason: t.string(), thumbUrl: t.string(), tUnix: t.f64(),
  },
  (ctx, a) => {
    requireWriter(ctx);
    oneOf('status', a.status, ['new', 'queued', 'active', 'visited', 'skipped', 'merged', 'excluded', 'failed']);
    const row = { key: runKey(a.runId, a.candId), ...a };
    if (ctx.db.candidate.key.find(row.key)) ctx.db.candidate.key.update(row);
    else ctx.db.candidate.insert(row);
  }
);

const CANDIDATE_STATUS_FOR_OUTCOME: Record<string, string> = {
  sampled: 'visited',
  skipped: 'skipped',
  unreachable: 'failed',
};

export const mark_visited = spacetimedb.reducer(
  {
    visitId: t.string(), runId: t.string(), candId: t.string(), x: t.f64(), y: t.f64(),
    radiusM: t.f64(), outcome: t.string(), tUnix: t.f64(),
  },
  (ctx, a) => {
    requireWriter(ctx);
    oneOf('outcome', a.outcome, Object.keys(CANDIDATE_STATUS_FOR_OUTCOME));
    const row = { key: runKey(a.runId, a.visitId), ...a };
    if (ctx.db.visit.key.find(row.key)) ctx.db.visit.key.update(row);
    else ctx.db.visit.insert(row);
    const cand = ctx.db.candidate.key.find(runKey(a.runId, a.candId));
    if (cand) {
      ctx.db.candidate.key.update({ ...cand, status: CANDIDATE_STATUS_FOR_OUTCOME[a.outcome], rank: -1 });
    }
  }
);

export const add_analysis = spacetimedb.reducer(
  {
    runId: t.string(), sampleId: t.string(), candId: t.string(), visitId: t.string(), tUnix: t.f64(),
    imageUrl: t.string(), label: t.string(), confidence: t.f64(), caption: t.string(),
    assayType: t.string(), a595: t.f64(), concentration: t.f64(), units: t.string(), verdict: t.string(),
    inLinearRange: t.bool(), standardCurveParams: t.string(), simulated: t.bool(), seed: t.u32(),
    modelVersion: t.string(),
  },
  (ctx, a) => {
    requireWriter(ctx);
    if (a.simulated !== true) throw new SenderError('analysis must be simulated=true');
    oneOf('assayType', a.assayType, ['', 'bradford_sim']);
    oneOf('verdict', a.verdict, ['', 'below_detection', 'low', 'elevated']);
    const key = runKey(a.runId, a.sampleId);
    const existing = ctx.db.analysisRecord.key.find(key);
    if (existing) ctx.db.analysisRecord.id.update({ ...existing, ...a, key });
    else ctx.db.analysisRecord.insert({ id: 0n, key, ...a });
  }
);

// --- events ---------------------------------------------------------------------------------

export const log_event = spacetimedb.reducer(
  {
    runId: t.string(), tUnix: t.f64(), src: t.string(), level: t.string(), kind: t.string(),
    msg: t.string(), dataJson: t.string(),
  },
  (ctx, a) => {
    requireWriter(ctx);
    oneOf('level', a.level, ['info', 'warn', 'error']);
    ctx.db.event.insert({ id: 0n, ...a, msg: a.msg.slice(0, MAX_EVENT_MSG) });
  }
);
