import { createFileRoute } from "@tanstack/react-router"
import { OperationDetailPage } from "./page"

export const Route = createFileRoute("/_app/operations/$operationId")({
  component: OperationDetailPage,
})
