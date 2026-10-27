/**
 * `GET /v1/system/config`: the plane's effective, non-secret configuration.
 *
 * The plane names every exposed field by hand, so this is an allowlist rather
 * than a dump of its environment. Nothing here is a credential: a token or a
 * password appears only as a `*_set` boolean, and `DATABASE_URL` arrives in
 * parts with the userinfo already gone.
 */

/** One `METRICS_SOURCES` entry, as `Source.public()` plus its `cluster` matcher. */
export interface MetricsSourceConfig {
  /** The entry's key: matches a cluster's `name` or `uid`. */
  cluster: string
  url: string
  kind: string
  /** The `cluster` label value queries narrow by. Empty means a dedicated store. */
  label: string
}

export interface AppConfig {
  env: string
  debug: boolean
  log_level: string
  log_format: string
  allowed_origins: string[]
}

export interface ApiConfig {
  host: string
  port: number
}

export interface HubConfig {
  host: string
  port: number
  path: string
  public_host: string
  public_url: string
  /** As configured. Empty means each hub process resolves its own. */
  instance_id: string
  session_ttl_seconds: number
  reaper_interval_seconds: number
  ownership_check_seconds: number
}

export interface ReactorConfig {
  enabled: boolean
  interval_seconds: number
  lease_seconds: number
  event_batch: number
  notify_timeout: number
}

/** `DATABASE_URL` in parts. The plane never sends the string. */
export interface DatabaseConfig {
  driver: string
  host: string
  port: number | null
  database: string
  echo: boolean
  pool_size: number
  pool_max_overflow: number
  pool_pre_ping: boolean
}

export interface MetricsConfig {
  timeout: number
  window: string
  step: string
  /** The shared `default=` endpoint. Empty means no store is configured at all. */
  shared_url: string
  /** Where the fleet is told to push. Empty means enrollment renders `monitoring.enabled: false`. */
  remote_write_url: string
  /** `<namespace>/<name>` of the htpasswd Secret the plane maintains, or empty when it does not. */
  remote_write_auth_secret: string
  sources: MetricsSourceConfig[]
}

/** Every credential reduced to a boolean, and the one mode switch to its enum. */
export interface AuthConfig {
  admin_auth: "enabled" | "disabled" | (string & {})
  admin_password_hash_set: boolean
  session_signing_secret_set: boolean
  session_ttl_seconds: number
  operator_token_set: boolean
  huggingface_token_set: boolean
}

export interface SetupConfig {
  operator_image: string
  chart_ref: string
  chart_version: string
  chart_private: boolean
}

export interface PlaneConfig {
  app: AppConfig
  api: ApiConfig
  hub: HubConfig
  reactor: ReactorConfig
  database: DatabaseConfig
  metrics: MetricsConfig
  auth: AuthConfig
  setup: SetupConfig
}
