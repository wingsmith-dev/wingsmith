import { StubPage } from "@/components/layout/stub-page"
import { withPage } from "@/lib/with-page"
import { createFileRoute } from "@tanstack/react-router"

function Page() {
  return <StubPage title="Operations" description="imperative · drain, reset, warm, upgrade" />
}

export const Route = createFileRoute("/_app/operations")({
  component: withPage(Page, "/_app/operations"),
})
