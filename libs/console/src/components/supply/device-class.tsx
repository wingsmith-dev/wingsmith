import { SelectField } from "@/components/layout/form"
import { Badge } from "@/components/ui/badge"
import { Label } from "@/components/ui/label"
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip"
import { cn } from "@/lib/utils"
import {
  useDeviceClassLabel,
  useDeviceClasses,
  useDeviceClassOptions,
} from "@/modules/supply/queries"

/**
 * The class picker every form that takes one should use.
 *
 * The hint carries the identifier because that is the string the operator will
 * see again in `wings`, in a pool name and in a serving shape: the friendly name
 * exists to choose by, not to work from.
 */
export function DeviceClassField({
  label = "Device class",
  value,
  onChange,
  hint,
  className,
}: {
  label?: string
  value: string
  onChange: (value: string) => void
  hint?: string
  className?: string
}) {
  const { options } = useDeviceClassOptions(value)
  return (
    <SelectField
      label={label}
      value={value}
      onChange={onChange}
      options={options}
      placeholder="Select a GPU model…"
      hint={hint ?? (value ? `stored as ${value}` : undefined)}
      className={className}
    />
  )
}

/**
 * A class as a table cell or a field value: the friendly name, with the
 * identifier a hover away. Falls back to the identifier alone, which is what an
 * unlisted class has and is still perfectly readable.
 */
export function DeviceClassLabel({
  name,
  className,
}: {
  name: string
  className?: string
}) {
  const label = useDeviceClassLabel()(name)
  if (label === name) {
    return <span className={cn("font-mono text-xs", className)}>{name}</span>
  }
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <span className={cn("text-xs", className)}>{label}</span>
      </TooltipTrigger>
      <TooltipContent className="font-mono text-2xs">{name}</TooltipContent>
    </Tooltip>
  )
}

/** The same, as the badge the tables and cards already use for a class. */
export function DeviceClassBadge({ name }: { name: string }) {
  const label = useDeviceClassLabel()(name)
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <Badge variant="secondary" className="text-2xs">
          {label}
        </Badge>
      </TooltipTrigger>
      <TooltipContent className="font-mono text-2xs">{name}</TooltipContent>
    </Tooltip>
  )
}

/**
 * The allow-list form of the picker: every class in the catalog, each one on or
 * off, plus any class already named that the catalog does not carry.
 *
 * Empty means no opinion, which is what the plane reads an empty list as, so
 * that is said in the hint, not left for someone to discover by
 * selecting nothing and watching what happens.
 */
export function DeviceClassMultiField({
  label = "Allowed device classes",
  values,
  onChange,
  hint = "None selected means no opinion: every class is allowed.",
}: {
  label?: string
  values: string[]
  onChange: (values: string[]) => void
  hint?: string
}) {
  const { data } = useDeviceClasses()
  const catalog = data ?? []
  const unlisted = values.filter(
    (name) => !catalog.some((entry) => entry.name === name)
  )
  const entries = [
    ...catalog.map((entry) => ({ name: entry.name, label: entry.label })),
    ...unlisted.map((name) => ({ name, label: name })),
  ]

  const toggle = (name: string) =>
    onChange(
      values.includes(name)
        ? values.filter((value) => value !== name)
        : [...values, name]
    )

  return (
    <div className="space-y-1.5">
      <Label>{label}</Label>
      <div className="flex flex-wrap gap-1.5 rounded-md border p-2">
        {entries.length === 0 && (
          <span className="font-mono text-2xs text-muted-foreground">
            catalog unavailable
          </span>
        )}
        {entries.map((entry) => {
          const on = values.includes(entry.name)
          return (
            <button
              key={entry.name}
              type="button"
              onClick={() => toggle(entry.name)}
              aria-pressed={on}
              title={entry.name}
              className={cn(
                "rounded-full border px-2 py-0.5 text-2xs transition-colors",
                on
                  ? "border-primary/40 bg-primary/10 text-foreground"
                  : "border-border text-muted-foreground hover:bg-accent/50"
              )}
            >
              {entry.label}
            </button>
          )
        })}
      </div>
      <p className="text-2xs text-muted-foreground">{hint}</p>
    </div>
  )
}
