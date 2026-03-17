import { StubPage } from "@/components/layout/stub-page"
import { withPage } from "@/lib/with-page"
import { createFileRoute } from "@tanstack/react-router"

function Page() {
  return <StubPage title="Projects" description="declared · the unit quota and billing attach to" />
}

export const Route = createFileRoute("/_app/projects")({
  component: withPage(Page, "/_app/projects"),
})
