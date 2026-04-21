import { SelectField, TextField } from "@/components/layout/form"
import { useState } from "react"

const NONE = "__none__"
const CUSTOM = "__custom__"

/**
 * A pick from the values we have run, with a way to type one we have not.
 *
 * Parsers and grammar backends are free strings on the wire on purpose — an
 * engine build knows names that postdate this console — so a plain select would
 * make a recipe unwritable rather than making it correct. The list is the common
 * case and the escape hatch is one click away.
 */
export function SuggestField({
  label,
  value,
  onChange,
  options,
  hint,
  placeholder,
  className,
}: {
  label: string
  value: string | null
  onChange: (value: string | null) => void
  options: readonly string[]
  hint?: string
  placeholder?: string
  className?: string
}) {
  const [custom, setCustom] = useState(
    () => !!value && !options.includes(value)
  )

  const pick = (next: string) => {
    if (next === CUSTOM) {
      setCustom(true)
      onChange(null)
      return
    }
    setCustom(false)
    onChange(next === NONE ? null : next)
  }

  return (
    <div className={className}>
      <SelectField
        label={label}
        value={custom ? CUSTOM : (value ?? NONE)}
        onChange={pick}
        hint={custom ? undefined : hint}
        options={[
          { value: NONE, label: "none — emit no flag" },
          ...options.map((option) => ({ value: option, label: option })),
          { value: CUSTOM, label: "other…" },
        ]}
      />
      {custom && (
        <TextField
          className="mt-1.5"
          label={`${label} (custom)`}
          value={value ?? ""}
          onChange={(next) => onChange(next || null)}
          placeholder={placeholder}
          hint={hint}
        />
      )}
    </div>
  )
}
