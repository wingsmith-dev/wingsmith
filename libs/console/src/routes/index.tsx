import { createFileRoute, redirect } from "@tanstack/react-router"

/** The console has no root screen of its own; the overview is the front door. */
export const Route = createFileRoute("/")({
  beforeLoad: () => {
    throw redirect({ to: "/overview" })
  },
})
