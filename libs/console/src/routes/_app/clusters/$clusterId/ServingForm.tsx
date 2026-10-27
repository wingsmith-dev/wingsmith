import {
  FormGrid,
  FormDialog,
  SwitchField,
  TextField,
} from "@/components/layout/form"
import { formatError } from "@/lib/format"
import { useSetClusterServing } from "@/modules/clusters/queries"
import type { Cluster } from "@/modules/clusters/types"
import { useState } from "react"
import { toast } from "sonner"

/**
 * A full replacement, matching the plane: serving config is three fields and a
 * partial update would buy nothing but a way for them to disagree.
 */
export function ServingForm({
  cluster,
  open,
  onOpenChange,
}: {
  cluster: Cluster
  open: boolean
  onOpenChange: (open: boolean) => void
}) {
  const [url, setUrl] = useState(cluster.serving?.url ?? "")
  const [credential, setCredential] = useState(
    cluster.serving?.credential_id ?? ""
  )
  const [enabled, setEnabled] = useState(cluster.serving?.enabled ?? true)
  const set = useSetClusterServing()

  return (
    <FormDialog
      icon="cluster"
      open={open}
      onOpenChange={onOpenChange}
      title="Serving address"
      description="Where the data plane reaches this cluster. Opaque to hangar: an ingress, an in-cluster service, anything dialable."
      error={set.error}
      pending={set.isPending}
      submitLabel="Save"
      disabled={url.trim() === ""}
      onSubmit={() =>
        set.mutate(
          {
            ref: cluster.id,
            body: {
              url: url.trim(),
              credential_id: credential.trim() || null,
              enabled,
            },
          },
          {
            onSuccess: () => {
              onOpenChange(false)
              toast.success("Serving address saved")
            },
            onError: (error) =>
              toast.error(formatError(error, "Could not save the address")),
          }
        )
      }
    >
      <FormGrid>
        <TextField
          label="URL"
          value={url}
          onChange={setUrl}
          placeholder="http://dynamo-frontend.dynamo.svc:8000"
          className="sm:col-span-2"
        />
        <TextField
          label="Credential"
          value={credential}
          onChange={setCredential}
          placeholder="uuid of a declared credential"
          hint="Whatever fronts the address. Leave empty for a private in-cluster one."
        />
      </FormGrid>

      <SwitchField
        label="Enabled"
        checked={enabled}
        onChange={setEnabled}
        hint="Off takes the cluster out of rotation without forgetting its address."
      />
    </FormDialog>
  )
}
