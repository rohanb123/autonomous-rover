// Tables for the "marsrover" module. Shape is binding: docs/02-spacetimedb.md §5.
// SQL/HTTP names are snake_case (`name`); ctx.db / client accessors are the camelCase keys
// passed to schema({...}). Every run-scoped table has a btree index on runId.
// SQL names put an underscore before digits (pngB64 -> png_b_64, a595 -> a_595); column
// names can't be overridden in 2.13, see docs/02 §3.
import { schema, table, t } from 'spacetimedb/server';

const runId = () => t.string().index('btree');

const run = table(
  { name: 'run', public: true },
  {
    runId: t.string().primaryKey(),
    status: t.string(), // active | done | aborted
    startedUnix: t.f64(),
    endedUnix: t.f64(), // -1 while active
    notes: t.string(),
    fused: t.bool(),
    at: t.timestamp(),
  }
);

const mission = table(
  { name: 'mission', public: true },
  {
    missionId: t.string().primaryKey(),
    runId: runId(),
    text: t.string(),
    paramsJson: t.string(),
    assumptionsJson: t.string(),
    status: t.string(), // parsed | active | done | rejected
  }
);

const taskQueue = table(
  { name: 'task_queue', public: true },
  {
    taskId: t.string().primaryKey(),
    runId: runId(),
    missionId: t.string(),
    kind: t.string(), // station | visit | return
    ref: t.string(),
    seq: t.u32(),
    status: t.string(), // pending | active | done | failed | skipped
    note: t.string(),
  }
);

const pose = table(
  { name: 'pose', public: true },
  {
    id: t.u64().primaryKey().autoInc(),
    runId: runId(),
    tUnix: t.f64(),
    x: t.f64(),
    y: t.f64(),
    headingDeg: t.f64(),
    source: t.string(),
    sigmaXy: t.f64(), // -1 if unknown
  }
);

const keyframe = table(
  { name: 'keyframe', public: true },
  {
    kfId: t.string().primaryKey(),
    runId: runId(),
    stationId: t.string(),
    tUnix: t.f64(),
    x: t.f64(),
    y: t.f64(),
    headingDeg: t.f64(),
    thumbB64: t.string(),
    url: t.string(),
  }
);

const localMapTile = table(
  { name: 'local_map_tile', public: true },
  {
    stationId: t.string().primaryKey(),
    runId: runId(),
    tUnix: t.f64(),
    x: t.f64(),
    y: t.f64(),
    headingDeg: t.f64(),
    resMPx: t.f64(),
    wPx: t.u32(),
    hPx: t.u32(),
    pngB64: t.string(),
    url: t.string(),
  }
);

const globalTile = table(
  { name: 'global_tile', public: true },
  {
    tileId: t.string().primaryKey(), // g_<run>_<ix>_<iy>
    runId: runId(),
    ix: t.i32(),
    iy: t.i32(),
    originX: t.f64(),
    originY: t.f64(),
    resMPx: t.f64(),
    wPx: t.u32(),
    hPx: t.u32(),
    pngB64: t.string(),
    version: t.u32(),
  }
);

const candidate = table(
  { name: 'candidate', public: true },
  {
    candId: t.string().primaryKey(),
    runId: runId(),
    stationId: t.string(),
    x: t.f64(),
    y: t.f64(),
    score: t.f64(),
    componentsJson: t.string(),
    category: t.string(),
    sizeM: t.f64(),
    status: t.string(),
    rank: t.i32(), // -1 = not in top-k
    reason: t.string(),
    thumbUrl: t.string(),
    tUnix: t.f64(),
  }
);

const visit = table(
  { name: 'visit', public: true },
  {
    visitId: t.string().primaryKey(),
    runId: runId(),
    candId: t.string(),
    x: t.f64(),
    y: t.f64(),
    radiusM: t.f64(),
    outcome: t.string(), // sampled | empty_scoop | unreachable | skipped
    tUnix: t.f64(),
  }
);

const analysisRecord = table(
  { name: 'analysis_record', public: true },
  {
    id: t.u64().primaryKey().autoInc(),
    runId: runId(),
    sampleId: t.string().unique(),
    candId: t.string(),
    visitId: t.string(),
    tUnix: t.f64(),
    imageUrl: t.string(),
    label: t.string(),
    confidence: t.f64(),
    caption: t.string(),
    scoopHasItem: t.bool(),
    // Simulated Bradford assay (docs/02 §5, Steering 2). "" / -1 when there is no assay.
    assayType: t.string(),
    a595: t.f64(),
    concentration: t.f64(),
    units: t.string(),
    verdict: t.string(),
    inLinearRange: t.bool(),
    standardCurveParams: t.string(), // JSON
    simulated: t.bool(), // always true; add_analysis rejects false
    seed: t.u32(),
    modelVersion: t.string(),
  }
);

const telemetry = table(
  { name: 'telemetry', public: true },
  {
    id: t.u64().primaryKey().autoInc(),
    runId: runId(),
    tUnix: t.f64(),
    state: t.string(),
    batteryV: t.f64(), // -1 if unknown
    cpuTempC: t.f64(),
    imuJson: t.string(),
    scoopPose: t.string(),
    uplinkBacklog: t.u32(),
  }
);

const roverStatus = table(
  { name: 'rover_status', public: true },
  {
    key: t.string().primaryKey(), // always "rover"
    runId: t.string(),
    tUnix: t.f64(),
    state: t.string(),
    estop: t.bool(),
    x: t.f64(),
    y: t.f64(),
    headingDeg: t.f64(),
    fsmState: t.string(),
    activeCandId: t.string(),
    linkOk: t.bool(),
    updatedUnix: t.f64(),
  }
);

const event = table(
  { name: 'event', public: true },
  {
    id: t.u64().primaryKey().autoInc(),
    runId: runId(),
    tUnix: t.f64(),
    src: t.string(),
    level: t.string(), // info | warn | error
    kind: t.string(),
    msg: t.string(),
    dataJson: t.string(),
  }
);

// Private: only identities in here may call reducers (docs/02 §7).
const writer = table(
  { name: 'writer' },
  {
    identityHex: t.string().primaryKey(),
    addedUnix: t.f64(),
    label: t.string(),
  }
);

const spacetimedb = schema({
  run,
  mission,
  taskQueue,
  pose,
  keyframe,
  localMapTile,
  globalTile,
  candidate,
  visit,
  analysisRecord,
  telemetry,
  roverStatus,
  event,
  writer,
});
export default spacetimedb;
