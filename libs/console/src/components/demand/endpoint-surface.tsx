import { ChoiceSet, ChoiceSetField } from "@/components/demand/choice-set"
import {
  DeclaredSection,
  EnforcementBadge,
} from "@/components/demand/enforcement"
import { Field, FieldGrid } from "@/components/layout/field"
import { FormGrid, NumberField, SelectField } from "@/components/layout/form"
import { Dash } from "@/components/layout/mono"
import { SwitchField } from "@/components/layout/form"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip"
import { SURFACE_ENFORCEMENT } from "@/modules/demand/display"
import {
  AUTH_MODES,
  ENDPOINT_PROTOCOLS,
  ERROR_SHAPES,
  OUTPUT_LENGTH_PARAMS,
  STREAMING_TRANSPORTS,
  type EndpointSurface,
} from "@/modules/demand/types"
import { IconInfoCircle } from "@tabler/icons-react"

/** The note that says what honours a field, or what it means that nothing does. */
function Why({ field }: { field: keyof EndpointSurface }) {
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <button
          type="button"
          aria-label={`What honours ${field}`}
          className="text-muted-foreground/50 hover:text-foreground"
        >
          <IconInfoCircle className="size-3.5" />
        </button>
      </TooltipTrigger>
      <TooltipContent className="max-w-xs text-2xs">
        {SURFACE_ENFORCEMENT[field].note}
      </TooltipContent>
    </Tooltip>
  )
}

function Titled({
  field,
  children,
}: React.PropsWithChildren<{ field: keyof EndpointSurface }>) {
  return (
    <div className="space-y-1">
      {children}
      <div className="flex items-center gap-1.5">
        <EnforcementBadge kind={SURFACE_ENFORCEMENT[field].enforcement} />
        <Why field={field} />
      </div>
    </div>
  )
}

/**
 * Which protocols answer at this name, how callers authenticate, and the
 * rate-limit policy.
 *
 * Split into the half the gateway acts on and the half it only reports, because
 * four of these eight are honoured by nothing in any configuration, and a
 * disclosure drawn as a term is a promise the operator repeats to a customer.
 */
export function SurfaceFields({
  value,
  onChange,
}: {
  value: EndpointSurface
  onChange: (value: EndpointSurface) => void
}) {
  const set = (patch: Partial<EndpointSurface>) =>
    onChange({ ...value, ...patch })

  return (
    <>
      <DeclaredSection
        title="Surface — acted on by the gateway"
        kind="obeyed"
        hint="These the gateway reads off the routing table and honours on the request path."
      >
        <FormGrid>
          <Titled field="request_timeout_seconds">
            <NumberField
              label="Request timeout (seconds)"
              value={value.request_timeout_seconds}
              onChange={(request_timeout_seconds) =>
                set({ request_timeout_seconds })
              }
              min={1}
              hint="Blank leaves the transport's own deadline."
            />
          </Titled>
          <Titled field="streaming_transport">
            <SelectField
              label="Streaming transport"
              value={value.streaming_transport}
              onChange={(streaming_transport) => set({ streaming_transport })}
              options={[...STREAMING_TRANSPORTS]}
              hint="A gateway that buffers turns a streaming engine into a non-streaming API."
            />
          </Titled>
          <Titled field="output_length_params">
            <ChoiceSetField
              label="Output length params"
              value={value.output_length_params}
              onChange={(output_length_params) => set({ output_length_params })}
              options={OUTPUT_LENGTH_PARAMS}
              ordered
              hint="Most significant first: membership is which fields are accepted, position is which one wins. It names the field a serving quota's max_output_tokens applies to and states no number of its own."
            />
          </Titled>
          <Titled field="cors_enabled">
            <SwitchField
              label="CORS"
              hint="Whether the exposure answers a preflight."
              checked={value.cors_enabled}
              onChange={(cors_enabled) => set({ cors_enabled })}
            />
          </Titled>
        </FormGrid>
      </DeclaredSection>

      <DeclaredSection
        title="Surface — disclosure only"
        kind="disclosure"
        hint="Recorded so an acceptance review can read it. Nothing applies any of it, in any configuration — do not repeat these to a customer as limits."
      >
        <FormGrid>
          <Titled field="protocols">
            <ChoiceSetField
              label="Protocols"
              value={value.protocols}
              onChange={(protocols) => set({ protocols })}
              options={ENDPOINT_PROTOCOLS}
              hint="Which routes this name claims to answer."
            />
          </Titled>
          <Titled field="auth_mode">
            <SelectField
              label="Auth mode"
              value={value.auth_mode}
              onChange={(auth_mode) => set({ auth_mode })}
              options={[...AUTH_MODES]}
              hint="How the system in front of us authenticates callers. No credential material lives here."
            />
          </Titled>
          <Titled field="error_shape">
            <SelectField
              label="Error shape"
              value={value.error_shape}
              onChange={(error_shape) => set({ error_shape })}
              options={[...ERROR_SHAPES]}
              hint="One value: you get what the engine said."
            />
          </Titled>
          <Titled field="max_request_bytes">
            <NumberField
              label="Max request bytes"
              value={value.max_request_bytes}
              onChange={(max_request_bytes) => set({ max_request_bytes })}
              min={1}
              hint="A documented ceiling, not an applied one."
            />
          </Titled>
        </FormGrid>
      </DeclaredSection>
    </>
  )
}

/** The same split, read-only, on an endpoint's detail page. */
export function SurfaceCard({ surface }: { surface?: EndpointSurface }) {
  if (!surface) {
    return (
      <Card>
        <CardHeader className="pb-2">
          <CardTitle className="text-sm">Surface</CardTitle>
        </CardHeader>
        <CardContent>
          <p className="font-mono text-2xs text-muted-foreground/60">
            No surface declared for this endpoint.
          </p>
        </CardContent>
      </Card>
    )
  }
  return (
    <Card>
      <CardHeader className="pb-2">
        <CardTitle className="text-sm">Surface</CardTitle>
      </CardHeader>
      <CardContent className="space-y-4">
        <div className="space-y-2">
          <p className="font-mono text-2xs tracking-[0.18em] text-muted-foreground/50 uppercase">
            acted on by the gateway
          </p>
          <FieldGrid>
            <Field label="Request timeout">
              <Dash>
                {surface.request_timeout_seconds !== null
                  ? `${surface.request_timeout_seconds}s`
                  : null}
              </Dash>{" "}
              <EnforcementBadge kind="obeyed" />
            </Field>
            <Field label="Streaming transport">
              {surface.streaming_transport} <EnforcementBadge kind="obeyed" />
            </Field>
            <Field label="CORS">
              {surface.cors_enabled ? "answers preflight" : "off"}{" "}
              <EnforcementBadge kind="obeyed" />
            </Field>
            <Field label="Output length params">
              <span className="flex flex-wrap items-center gap-1">
                <ChoiceSet value={surface.output_length_params} ordered />
                <EnforcementBadge kind="conditional" />
              </span>
            </Field>
          </FieldGrid>
        </div>

        <div className="space-y-2">
          <p className="font-mono text-2xs tracking-[0.18em] text-muted-foreground/50 uppercase">
            disclosure only — nothing applies these
          </p>
          <FieldGrid>
            <Field label="Protocols" className="sm:col-span-2">
              <span className="flex flex-wrap items-center gap-1">
                <ChoiceSet value={surface.protocols} />
                <EnforcementBadge kind="disclosure" />
              </span>
            </Field>
            <Field label="Auth mode">
              {surface.auth_mode} <EnforcementBadge kind="disclosure" />
            </Field>
            <Field label="Error shape">
              {surface.error_shape} <EnforcementBadge kind="disclosure" />
            </Field>
            <Field label="Max request bytes" className="sm:col-span-2">
              <Dash>{surface.max_request_bytes?.toLocaleString()}</Dash>{" "}
              <EnforcementBadge kind="disclosure" />
              <span className="ml-1 text-2xs text-muted-foreground/60">
                {SURFACE_ENFORCEMENT.max_request_bytes.note}
              </span>
            </Field>
          </FieldGrid>
        </div>
      </CardContent>
    </Card>
  )
}
