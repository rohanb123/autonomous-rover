/// <reference types="vite/client" />

interface ImportMetaEnv {
  readonly VITE_STDB_URI: string;
  readonly VITE_STDB_DB: string;
  readonly VITE_BRAIN_URL: string;
  readonly VITE_MAPPER_URL: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
