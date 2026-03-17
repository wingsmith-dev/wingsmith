import { StubPage } from "@/components/layout/stub-page"
import { withPage } from "@/lib/with-page"
import { createFileRoute } from "@tanstack/react-router"

function Page() {
  return <StubPage title="Endpoints" description="declared · the stable public name clients call" />
}

export const Route = createFileRoute("/_app/endpoints")({
  component: withPage(Page, "/_app/endpoints"),
})
