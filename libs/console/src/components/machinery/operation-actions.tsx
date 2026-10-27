import { NewOperationDialog } from "@/components/machinery/new-operation"
import { Button } from "@/components/ui/button"
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip"
import {
  applicability,
  operationsForPrefix,
  type OperationContext,
} from "@/modules/machinery/operations"

/**
 * The operations a resource affords, as primary buttons. The set is read off
 * the registry by target prefix, so a new operation type appears here without
 * anyone editing this file, and one that does not currently apply is rendered
 * disabled with its reason, not dropped.
 */
export function OperationActions({
  target,
  prefix,
  context,
}: {
  target: string
  prefix: string
  context: OperationContext
}) {
  return (
    <>
      {operationsForPrefix(prefix).map((definition) => {
        const { enabled, reason } = applicability(definition.type, context)

        if (!enabled) {
          return (
            <Tooltip key={definition.type}>
              <TooltipTrigger asChild>
                <span tabIndex={0} title={reason ?? undefined}>
                  <Button size="sm" variant="outline" disabled>
                    {definition.label}
                  </Button>
                </span>
              </TooltipTrigger>
              <TooltipContent>{reason}</TooltipContent>
            </Tooltip>
          )
        }

        return (
          <NewOperationDialog
            key={definition.type}
            stayOnPage
            defaultType={definition.type}
            defaultTarget={target}
            trigger={
              <Button size="sm" variant="outline">
                {definition.label}
              </Button>
            }
          />
        )
      })}
    </>
  )
}
