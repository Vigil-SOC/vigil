/// <reference types="vite/client" />
/// <reference types="vitest/globals" />

interface ImportMetaEnv {
  readonly VITE_DEV_MODE?: string
  /** Base URL for the profile menu's Share feedback link. */
  readonly VITE_FEEDBACK_URL?: string
}

interface ImportMeta {
  readonly env: ImportMetaEnv
}

// Global test utilities
declare const global: typeof globalThis

