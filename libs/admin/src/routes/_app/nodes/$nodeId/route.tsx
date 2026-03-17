import { createFileRoute } from "@tanstack/react-router"
import { NodeDetailPage } from "./page"

export const Route = createFileRoute("/_app/nodes/$nodeId")({
  component: NodeDetailPage,
})
