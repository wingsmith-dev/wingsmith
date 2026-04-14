import { useState } from "react"
import { IconRefresh } from "@tabler/icons-react"
import { useQueryClient, type QueryKey } from "@tanstack/react-query"

import { Button } from "@/components/ui/button"
import { cn } from "@/lib/utils"

/**
 * Re-reads everything a page is showing, now, without a browser reload.
 *
 * A page is not one query. The deployment screen alone holds its declared state, its observed
 * replicas, its event log, its operations, its benchmark runs and its metric panels, each with
 * its own cache entry and its own idea of when to poll — so "refresh" has to name the set rather
 * than a single key, and reloading the browser to get all of them costs the router state and the
 * open tab.
 *
 * `keys` are matched as prefixes, which is what makes them stable: `["metrics"]` reaches every
 * panel on the page without this component knowing the scope, ref or window each one was keyed
 * with.
 *
 * It is a read, so it belongs in `readActions` and survives read-only mode. Nothing here writes.
 */
export function RefreshButton({
  keys,
  label = "Refresh",
  className,
}: {
  keys: QueryKey[]
  label?: string
  className?: string
}) {
  const client = useQueryClient()
  const [busy, setBusy] = useState(false)

  async function refresh() {
    setBusy(true)
    try {
      await Promise.all(
        keys.map((key) => client.invalidateQueries({ queryKey: key }))
      )
    } finally {
      // Always cleared, including when a query rejects. A spinner that outlives its
      // request reads as a page still loading, which is the one thing refresh is for
      // disproving.
      setBusy(false)
    }
  }

  return (
    <Button
      size="sm"
      variant="outline"
      onClick={refresh}
      disabled={busy}
      aria-label={label}
      className={className}
    >
      <IconRefresh className={cn("size-4", busy && "animate-spin")} />
      {label}
    </Button>
  )
}
