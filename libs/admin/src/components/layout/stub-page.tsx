import { PageHeader } from "./page-header"

export function StubPage({
  title,
  description,
}: {
  title: string
  description?: string
}) {
  return (
    <div className="flex flex-col">
      <PageHeader title={title} description={description} />
      <div className="flex flex-1 items-center justify-center p-12 text-center">
        <div>
          <p className="font-mono text-2xs uppercase tracking-widest text-muted-foreground/40">
            Not built yet
          </p>
          <p className="mt-2 text-sm text-muted-foreground">
            This surface is waiting on the plane API contract.
          </p>
        </div>
      </div>
    </div>
  )
}
