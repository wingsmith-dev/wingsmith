const RELATIVE_UNITS: [Intl.RelativeTimeFormatUnit, number][] = [
  ["second", 60],
  ["minute", 60],
  ["hour", 24],
  ["day", 7],
  ["week", 4.35],
  ["month", 12],
]

const relative = new Intl.RelativeTimeFormat("en", { numeric: "auto" })

export function formatRelative(value: string | null): string {
  if (!value) return "never"
  let delta = (new Date(value).getTime() - Date.now()) / 1000
  for (const [unit, span] of RELATIVE_UNITS) {
    if (Math.abs(delta) < span) return relative.format(Math.round(delta), unit)
    delta /= span
  }
  return relative.format(Math.round(delta), "year")
}

export function formatAbsolute(value: string | null): string {
  if (!value) return "—"
  return new Date(value).toLocaleString(undefined, {
    dateStyle: "medium",
    timeStyle: "short",
  })
}

export function formatPercent(numerator: number, denominator: number): string {
  if (denominator === 0) return "0%"
  return `${Math.round((numerator / denominator) * 100)}%`
}

export function formatError(
  error: unknown,
  fallback = "Failed to load"
): string {
  return error instanceof Error ? error.message : fallback
}

/**
 * The readable head of an opaque identifier. Prefixed ids keep their prefix so
 * `dev_` still reads as a device, tagged vendor ids (`GPU-…`) keep their tag,
 * and everything else is cut to the same width. Enough to tell two rows apart,
 * which is all a table ever needs; the full value is a copy away.
 */
export function shortenId(value: string, chars = 6): string {
  const prefixed = /^([a-z]{2,4})_(.+)$/.exec(value)
  if (prefixed) return `${prefixed[1]}_${prefixed[2].slice(0, chars)}`

  const tagged = /^([A-Za-z]{2,4})-(.+)$/.exec(value)
  if (tagged) return `${tagged[1]}-${tagged[2].slice(0, chars)}`

  return value.length > chars + 4 ? value.slice(0, chars + 4) : value
}

const CURRENCY = new Intl.NumberFormat("en-US", {
  style: "currency",
  currency: "USD",
  maximumFractionDigits: 2,
})

const CURRENCY_PRECISE = new Intl.NumberFormat("en-US", {
  style: "currency",
  currency: "USD",
  minimumFractionDigits: 2,
  maximumFractionDigits: 4,
})

/**
 * Money, in the one currency this console knows about.
 *
 * `precise` keeps four decimals, for the figures that live below a cent — a
 * per-million-token rate is quoted at $0.0125 and rounding it to $0.01 loses
 * a fifth of the number.
 */
export function formatCurrency(
  value: number | null | undefined,
  precise = false
): string {
  if (value === null || value === undefined || !Number.isFinite(value))
    return "—"
  return (precise ? CURRENCY_PRECISE : CURRENCY).format(value)
}

/** The same, shortened, for a figure whose magnitude is the point. */
export function formatCurrencyCompact(
  value: number | null | undefined
): string {
  if (value === null || value === undefined || !Number.isFinite(value))
    return "—"
  const sign = value < 0 ? "-" : ""
  const magnitude = Math.abs(value)
  if (magnitude >= 1e6) return `${sign}$${(magnitude / 1e6).toFixed(1)}M`
  if (magnitude >= 1e3) return `${sign}$${(magnitude / 1e3).toFixed(1)}k`
  return formatCurrency(value)
}
