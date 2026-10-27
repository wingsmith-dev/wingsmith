import type { MetricsQuery } from "./api"

/**
 * The spans a chart can be read over, and what each one asks the store for.
 *
 * Three numbers move together and they are not independent. `span` is how far
 * back the curve reaches, `step` is how coarse each point is, an hour of 5m
 * points and a week of 5m points are the same picture and very different
 * queries, and `window` is the rate and quantile lookback, which must not be
 * finer than the step or the curve draws spikes between the points that carry
 * them.
 */
export interface TimeWindow {
  key: "15m" | "1h" | "24h" | "7d"
  label: string
  span: number
  step: string
  window: string
}

/**
 * `15m` is the finest this can be and it is set by the scrape interval, not by
 * taste: the gateway is scraped every 30s, so a `rate()` over anything under 2m
 * has too few samples to be sure of and empties out: asked for a 1m window, the
 * request rate comes back 0.0 while traffic is running, which is worse than
 * coarse. 30s steps over a 2m window is the floor that holds.
 *
 * It exists for one job the other three cannot do: watching a load test while it
 * runs. Over 24h a ten-minute run is a single 15m point.
 */
export const TIME_WINDOWS: readonly TimeWindow[] = [
  { key: "15m", label: "15m", span: 900, step: "30s", window: "2m" },
  { key: "1h", label: "1h", span: 3600, step: "1m", window: "5m" },
  { key: "24h", label: "24h", span: 24 * 3600, step: "15m", window: "15m" },
  { key: "7d", label: "7d", span: 7 * 24 * 3600, step: "1h", window: "1h" },
]

export const DEFAULT_WINDOW: TimeWindow = TIME_WINDOWS[2]

/** What a tenant's page opens on: an hour, fine enough to see a run in. */
export const DEFAULT_SERVING_WINDOW: TimeWindow = TIME_WINDOWS[1]

/** The window as the metrics query spells it, with series asked for. */
export function windowQuery(window: TimeWindow): MetricsQuery {
  return {
    window: window.window,
    step: window.step,
    span: window.span,
    series: true,
  }
}
