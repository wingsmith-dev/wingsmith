import { Panel } from "./Panel"

/** The windows the header offers. The chart is the only thing they change. */
export type TrafficWindow = "1h" | "24h" | "7d"

export const TRAFFIC_WINDOWS: TrafficWindow[] = ["1h", "24h", "7d"]

/**
 * Serving traffic, entirely from the mockup.
 *
 * There is no fleet-wide serving measurement to read. `/v1/metrics/{scope}/{ref}`
 * takes a cluster, pool, node, device or deployment and the console's
 * `MetricScope` has no endpoint and no fleet member, so "req/s and p95 across all
 * endpoints" cannot be asked for — and by rule 4 of the architecture the request
 * path never crosses the control plane, so the numbers exist in the observability
 * stack rather than here. Every point, both legend values and all three windows
 * below are placeholders.
 *
 * TODO-API: fleet-wide serving traffic. Needs a `fleet` (or `endpoint`) scope on
 * `GET /v1/metrics/...` returning `inference.requests_finished` and
 * `inference.ttft_p95` as series over the requested window, summed across
 * clusters by the plane — the console must not fan out per cluster and add
 * quantiles together.
 */
const MOCK_TRAFFIC: Record<
  TrafficWindow,
  { rate: string; latency: string; requests: number[]; p95: number[] }
> = {
  "1h": {
    rate: "1,402",
    latency: "388ms",
    requests: [
      58, 52, 61, 47, 44, 51, 38, 42, 35, 40, 33, 36, 30, 34, 41, 37, 32,
    ],
    p95: [
      104, 101, 108, 99, 96, 100, 92, 95, 88, 94, 86, 90, 84, 88, 96, 92, 89,
    ],
  },
  "24h": {
    rate: "1,284",
    latency: "412ms",
    requests: [
      96, 92, 99, 88, 74, 66, 58, 44, 38, 42, 30, 34, 26, 40, 52, 46, 50,
    ],
    p95: [
      112, 110, 113, 108, 104, 106, 100, 96, 92, 98, 86, 90, 72, 60, 88, 94, 90,
    ],
  },
  "7d": {
    rate: "1,106",
    latency: "437ms",
    requests: [
      104, 98, 90, 84, 88, 76, 70, 64, 58, 62, 54, 60, 48, 56, 66, 72, 68,
    ],
    p95: [
      118, 114, 116, 110, 112, 104, 100, 96, 94, 100, 90, 96, 82, 78, 92, 100,
      96,
    ],
  },
}

const GRID = [8, 45, 82, 119]
const WIDTH = 640

function points(series: number[]): string {
  const step = WIDTH / (series.length - 1)
  return series.map((value, i) => `${Math.round(i * step)},${value}`).join(" ")
}

/**
 * The shape of serving traffic over one window. Trend only — the analysis lives
 * in Benchmarks, and this panel exists to say whether the fleet is busier than
 * it was, not why.
 */
export function ServingTraffic({ window }: { window: TrafficWindow }) {
  const traffic = MOCK_TRAFFIC[window]

  return (
    <Panel
      title="Serving traffic"
      description={`all endpoints · last ${window} · placeholder measurement`}
      action={
        <div className="flex flex-wrap items-center gap-3.5">
          <span className="inline-flex items-center gap-1.5 text-2xs text-muted-foreground">
            <span className="size-2 rounded-full bg-primary" />
            req/s{" "}
            <span className="font-mono text-foreground">{traffic.rate}</span>
          </span>
          <span className="inline-flex items-center gap-1.5 text-2xs text-muted-foreground">
            <span className="size-2 rounded-full bg-amber-500" />
            p95{" "}
            <span className="font-mono text-foreground">{traffic.latency}</span>
          </span>
        </div>
      }
    >
      <svg
        role="img"
        aria-label={`Requests per second and p95 latency over the last ${window}`}
        viewBox={`0 0 ${WIDTH} 130`}
        preserveAspectRatio="none"
        className="block h-32 w-full"
      >
        {GRID.map((y) => (
          <line
            key={y}
            x1={0}
            y1={y}
            x2={WIDTH}
            y2={y}
            className="stroke-foreground/10"
            strokeWidth={1}
          />
        ))}
        <polyline
          points={points(traffic.requests)}
          fill="none"
          strokeWidth={1.75}
          strokeLinecap="round"
          strokeLinejoin="round"
          className="stroke-primary"
        />
        <polyline
          points={points(traffic.p95)}
          fill="none"
          strokeWidth={1.75}
          strokeLinecap="round"
          strokeLinejoin="round"
          className="stroke-amber-500"
        />
      </svg>
    </Panel>
  )
}
