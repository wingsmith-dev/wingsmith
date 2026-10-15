export type StatTone = "default" | "ok" | "warn" | "danger"

/** Text colour for a number read as good, borderline or bad. */
export const STAT_TONE_CLASS: Record<StatTone, string> = {
  default: "",
  ok: "text-success",
  warn: "text-warning",
  danger: "text-destructive",
}
