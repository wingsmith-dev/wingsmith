import { useIsFetching, useQueryClient } from "@tanstack/react-query"

import { Button } from "@/components/ui/button"

/**
 * Re-reads the page now, not at the next poll.
 *
 * The page polls every 30 seconds and that is already the floor worth having:
 * the gateway is scraped every 30 seconds, so a faster poll re-fetches a number
 * the store has not been given a new sample for. What a person watching a run
 * actually wants is not a faster clock but control of it: this asks once, on
 * demand, and says while it is asking.
 */
export function RefreshButton() {
  const client = useQueryClient()
  const fetching = useIsFetching({ queryKey: ["serving"] }) > 0

  return (
    <Button
      variant="ghost"
      size="sm"
      disabled={fetching}
      onClick={() => void client.invalidateQueries({ queryKey: ["serving"] })}
    >
      {fetching ? "Refreshing…" : "Refresh"}
    </Button>
  )
}
