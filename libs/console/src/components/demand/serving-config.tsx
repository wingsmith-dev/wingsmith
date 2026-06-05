import { ChoiceSet, ChoiceSetField } from "@/components/demand/choice-set"
import { DeclaredSection } from "@/components/demand/enforcement"
import { Provenance, ProvenanceFields } from "@/components/demand/provenance"
import { SuggestField } from "@/components/demand/suggest-field"
import { Field, FieldGrid } from "@/components/layout/field"
import {
  FormGrid,
  NumberField,
  SelectField,
  SwitchField,
  TextField,
} from "@/components/layout/form"
import { Dash } from "@/components/layout/mono"
import {
  Card,
  CardAction,
  CardContent,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import { Button } from "@/components/ui/button"
import { WriteAction } from "@/components/layout/write-action"
import { IconPencil } from "@tabler/icons-react"
import { EnforcementBadge } from "@/components/demand/enforcement"
import {
  servingOverrides,
  type ServingOverride,
} from "@/modules/demand/display"
import {
  CONTEXT_OVERFLOW_MODES,
  INPUT_MODALITIES,
  PROMPT_CACHE_MODES,
  REASONING_PARSERS,
  REASONING_TRANSPORTS,
  STREAM_USAGE_MODES,
  STRUCTURED_OUTPUT_BACKENDS,
  STRUCTURED_OUTPUT_MODES,
  TOOL_CALL_PARSERS,
  TOOL_CHOICE_MODES,
  type ServingCapabilities,
  type ServingBehaviour,
} from "@/modules/demand/types"

/**
 * The rendered half of a workload's serving behaviour, as typed controls.
 *
 * Before these fields existed every one of them went into `engine_args`, where
 * the plane could neither validate them nor say what it had asked for. Honouring
 * any of them means a replica restart, which is why they sit in the serving block.
 */
export function ServingFields({
  engine,
  value,
  onChange,
  engineArgs,
  plain,
}: {
  engine: string
  value: ServingBehaviour
  onChange: (value: ServingBehaviour) => void
  engineArgs: Record<string, unknown>
  /** True in the dialog that edits nothing else — see `DeclaredSection`. */
  plain?: boolean
}) {
  const set = (patch: Partial<ServingBehaviour>) =>
    onChange({ ...value, ...patch })
  const setReasoning = (patch: Partial<ServingBehaviour["reasoning"]>) =>
    onChange({ ...value, reasoning: { ...value.reasoning, ...patch } })

  const overrides = servingOverrides(engine, engineArgs, value)
  const inlineTags = value.reasoning.transport === "inline_tags"

  return (
    <DeclaredSection
      plain={plain}
      title="Serving"
      kind="obeyed"
      hint="What the served API does, as the engine will be told to behave. Every field here becomes a flag, so a change means a replica restart."
    >
      <FormGrid>
        <SuggestField
          label="Reasoning parser"
          value={value.reasoning.parser}
          onChange={(parser) => setReasoning({ parser })}
          options={REASONING_PARSERS}
          hint="What separates thinking from the answer."
        />
        <SelectField
          label="Reasoning transport"
          value={value.reasoning.transport}
          onChange={(transport) => setReasoning({ transport })}
          options={[...REASONING_TRANSPORTS]}
          hint="Where the thinking goes in the response."
        />
        <TextField
          label="Think begin token"
          value={value.reasoning.think_begin_token ?? ""}
          onChange={(token) =>
            setReasoning({ think_begin_token: token || null })
          }
          placeholder="<think>"
          hint={inlineTags ? "Required by inline_tags." : undefined}
        />
        <TextField
          label="Think end token"
          value={value.reasoning.think_end_token ?? ""}
          onChange={(token) => setReasoning({ think_end_token: token || null })}
          placeholder="</think>"
          hint={inlineTags ? "Required by inline_tags." : undefined}
        />
        <SelectField
          label="Stream usage"
          value={value.stream_usage}
          onChange={(stream_usage) => set({ stream_usage })}
          options={[...STREAM_USAGE_MODES]}
          hint="Whether a caller who says nothing still gets a token count."
        />
        <SelectField
          label="Context overflow"
          value={value.context_overflow}
          onChange={(context_overflow) => set({ context_overflow })}
          options={[...CONTEXT_OVERFLOW_MODES]}
          hint="What happens when prompt plus output exceeds the context length."
        />
        <SuggestField
          label="Tool call parser"
          value={value.tool_call_parser}
          onChange={(tool_call_parser) => set({ tool_call_parser })}
          options={TOOL_CALL_PARSERS}
          hint="Setting this is, in practice, what makes tool calling work."
        />
        <SuggestField
          label="Structured output backend"
          value={value.structured_output_backend}
          onChange={(structured_output_backend) =>
            set({ structured_output_backend })
          }
          options={STRUCTURED_OUTPUT_BACKENDS}
          hint="Meaningless unless json_schema is a claimed mode."
        />
        <NumberField
          label="Max images per request"
          value={value.max_images_per_request}
          onChange={(max_images_per_request) => set({ max_images_per_request })}
          min={1}
          hint="Only meaningful when image is a claimed modality."
        />
      </FormGrid>

      <SwitchField
        label="Thinking on by default"
        hint="Whether a caller who asks for nothing gets a thinking response."
        checked={value.reasoning.default_on}
        onChange={(default_on) => setReasoning({ default_on })}
      />
      <SwitchField
        label="Suppress EOS until the think-end token"
        hint="Needs a think end token to suppress up to."
        checked={value.reasoning.suppress_eos_until_think_end}
        onChange={(suppress_eos_until_think_end) =>
          setReasoning({ suppress_eos_until_think_end })
        }
      />

      <ServingPrecedence overrides={overrides} />
    </DeclaredSection>
  )
}

/**
 * The flags both spellings set, and which one the plane will render.
 *
 * Dropping the losing value silently is how somebody spends an afternoon on a
 * flag they can see in the form and cannot find in the process.
 */
export function ServingPrecedence({
  overrides,
}: {
  overrides: ServingOverride[]
}) {
  if (overrides.length === 0) return null
  return (
    <div className="space-y-1.5 rounded-md border border-amber-500/40 bg-amber-500/10 px-3 py-2 text-2xs text-amber-700 dark:text-amber-400">
      <p>
        {overrides.length === 1 ? "One engine arg is" : "Engine args are"}{" "}
        overridden. Where a typed serving field and an engine arg set the same
        flag, the typed field wins — it is the one the plane can validate and
        verify.
      </p>
      <ul className="space-y-0.5 font-mono">
        {overrides.map((override) => (
          <li key={override.flag}>
            --{override.flag}: {override.effective} from {override.field},{" "}
            <span className="line-through opacity-70">{override.ignored}</span>{" "}
            ignored
          </li>
        ))}
      </ul>
    </div>
  )
}

/**
 * The asserted half. Nothing here becomes a flag or reaches a cluster, because a
 * cluster has no use for a claim about itself — the plane holds no table of what
 * an engine can do, so a block that is internally consistent and entirely false
 * is accepted, stored, and falsified by the first conformance run.
 */
export function CapabilityFields({
  value,
  onChange,
  plain,
}: {
  value: ServingCapabilities
  onChange: (value: ServingCapabilities) => void
  /** True in the dialog that edits nothing else — see `DeclaredSection`. */
  plain?: boolean
}) {
  const set = (patch: Partial<ServingCapabilities>) =>
    onChange({ ...value, ...patch })

  return (
    <DeclaredSection
      plain={plain}
      title="Capabilities"
      kind="asserted"
      hint="What this workload claims it can do. Nothing here renders and nothing here is checked — a conformance run is what proves it wrong."
    >
      <FormGrid>
        <ChoiceSetField
          label="Structured output modes"
          value={value.structured_output_modes}
          onChange={(structured_output_modes) =>
            set({ structured_output_modes })
          }
          options={STRUCTURED_OUTPUT_MODES}
          hint="Three support levels, not one flag. Valid JSON is not an honoured schema."
        />
        <ChoiceSetField
          label="Tool choice modes"
          value={value.tool_choice_modes}
          onChange={(tool_choice_modes) => set({ tool_choice_modes })}
          options={TOOL_CHOICE_MODES}
          hint="Non-empty exactly when tool calling is claimed."
        />
        <ChoiceSetField
          label="Input modalities"
          value={value.input_modalities}
          onChange={(input_modalities) => set({ input_modalities })}
          options={INPUT_MODALITIES}
          hint="What this workload serves, which can be less than the weights can do."
        />
        <div className="space-y-4">
          <SelectField
            label="Prompt cache mode"
            value={value.prompt_cache_mode}
            onChange={(prompt_cache_mode) => set({ prompt_cache_mode })}
            options={[...PROMPT_CACHE_MODES]}
            hint="The contract, not the mechanism — that is the prefix cache switch."
          />
          <NumberField
            label="Prompt cache TTL (seconds)"
            value={value.prompt_cache_ttl_seconds}
            onChange={(prompt_cache_ttl_seconds) =>
              set({ prompt_cache_ttl_seconds })
            }
            min={0}
            hint="Blank promises nothing: the engine's own eviction decides."
          />
        </div>
      </FormGrid>

      <SwitchField
        label="Reasoning supported"
        hint="Required before any rendered reasoning field means anything."
        checked={value.reasoning_supported}
        onChange={(reasoning_supported) => set({ reasoning_supported })}
      />
      <SwitchField
        label="Streaming supported"
        checked={value.streaming_supported}
        onChange={(streaming_supported) => set({ streaming_supported })}
      />
      <SwitchField
        label="Tool calling supported"
        checked={value.tool_calling_supported}
        onChange={(tool_calling_supported) => set({ tool_calling_supported })}
      />
      <SwitchField
        label="Parallel tool calls"
        hint="Whether more than one call can come back per turn."
        checked={value.parallel_tool_calls}
        onChange={(parallel_tool_calls) => set({ parallel_tool_calls })}
      />

      <ProvenanceFields
        byLabel="Asserted by"
        atLabel="Asserted at"
        value={{
          by: value.asserted_by,
          at: value.asserted_at,
          uri: value.source_uri,
        }}
        onChange={(provenance) =>
          set({
            asserted_by: provenance.by,
            asserted_at: provenance.at,
            source_uri: provenance.uri,
          })
        }
      />
    </DeclaredSection>
  )
}

/** The pencil on a card that has a dialog behind it. */
function EditIcon({ label, onClick }: { label: string; onClick: () => void }) {
  return (
    <WriteAction>
      <Button
        variant="ghost"
        size="icon-sm"
        aria-label={label}
        title={label}
        onClick={onClick}
      >
        <IconPencil />
      </Button>
    </WriteAction>
  )
}

/** The two blocks side by side on a workload, never merged into one card. */
export function ServingCards({
  serving,
  capabilities,
  resolvedArgs,
  engineArgs,
  onEditServing,
  onEditCapabilities,
}: {
  serving: ServingBehaviour
  capabilities: ServingCapabilities
  resolvedArgs: Record<string, string>
  engineArgs: Record<string, unknown>
  onEditServing?: () => void
  onEditCapabilities?: () => void
}) {
  const overrides = Object.keys(engineArgs).length
    ? Object.entries(resolvedArgs).filter(([key, effective]) => {
        const declared = engineArgs[key] ?? engineArgs[`--${key}`]
        return declared !== undefined && String(declared) !== effective
      })
    : []

  return (
    <div className="grid gap-4 lg:grid-cols-2">
      <Card>
        <CardHeader className="pb-2">
          <CardTitle className="flex items-center gap-2 text-sm">
            Serving
            <EnforcementBadge kind="obeyed" />
          </CardTitle>
          {onEditServing && (
            <CardAction>
              <EditIcon label="Edit serving" onClick={onEditServing} />
            </CardAction>
          )}
        </CardHeader>
        <CardContent className="space-y-3">
          <FieldGrid>
            <Field label="Thinking default">
              {serving.reasoning.default_on ? "on" : "off"}
            </Field>
            <Field label="Reasoning parser">
              <Dash>{serving.reasoning.parser}</Dash>
            </Field>
            <Field label="Reasoning transport">
              {serving.reasoning.transport}
            </Field>
            <Field label="Suppress EOS">
              {serving.reasoning.suppress_eos_until_think_end ? "yes" : "no"}
            </Field>
            <Field label="Think begin token">
              <Dash>{serving.reasoning.think_begin_token}</Dash>
            </Field>
            <Field label="Think end token">
              <Dash>{serving.reasoning.think_end_token}</Dash>
            </Field>
            <Field label="Stream usage">{serving.stream_usage}</Field>
            <Field label="Context overflow">{serving.context_overflow}</Field>
            <Field label="Tool call parser">
              <Dash>{serving.tool_call_parser}</Dash>
            </Field>
            <Field label="Structured output backend">
              <Dash>{serving.structured_output_backend}</Dash>
            </Field>
            <Field label="Max images per request" className="sm:col-span-2">
              <Dash>{serving.max_images_per_request}</Dash>
            </Field>
          </FieldGrid>
        </CardContent>
      </Card>

      <Card>
        <CardHeader className="pb-2">
          <CardTitle className="flex items-center gap-2 text-sm">
            Capabilities
            <EnforcementBadge kind="asserted" />
          </CardTitle>
          {onEditCapabilities && (
            <CardAction>
              <EditIcon
                label="Edit capabilities"
                onClick={onEditCapabilities}
              />
            </CardAction>
          )}
        </CardHeader>
        <CardContent className="space-y-3">
          <FieldGrid>
            <Field label="Reasoning">
              {capabilities.reasoning_supported ? "claimed" : "not claimed"}
            </Field>
            <Field label="Streaming">
              {capabilities.streaming_supported ? "claimed" : "not claimed"}
            </Field>
            <Field label="Tool calling">
              {capabilities.tool_calling_supported ? "claimed" : "not claimed"}
            </Field>
            <Field label="Parallel tool calls">
              {capabilities.parallel_tool_calls ? "claimed" : "not claimed"}
            </Field>
            <Field label="Structured output modes" className="sm:col-span-2">
              <ChoiceSet value={capabilities.structured_output_modes} />
            </Field>
            <Field label="Tool choice modes" className="sm:col-span-2">
              <ChoiceSet value={capabilities.tool_choice_modes} />
            </Field>
            <Field label="Input modalities" className="sm:col-span-2">
              <ChoiceSet value={capabilities.input_modalities} />
            </Field>
            <Field label="Prompt cache">{capabilities.prompt_cache_mode}</Field>
            <Field label="Prompt cache TTL">
              <Dash>{capabilities.prompt_cache_ttl_seconds}</Dash>
            </Field>
          </FieldGrid>
          <Provenance
            byLabel="Asserted by"
            claimed
            value={{
              by: capabilities.asserted_by,
              at: capabilities.asserted_at,
              uri: capabilities.source_uri,
            }}
          />
        </CardContent>
      </Card>

      <Card className="lg:col-span-2">
        <CardHeader className="pb-2">
          <CardTitle className="flex items-center gap-2 text-sm">
            Resolved engine args
            <EnforcementBadge kind="obeyed" />
          </CardTitle>
        </CardHeader>
        <CardContent className="space-y-2">
          <p className="text-2xs text-muted-foreground/70">
            The declared engine args with the flags the typed serving block
            dictates laid over the top. Derived on every read and stored
            nowhere.
          </p>
          {Object.keys(resolvedArgs).length === 0 ? (
            <p className="font-mono text-2xs text-muted-foreground/50">
              None — the engine runs on its own defaults.
            </p>
          ) : (
            <ul className="space-y-0.5">
              {Object.entries(resolvedArgs).map(([key, value]) => (
                <li key={key} className="font-mono text-2xs">
                  --{key} {value}
                </li>
              ))}
            </ul>
          )}
          {overrides.length > 0 && (
            <p className="text-2xs text-amber-700 dark:text-amber-400">
              {overrides.map(([key]) => `--${key}`).join(", ")} came from the
              typed serving block; the engine args spelling of each is ignored.
            </p>
          )}
        </CardContent>
      </Card>
    </div>
  )
}
