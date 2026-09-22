/// <reference types="vite/client" />

/**
 * Vite env surface. Values come from `.env` / `.env.local` at build time.
 * In production Nginx serves the SPA and the API from ONE origin (:18000), so the
 * default keeps requests same-origin under `/api/v1`.
 *
 * NOTE: `vite/client` already declares `ImportMetaEnv` / `ImportMeta` globally, so
 * these are *extensions* of those interfaces, not redeclarations.
 */
interface ImportMetaEnv {
  readonly VITE_API_BASE_URL?: string
  /** Milliseconds; falls back to DEFAULT_TIMEOUT_MS in src/api/client.ts */
  readonly VITE_API_TIMEOUT?: string
  readonly VITE_APP_TITLE?: string
  readonly VITE_ENABLE_MOCK?: string
}
