import {
  FormDialog,
  FormGrid,
  JsonField,
  SwitchField,
  TextField,
} from "@/components/layout/form"
import { formatError } from "@/lib/format"
import { useCreatePolicy, useUpdatePolicy } from "@/modules/binding/queries"
import type { Policy, PolicyAttachmentRequest } from "@/modules/binding/types"
import { useState } from "react"
import { toast } from "sonner"

function split(text: string): string[] {
  return text
    .split(",")
    .map((entry) => entry.trim())
    .filter(Boolean)
}

/**
 * Allow-lists and deny-lists stay separate fields. An empty allow-list means
 * "no opinion", so folding the two into one control would turn silence into a
 * refusal the moment somebody saved the form.
 */
export function PolicyForm({
  open,
  onOpenChange,
  policy,
}: {
  open: boolean
  onOpenChange: (open: boolean) => void
  policy?: Policy
}) {
  const editing = !!policy
  const create = useCreatePolicy()
  const update = useUpdatePolicy()

  const [name, setName] = useState(policy?.name ?? "")
  const [description, setDescription] = useState(policy?.description ?? "")
  const [regions, setRegions] = useState(
    (policy?.allowed_regions ?? []).join(", ")
  )
  const [residencies, setResidencies] = useState(
    (policy?.allowed_residencies ?? []).join(", ")
  )
  const [deviceClasses, setDeviceClasses] = useState(
    (policy?.allowed_device_classes ?? []).join(", ")
  )
  const [allowedClusters, setAllowedClusters] = useState(
    (policy?.allowed_clusters ?? []).join(", ")
  )
  const [deniedClusters, setDeniedClusters] = useState(
    (policy?.denied_clusters ?? []).join(", ")
  )
  const [enabled, setEnabled] = useState(policy?.enabled ?? true)
  const [rules, setRules] = useState<unknown>(policy?.rules ?? {})
  const [rulesValid, setRulesValid] = useState(true)
  const [attachments, setAttachments] = useState<unknown>(
    policy?.attachments.map((a) => ({
      subject_type: a.subject_type,
      subject: a.subject ?? a.subject_id,
    })) ?? []
  )
  const [attachmentsValid, setAttachmentsValid] = useState(true)

  const pending = create.isPending || update.isPending
  const error = create.error ?? update.error

  const body = {
    name,
    description: description || null,
    allowed_regions: split(regions),
    allowed_residencies: split(residencies),
    allowed_device_classes: split(deviceClasses),
    allowed_clusters: split(allowedClusters),
    denied_clusters: split(deniedClusters),
    rules: (rules as Record<string, unknown>) ?? {},
    enabled,
    attachments: (attachments as PolicyAttachmentRequest[]) ?? [],
  }

  const done = (verb: string) => {
    onOpenChange(false)
    toast.success(`Policy ${verb}`, { description: name })
  }

  const submit = () => {
    if (editing) {
      update.mutate(
        { ref: policy.id, body, revision: policy.revision },
        {
          onSuccess: () => done("updated"),
          onError: (e) =>
            toast.error(formatError(e, "Could not update policy")),
        }
      )
      return
    }
    create.mutate(body, {
      onSuccess: () => done("created"),
      onError: (e) => toast.error(formatError(e, "Could not create policy")),
    })
  }

  return (
    <FormDialog
      icon="policy"
      wide
      open={open}
      onOpenChange={onOpenChange}
      title={editing ? "Edit policy" : "New policy"}
      description="A placement constraint and everything it binds to."
      error={error}
      pending={pending}
      disabled={!name || !rulesValid || !attachmentsValid}
      onSubmit={submit}
    >
      <FormGrid>
        <TextField label="Name" value={name} onChange={setName} />
        <TextField
          label="Description"
          value={description}
          onChange={setDescription}
          mono={false}
        />
        <TextField
          label="Allowed regions"
          value={regions}
          onChange={setRegions}
          placeholder="eu-west-1, us-east-2"
          hint="Comma separated. Empty means no opinion."
        />
        <TextField
          label="Allowed residencies"
          value={residencies}
          onChange={setResidencies}
          placeholder="eu"
          hint="Comma separated. Empty means no opinion."
        />
        <TextField
          label="Allowed device classes"
          value={deviceClasses}
          onChange={setDeviceClasses}
          placeholder="h100-80gb"
          hint="Comma separated. Empty means no opinion."
        />
        <TextField
          label="Allowed clusters"
          value={allowedClusters}
          onChange={setAllowedClusters}
          hint="Comma separated cluster refs."
        />
        <TextField
          label="Denied clusters"
          value={deniedClusters}
          onChange={setDeniedClusters}
          hint="A denial outlives somebody widening the allow-list."
        />
      </FormGrid>

      <SwitchField label="Enabled" checked={enabled} onChange={setEnabled} />

      <JsonField
        label="Attachments"
        value={attachments}
        rows={4}
        hint="Array of {subject_type, subject}. Types: cluster, endpoint, project, tenant."
        onChange={(value, valid) => {
          setAttachments(value ?? [])
          setAttachmentsValid(valid)
        }}
      />

      <JsonField
        label="Rules"
        value={rules}
        rows={4}
        onChange={(value, valid) => {
          setRules(value ?? {})
          setRulesValid(valid)
        }}
      />
    </FormDialog>
  )
}
