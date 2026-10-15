import { Button } from "@/components/ui/button"
import { Card, CardContent, CardHeader } from "@/components/ui/card"
import { cn } from "@/lib/utils"
import { IconCheck } from "@tabler/icons-react"

/**
 * One numbered question in the deploy flow, open or answered.
 *
 * A card each, like everything else on every other page here. Bare sections divided by
 * rules read as one long form, and this is not one form — it is four questions whose
 * answers are independently re-openable, so each needs an edge to be re-openable within.
 *
 * Numbered rather than tabbed or wizarded, because all of it stays on one page: a
 * wizard hides step two while you are deciding step one, and the whole argument for
 * this flow is that the answer to "which card" is worth seeing next to "how many". An
 * answered step collapses to its answer and keeps a way back, so the page reads as a
 * sentence you can re-open any clause of.
 *
 * A step that is not `ready` renders as a dimmed header with no content at all. That is
 * the honest rendering of a question that cannot be answered yet — a disabled field
 * implies you could fill it in if you tried, and here you genuinely cannot until the
 * step above is settled.
 */
export function Step({
  index,
  title,
  hint,
  answer,
  ready = true,
  open,
  onReopen,
  children,
}: {
  index: number
  title: string
  hint?: string
  /** The collapsed rendering. Absent means unanswered. */
  answer?: React.ReactNode
  ready?: boolean
  open: boolean
  onReopen?: () => void
  children?: React.ReactNode
}) {
  const answered = !!answer && !open
  const body = answered ? answer : ready ? children : null

  return (
    <Card size="sm">
      <CardHeader>
        <div className="flex items-start gap-2.5">
          <div
            className={cn(
              "mt-px flex size-5 shrink-0 items-center justify-center rounded-full font-mono text-2xs tabular-nums",
              answered
                ? "bg-primary/15 text-primary"
                : ready
                  ? "bg-primary/10 text-primary"
                  : "bg-muted text-muted-foreground"
            )}
          >
            {answered ? <IconCheck className="size-3" /> : index}
          </div>

          <div className="min-w-0 flex-1">
            <h2
              className={cn(
                "text-sm leading-tight font-semibold tracking-tight",
                !ready && "text-muted-foreground"
              )}
            >
              {title}
            </h2>
            {hint && (
              <p className="mt-0.5 font-mono text-2xs text-muted-foreground">
                {hint}
              </p>
            )}
          </div>

          {answered && onReopen && (
            <Button
              variant="ghost"
              size="sm"
              className="-mt-1 h-6 shrink-0 font-mono text-2xs"
              onClick={onReopen}
            >
              change
            </Button>
          )}
        </div>
      </CardHeader>

      {body && <CardContent className="pl-[2.1rem]">{body}</CardContent>}
    </Card>
  )
}
