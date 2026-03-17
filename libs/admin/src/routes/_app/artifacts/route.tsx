import { StubPage } from "@/components/layout/stub-page"
import { withPage } from "@/lib/with-page"
import { createFileRoute } from "@tanstack/react-router"

function Page() {
  return <StubPage title="Artifacts" description="declared · content-addressed weights" />
}

export const Route = createFileRoute("/_app/artifacts")({
  component: withPage(Page, "/_app/artifacts"),
})
