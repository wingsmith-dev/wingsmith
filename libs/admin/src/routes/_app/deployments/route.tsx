import { StubPage } from "@/components/layout/stub-page"
import { withPage } from "@/lib/with-page"
import { createFileRoute } from "@tanstack/react-router"

function Page() {
  return <StubPage title="Deployments" description="declared · recipe × cluster × scale bounds" />
}

export const Route = createFileRoute("/_app/deployments")({
  component: withPage(Page, "/_app/deployments"),
})
