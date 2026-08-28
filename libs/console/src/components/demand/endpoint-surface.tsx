import { EnforcementBadge } from "@/components/demand/enforcement"
import { Dash } from "@/components/layout/mono"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table"
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip"
import { SURFACE_ENFORCEMENT } from "@/modules/demand/display"
import type { EndpointSurface } from "@/modules/demand/types"

type SurfaceScalar = Exclude<keyof EndpointSurface, "policy">

/**
 * The surface read top to bottom: first what the gateway upholds on every
 * request, then what is conditionally one, then the claims nothing enforces.
 * Position is the enforcement story, so the column of badges repeats it.
 */
const SURFACE_ROWS: { key: SurfaceScalar; label: string }[] = [
  { key: "request_timeout_seconds", label: "Request timeout" },
  { key: "streaming_transport", label: "Streaming transport" },
  { key: "cors_enabled", label: "CORS" },
  { key: "output_length_params", label: "Output length params" },
  { key: "protocols", label: "Protocols" },
  { key: "auth_mode", label: "Auth mode" },
  { key: "error_shape", label: "Error shape" },
  { key: "max_request_bytes", label: "Max request bytes" },
]

/** One field's value as it is read in the table, or null where the endpoint sets nothing. */
function surfaceValue(key: SurfaceScalar, surface: EndpointSurface) {
  switch (key) {
    case "protocols":
      return surface.protocols.length > 0 ? surface.protocols.join(", ") : null
    case "output_length_params":
      return surface.output_length_params.length > 0
        ? surface.output_length_params.join(" → ")
        : null
    case "cors_enabled":
      return surface.cors_enabled == null
        ? null
        : surface.cors_enabled
          ? "enabled"
          : "disabled"
    case "max_request_bytes":
      return surface.max_request_bytes?.toLocaleString() ?? null
    case "request_timeout_seconds":
      return surface.request_timeout_seconds != null
        ? `${surface.request_timeout_seconds}s`
        : null
    default:
      return surface[key]
  }
}

/**
 * A term's name. What the gateway actually does with it — the note on its
 * enforcement entry — is the hover affordance, because eight paragraphs of
 * small print read as none.
 */
function SurfaceTerm({
  field,
  label,
}: {
  field: SurfaceScalar
  label: string
}) {
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <span
          tabIndex={0}
          className="cursor-help rounded-sm underline decoration-muted-foreground/40 decoration-dotted underline-offset-[3px] focus-visible:ring-1 focus-visible:ring-ring focus-visible:outline-none"
        >
          {label}
        </span>
      </TooltipTrigger>
      <TooltipContent className="max-w-xs text-2xs">
        {SURFACE_ENFORCEMENT[field].note}
      </TooltipContent>
    </Tooltip>
  )
}

/**
 * The terms on which this endpoint's API is exposed, read-only, as one table:
 * a term, its value, and what the gateway does with it.
 *
 * Half the surface is a term the gateway acts on and half is a disclosure
 * nothing upholds. A table is what keeps that distinction honest — every
 * field carries its enforcement in its row, so a claim can never be drawn to
 * look like a term somebody sold to a customer.
 *
 * The request policy is its own card: a rule has a scope, a subject and terms
 * of its own, and it answers a different question — not what this name
 * exposes, but what the gateway will do to a request on the way through.
 */
export function EndpointSurfaceCard({
  surface,
}: {
  surface: EndpointSurface | undefined
}) {
  return (
    <Card>
      <CardHeader className="pb-2">
        <CardTitle className="text-sm">API surface</CardTitle>
        <p className="mt-1 text-2xs text-muted-foreground">
          Which of these the gateway upholds, and which it only documents
        </p>
      </CardHeader>
      <CardContent>
        {surface ? (
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Term</TableHead>
                <TableHead>Value</TableHead>
                <TableHead>Enforcement</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {SURFACE_ROWS.map(({ key, label }) => (
                <TableRow key={key}>
                  <TableCell className="text-xs">
                    <SurfaceTerm field={key} label={label} />
                  </TableCell>
                  <TableCell className="font-mono text-xs">
                    <Dash>{surfaceValue(key, surface)}</Dash>
                  </TableCell>
                  <TableCell>
                    <EnforcementBadge kind={SURFACE_ENFORCEMENT[key].enforcement} />
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        ) : (
          <p className="font-mono text-2xs text-muted-foreground/60">
            No surface declared. The gateway forwards what it is given.
          </p>
        )}
      </CardContent>
    </Card>
  )
}
