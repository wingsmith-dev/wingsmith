import { useCallback, useMemo, useState } from "react"
import {
  DEFAULT_PREFILL_MULTIPLE,
  DEFAULT_UTILIZATION_CURVE,
  type CapacitySource,
  type EconomicsInputs,
} from "./model"

/**
 * What the operator typed, kept per workload in this browser.
 *
 * Separate from `EconomicsInputs` by one field: `gpusPerReplica`. The model
 * wants a total card count and the operator wants to ask "what if we ran four
 * of these", and those are the same question only if the cards move with the
 * replicas. Keeping the per-replica shape and multiplying is what makes
 * changing `replicas` move capacity and cost together, which is the whole point
 * of being able to change it.
 */
export interface EconomicsForm
  extends Omit<EconomicsInputs, "gpuCount"> {
  gpusPerReplica: number
}

/** The live facts the console already holds, which seed a form nobody has filled in. */
export interface EconomicsSeed {
  /** `peak_output_tokens_per_second` for this serving digest, when measured. */
  measuredOutputTokensPerSecond: number | null
  /** The workload's desired scale. */
  replicas: number
  /** Cards per replica, from live placement. */
  gpusPerReplica: number
}

/**
 * A form nobody has filled in.
 *
 * The prices are a plausible mid-market shape and are meant to be replaced;
 * they are not a quote. Capacity is measured whenever a graded run of this
 * serving configuration reported a peak, and left empty when none did — an
 * invented throughput would be the one number in here that looks like a
 * measurement without being one.
 */
export function defaultForm(seed: EconomicsSeed): EconomicsForm {
  const measured = seed.measuredOutputTokensPerSecond
  const outputTokensPerSecond = measured ?? 0
  return {
    inputPricePerMTok: 0.5,
    outputPricePerMTok: 1.5,
    cachedInputPricePerMTok: 0.05,
    inputPerOutputToken: 8,
    cacheHitRate: 0.3,
    outputTokensPerSecond,
    prefillTokensPerSecond: outputTokensPerSecond * DEFAULT_PREFILL_MULTIPLE,
    capacitySource: measured === null ? "assumed" : "measured",
    replicas: Math.max(seed.replicas, 1),
    gpusPerReplica: Math.max(seed.gpusPerReplica, 0),
    spotRatePerGpuHour: 2.5,
    costPerHourOverride: null,
    utilization: [...DEFAULT_UTILIZATION_CURVE],
    upfrontCommitment: 0,
    referenceInputPricePerMTok: null,
    referenceOutputPricePerMTok: null,
  }
}

export function toInputs(form: EconomicsForm): EconomicsInputs {
  const { gpusPerReplica, ...rest } = form
  return { ...rest, gpuCount: Math.max(form.replicas, 0) * gpusPerReplica }
}

function storageKey(workloadId: string): string {
  return `hangar.economics.${workloadId}`
}

function read(workloadId: string): EconomicsForm | null {
  try {
    const raw = window.localStorage.getItem(storageKey(workloadId))
    return raw ? (JSON.parse(raw) as EconomicsForm) : null
  } catch {
    return null
  }
}

export interface EconomicsFormState {
  form: EconomicsForm
  /** Whether anything has been saved for this workload, or it is still the seed. */
  saved: boolean
  set: <K extends keyof EconomicsForm>(key: K, value: EconomicsForm[K]) => void
  setUtilizationPoint: (index: number, value: number) => void
  reset: () => void
}

/**
 * One workload's model, surviving a reload.
 *
 * Keyed by workload id, because the experiment is about this workload and
 * carrying one workload's prices onto another silently would be worse than
 * starting again. Nothing is written until something is changed, so a form that
 * has only ever been looked at keeps tracking the seed as measurement lands —
 * a capacity that filled in from a benchmark taken this morning should not be
 * frozen by having opened the tab yesterday.
 */
export function useEconomicsForm(
  workloadId: string,
  seed: EconomicsSeed
): EconomicsFormState {
  const [stored, setStored] = useState<EconomicsForm | null>(() =>
    read(workloadId)
  )

  const form = useMemo(
    () => stored ?? defaultForm(seed),
    [stored, seed]
  )

  const write = useCallback(
    (next: EconomicsForm) => {
      setStored(next)
      try {
        window.localStorage.setItem(storageKey(workloadId), JSON.stringify(next))
      } catch {
        /* A browser refusing storage still gets a working model, just not a durable one. */
      }
    },
    [workloadId]
  )

  const set = useCallback(
    <K extends keyof EconomicsForm>(key: K, value: EconomicsForm[K]) => {
      write({ ...form, [key]: value })
    },
    [form, write]
  )

  const setUtilizationPoint = useCallback(
    (index: number, value: number) => {
      const utilization = form.utilization.map((point, i) =>
        i === index ? value : point
      )
      write({ ...form, utilization })
    },
    [form, write]
  )

  const reset = useCallback(() => {
    setStored(null)
    try {
      window.localStorage.removeItem(storageKey(workloadId))
    } catch {
      /* Nothing to clear if nothing could be written. */
    }
  }, [workloadId])

  return { form, saved: stored !== null, set, setUtilizationPoint, reset }
}
