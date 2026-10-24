// One memoized SpacetimeDB connection for the app (docs/02 §9). The dashboard connects
// anonymously: it can read public tables, and every reducer refuses it (docs/02 §7).
import { DbConnection } from '../module_bindings';

export const STDB_URI = import.meta.env.VITE_STDB_URI ?? 'ws://192.168.50.10:3000';
export const STDB_DB = import.meta.env.VITE_STDB_DB ?? 'marsrover';

export const connectionBuilder = DbConnection.builder()
  .withUri(STDB_URI)
  .withDatabaseName(STDB_DB)
  .onConnect((_conn, identity) => console.info('[stdb] connected as', identity.toHexString().slice(0, 12)))
  .onDisconnect(() => console.warn('[stdb] disconnected'))
  .onConnectError((_ctx, err) => console.error('[stdb] connect error', err));
