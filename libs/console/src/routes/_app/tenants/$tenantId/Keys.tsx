import { useState } from "react"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import { Button } from "@/components/ui/button"
import {
  DataTable,
  type Column,
  type ListState,
} from "@/components/layout/data-table"
import { ConfirmDialog, RowActions } from "@/components/layout/destructive"
import { TextField } from "@/components/layout/form"
import { CopyableBlock } from "@/components/layout/mono"
import { StatusLabel } from "@/components/layout/status-dot"
import { formatError, formatRelative } from "@/lib/format"
import { useReadOnly } from "@/modules/settings/store"
import {
  useMintTenantKey,
  useRevokeTenantKey,
  useTenantKeys,
} from "@/modules/binding/queries"
import type { TenantAPIKey } from "@/modules/binding/types"
import { toast } from "sonner"

/**
 * Mints a key and, once minted, shows the plaintext exactly once: the same
 * shape as a cluster's enrollment token. There is nowhere it could come from
 * afterwards; only the hash is stored.
 */
function MintKeyDialog({
  tenant,
  open,
  onOpenChange,
}: {
  tenant: string
  open: boolean
  onOpenChange: (open: boolean) => void
}) {
  const [name, setName] = useState("")
  const mint = useMintTenantKey(tenant)

  const close = (next: boolean) => {
    onOpenChange(next)
    if (!next) {
      setName("")
      mint.reset()
    }
  }

  return (
    <Dialog open={open} onOpenChange={close}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>New inference key</DialogTitle>
          <DialogDescription>
            {mint.data
              ? "Shown once. It cannot be read again: copy it now."
              : "A credential a caller presents to the gateway on this tenant's public listener."}
          </DialogDescription>
        </DialogHeader>

        {mint.data ? (
          <CopyableBlock value={mint.data.key ?? ""} />
        ) : (
          <TextField
            label="Name"
            value={name}
            onChange={setName}
            placeholder="default"
            hint="For the human who has to decide later whether to revoke it."
          />
        )}

        {!!mint.error && (
          <p className="rounded-md border border-destructive/30 bg-destructive/10 px-3 py-2 text-xs text-destructive">
            {formatError(mint.error, "Could not mint key")}
          </p>
        )}

        <DialogFooter>
          {mint.data ? (
            <Button onClick={() => close(false)}>Done</Button>
          ) : (
            <>
              <Button variant="outline" onClick={() => close(false)}>
                Cancel
              </Button>
              <Button
                disabled={mint.isPending}
                onClick={() => mint.mutate({ name: name || undefined })}
              >
                {mint.isPending ? "Minting…" : "Mint key"}
              </Button>
            </>
          )}
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

/**
 * The tenant's gateway inference keys: the credential a public-listener caller
 * presents, never the console sign-in token. One tenant, many keys, so
 * revoking one caller never touches another.
 */
export function TenantKeys({ tenant }: { tenant: string }) {
  const readOnly = useReadOnly()
  const query = useTenantKeys(tenant)
  const revoke = useRevokeTenantKey(tenant)
  const [minting, setMinting] = useState(false)
  const [revoking, setRevoking] = useState<TenantAPIKey | null>(null)

  const list: ListState<TenantAPIKey> = {
    items: query.data,
    loaded: query.data?.length ?? 0,
    isLoading: query.isLoading,
    error: query.error,
    hasNextPage: false,
    isFetchingNextPage: false,
    fetchNextPage: () => {},
  }

  const columns: Column<TenantAPIKey>[] = [
    {
      key: "name",
      header: "Name",
      cell: (key) => <span className="font-mono text-xs">{key.name}</span>,
    },
    {
      key: "status",
      header: "Status",
      cell: (key) => (
        <StatusLabel tone={key.revoked_at ? "danger" : "ok"}>
          {key.revoked_at ? "revoked" : "active"}
        </StatusLabel>
      ),
    },
    {
      key: "last_used",
      header: "Last used",
      secondary: true,
      cell: (key) => (
        <span className="font-mono text-2xs text-muted-foreground">
          {key.last_used_at ? formatRelative(key.last_used_at) : "never"}
        </span>
      ),
    },
    {
      key: "created",
      header: "Created",
      secondary: true,
      cell: (key) => (
        <span className="font-mono text-2xs text-muted-foreground">
          {formatRelative(key.created_at)}
        </span>
      ),
    },
    {
      key: "actions",
      header: "",
      align: "right",
      cell: (key) =>
        key.revoked_at ? null : (
          <RowActions
            actions={[
              {
                label: "Revoke",
                destructive: true,
                onSelect: () => setRevoking(key),
              },
            ]}
          />
        ),
    },
  ]

  return (
    <div className="space-y-2">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h2 className="font-mono text-2xs tracking-[0.18em] text-muted-foreground uppercase">
          Gateway keys
        </h2>
        {!readOnly && (
          <Button variant="outline" size="sm" onClick={() => setMinting(true)}>
            New key
          </Button>
        )}
      </div>

      <DataTable
        columns={columns}
        list={list}
        rowKey={(key) => key.id}
        empty="No keys minted. A tenant on the public listener needs one to call the gateway."
      />

      <MintKeyDialog tenant={tenant} open={minting} onOpenChange={setMinting} />

      <ConfirmDialog
        open={!!revoking}
        onOpenChange={(open) => !open && setRevoking(null)}
        title="Revoke this key?"
        description={
          <>
            <span className="font-mono text-foreground">{revoking?.name}</span>{" "}
            will stop authenticating requests immediately.
          </>
        }
        confirmLabel="Revoke"
        pendingLabel="Revoking…"
        pending={revoke.isPending}
        error={revoke.error}
        onConfirm={() =>
          revoking &&
          revoke.mutate(revoking.id, {
            onSuccess: () => {
              toast.success("Key revoked", { description: revoking.name })
              setRevoking(null)
            },
          })
        }
      />
    </div>
  )
}
