/// <reference types="vite/client" />
/// <reference types="vitest/globals" />

interface ImportMetaEnv {
  readonly VITE_DEV_MODE?: string
  /** Base URL for the profile menu's Share feedback link. */
  readonly VITE_FEEDBACK_URL?: string
  readonly DEV: boolean
  readonly PROD: boolean
  readonly MODE: string
}

interface ImportMeta {
  readonly env: ImportMetaEnv
}

// NodeJS namespace for timer types
declare namespace NodeJS {
  type Timeout = ReturnType<typeof setTimeout>
  type Immediate = ReturnType<typeof setImmediate>
}

// Window extensions for optional features
interface Window {
  gc?: () => void
}

// Global test utilities
declare const global: typeof globalThis

