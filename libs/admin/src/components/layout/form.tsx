import { Button } from "@/components/ui/button"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import { Switch } from "@/components/ui/switch"
import { Textarea } from "@/components/ui/textarea"
import { ResourceIcon, type ResourceKind } from "@/components/layout/resource-icon"
import { formatError } from "@/lib/format"
import { cn } from "@/lib/utils"
import { useId, useState } from "react"

/**
 * The shell every create and edit form sits in. It owns the two things a form
 * over this API always has to do — surface the plane's own error text rather
 * than a generic failure, and refuse a second submit while one is in flight.
 */
export function FormDialog({
  open,
  onOpenChange,
  title,
  icon,
  description,
  error,
  pending,
  submitLabel = "Save",
  disabled,
  onSubmit,
  children,
  wide,
}: React.PropsWithChildren<{
  open: boolean
  onOpenChange: (open: boolean) => void
  title: string
  icon?: ResourceKind
  description?: string
  error?: unknown
  pending?: boolean
  submitLabel?: string
  disabled?: boolean
  onSubmit: () => void
  wide?: boolean
}>) {
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className={cn("max-h-[85vh] overflow-y-auto", wide && "sm:max-w-2xl")}>
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            {icon && <ResourceIcon kind={icon} className="text-muted-foreground" />}
            {title}
          </DialogTitle>
          {description && <DialogDescription>{description}</DialogDescription>}
        </DialogHeader>

        <form
          className="space-y-4"
          onSubmit={(e) => {
            e.preventDefault()
            if (!disabled && !pending) onSubmit()
          }}
        >
          {children}

          {!!error && (
            <p className="rounded-md border border-destructive/30 bg-destructive/10 px-3 py-2 text-xs text-destructive">
              {formatError(error)}
            </p>
          )}

          <DialogFooter>
            <Button type="button" variant="outline" onClick={() => onOpenChange(false)}>
              Cancel
            </Button>
            <Button type="submit" disabled={disabled || pending}>
              {pending ? "Saving…" : submitLabel}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  )
}

function Row({
  label,
  hint,
  htmlFor,
  className,
  children,
}: React.PropsWithChildren<{
  label: string
  hint?: string
  htmlFor?: string
  className?: string
}>) {
  return (
    <div className={cn("space-y-1.5", className)}>
      <Label htmlFor={htmlFor}>{label}</Label>
      {children}
      {hint && <p className="text-2xs text-muted-foreground/70">{hint}</p>}
    </div>
  )
}

export function TextField({
  label,
  value,
  onChange,
  placeholder,
  hint,
  mono = true,
  type,
  className,
  disabled,
}: {
  label: string
  value: string
  onChange: (value: string) => void
  placeholder?: string
  hint?: string
  mono?: boolean
  type?: string
  className?: string
  disabled?: boolean
}) {
  const id = useId()
  return (
    <Row label={label} hint={hint} htmlFor={id} className={className}>
      <Input
        id={id}
        type={type}
        value={value}
        disabled={disabled}
        placeholder={placeholder}
        onChange={(e) => onChange(e.target.value)}
        className={mono ? "font-mono text-xs" : undefined}
      />
    </Row>
  )
}

export function NumberField({
  label,
  value,
  onChange,
  placeholder,
  hint,
  min,
  className,
}: {
  label: string
  value: number | null
  onChange: (value: number | null) => void
  placeholder?: string
  hint?: string
  min?: number
  className?: string
}) {
  const id = useId()
  return (
    <Row label={label} hint={hint} htmlFor={id} className={className}>
      <Input
        id={id}
        type="number"
        min={min}
        value={value ?? ""}
        placeholder={placeholder}
        onChange={(e) =>
          onChange(e.target.value === "" ? null : Number(e.target.value))
        }
        className="font-mono text-xs"
      />
    </Row>
  )
}

export function SelectField({
  label,
  value,
  onChange,
  options,
  hint,
  placeholder,
  className,
}: {
  label: string
  value: string
  onChange: (value: string) => void
  options: readonly (string | { value: string; label: string })[]
  hint?: string
  placeholder?: string
  className?: string
}) {
  const id = useId()
  const entries = options.map((o) => (typeof o === "string" ? { value: o, label: o } : o))
  return (
    <Row label={label} hint={hint} htmlFor={id} className={className}>
      <Select value={value} onValueChange={onChange}>
        <SelectTrigger id={id} className="w-full font-mono text-xs">
          <SelectValue placeholder={placeholder ?? "Select…"} />
        </SelectTrigger>
        <SelectContent>
          {entries.map((entry) => (
            <SelectItem key={entry.value} value={entry.value} className="font-mono text-xs">
              {entry.label}
            </SelectItem>
          ))}
        </SelectContent>
      </Select>
    </Row>
  )
}

export function SwitchField({
  label,
  hint,
  checked,
  onChange,
}: {
  label: string
  hint?: string
  checked: boolean
  onChange: (checked: boolean) => void
}) {
  const id = useId()
  return (
    <div className="flex items-center justify-between gap-4 rounded-md border px-3 py-2.5">
      <div className="space-y-0.5">
        <Label htmlFor={id}>{label}</Label>
        {hint && <p className="text-2xs text-muted-foreground/70">{hint}</p>}
      </div>
      <Switch id={id} checked={checked} onCheckedChange={onChange} />
    </div>
  )
}

/**
 * `k=v` pairs, one per line — the same notation the `label=` query parameter and
 * `hgr` both use, so what an operator types here is what they can paste back
 * into a filter.
 */
export function LabelsField({
  label = "Labels",
  value,
  onChange,
  className,
}: {
  label?: string
  value: Record<string, string>
  onChange: (value: Record<string, string>) => void
  className?: string
}) {
  const id = useId()
  const [text, setText] = useState(() =>
    Object.entries(value)
      .map(([k, v]) => `${k}=${v}`)
      .join("\n")
  )

  const apply = (next: string) => {
    setText(next)
    const parsed: Record<string, string> = {}
    for (const line of next.split("\n")) {
      const trimmed = line.trim()
      if (!trimmed) continue
      const at = trimmed.indexOf("=")
      if (at <= 0) continue
      parsed[trimmed.slice(0, at).trim()] = trimmed.slice(at + 1).trim()
    }
    onChange(parsed)
  }

  return (
    <Row label={label} hint="One k=v per line." htmlFor={id} className={className}>
      <Textarea
        id={id}
        rows={3}
        value={text}
        onChange={(e) => apply(e.target.value)}
        placeholder="tier=gold"
        className="font-mono text-xs"
      />
    </Row>
  )
}

/**
 * A free-form JSON object field. Returns `undefined` while the text does not
 * parse, so a caller can disable submit rather than post something malformed.
 */
export function JsonField({
  label,
  value,
  onChange,
  hint,
  rows = 5,
  className,
}: {
  label: string
  value: unknown
  onChange: (value: unknown, valid: boolean) => void
  hint?: string
  rows?: number
  className?: string
}) {
  const id = useId()
  const [text, setText] = useState(() =>
    value === undefined || value === null ? "" : JSON.stringify(value, null, 2)
  )
  const [broken, setBroken] = useState(false)

  const apply = (next: string) => {
    setText(next)
    if (next.trim() === "") {
      setBroken(false)
      onChange(undefined, true)
      return
    }
    try {
      onChange(JSON.parse(next), true)
      setBroken(false)
    } catch {
      setBroken(true)
      onChange(undefined, false)
    }
  }

  return (
    <Row
      label={label}
      hint={broken ? "Not valid JSON yet." : hint}
      htmlFor={id}
      className={className}
    >
      <Textarea
        id={id}
        rows={rows}
        value={text}
        onChange={(e) => apply(e.target.value)}
        className={cn("font-mono text-xs", broken && "border-destructive")}
      />
    </Row>
  )
}

export function FormGrid({ children }: React.PropsWithChildren) {
  return <div className="grid gap-4 sm:grid-cols-2">{children}</div>
}
