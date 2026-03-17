import { StubPage } from "@/components/layout/stub-page"
import { withPage } from "@/lib/with-page"
import { createFileRoute } from "@tanstack/react-router"

function Page() {
  return <StubPage title="Quotas" description="declared · guaranteed floor plus over-quota fair share" />
}

export const Route = createFileRoute("/_app/quotas")({
  component: withPage(Page, "/_app/quotas"),
})
