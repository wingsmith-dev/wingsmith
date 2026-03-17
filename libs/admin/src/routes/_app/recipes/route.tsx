import { StubPage } from "@/components/layout/stub-page"
import { withPage } from "@/lib/with-page"
import { createFileRoute } from "@tanstack/react-router"

function Page() {
  return <StubPage title="Recipes" description="declared · a reproducible way to serve an artifact" />
}

export const Route = createFileRoute("/_app/recipes")({
  component: withPage(Page, "/_app/recipes"),
})
