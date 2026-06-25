import { EnforcementBadge } from "@/components/demand/enforcement"
import { Field, FieldGrid } from "@/components/layout/field"
import { Dash } from "@/components/layout/mono"
import { Badge } from "@/components/ui/badge"
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
  SURFACE_ENFORCEMENT,
  policyRuleScope,
  policyRuleTerms,
} from "@/modules/demand/display"
import type { EndpointSurface, PolicyRule } from "@/modules/demand/types"

/** One declared field, with the badge that says what is done with it. */
function SurfaceField({
  label,
  field,
  children,
}: React.PropsWithChildren<{
  label: string
  field: keyof EndpointSurface
}>) {
  const entry = SURFACE_ENFORCEMENT[field]
  return (
    <Field label={label}>
      <div className="flex items-center gap-2">
        <span>{children}</span>
        <EnforcementBadge kind={entry.enforcement} />
      </div>
      <p className="mt-1 text-2xs leading-relaxed text-muted-foreground/60">
        {entry.note}
      </p>
    </Field>
  )
}

/** A rule's subject: a body path, or the name of a rule the gateway implements. */
function RuleSubject({ rule }: { rule: PolicyRule }) {
  if (rule.key) return <span className="font-mono text-xs">{rule.key}</span>
  if (rule.predicate)
    return (
      <span className="inline-flex items-center gap-1.5">
        <Badge variant="outline" className="font-mono text-2xs">
          predicate
        </Badge>
        <span className="font-mono text-xs">{rule.predicate}</span>
      </span>
    )
  return <Dash>{null}</Dash>
}

/**
 * The terms on which this endpoint's API is exposed, read-only.
 *
 * Every field carries its own enforcement badge because half of the surface is
 * a term the gateway acts on and half is a disclosure nothing upholds. Drawing
 * the eight of them identically is what turns a disclosure into a promise an
 * operator repeats to a customer.
 *
 * The policy is a separate block and a table rather than a field, because a
 * rule has a scope, a subject and terms — three columns — and because there is
 * no precedence in the list: every rule whose scope matches applies, so the
 * order is never something to read.
 */
export function EndpointSurfaceCard({
  surface,
}: {
  surface: EndpointSurface | undefined
}) {
  const rules = surface?.policy?.rules ?? []
  return (
    <Card>
      <CardHeader className="pb-2">
        <CardTitle className="text-sm">API surface</CardTitle>
        <p className="mt-1 text-2xs text-muted-foreground">
          What this name exposes, and what the gateway does with each term
        </p>
      </CardHeader>
      <CardContent className="space-y-4">
        {surface ? (
          <FieldGrid>
            <SurfaceField label="Protocols" field="protocols">
              <Dash>
                {surface.protocols.length > 0
                  ? surface.protocols.join(", ")
                  : null}
              </Dash>
            </SurfaceField>
            <SurfaceField label="Auth mode" field="auth_mode">
              <Dash>{surface.auth_mode}</Dash>
            </SurfaceField>
            <SurfaceField label="Error shape" field="error_shape">
              <Dash>{surface.error_shape}</Dash>
            </SurfaceField>
            <SurfaceField
              label="Streaming transport"
              field="streaming_transport"
            >
              <Dash>{surface.streaming_transport}</Dash>
            </SurfaceField>
            <SurfaceField label="CORS" field="cors_enabled">
              <Dash>
                {surface.cors_enabled == null
                  ? null
                  : surface.cors_enabled
                    ? "enabled"
                    : "disabled"}
              </Dash>
            </SurfaceField>
            <SurfaceField label="Max request bytes" field="max_request_bytes">
              <Dash>
                {surface.max_request_bytes != null
                  ? surface.max_request_bytes.toLocaleString()
                  : null}
              </Dash>
            </SurfaceField>
            <SurfaceField
              label="Request timeout"
              field="request_timeout_seconds"
            >
              <Dash>
                {surface.request_timeout_seconds != null
                  ? `${surface.request_timeout_seconds}s`
                  : null}
              </Dash>
            </SurfaceField>
            <SurfaceField
              label="Output length params"
              field="output_length_params"
            >
              <Dash>
                {surface.output_length_params.length > 0
                  ? surface.output_length_params.join(" → ")
                  : null}
              </Dash>
            </SurfaceField>
          </FieldGrid>
        ) : (
          <p className="font-mono text-2xs text-muted-foreground/60">
            No surface declared. The gateway forwards what it is given.
          </p>
        )}

        <div className="border-t pt-3">
          <div className="mb-3 flex items-center gap-2">
            <p className="font-mono text-2xs tracking-[0.18em] text-muted-foreground/50 uppercase">
              request policy
            </p>
            <EnforcementBadge kind={SURFACE_ENFORCEMENT.policy.enforcement} />
            {surface?.policy ? (
              <Badge variant="outline" className="font-mono text-2xs">
                v{surface.policy.version}
              </Badge>
            ) : null}
          </div>
          {rules.length > 0 ? (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Scope</TableHead>
                  <TableHead>Subject</TableHead>
                  <TableHead>Terms</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {rules.map((rule, index) => (
                  <TableRow key={index}>
                    <TableCell className="font-mono text-2xs text-muted-foreground">
                      {policyRuleScope(rule)}
                    </TableCell>
                    <TableCell>
                      <RuleSubject rule={rule} />
                    </TableCell>
                    <TableCell className="font-mono text-2xs">
                      <Dash>{policyRuleTerms(rule).join(" · ") || null}</Dash>
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          ) : (
            <p className="font-mono text-2xs text-muted-foreground/60">
              No consistency term sold on this endpoint. The gateway forwards
              every request as it was written.
            </p>
          )}
        </div>
      </CardContent>
    </Card>
  )
}
