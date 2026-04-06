import { useReadOnly } from "@/modules/settings/store"

/**
 * Wraps an affordance that starts a write. Renders nothing in read-only mode,
 * so a console that cannot write does not offer buttons that would fail.
 */
export function WriteAction({ children }: React.PropsWithChildren) {
  const readOnly = useReadOnly()
  if (readOnly) return null
  return <>{children}</>
}
