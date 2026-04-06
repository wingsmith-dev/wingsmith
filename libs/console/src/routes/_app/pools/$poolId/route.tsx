import { createFileRoute } from "@tanstack/react-router"
import { PoolDetailPage } from "./page"

export const Route = createFileRoute("/_app/pools/$poolId")({
  component: PoolDetailPage,
})
