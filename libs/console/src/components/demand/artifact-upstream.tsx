import { ChoiceSetField } from "@/components/demand/choice-set"
import { DeclaredSection } from "@/components/demand/enforcement"
import { ProvenanceFields } from "@/components/demand/provenance"
import {
  FormGrid,
  NumberField,
  SelectField,
  TextField,
} from "@/components/layout/form"
import {} from "@/components/layout/mono"
import {} from "@/lib/format"
import {} from "@/modules/demand/display"
import {
  INPUT_MODALITIES,
  STRUCTURED_OUTPUT_MODES,
  type ArtifactUpstream,
} from "@/modules/demand/types"

const UNKNOWN = "__unknown__"

/** Three states, because "the publisher did not say" is not "the publisher said no". */
function ClaimField({
  label,
  value,
  onChange,
  hint,
}: {
  label: string
  value: boolean | null
  onChange: (value: boolean | null) => void
  hint?: string
}) {
  return (
    <SelectField
      label={label}
      hint={hint}
      value={value === null ? UNKNOWN : value ? "yes" : "no"}
      onChange={(next) =>
        onChange(next === UNKNOWN ? null : next === "yes" ? true : false)
      }
      options={[
        { value: UNKNOWN, label: "not stated" },
        { value: "yes", label: "yes" },
        { value: "no", label: "no" },
      ]}
    />
  )
}

/**
 * What the publisher said this release does.
 *
 * The reference a workload is judged against, never our configuration: a serving
 * block declaring a 256K context is only a finding when there is a published number
 * beside it. Everything here is a claim, so the provenance triple is part of the
 * block instead of an afterthought.
 */
export function UpstreamFields({
  value,
  onChange,
}: {
  value: ArtifactUpstream
  onChange: (value: ArtifactUpstream) => void
}) {
  const set = (patch: Partial<ArtifactUpstream>) =>
    onChange({ ...value, ...patch })

  return (
    <DeclaredSection
      title="Upstream"
      kind="asserted"
      hint="What the publisher states, not what was measured or configured. Nothing here reaches a cluster; the gap between it and what is served is what conformance reports."
    >
      <FormGrid>
        <TextField
          label="Provider"
          value={value.provider ?? ""}
          onChange={(provider) => set({ provider: provider || null })}
          placeholder="openai"
        />
        <TextField
          label="Provider model id"
          value={value.provider_model_id ?? ""}
          onChange={(provider_model_id) =>
            set({ provider_model_id: provider_model_id || null })
          }
          placeholder="gpt-oss-20b"
        />
        <NumberField
          label="Context length"
          value={value.context_length}
          onChange={(context_length) => set({ context_length })}
          min={1}
          hint="As published. What a workload serves at is a separate number."
        />
        <NumberField
          label="Max output tokens"
          value={value.max_output_tokens}
          onChange={(max_output_tokens) => set({ max_output_tokens })}
          min={1}
        />
        <ClaimField
          label="Reasoning"
          value={value.reasoning}
          onChange={(reasoning) => set({ reasoning })}
        />
        <ClaimField
          label="Tool calling"
          value={value.tool_calling}
          onChange={(tool_calling) => set({ tool_calling })}
        />
        <ChoiceSetField
          label="Input modalities"
          value={value.input_modalities}
          onChange={(input_modalities) => set({ input_modalities })}
          options={INPUT_MODALITIES}
        />
        <ChoiceSetField
          label="Structured output modes"
          value={value.structured_output_modes}
          onChange={(structured_output_modes) =>
            set({ structured_output_modes })
          }
          options={STRUCTURED_OUTPUT_MODES}
        />
        <TextField
          label="License as published"
          value={value.licence ?? ""}
          onChange={(license) => set({ licence: license || null })}
          hint="A second copy: a difference from the artifact's own license is a finding, not a bug."
        />
        <TextField
          label="Released at"
          type="datetime-local"
          value={value.released_at ? value.released_at.slice(0, 16) : ""}
          onChange={(released_at) =>
            set({
              released_at: released_at
                ? new Date(released_at).toISOString()
                : null,
            })
          }
        />
      </FormGrid>

      <ProvenanceFields
        value={{
          by: value.recorded_by,
          at: value.recorded_at,
          uri: value.source_uri,
        }}
        onChange={(provenance) =>
          set({
            recorded_by: provenance.by,
            recorded_at: provenance.at,
            source_uri: provenance.uri,
          })
        }
      />
    </DeclaredSection>
  )
}
