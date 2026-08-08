/// <reference types="vite/client" />
/// <reference types="vitest/globals" />

interface ImportMetaEnv {
  readonly VITE_DEV_MODE?: string
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

