import { IconFlask2 } from "@tabler/icons-react"

/**
 * Says, on the screen, that what is under it is invented.
 *
 * The rest of this console refuses to show a number the plane did not produce —
 * the workload page has a whole component for saying "nothing computes this, so
 * no figure is drawn". These views are the deliberate exception: the conformance
 * ledger, the agent transcript and the evaluation scores are being designed
 * before the harnesses that fill them exist, and a layout cannot be judged
 * against an empty state.
 *
 * The exception only holds while it is labelled. Every panel fed by
 * `modules/benchmarks/mock` carries this, and the banner is deleted in the same
 * commit that deletes the fixture it warns about.
 */
export function MockNotice({ what }: { what: string }) {
  return (
    <div className="flex items-start gap-2 rounded-lg border border-dashed border-amber-500/40 bg-amber-500/5 px-3 py-2 text-2xs text-warning">
      <IconFlask2 className="mt-px size-3.5 shrink-0" />
      <span>
        <span className="font-semibold tracking-[0.12em] uppercase">
          Mock ·{" "}
        </span>
        {what} Nothing here came from the plane, and no harness produces this
        shape yet.
      </span>
    </div>
  )
}
