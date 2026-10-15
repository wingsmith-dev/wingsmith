/**
 * Deployment settings, read when the page loads rather than baked into the bundle.
 *
 * `index.html` loads `/config.js` before the app. The bundle ships a default one that sets
 * nothing; the standalone nginx image rewrites it at container start from `HANGAR_API_BASE_URL`
 * and `HANGAR_READ_ONLY`, so one image serves any plane. The `VITE_*` variables still apply as
 * build-time defaults, which is how `pnpm dev` is configured.
 */
export interface RuntimeConfig {
  /**
   * Where the plane lives. Empty means the origin that served the console — the plane image's
   * setting, and the standalone image's default when it sits behind the same ingress.
   */
  apiBaseUrl: string
  /** Start every new browser session in read-only mode. */
  readOnly: boolean
}

declare global {
  interface Window {
    __HANGAR_CONFIG__?: Partial<Record<keyof RuntimeConfig, unknown>>
  }
}

type Source = Partial<Record<keyof RuntimeConfig, unknown>>

function readBool(value: unknown): boolean | undefined {
  if (typeof value === "boolean") return value
  if (typeof value === "string" && value !== "") return value === "true"
  return undefined
}

function readString(value: unknown): string | undefined {
  return typeof value === "string" ? value.replace(/\/+$/, "") : undefined
}

/**
 * Runtime values win over build-time ones. A runtime value that is present but empty is a
 * deliberate setting (same origin), not a missing one, so only `undefined` falls through.
 */
export function resolveConfig(
  runtime: Source | undefined,
  env: Record<string, string | boolean | undefined>
): RuntimeConfig {
  return {
    apiBaseUrl:
      readString(runtime?.apiBaseUrl) ??
      readString(env.VITE_API_BASE_URL) ??
      "",
    readOnly:
      readBool(runtime?.readOnly) ?? readBool(env.VITE_READ_ONLY) ?? false,
  }
}

export const config: RuntimeConfig = resolveConfig(
  typeof window === "undefined" ? undefined : window.__HANGAR_CONFIG__,
  import.meta.env
)
