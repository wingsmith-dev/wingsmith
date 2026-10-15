import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Label } from "@/components/ui/label"
import { cn } from "@/lib/utils"
import { IconChevronLeft, IconChevronRight, IconX } from "@tabler/icons-react"
import { useId } from "react"

/**
 * A closed set picked as chips, optionally with the order kept.
 *
 * `ordered` is not decoration. `output_length_params` answers two questions with
 * one list — membership is which fields are accepted, position is which one wins
 * when a caller sends two — and a set of checkboxes can only answer the first.
 */
export function ChoiceSetField({
  label,
  value,
  onChange,
  options,
  hint,
  ordered = false,
  className,
}: {
  label: string
  value: string[]
  onChange: (value: string[]) => void
  options: readonly string[]
  hint?: string
  ordered?: boolean
  className?: string
}) {
  const id = useId()
  const available = options.filter((option) => !value.includes(option))

  const move = (index: number, by: number) => {
    const next = [...value]
    const target = index + by
    if (target < 0 || target >= next.length) return
    ;[next[index], next[target]] = [next[target], next[index]]
    onChange(next)
  }

  return (
    <div className={cn("space-y-1.5", className)} data-testid={`set-${label}`}>
      <Label htmlFor={id}>{label}</Label>
      <div
        id={id}
        className="flex flex-wrap items-center gap-1.5 rounded-md border px-2 py-2"
      >
        {value.length === 0 && (
          <span className="font-mono text-2xs text-muted-foreground">none</span>
        )}
        {value.map((member, index) => (
          <Badge
            key={member}
            variant="secondary"
            className="gap-1 pr-1 font-mono text-2xs"
          >
            {ordered && (
              <span className="text-muted-foreground">{index + 1}.</span>
            )}
            {member}
            {ordered && (
              <>
                <button
                  type="button"
                  aria-label={`Move ${member} earlier`}
                  disabled={index === 0}
                  onClick={() => move(index, -1)}
                  className="text-muted-foreground hover:text-foreground disabled:opacity-30"
                >
                  <IconChevronLeft className="size-3" />
                </button>
                <button
                  type="button"
                  aria-label={`Move ${member} later`}
                  disabled={index === value.length - 1}
                  onClick={() => move(index, 1)}
                  className="text-muted-foreground hover:text-foreground disabled:opacity-30"
                >
                  <IconChevronRight className="size-3" />
                </button>
              </>
            )}
            <button
              type="button"
              aria-label={`Remove ${member}`}
              onClick={() => onChange(value.filter((v) => v !== member))}
              className="text-muted-foreground hover:text-foreground"
            >
              <IconX className="size-3" />
            </button>
          </Badge>
        ))}
      </div>
      {available.length > 0 && (
        <div className="flex flex-wrap gap-1.5">
          {available.map((option) => (
            <Button
              key={option}
              type="button"
              size="sm"
              variant="outline"
              className="h-6 font-mono text-2xs"
              onClick={() => onChange([...value, option])}
            >
              + {option}
            </Button>
          ))}
        </div>
      )}
      {hint && <p className="text-2xs text-muted-foreground">{hint}</p>}
    </div>
  )
}

/** The same set, read-only. An empty one says so rather than rendering blank. */
export function ChoiceSet({
  value,
  ordered = false,
}: {
  value: string[]
  ordered?: boolean
}) {
  if (value.length === 0) {
    return (
      <span className="font-mono text-2xs text-muted-foreground">none</span>
    )
  }
  return (
    <div className="flex flex-wrap gap-1">
      {value.map((member, index) => (
        <Badge key={member} variant="outline" className="font-mono text-2xs">
          {ordered && (
            <span className="mr-1 text-muted-foreground">{index + 1}.</span>
          )}
          {member}
        </Badge>
      ))}
    </div>
  )
}
