import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { Switch } from "@/components/ui/switch"
import type { ArtifactLocationRequest } from "@/modules/demand/types"
import { IconPlus, IconX } from "@tabler/icons-react"

/**
 * Where the bytes can be fetched from — the one field on an artifact that is a
 * list of places rather than a fact about the weights.
 *
 * A row per location, because a JSON textarea asks a person to know a wire shape
 * they have no reason to know, and the failure mode is a form that will not
 * submit over a missing brace. Resolving a HuggingFace repo fills the first row
 * in; a set of weights already mirrored to object storage gets a second, and
 * `preferred` is which one a cluster should reach for first.
 *
 * The plane never fetches through any of these — a location is what the operator
 * pulls with, so nothing here is validated against reachability from the plane.
 */
export function LocationsField({
  value,
  onChange,
}: {
  value: ArtifactLocationRequest[]
  onChange: (value: ArtifactLocationRequest[]) => void
}) {
  const patch = (index: number, part: Partial<ArtifactLocationRequest>) =>
    onChange(value.map((row, i) => (i === index ? { ...row, ...part } : row)))

  /** Exactly one row can be preferred, so choosing one clears the others. */
  const prefer = (index: number, preferred: boolean) =>
    onChange(
      value.map((row, i) => ({ ...row, preferred: preferred && i === index }))
    )

  return (
    <div className="space-y-2">
      <div className="flex items-center justify-between gap-2">
        <Label>Locations</Label>
        <Button
          type="button"
          variant="outline"
          size="sm"
          onClick={() =>
            onChange([
              ...value,
              {
                scheme: "s3",
                uri: "",
                region: null,
                preferred: value.length === 0,
              },
            ])
          }
        >
          <IconPlus data-icon="inline-start" />
          Add location
        </Button>
      </div>

      {value.length === 0 ? (
        <p className="rounded-md border border-dashed px-3 py-3 text-2xs text-muted-foreground">
          No locations. A cluster has nowhere to pull these weights from until
          one is added, or until a source is resolved above.
        </p>
      ) : (
        <div className="space-y-2">
          {value.map((row, index) => (
            <div
              key={index}
              className="grid grid-cols-[5rem_1fr_6rem_auto] items-center gap-2"
            >
              <Input
                value={row.scheme}
                placeholder="s3"
                onChange={(e) => patch(index, { scheme: e.target.value })}
                className="font-mono text-xs"
              />
              <Input
                value={row.uri}
                placeholder="s3://bucket/prefix"
                onChange={(e) => patch(index, { uri: e.target.value })}
                className="font-mono text-xs"
              />
              <Input
                value={row.region ?? ""}
                placeholder="region"
                onChange={(e) =>
                  patch(index, { region: e.target.value || null })
                }
                className="font-mono text-xs"
              />
              <div className="flex items-center gap-1.5">
                <Switch
                  checked={!!row.preferred}
                  onCheckedChange={(checked) => prefer(index, checked)}
                  aria-label="Preferred"
                />
                <Button
                  type="button"
                  variant="ghost"
                  size="icon-sm"
                  aria-label="Remove location"
                  onClick={() => onChange(value.filter((_, i) => i !== index))}
                >
                  <IconX />
                </Button>
              </div>
            </div>
          ))}
        </div>
      )}

      <p className="text-2xs text-muted-foreground/70">
        Scheme, URI, region, and which one to prefer.
      </p>
    </div>
  )
}
