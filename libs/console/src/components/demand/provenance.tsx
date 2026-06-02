import { FormGrid, TextField } from "@/components/layout/form"
import { formatAbsolute } from "@/lib/format"

export interface ProvenanceValue {
  by: string | null
  at: string | null
  uri: string | null
}

/**
 * Who said this, from where, and when.
 *
 * One vocabulary across every claim-bearing surface in the model — a workload's
 * `capabilities`, an artifact's `upstream`, a baseline row — because an
 * acceptance review is an argument about assertions, and two spellings of "who
 * said this and when" is one argument nobody can follow.
 */
export function ProvenanceFields({
  value,
  onChange,
  byLabel = "Recorded by",
  atLabel = "Recorded at",
}: {
  value: ProvenanceValue
  onChange: (value: ProvenanceValue) => void
  byLabel?: string
  atLabel?: string
}) {
  const set = (patch: Partial<ProvenanceValue>) =>
    onChange({ ...value, ...patch })
  return (
    <FormGrid>
      <TextField
        label={byLabel}
        value={value.by ?? ""}
        onChange={(by) => set({ by: by || null })}
        placeholder="someone@acme"
      />
      <TextField
        label={atLabel}
        type="datetime-local"
        value={toLocalInput(value.at)}
        onChange={(at) => set({ at: fromLocalInput(at) })}
      />
      <TextField
        className="sm:col-span-2"
        label="Source URI"
        value={value.uri ?? ""}
        onChange={(uri) => set({ uri: uri || null })}
        placeholder="https://…/model-card"
        hint="The document this was asserted from."
      />
    </FormGrid>
  )
}

/**
 * The triple beside the claim it belongs to. A claim with nothing recorded says
 * so loudly: a reference value nobody signed is an assertion, and rendering it
 * bare is what makes it look like a measurement.
 */
export function Provenance({
  value,
  claimed,
  byLabel = "Recorded by",
}: {
  value: ProvenanceValue
  /** Whether anything was actually claimed. Missing provenance on nothing is fine. */
  claimed: boolean
  byLabel?: string
}) {
  const empty = !value.by && !value.at && !value.uri
  if (empty) {
    return claimed ? (
      <p className="rounded-md border border-amber-500/40 bg-amber-500/10 px-3 py-2 text-2xs text-amber-700 dark:text-amber-400">
        No provenance. Nobody recorded who asserted this or from where, so
        nothing on this block can be argued about.
      </p>
    ) : (
      <p className="font-mono text-2xs text-muted-foreground/50">
        Nothing recorded.
      </p>
    )
  }
  return (
    <p className="font-mono text-2xs text-muted-foreground/70">
      {byLabel.toLowerCase()} {value.by ?? "—"} · {formatAbsolute(value.at)}
      {value.uri && (
        <>
          {" · "}
          <a
            href={value.uri}
            target="_blank"
            rel="noreferrer"
            className="break-all underline underline-offset-2"
          >
            {value.uri}
          </a>
        </>
      )}
    </p>
  )
}

/**
 * `datetime-local` wants `YYYY-MM-DDTHH:mm` in the viewer's own zone, and the
 * plane speaks ISO-8601 in UTC. Converting at the seam keeps the offset out of
 * everything else.
 */
function toLocalInput(value: string | null): string {
  if (!value) return ""
  const parsed = new Date(value)
  if (Number.isNaN(parsed.getTime())) return ""
  const offset = parsed.getTimezoneOffset() * 60_000
  return new Date(parsed.getTime() - offset).toISOString().slice(0, 16)
}

function fromLocalInput(value: string): string | null {
  if (!value) return null
  const parsed = new Date(value)
  return Number.isNaN(parsed.getTime()) ? null : parsed.toISOString()
}
