import { useState } from "react"
import { EnforcementBadge } from "@/components/demand/enforcement"
import { Panel } from "@/components/layout/panel"
import { cn } from "@/lib/utils"
import { servingOverrides } from "@/modules/demand/display"
import type { Workload } from "@/modules/demand/types"
import { EditButton } from "./EditButton"

/**
 * The engine's command line, resolved as the operator keys it.
 *
 * Carried over from the recipe page, because the question it answers did not go
 * away with the object: `engine_args` is what somebody declared and
 * `resolved_engine_args` is what the typed behavior block made of it, and the
 * only way to tell which value took effect is to see both. One block and a
 * toggle rather than two stacked blocks — they are the same argument list read
 * at two points, so they belong in the same place on the page, and the toggle
 * is what makes the difference between them legible.
 */
export function EngineArgsPanel({
  workload,
  onEdit,
}: {
  workload: Workload
  onEdit?: () => void
}) {
  const resolved = workload.serving.resolved_engine_args
  const declared = workload.serving.engine_args
  const overrides = servingOverrides(
    workload.serving.engine,
    declared,
    workload.serving.serving
  )
  const [shown, setShown] = useState<"resolved" | "declared">("resolved")
  const empty =
    shown === "resolved"
      ? Object.keys(resolved).length === 0
      : Object.keys(declared).length === 0
  return (
    <Panel
      title="Engine arguments"
      badge={<EnforcementBadge kind="obeyed" />}
      description="What the engine is started with: the declared args, with the flags the typed serving block dictates laid over the top. Derived on every read and stored nowhere."
      action={
        <div className="flex items-center gap-2">
          <Segmented
            value={shown}
            onChange={setShown}
            options={[
              { value: "resolved", label: "resolved" },
              { value: "declared", label: "declared" },
            ]}
          />
          {onEdit && (
            <EditButton label="Edit engine arguments" onClick={onEdit} />
          )}
        </div>
      }
    >
      <div className="space-y-3">
        {empty ? (
          <p className="font-mono text-2xs text-muted-foreground">
            {shown === "resolved"
              ? "None — the engine runs on its own defaults."
              : "Nothing declared — every argument below was resolved."}
          </p>
        ) : (
          <pre className="rounded-lg border bg-background/60 p-4 font-mono text-2xs leading-relaxed break-all whitespace-pre-wrap">
            {shown === "resolved"
              ? Object.entries(resolved)
                  .map(([key, value]) => `--${key} ${value}`)
                  .join("\n")
              : JSON.stringify(declared, null, 2)}
          </pre>
        )}
        {overrides.length > 0 && (
          <p className="text-2xs text-warning">
            {overrides.map((o) => `--${o.flag}`).join(", ")} came from the typed
            serving block; the engine args spelling of each is ignored.
          </p>
        )}
      </div>
    </Panel>
  )
}

/** The small two-state switch a panel header carries above one block. */
export function Segmented<T extends string>({
  value,
  onChange,
  options,
}: {
  value: T
  onChange: (value: T) => void
  options: readonly { value: T; label: string }[]
}) {
  return (
    <div className="flex gap-0.5 rounded-md bg-muted p-0.5">
      {options.map((option) => (
        <button
          key={option.value}
          type="button"
          onClick={() => onChange(option.value)}
          className={cn(
            "rounded px-2 py-0.5 font-mono text-2xs",
            value === option.value
              ? "bg-card text-foreground"
              : "text-muted-foreground hover:text-foreground"
          )}
        >
          {option.label}
        </button>
      ))}
    </div>
  )
}
