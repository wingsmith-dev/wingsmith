/**
 * The unit economics of one workload, computed entirely in the browser.
 *
 * Nothing here is a fact the plane holds. The plane records what a workload is
 * and how it is behaving; it records no price and no cost, and until it does
 * every figure below is the operator's own model of a workload they can see.
 * That is the whole reason this is a module of pure functions: the numbers are
 * an argument, and an argument has to be readable and testable on its own.
 *
 * The asymmetry the whole file is built around: **cost is fixed per hour and
 * revenue is variable**. A card costs the same idle as saturated, so margin is
 * violently nonlinear in utilization and an average utilization is not an
 * answer. Everything that produces a currency figure integrates over the day's
 * curve rather than multiplying a mean.
 */

/** Points in the demand curve, and what each one spans. A day, every two hours. */
export const DAY_POINTS = 12
export const HOURS_PER_POINT = 24 / DAY_POINTS

/** Billing months are quoted in hours, not in calendar days. 730 = 365×24/12. */
export const HOURS_PER_MONTH = 730

/**
 * What a cached input token costs in capacity, as a share of a fresh one.
 *
 * Zero, and deliberately not an input. A cache hit skips the prefill forward
 * pass; what remains is reading KV blocks that are already resident, which is
 * memory traffic and not compute. It is not literally free, but it is small
 * enough that making it a field would invite it to be tuned in place of the
 * things that actually move the answer.
 */
const CACHED_PREFILL_SHARE = 0

/**
 * Prefill runs this many tokens/sec for every output token/sec, still assumed.
 *
 * Not arbitrary any more, and not measured either. vLLM reports prefill and
 * decode separately per request — `request_prefill_time_seconds` against
 * `request_prefill_kv_computed_tokens`, which excludes prompt-cache hits — and
 * on a `gpt-oss-20b` replica those come out around 61 to 1. That figure is an
 * upper bound: both halves are sums of per-request wall time under concurrent
 * load, so neither is a clean per-replica rate, and a FLOP budget for the card
 * will not support 61x once it is expressed as an aggregate.
 *
 * 15 is deliberately nearer the pessimistic end of the range the evidence
 * allows. It is chosen to be wrong in the direction that understates capacity,
 * because the number it replaced was wrong by six-fold in that same direction
 * and made a profitable workload read as a loss. It stops being a guess when a
 * run captures those two counters against wall-clock and `summaries.py` reads a
 * prefill rate like any other field.
 */
export const DEFAULT_PREFILL_MULTIPLE = 15

/**
 * A day of offered load, one point every two hours, as a fraction of capacity.
 *
 * Values above 1 are meaningful and are kept: they are demand the workload
 * cannot serve, and clipping them is where lost revenue comes from. Low
 * overnight, a morning ramp, a business-hours plateau, an evening decay.
 */
export const DEFAULT_UTILIZATION_CURVE = [
  0.12, 0.08, 0.1, 0.34, 0.62, 0.78, 0.85, 0.8, 0.66, 0.48, 0.3, 0.18,
]

/** Where the capacity figure came from. Measured and assumed are not the same claim. */
export type CapacitySource = "measured" | "assumed" | "operating-point"

export interface EconomicsInputs {
  /** $ per million fresh (uncached) input tokens. */
  inputPricePerMTok: number
  /** $ per million output tokens. */
  outputPricePerMTok: number
  /** $ per million input tokens served from the prompt cache. */
  cachedInputPricePerMTok: number

  /**
   * Input tokens prefilled for every output token decoded — the `input:output`
   * half of the traffic mix, written as a ratio to 1 output token.
   */
  inputPerOutputToken: number
  /** Share of those input tokens that hit the prompt cache, 0…1. */
  cacheHitRate: number

  /** Decode throughput of ONE replica, output tokens/sec. */
  outputTokensPerSecond: number
  /** Prefill throughput of ONE replica, input tokens/sec. */
  prefillTokensPerSecond: number
  capacitySource: CapacitySource
  replicas: number

  /** Cards the workload holds, from live placement. */
  gpuCount: number
  /** $ per GPU-hour. Spot: this outfit buys nothing else. */
  spotRatePerGpuHour: number
  /** Set to override `gpuCount × spotRatePerGpuHour` with a flat figure. */
  costPerHourOverride: number | null

  /** `DAY_POINTS` fractions of capacity, midnight first. May exceed 1. */
  utilization: number[]

  /** A reservation, a commitment or bought hardware. Zero means pure spot. */
  upfrontCommitment: number

  /** What a public provider charges for the same model, for comparison. */
  referenceInputPricePerMTok: number | null
  referenceOutputPricePerMTok: number | null
}

/**
 * Capacity in one currency: output-token-equivalents per second.
 *
 * Prefill and decode do not consume a machine at the same rate, so a single
 * scalar "tokens/sec" is wrong and this file never uses one. The operator gives
 * two measurable rates — decode tokens/sec and prefill tokens/sec — and the
 * model converts the second into the first. Serving `prefillTokensPerSecond`
 * input tokens/sec occupies the same machine that would otherwise decode
 * `outputTokensPerSecond` output tokens/sec, so one uncached input token costs
 * `outputTokensPerSecond / prefillTokensPerSecond` output-token-equivalents.
 * That number is the prefill tax, and it is why a request with a long prompt
 * and a short answer is not cheap.
 *
 * The rate is the field and the tax is the derivation, rather than the reverse,
 * because a rate is something a benchmark reports and a tax is not.
 *
 * A measured `outputTokensPerSecond` comes from `peak_output_tokens_per_second`,
 * which is an **upper** bound: the best a sweep ever saw, usually at a
 * concurrency where latency has already left the acceptable band. The
 * sustainable figure is the knee of the load curve, which the Workbench will
 * expose later. Sizing revenue on peak overstates it, and by more than a little.
 */
export interface Capacity {
  /** Output-token-equivalents per second across every replica. */
  otePerSecond: number
  /** Output-token-equivalents consumed by one uncached input token. */
  prefillTax: number
}

export function capacityOf(inputs: EconomicsInputs): Capacity {
  const perReplica = Math.max(inputs.outputTokensPerSecond, 0)
  const prefill = Math.max(inputs.prefillTokensPerSecond, 0)
  return {
    otePerSecond: perReplica * Math.max(inputs.replicas, 0),
    prefillTax: prefill > 0 ? perReplica / prefill : 0,
  }
}

/**
 * One output token and the input tokens that had to be prefilled to produce it.
 *
 * The whole model is denominated in this unit, because the traffic mix is a
 * ratio and not three independent streams: you cannot sell an output token
 * without paying for its prompt.
 */
export interface DemandUnit {
  /** Fresh input tokens per output token. */
  freshInput: number
  /** Cached input tokens per output token. */
  cachedInput: number
  /** $ earned per unit. */
  revenue: number
  /** Output-token-equivalents of capacity per unit. Always ≥ 1. */
  ote: number
}

export function demandUnit(inputs: EconomicsInputs): DemandUnit {
  const ratio = Math.max(inputs.inputPerOutputToken, 0)
  const hit = clamp(inputs.cacheHitRate, 0, 1)
  const fresh = ratio * (1 - hit)
  const cached = ratio * hit
  const { prefillTax } = capacityOf(inputs)
  return {
    freshInput: fresh,
    cachedInput: cached,
    revenue:
      (inputs.outputPricePerMTok +
        fresh * inputs.inputPricePerMTok +
        cached * inputs.cachedInputPricePerMTok) /
      1e6,
    ote: 1 + fresh * prefillTax + cached * prefillTax * CACHED_PREFILL_SHARE,
  }
}

/** What the workload earns in an hour with every replica saturated. */
export function revenuePerHourAtCapacity(inputs: EconomicsInputs): number {
  const unit = demandUnit(inputs)
  const { otePerSecond } = capacityOf(inputs)
  if (otePerSecond <= 0 || unit.ote <= 0) return 0
  return (otePerSecond / unit.ote) * 3600 * unit.revenue
}

/** Output tokens/sec the workload sustains when saturated, after the prefill tax. */
export function outputTokensPerSecondAtCapacity(
  inputs: EconomicsInputs
): number {
  const unit = demandUnit(inputs)
  const { otePerSecond } = capacityOf(inputs)
  if (unit.ote <= 0) return 0
  return otePerSecond / unit.ote
}

/**
 * Cost per hour, derived from placement unless the operator overrode it.
 *
 * Spot only. Nothing here prices on-demand or a capacity block, because this
 * outfit does not buy them — and nothing here prices interruption either. A
 * spot reclaim is a risk the model is silent about, not one it has covered.
 */
export function costPerHour(inputs: EconomicsInputs): number {
  if (inputs.costPerHourOverride !== null)
    return Math.max(inputs.costPerHourOverride, 0)
  return Math.max(inputs.gpuCount, 0) * Math.max(inputs.spotRatePerGpuHour, 0)
}

export interface DayPoint {
  /** Hour of day this point starts at. */
  hour: number
  /** Offered load as a fraction of capacity, as given. */
  offered: number
  /** What was actually served, capped at capacity. */
  served: number
  /** $ earned across this point's two hours. */
  revenue: number
  /** $ that would have been earned had capacity not clipped it. */
  clipped: number
}

export interface DayEconomics {
  points: DayPoint[]
  meanOffered: number
  meanServed: number
  peakOffered: number
  revenuePerDay: number
  /** Demand the workload could not serve, in the money it would have earned. */
  clippedRevenuePerDay: number
  costPerDay: number
  marginPerDay: number
  /** Gross margin as a share of revenue, or null when nothing was earned. */
  marginRatio: number | null
  outputTokensPerDay: number
  freshInputTokensPerDay: number
  cachedInputTokensPerDay: number
  /** $ of cost per million output tokens actually produced, or null at zero volume. */
  costPerMOutputTokens: number | null
  /** The same over every token billed, input and output alike. */
  costPerMBilledTokens: number | null
  /** GPU-hours held and not used, and what they cost. */
  idleGpuHoursPerDay: number
  idleCostPerDay: number
}

/**
 * The day integrated point by point, which is the only honest way to do it.
 *
 * A day that averages 50% by sitting at 100% for twelve hours and 0% for twelve
 * earns exactly what a flat 50% day earns — until it does not, because the peak
 * half was really at 130% and a third of that hour's demand hit a wall. Only
 * the curve knows. `clipped` is that lost revenue, kept as its own figure
 * rather than folded silently into the total.
 */
export function summariseDay(inputs: EconomicsInputs): DayEconomics {
  const unit = demandUnit(inputs)
  const capacity = capacityOf(inputs)
  const fullHour = revenuePerHourAtCapacity(inputs)
  const curve = normaliseCurve(inputs.utilization)

  let served = 0
  let offered = 0
  let peak = 0
  const points = curve.map((value, index) => {
    const capped = Math.min(Math.max(value, 0), 1)
    const over = Math.max(value - 1, 0)
    served += capped
    offered += Math.max(value, 0)
    peak = Math.max(peak, Math.max(value, 0))
    return {
      hour: index * HOURS_PER_POINT,
      offered: Math.max(value, 0),
      served: capped,
      revenue: capped * fullHour * HOURS_PER_POINT,
      clipped: over * fullHour * HOURS_PER_POINT,
    }
  })

  const meanServed = served / curve.length
  const revenuePerDay = points.reduce((sum, point) => sum + point.revenue, 0)
  const clippedRevenuePerDay = points.reduce(
    (sum, point) => sum + point.clipped,
    0
  )
  const hourly = costPerHour(inputs)
  const costPerDay = hourly * 24
  const marginPerDay = revenuePerDay - costPerDay

  const outputTokensPerDay =
    unit.ote > 0 ? (capacity.otePerSecond / unit.ote) * meanServed * 86400 : 0
  const billed = outputTokensPerDay * (1 + unit.freshInput + unit.cachedInput)
  const idleGpuHoursPerDay =
    Math.max(inputs.gpuCount, 0) * 24 * (1 - meanServed)

  return {
    points,
    meanOffered: offered / curve.length,
    meanServed,
    peakOffered: peak,
    revenuePerDay,
    clippedRevenuePerDay,
    costPerDay,
    marginPerDay,
    marginRatio: revenuePerDay > 0 ? marginPerDay / revenuePerDay : null,
    outputTokensPerDay,
    freshInputTokensPerDay: outputTokensPerDay * unit.freshInput,
    cachedInputTokensPerDay: outputTokensPerDay * unit.cachedInput,
    costPerMOutputTokens:
      outputTokensPerDay > 0 ? costPerDay / (outputTokensPerDay / 1e6) : null,
    costPerMBilledTokens: billed > 0 ? costPerDay / (billed / 1e6) : null,
    idleGpuHoursPerDay,
    idleCostPerDay: idleGpuHoursPerDay * Math.max(inputs.spotRatePerGpuHour, 0),
  }
}

export interface BreakEven {
  /**
   * The flat utilization at which an hour pays for itself. Closed form, because
   * revenue is linear in utilization below capacity. Null when no utilization
   * reaches it — zero capacity, zero price, or a day already free.
   */
  flatUtilization: number | null
  /**
   * The same question asked of *this* day's shape: the factor the whole curve
   * must be multiplied by before the day breaks even, and the offered load the
   * day then averages.
   *
   * The mean load that is *served* at break-even is always `flatUtilization` —
   * revenue is proportional to served capacity and nothing else, so that number
   * carries no information about the shape. The offered mean does: it equals
   * the flat answer while nothing clips, and runs away from it as soon as the
   * peak hits the ceiling, because demand thrown away at midday has to be paid
   * for somewhere. Null when no amount of demand gets there.
   */
  curveScale: number | null
  curveMeanOffered: number | null
  /** Output tokens a day must produce to cover its cost, at these prices. */
  outputTokensPerDay: number | null
  /**
   * The output price that would break this day even at today's utilization —
   * the inverse question, and often the one being asked. Negative means the day
   * is already above water with output given away.
   */
  outputPricePerMTok: number | null
}

/** The largest factor worth searching. Beyond this the curve is all clipping. */
const MAX_CURVE_SCALE = 1000

export function breakEven(inputs: EconomicsInputs): BreakEven {
  const unit = demandUnit(inputs)
  const day = summariseDay(inputs)
  const fullHour = revenuePerHourAtCapacity(inputs)
  const hourly = costPerHour(inputs)

  const flat = fullHour > 0 ? hourly / fullHour : hourly === 0 ? 0 : null
  const curve = normaliseCurve(inputs.utilization)

  const revenueAtScale = (scale: number) =>
    curve.reduce(
      (sum, value) =>
        sum +
        Math.min(Math.max(value * scale, 0), 1) * fullHour * HOURS_PER_POINT,
      0
    )

  const curveScale = solveIncreasing(
    (scale) => revenueAtScale(scale) - day.costPerDay,
    0,
    MAX_CURVE_SCALE
  )
  const curveMeanOffered = curveScale === null ? null : mean(curve) * curveScale

  const servedUnitsPerDay =
    unit.ote > 0
      ? (capacityOf(inputs).otePerSecond / unit.ote) * day.meanServed * 86400
      : 0

  return {
    flatUtilization: flat,
    curveScale,
    curveMeanOffered,
    outputTokensPerDay: unit.revenue > 0 ? day.costPerDay / unit.revenue : null,
    outputPricePerMTok:
      servedUnitsPerDay > 0
        ? (day.costPerDay / servedUnitsPerDay) * 1e6 -
          unit.freshInput * inputs.inputPricePerMTok -
          unit.cachedInput * inputs.cachedInputPricePerMTok
        : null,
  }
}

/**
 * Days to recover an upfront commitment out of margin.
 *
 * Null on pure spot rental, which is the normal case here: there is no capex,
 * so payback is not a small number, it is not a question. Null also when the
 * day loses money — a negative payback period reads as a date, and it is not one.
 */
export function paybackDays(inputs: EconomicsInputs): number | null {
  const commitment = Math.max(inputs.upfrontCommitment, 0)
  if (commitment === 0) return null
  const margin = summariseDay(inputs).marginPerDay
  if (margin <= 0) return null
  return commitment / margin
}

export interface Reference {
  /** Blended $/MTok we charge for this traffic mix. */
  ours: number
  /** The same blend at the public provider's rates. */
  theirs: number
  /** Negative means we undercut them, as a share of their price. */
  delta: number
}

/**
 * Our blended price against a public provider's, on the same traffic mix.
 *
 * Blended and not compared rate by rate, because a mix that is 90% input makes
 * an output-price win irrelevant. The reference has no cached rate: a provider
 * quotes one, but we cannot claim their cache hit rate, so the comparison is
 * made on the fresh rates and the mix we actually serve.
 */
export function reference(inputs: EconomicsInputs): Reference | null {
  const { referenceInputPricePerMTok, referenceOutputPricePerMTok } = inputs
  if (
    referenceInputPricePerMTok === null ||
    referenceOutputPricePerMTok === null
  )
    return null
  const ratio = Math.max(inputs.inputPerOutputToken, 0)
  const unit = demandUnit(inputs)
  const ours = unit.revenue * 1e6
  const theirs =
    referenceOutputPricePerMTok + ratio * referenceInputPricePerMTok
  if (theirs <= 0) return null
  return { ours, theirs, delta: (ours - theirs) / theirs }
}

export type LeverKey =
  | "outputPricePerMTok"
  | "inputPricePerMTok"
  | "cachedInputPricePerMTok"
  | "cacheHitRate"
  | "inputPerOutputToken"
  | "outputTokensPerSecond"
  | "spotRatePerGpuHour"
  | "demand"

export interface Lever {
  key: LeverKey
  label: string
  /** How the current value reads, for a table cell. */
  unit: "currency-mtok" | "ratio" | "percent" | "rate" | "currency-gpu-hour"
  read: (inputs: EconomicsInputs) => number
  write: (inputs: EconomicsInputs, value: number) => EconomicsInputs
  /** The range worth searching for a break-even value of this lever. */
  range: [number, number]
}

/**
 * `demand` is not a field. It scales the whole day's curve, because "how much
 * traffic" is the lever an operator actually has and no single point of the
 * curve is it.
 */
export const LEVERS: readonly Lever[] = [
  {
    key: "outputPricePerMTok",
    label: "Output price",
    unit: "currency-mtok",
    read: (i) => i.outputPricePerMTok,
    write: (i, v) => ({ ...i, outputPricePerMTok: v }),
    range: [0, 500],
  },
  {
    key: "inputPricePerMTok",
    label: "Input price",
    unit: "currency-mtok",
    read: (i) => i.inputPricePerMTok,
    write: (i, v) => ({ ...i, inputPricePerMTok: v }),
    range: [0, 500],
  },
  {
    key: "cachedInputPricePerMTok",
    label: "Cached input price",
    unit: "currency-mtok",
    read: (i) => i.cachedInputPricePerMTok,
    write: (i, v) => ({ ...i, cachedInputPricePerMTok: v }),
    range: [0, 500],
  },
  {
    key: "cacheHitRate",
    label: "Cache hit rate",
    unit: "percent",
    read: (i) => i.cacheHitRate,
    write: (i, v) => ({ ...i, cacheHitRate: v }),
    range: [0, 1],
  },
  {
    key: "inputPerOutputToken",
    label: "Input per output token",
    unit: "ratio",
    read: (i) => i.inputPerOutputToken,
    write: (i, v) => ({ ...i, inputPerOutputToken: v }),
    range: [0, 200],
  },
  {
    key: "outputTokensPerSecond",
    label: "Decode capacity",
    unit: "rate",
    read: (i) => i.outputTokensPerSecond,
    write: (i, v) => ({ ...i, outputTokensPerSecond: v }),
    range: [0, 200_000],
  },
  {
    key: "spotRatePerGpuHour",
    label: "Spot rate",
    unit: "currency-gpu-hour",
    read: (i) => i.spotRatePerGpuHour,
    write: (i, v) => ({ ...i, spotRatePerGpuHour: v }),
    range: [0, 100],
  },
  {
    key: "demand",
    label: "Demand",
    unit: "percent",
    read: (i) => mean(normaliseCurve(i.utilization)),
    write: (i, v) => {
      const current = mean(normaliseCurve(i.utilization))
      const factor = current > 0 ? v / current : 0
      return {
        ...i,
        utilization: normaliseCurve(i.utilization).map((u) => u * factor),
      }
    },
    range: [0, 4],
  },
]

export interface Sensitivity {
  key: LeverKey
  label: string
  unit: Lever["unit"]
  current: number
  /** Daily margin if this input alone were 10% better, and 10% worse. */
  marginUp: number
  marginDown: number
  /** The spread between the two — how much this one input moves the answer. */
  swing: number
  /** The value of this input at which the day stops making money, or null. */
  breakEvenValue: number | null
  /** How far it can move before that, as a share of today's value. */
  headroom: number | null
}

/** The perturbation each lever is tested at. Small enough to stay local. */
const NUDGE = 0.1

/**
 * Which single input moves margin most, and how far each can move before the
 * workload stops paying for itself.
 *
 * A ±10% nudge rather than a derivative, because the operator reads the table
 * as "what if this were a tenth better" and because the curve has a kink at
 * capacity that a derivative taken at the kink would report as nonsense. The
 * break-even value is found by scanning the lever's range for a sign change and
 * bisecting inside it — margin is not monotone in every lever (a longer prompt
 * earns more and costs prefill), so a naive bisection would miss.
 *
 * A lever already sitting at zero has no proportional neighbourhood, so its
 * swing is zero. Its break-even value still comes back, because the scan does
 * not start from where the lever is.
 */
export function sensitivity(inputs: EconomicsInputs): Sensitivity[] {
  return LEVERS.map((lever) => {
    const current = lever.read(inputs)
    const up = summariseDay(
      lever.write(inputs, current * (1 + NUDGE))
    ).marginPerDay
    const down = summariseDay(
      lever.write(inputs, current * (1 - NUDGE))
    ).marginPerDay
    const zero = findSignChange(
      (value) => summariseDay(lever.write(inputs, value)).marginPerDay,
      lever.range
    )
    return {
      key: lever.key,
      label: lever.label,
      unit: lever.unit,
      current,
      marginUp: up,
      marginDown: down,
      swing: Math.abs(up - down),
      breakEvenValue: zero,
      headroom:
        zero !== null && current > 0 ? (zero - current) / current : null,
    }
  }).sort((a, b) => b.swing - a.swing)
}

export interface Economics {
  capacity: Capacity
  unit: DemandUnit
  day: DayEconomics
  costPerHour: number
  revenuePerHourAtCapacity: number
  outputTokensPerSecondAtCapacity: number
  revenuePerMonth: number
  costPerMonth: number
  marginPerMonth: number
  breakEven: BreakEven
  paybackDays: number | null
  reference: Reference | null
  sensitivity: Sensitivity[]
}

/** Everything at once, so a component renders one object and computes nothing. */
export function economics(inputs: EconomicsInputs): Economics {
  const day = summariseDay(inputs)
  const months = HOURS_PER_MONTH / 24
  return {
    capacity: capacityOf(inputs),
    unit: demandUnit(inputs),
    day,
    costPerHour: costPerHour(inputs),
    revenuePerHourAtCapacity: revenuePerHourAtCapacity(inputs),
    outputTokensPerSecondAtCapacity: outputTokensPerSecondAtCapacity(inputs),
    revenuePerMonth: day.revenuePerDay * months,
    costPerMonth: day.costPerDay * months,
    marginPerMonth: day.marginPerDay * months,
    breakEven: breakEven(inputs),
    paybackDays: paybackDays(inputs),
    reference: reference(inputs),
    sensitivity: sensitivity(inputs),
  }
}

/* ---- arithmetic helpers ---------------------------------------------- */

/** Exactly `DAY_POINTS` finite, non-negative fractions, whatever was handed in. */
export function normaliseCurve(values: number[]): number[] {
  return Array.from({ length: DAY_POINTS }, (_, i) => {
    const value = values[i]
    return Number.isFinite(value) && value > 0 ? value : 0
  })
}

function mean(values: number[]): number {
  return values.length === 0
    ? 0
    : values.reduce((sum, value) => sum + value, 0) / values.length
}

function clamp(value: number, low: number, high: number): number {
  return Math.min(Math.max(Number.isFinite(value) ? value : low, low), high)
}

/** The smallest x in [low, high] where a non-decreasing f crosses zero. */
function solveIncreasing(
  f: (x: number) => number,
  low: number,
  high: number
): number | null {
  if (f(low) >= 0) return low
  if (f(high) < 0) return null
  let a = low
  let b = high
  for (let i = 0; i < 60; i++) {
    const mid = (a + b) / 2
    if (f(mid) < 0) a = mid
    else b = mid
  }
  return b
}

/** The first crossing of zero over a range, found by scanning then bisecting. */
function findSignChange(
  f: (x: number) => number,
  [low, high]: [number, number]
): number | null {
  const steps = 64
  let previousX = low
  let previousY = f(low)
  if (previousY === 0) return low
  for (let i = 1; i <= steps; i++) {
    const x = low + ((high - low) * i) / steps
    const y = f(x)
    if (y === 0) return x
    if (Math.sign(y) !== Math.sign(previousY)) {
      let a = previousX
      let b = x
      const rising = previousY < 0
      for (let j = 0; j < 50; j++) {
        const mid = (a + b) / 2
        const value = f(mid)
        if (rising === value < 0) a = mid
        else b = mid
      }
      return (a + b) / 2
    }
    previousX = x
    previousY = y
  }
  return null
}
