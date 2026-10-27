import { EnforcementBadge } from "@/components/demand/enforcement"
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
import { SURFACE_ENFORCEMENT, policyRuleTerms } from "@/modules/demand/display"
import type { EndpointPolicy, PolicyRule } from "@/modules/demand/types"

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
 * What one rule says. `deny` refuses and is the only term that can turn a
 * request away, so it alone is colored; the bounds and rewrites (which never
 * fail a request) stay quiet.
 */
function RuleTerms({ rule }: { rule: PolicyRule }) {
  const terms = policyRuleTerms(rule)
  if (terms.length === 0) return <Dash>{null}</Dash>
  return (
    <span className="font-mono text-2xs">
      {terms.map((term, index) => (
        <span key={index}>
          {index > 0 && " · "}
          {index === 0 && rule.deny ? (
            <span className="text-destructive">{term}</span>
          ) : (
            term
          )}
        </span>
      ))}
    </span>
  )
}

/**
 * Where a rule applies, kept to one small tag per condition set: the route it
 * names, the transport it is limited to, the field whose presence triggers it.
 *
 * All three absent is not a gap but the common case (an unscoped rule applies
 * to every route and both transport modes), and it says so instead of
 * leaving a blank an operator reads as missing data.
 */
function RuleScope({ rule }: { rule: PolicyRule }) {
  if (rule.route == null && rule.stream == null && !rule.when_present) {
    return (
      <span className="font-mono text-2xs text-muted-foreground">
        every request
      </span>
    )
  }
  return (
    <span className="inline-flex flex-wrap items-center gap-1">
      {rule.route && (
        <Badge variant="outline" className="font-mono text-2xs">
          {rule.route}
        </Badge>
      )}
      {rule.stream != null && (
        <Badge variant="secondary" className="font-mono text-2xs">
          {rule.stream ? "streaming" : "non-streaming"}
        </Badge>
      )}
      {rule.when_present && (
        <Badge variant="secondary" className="font-mono text-2xs">
          when {rule.when_present}
        </Badge>
      )}
    </span>
  )
}

/**
 * The request terms this endpoint sells, read-only. One row per rule, with
 * the parameter it is about, what it says, and where it applies.
 *
 * Every rule whose scope matches applies, so the list has no precedence and
 * the rows are never something to read in order.
 */
export function EndpointPolicyCard({
  policy,
}: {
  policy: EndpointPolicy | null | undefined
}) {
  const rules = policy?.rules ?? []
  return (
    <Card>
      <CardHeader className="flex-row items-start justify-between gap-2 pb-2">
        <div>
          <CardTitle className="text-sm">Request policy</CardTitle>
          <p className="mt-1 text-2xs text-muted-foreground">
            Terms this endpoint sells, applied to a request before it is proxied
          </p>
        </div>
        <div className="flex items-center gap-2">
          <EnforcementBadge kind={SURFACE_ENFORCEMENT.policy.enforcement} />
          {policy ? (
            <Badge variant="outline" className="font-mono text-2xs">
              v{policy.version}
            </Badge>
          ) : null}
        </div>
      </CardHeader>
      <CardContent>
        {rules.length > 0 ? (
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Param</TableHead>
                <TableHead>Rule</TableHead>
                <TableHead>Scope</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {rules.map((rule, index) => (
                <TableRow key={index}>
                  <TableCell>
                    <RuleSubject rule={rule} />
                  </TableCell>
                  <TableCell>
                    <RuleTerms rule={rule} />
                  </TableCell>
                  <TableCell>
                    <RuleScope rule={rule} />
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        ) : (
          <p className="font-mono text-2xs text-muted-foreground">
            No consistency term sold on this endpoint. The gateway forwards
            every request as it was written.
          </p>
        )}
      </CardContent>
    </Card>
  )
}
