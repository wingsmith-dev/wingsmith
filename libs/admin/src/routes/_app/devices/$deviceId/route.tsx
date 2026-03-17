import { createFileRoute } from "@tanstack/react-router"
import { DeviceDetailPage } from "./page"

export const Route = createFileRoute("/_app/devices/$deviceId")({
  component: DeviceDetailPage,
})
