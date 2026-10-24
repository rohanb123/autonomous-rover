// Row builders for map tests and the preview page. Not used by the app itself.
import type { Candidate, GlobalTile, LocalMapTile, Pose, RoverStatus, Visit } from '../module_bindings/types';

export const RUN = 'r_20261024_101500';

export const pose = (x: number, y: number, headingDeg: number, tUnix: number, source = 'dr+gyro'): Pose => ({
  id: BigInt(Math.round(tUnix * 10)),
  runId: RUN,
  tUnix,
  x,
  y,
  headingDeg,
  source,
  sigmaXy: 0.05,
});

export const localTile = (stationId: string, x: number, y: number, headingDeg: number, tUnix: number, pngB64 = ''): LocalMapTile => ({
  stationId,
  runId: RUN,
  tUnix,
  x,
  y,
  headingDeg,
  resMPx: 0.005,
  wPx: 360,
  hPx: 360,
  pngB64,
  url: '',
});

export const globalTile = (ix: number, iy: number, originX: number, originY: number, pngB64 = ''): GlobalTile => ({
  tileId: `g_${RUN}_${ix}_${iy}`,
  runId: RUN,
  ix,
  iy,
  originX,
  originY,
  resMPx: 0.005,
  wPx: 512,
  hPx: 512,
  pngB64,
  version: 1,
});

export const candidate = (candId: string, x: number, y: number, score: number, status: string, rank = -1): Candidate => ({
  candId,
  runId: RUN,
  stationId: 's_0001',
  x,
  y,
  score,
  componentsJson: '{}',
  category: 'float_rock',
  sizeM: 0.02,
  status,
  rank,
  reason: '',
  thumbUrl: '',
  tUnix: 0,
});

export const visit = (visitId: string, candId: string, x: number, y: number, tUnix: number): Visit => ({
  visitId,
  runId: RUN,
  candId,
  x,
  y,
  radiusM: 0.2,
  outcome: 'sampled',
  tUnix,
});

export const roverStatus = (x: number, y: number, headingDeg: number): RoverStatus => ({
  key: 'rover',
  runId: RUN,
  tUnix: 0,
  state: 'IDLE',
  estop: false,
  x,
  y,
  headingDeg,
  fsmState: 'SURVEY',
  activeCandId: '',
  linkOk: true,
  updatedUnix: 0,
});
