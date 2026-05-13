import type { MetricsQuery } from "./api"

/**
 * The spans a chart can be read over, and what each one asks the store for.
 *
 * Three numbers move together and they are not independent. `span` is how far
 * back the curve reaches, `step` is how coarse each point is — an hour of 5m
 * points and a week of 5m points are the same picture and very different
 * queries — and `window` is the rate and quantile lookback, which must not be
 * finer than the step or the curve draws spikes between the points that carry
 * them.
 */
export interface TimeWindow {
  key: "1h" | "24h" | "7d"
  label: string
  span: number
  step: string
  window: string
}

export const TIME_WINDOWS: readonly TimeWindow[] = [
  { key: "1h", label: "1h", span: 3600, step: "1m", window: "5m" },
  { key: "24h", label: "24h", span: 24 * 3600, step: "15m", window: "15m" },
  { key: "7d", label: "7d", span: 7 * 24 * 3600, step: "1h", window: "1h" },
]

export const DEFAULT_WINDOW: TimeWindow = TIME_WINDOWS[1]

/** The window as the metrics query spells it, with series asked for. */
export function windowQuery(window: TimeWindow): MetricsQuery {
  return {
    window: window.window,
    step: window.step,
    span: window.span,
    series: true,
  }
}
