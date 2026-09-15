/**
 * The measurement vocabulary, as the console spells it.
 *
 * Mirrors `app/modules/metrics/registry.py`: the same names, the same units,
 * and the same families. It carries two things the wire does not — which group
 * a metric belongs in, so nineteen serving numbers read as four short lists
 * rather than one wall, and how a metric behaves as scope widens, so nothing in
 * this console adds up a number that must not be added up.
 *
 * A sample arrives with the plane's own title. The catalogue's title wins where
 * one exists, because two of these measurements are told apart only by their
 * label and the console is where somebody reads it.
 */

import type { MetricSample, MetricUnit } from "./types"

/**
 * `serving` is the front door's own account of a request and is not a third
 * view of the `inference` numbers beside it. The two disagreeing is not a bug:
 * the gateway's latency includes the hop and the queue in front of it, and it
 * counts the requests that never reached an engine at all.
 */
export type MetricFamily = "gpu" | "inference" | "serving"

export type MetricGroup =
  | "occupancy"
  | "thermal"
  | "reliability"
  | "latency"
  | "throughput"
  | "load"
  | "shape"

/**
 * What widening the scope does to the number.
 *
 * `aggregate` rises with the number of replicas or cards in scope and is the
 * only kind that may be summed. `invariant` is an average or a quantile across
 * scope. `per_stream` is what one caller experiences and stays put however many
 * replicas serve it. `per_request` describes the requests, not the fleet.
 */
export type MetricScale =
  "aggregate" | "invariant" | "per_stream" | "per_request"

export interface MetricDefinition {
  name: string
  /** The human label. Short enough for a tile, exact enough to stand alone. */
  title: string
  unit: MetricUnit
  family: MetricFamily
  group: MetricGroup
  scale: MetricScale
  /** Shown beside the number wherever the name alone would mislead. */
  note?: string
}

const GPU: MetricDefinition[] = [
  {
    name: "gpu.utilization",
    title: "GPU utilization",
    unit: "percent",
    family: "gpu",
    group: "occupancy",
    scale: "invariant",
  },
  {
    name: "gpu.memory_used",
    title: "Framebuffer used",
    unit: "mib",
    family: "gpu",
    group: "occupancy",
    scale: "aggregate",
  },
  {
    name: "gpu.memory_free",
    title: "Framebuffer free",
    unit: "mib",
    family: "gpu",
    group: "occupancy",
    scale: "aggregate",
  },
  {
    name: "gpu.memory_utilization",
    title: "Memory bandwidth",
    unit: "percent",
    family: "gpu",
    group: "occupancy",
    scale: "invariant",
  },
  {
    name: "gpu.sm_clock",
    title: "SM clock",
    unit: "mhz",
    family: "gpu",
    group: "occupancy",
    scale: "invariant",
  },
  {
    name: "gpu.temperature",
    title: "GPU temperature",
    unit: "celsius",
    family: "gpu",
    group: "thermal",
    scale: "invariant",
    note: "The hottest card in scope, not the mean.",
  },
  {
    name: "gpu.memory_temperature",
    title: "Memory temperature",
    unit: "celsius",
    family: "gpu",
    group: "thermal",
    scale: "invariant",
    note: "The hottest card in scope, not the mean.",
  },
  {
    name: "gpu.power",
    title: "Power draw",
    unit: "watts",
    family: "gpu",
    group: "thermal",
    scale: "aggregate",
  },
  {
    name: "gpu.throttle_reasons",
    title: "Throttle reasons",
    unit: "count",
    family: "gpu",
    group: "thermal",
    scale: "invariant",
  },
  {
    name: "gpu.ecc_uncorrectable",
    title: "Uncorrectable ECC",
    unit: "count",
    family: "gpu",
    group: "reliability",
    scale: "aggregate",
  },
  {
    name: "gpu.pcie_replays",
    title: "PCIe replays",
    unit: "count",
    family: "gpu",
    group: "reliability",
    scale: "aggregate",
  },
]

const INFERENCE: MetricDefinition[] = [
  {
    name: "inference.ttft_p50",
    title: "Time to first token (p50)",
    unit: "seconds",
    family: "inference",
    group: "latency",
    scale: "invariant",
    note: "Its own registered series, not a percentile knob on the p95 one.",
  },
  {
    name: "inference.ttft_p90",
    title: "Time to first token (p90)",
    unit: "seconds",
    family: "inference",
    group: "latency",
    scale: "invariant",
    note: "Its own registered series, not a percentile knob on the p95 one.",
  },
  {
    name: "inference.ttft",
    title: "Time to first token (p95)",
    unit: "seconds",
    family: "inference",
    group: "latency",
    scale: "invariant",
  },
  {
    name: "inference.queue_time",
    title: "Time waiting in queue (p95)",
    unit: "seconds",
    family: "inference",
    group: "latency",
    scale: "invariant",
  },
  {
    name: "inference.prefill_time",
    title: "Prefill time (p95)",
    unit: "seconds",
    family: "inference",
    group: "latency",
    scale: "invariant",
  },
  {
    name: "inference.decode_time",
    title: "Decode time (p95)",
    unit: "seconds",
    family: "inference",
    group: "latency",
    scale: "invariant",
  },
  {
    name: "inference.tpot_p50",
    title: "Time per output token (p50)",
    unit: "seconds",
    family: "inference",
    group: "latency",
    scale: "invariant",
    note: "Its own registered series, not a percentile knob on the p95 one.",
  },
  {
    name: "inference.tpot",
    title: "Time per output token (p95)",
    unit: "seconds",
    family: "inference",
    group: "latency",
    scale: "invariant",
  },
  {
    name: "inference.latency",
    title: "End-to-end latency (p95)",
    unit: "seconds",
    family: "inference",
    group: "latency",
    scale: "invariant",
  },
  {
    name: "inference.output_tokens_per_second",
    title: "Output tokens/sec, all replicas",
    unit: "per_second",
    family: "inference",
    group: "throughput",
    scale: "aggregate",
    note: "Summed over every replica in scope, so it rises with scale. This is not the number an acceptance floor names.",
  },
  {
    name: "inference.output_tokens_per_stream",
    title: "Output tokens/sec, one stream",
    unit: "per_second",
    family: "inference",
    group: "throughput",
    scale: "per_stream",
    note: "What a single caller experiences. Scale-invariant, never summed, and the number a floor of 30 tokens/second means.",
  },
  {
    name: "inference.input_tokens_per_second",
    title: "Input tokens/sec, all replicas",
    unit: "per_second",
    family: "inference",
    group: "throughput",
    scale: "aggregate",
  },
  {
    name: "inference.requests_finished",
    title: "Requests completed/sec",
    unit: "per_second",
    family: "inference",
    group: "throughput",
    scale: "aggregate",
    note: "Requests the engine finished. It counts no request that never reached a cluster, so it is a rate, not a success rate.",
  },
  {
    name: "inference.requests_running",
    title: "Requests running",
    unit: "count",
    family: "inference",
    group: "load",
    scale: "aggregate",
  },
  {
    name: "inference.requests_waiting",
    title: "Requests queued",
    unit: "count",
    family: "inference",
    group: "load",
    scale: "aggregate",
  },
  {
    name: "inference.preemptions",
    title: "Preemptions/sec",
    unit: "per_second",
    family: "inference",
    group: "load",
    scale: "aggregate",
  },
  {
    name: "inference.kv_cache_utilization",
    title: "KV cache used",
    unit: "percent",
    family: "inference",
    group: "load",
    scale: "invariant",
  },
  {
    name: "inference.prefix_cache_hit_rate",
    title: "Prefix cache hit rate",
    unit: "percent",
    family: "inference",
    group: "load",
    scale: "invariant",
  },
  {
    name: "inference.prompt_tokens_per_request",
    title: "Prompt tokens per request (p95)",
    unit: "count",
    family: "inference",
    group: "shape",
    scale: "per_request",
  },
  {
    name: "inference.generation_tokens_per_request",
    title: "Generated tokens per request (p95)",
    unit: "count",
    family: "inference",
    group: "shape",
    scale: "per_request",
  },
]

const SERVING: MetricDefinition[] = [
  {
    name: "serving.availability",
    title: "Availability",
    unit: "percent",
    family: "serving",
    group: "reliability",
    scale: "invariant",
    note: "The share of requests the front door answered. It has no floor of its own — whether a figure is an outage is a threshold somebody declared, not a property of the measurement.",
  },
  {
    name: "serving.failure_ratio",
    title: "Failed requests",
    unit: "percent",
    family: "serving",
    group: "reliability",
    scale: "invariant",
    note: "Counted at the hop that saw the request, which is the only place it can be counted: an engine exporter never sees a request that reached no cluster.",
  },
  {
    name: "serving.requests_per_second",
    title: "Requests/sec, front door",
    unit: "per_second",
    family: "serving",
    group: "throughput",
    scale: "aggregate",
  },
  {
    name: "serving.input_tokens_per_second",
    title: "Input tokens/sec, front door",
    unit: "per_second",
    family: "serving",
    group: "throughput",
    scale: "aggregate",
  },
  {
    name: "serving.output_tokens_per_second",
    title: "Output tokens/sec, front door",
    unit: "per_second",
    family: "serving",
    group: "throughput",
    scale: "aggregate",
  },
  {
    name: "serving.latency_p50",
    title: "Latency (p50), front door",
    unit: "seconds",
    family: "serving",
    group: "latency",
    scale: "invariant",
    note: "The whole hop, including the queue in front of it and the requests that failed before an engine saw them.",
  },
  {
    name: "serving.latency_p95",
    title: "Latency (p95), front door",
    unit: "seconds",
    family: "serving",
    group: "latency",
    scale: "invariant",
  },
  {
    name: "serving.latency_p99",
    title: "Latency (p99), front door",
    unit: "seconds",
    family: "serving",
    group: "latency",
    scale: "invariant",
  },
  {
    name: "serving.ttft_p50",
    title: "Time to first token (p50), front door",
    unit: "seconds",
    family: "serving",
    group: "latency",
    scale: "invariant",
  },
  {
    name: "serving.ttft_p95",
    title: "Time to first token (p95), front door",
    unit: "seconds",
    family: "serving",
    group: "latency",
    scale: "invariant",
  },
  {
    name: "serving.ttft_p99",
    title: "Time to first token (p99), front door",
    unit: "seconds",
    family: "serving",
    group: "latency",
    scale: "invariant",
  },
  {
    name: "serving.cache_hit_ratio",
    title: "Cached input tokens, front door",
    unit: "percent",
    family: "serving",
    group: "throughput",
    scale: "invariant",
    note: "Cached input over input, off the usage object we return — the figure a caller can reconcile against their own prompt_tokens_details.cached_tokens.",
  },
  {
    name: "serving.requests_in_flight",
    title: "Requests in flight, front door",
    unit: "count",
    family: "serving",
    group: "load",
    scale: "aggregate",
  },
]

export const CATALOGUE: readonly MetricDefinition[] = [
  ...GPU,
  ...INFERENCE,
  ...SERVING,
]

const BY_NAME = new Map(CATALOGUE.map((metric) => [metric.name, metric]))

export const GROUP_LABELS: Record<MetricGroup, string> = {
  occupancy: "Occupancy",
  thermal: "Thermal",
  reliability: "Reliability",
  latency: "Latency",
  throughput: "Throughput",
  load: "Load",
  shape: "Request shape",
}

const GROUP_ORDER: Record<MetricFamily, MetricGroup[]> = {
  gpu: ["occupancy", "thermal", "reliability"],
  inference: ["latency", "throughput", "load", "shape"],
  serving: ["reliability", "latency", "throughput", "load"],
}

/**
 * Why no failure rate is charted here, said where somebody would go looking.
 *
 * Every series in this catalogue is counted by an engine exporter inside a
 * cluster, and a request that never reached a cluster is counted by nobody. A
 * ratio built out of these would read a total outage as a quiet afternoon.
 */
export const AVAILABILITY_SOURCE =
  "No failure or availability rate is charted here: engine exporters only count requests that reached them, so a ratio built from these series would read an outage as no traffic. That number comes from the gateway's own /metrics, which is the `serving` family an endpoint's page reads."

/** The tiles a panel opens with. Everything else in the family is a pick away. */
export const DEFAULT_TILES: Record<MetricFamily, string[]> = {
  gpu: [
    "gpu.utilization",
    "gpu.memory_used",
    "gpu.temperature",
    "gpu.power",
    "gpu.sm_clock",
    "gpu.memory_utilization",
  ],
  inference: [
    "inference.ttft",
    "inference.ttft_p50",
    "inference.ttft_p90",
    "inference.tpot",
    "inference.queue_time",
    "inference.latency",
    "inference.output_tokens_per_second",
    "inference.output_tokens_per_stream",
    "inference.input_tokens_per_second",
    "inference.requests_finished",
    "inference.requests_running",
    "inference.requests_waiting",
    "inference.kv_cache_utilization",
  ],
  serving: [
    "serving.availability",
    "serving.failure_ratio",
    "serving.requests_per_second",
    "serving.latency_p50",
    "serving.latency_p95",
    "serving.latency_p99",
    "serving.ttft_p50",
    "serving.ttft_p95",
    "serving.ttft_p99",
    "serving.requests_in_flight",
    "serving.input_tokens_per_second",
    "serving.output_tokens_per_second",
  ],
}

export function metricDefinition(name: string): MetricDefinition | undefined {
  return BY_NAME.get(name)
}

/** The catalogue's label where there is one, the plane's where there is not. */
export function metricTitle(name: string, fallback: string): string {
  return BY_NAME.get(name)?.title ?? fallback
}

export function metricsInFamily(family: MetricFamily): MetricDefinition[] {
  return CATALOGUE.filter((metric) => metric.family === family)
}

export interface MetricGroupList {
  group: MetricGroup
  label: string
  metrics: MetricDefinition[]
}

/**
 * A family split into its groups, in reading order, with empty groups dropped.
 * `only` narrows to a chosen set of names and keeps the catalogue's order, so
 * picking metrics never reorders the ones already on screen.
 */
export function metricGroups(
  family: MetricFamily,
  only?: readonly string[]
): MetricGroupList[] {
  const wanted = only ? new Set(only) : null
  return GROUP_ORDER[family]
    .map((group) => ({
      group,
      label: GROUP_LABELS[group],
      metrics: CATALOGUE.filter(
        (metric) =>
          metric.family === family &&
          metric.group === group &&
          (!wanted || wanted.has(metric.name))
      ),
    }))
    .filter((entry) => entry.metrics.length > 0)
}

/**
 * Whether adding this metric up across members means anything.
 *
 * False for quantiles, for per-request distributions, and for the per-stream
 * throughput — summing that one turns "each caller sees 30 tokens a second"
 * into a fleet number that grows every time a replica is added.
 */
export function isSummable(name: string): boolean {
  return BY_NAME.get(name)?.scale === "aggregate"
}

/**
 * Sums one metric over a set of members, and refuses to do it for any metric
 * the catalogue does not call summable. Members that reported nothing are not
 * zeroes; if none of them reported, the answer is `null`.
 */
export function sumAcross(
  name: string,
  samples: readonly MetricSample[]
): number | null {
  if (!isSummable(name)) {
    throw new Error(`${name} is not summable across members`)
  }
  const values = samples
    .filter((sample) => sample.metric === name)
    .map((sample) => sample.value)
    .filter((value): value is number => value !== null)
  if (values.length === 0) return null
  return values.reduce((total, value) => total + value, 0)
}
