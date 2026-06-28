import { useAuthStore } from "@/modules/auth/store"
import { createFileRoute, redirect } from "@tanstack/react-router"

/** Neither application has a root screen; each has a front door and they differ. */
export const Route = createFileRoute("/")({
  beforeLoad: () => {
    const { kind } = useAuthStore.getState()
    throw redirect({ to: kind === "tenant" ? "/serving" : "/overview" })
  },
})
