import { createFileRoute } from "@tanstack/react-router"
import { EndpointDetailPage } from "./page"

export const Route = createFileRoute("/_app/endpoints/$endpointId")({
  component: EndpointDetailPage,
})
