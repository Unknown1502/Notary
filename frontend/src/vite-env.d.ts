/// <reference types="vite/client" />

interface ImportMetaEnv {
  /** Override the API origin when the frontend is not served same-origin. */
  readonly VITE_API_BASE?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
