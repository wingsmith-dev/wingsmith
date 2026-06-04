import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import { Switch } from "@/components/ui/switch"
import type { Deployment, EndpointTargetRequest } from "@/modules/demand/types"
import {
  IconArrowDown,
  IconArrowUp,
  IconPlus,
  IconX,
} from "@tabler/icons-react"

const TARGET_ROLES = ["primary", "canary", "fallback"]

type DeploymentChoice = { value: string; label: string }

/** A deployment reads as its own name, never a bare id. */
function deploymentChoices(deployments: Deployment[]): DeploymentChoice[] {
  return deployments.map((d) => ({
    value: d.id,
    label: d.name,
  }))
}

function DeploymentSelect({
  value,
  choices,
  onChange,
  ariaLabel,
}: {
  value: string
  choices: DeploymentChoice[]
  onChange: (value: string) => void
  ariaLabel: string
}) {
  const known = choices.some((c) => c.value === value)
  return (
    <Select value={value} onValueChange={onChange}>
      <SelectTrigger
        aria-label={ariaLabel}
        className="w-full font-mono text-xs"
      >
        <SelectValue placeholder="Select deployment…" />
      </SelectTrigger>
      <SelectContent>
        {!known && value && (
          <SelectItem value={value} className="font-mono text-xs">
            {value}
          </SelectItem>
        )}
        {choices.map((choice) => (
          <SelectItem
            key={choice.value}
            value={choice.value}
            className="font-mono text-xs"
          >
            {choice.label}
          </SelectItem>
        ))}
      </SelectContent>
    </Select>
  )
}

/**
 * The weighted target set, a row per target.
 *
 * A JSON textarea asked an operator to know the wire shape of a target and made a
 * missing brace the reason a rollout would not submit. The set is still edited and
 * posted as one thing — the rows are how it is typed, not four independent edits.
 */
export function TargetsField({
  value,
  deployments,
  onChange,
}: {
  value: EndpointTargetRequest[]
  deployments: Deployment[]
  onChange: (value: EndpointTargetRequest[]) => void
}) {
  const choices = deploymentChoices(deployments)
  const taken = new Set(value.map((row) => row.deployment))
  const next = choices.find((c) => !taken.has(c.value))?.value ?? ""

  const patch = (index: number, part: Partial<EndpointTargetRequest>) =>
    onChange(value.map((row, i) => (i === index ? { ...row, ...part } : row)))

  return (
    <div className="space-y-2">
      <div className="flex items-center justify-between gap-2">
        <Label>Targets</Label>
        <Button
          type="button"
          variant="outline"
          size="sm"
          onClick={() =>
            onChange([
              ...value,
              {
                deployment: next,
                weight: 100,
                role: value.length === 0 ? "primary" : "canary",
                enabled: true,
              },
            ])
          }
        >
          <IconPlus data-icon="inline-start" />
          Add target
        </Button>
      </div>

      {value.length === 0 ? (
        <p className="rounded-md border border-dashed px-3 py-3 text-2xs text-muted-foreground">
          No targets. The gateway has nowhere to send a request for this
          endpoint until one is added.
        </p>
      ) : (
        <div className="space-y-2">
          {value.map((row, index) => (
            <div key={index} className="space-y-1.5">
              <div className="grid grid-cols-[1fr_5rem_7.5rem_auto] items-center gap-2">
                <DeploymentSelect
                  value={row.deployment}
                  choices={choices}
                  ariaLabel="Deployment"
                  onChange={(deployment) => patch(index, { deployment })}
                />
                <Input
                  type="number"
                  min={0}
                  value={row.weight ?? ""}
                  placeholder="weight"
                  aria-label="Weight"
                  onChange={(e) =>
                    patch(index, {
                      weight:
                        e.target.value === ""
                          ? undefined
                          : Number(e.target.value),
                    })
                  }
                  className="font-mono text-xs"
                />
                <Select
                  value={row.role ?? "primary"}
                  onValueChange={(role) => patch(index, { role })}
                >
                  <SelectTrigger
                    aria-label="Role"
                    className="w-full font-mono text-xs"
                  >
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    {TARGET_ROLES.map((role) => (
                      <SelectItem
                        key={role}
                        value={role}
                        className="font-mono text-xs"
                      >
                        {role}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
                <div className="flex items-center gap-1.5">
                  <Switch
                    checked={row.enabled !== false}
                    onCheckedChange={(enabled) => patch(index, { enabled })}
                    aria-label="Enabled"
                  />
                  <Button
                    type="button"
                    variant="ghost"
                    size="icon-sm"
                    aria-label="Remove target"
                    onClick={() =>
                      onChange(value.filter((_, i) => i !== index))
                    }
                  >
                    <IconX />
                  </Button>
                </div>
              </div>
              <Input
                value={row.served_model_name ?? ""}
                placeholder="served model name — blank to use the deployment's"
                aria-label="Served model name"
                onChange={(e) =>
                  patch(index, {
                    served_model_name: e.target.value.trim() || null,
                  })
                }
                className="font-mono text-xs"
              />
            </div>
          ))}
        </div>
      )}

      <p className="text-2xs text-muted-foreground/70">
        Weight, role, and whether the gateway may route to the deployment.
      </p>
    </div>
  )
}

/**
 * The failover order — a list where the position is the meaning, so it is edited
 * by moving a row rather than by retyping a sequence of ids.
 */
export function FailoverField({
  value,
  deployments,
  onChange,
}: {
  value: string[]
  deployments: Deployment[]
  onChange: (value: string[]) => void
}) {
  const choices = deploymentChoices(deployments)
  const next = choices.find((c) => !value.includes(c.value))?.value ?? ""

  const move = (index: number, to: number) => {
    if (to < 0 || to >= value.length) return
    const rows = [...value]
    const [row] = rows.splice(index, 1)
    rows.splice(to, 0, row)
    onChange(rows)
  }

  return (
    <div className="space-y-2">
      <div className="flex items-center justify-between gap-2">
        <Label>Failover order</Label>
        <Button
          type="button"
          variant="outline"
          size="sm"
          onClick={() => onChange([...value, next])}
        >
          <IconPlus data-icon="inline-start" />
          Add step
        </Button>
      </div>

      {value.length === 0 ? (
        <p className="rounded-md border border-dashed px-3 py-3 text-2xs text-muted-foreground">
          No failover order. The routing strategy decides on its own which
          target takes a request.
        </p>
      ) : (
        <div className="space-y-2">
          {value.map((deployment, index) => (
            <div
              key={index}
              className="grid grid-cols-[1.5rem_1fr_auto] items-center gap-2"
            >
              <span className="text-center font-mono text-2xs text-muted-foreground">
                {index + 1}
              </span>
              <DeploymentSelect
                value={deployment}
                choices={choices}
                ariaLabel={`Failover step ${index + 1}`}
                onChange={(chosen) =>
                  onChange(value.map((row, i) => (i === index ? chosen : row)))
                }
              />
              <div className="flex items-center gap-1">
                <Button
                  type="button"
                  variant="ghost"
                  size="icon-sm"
                  aria-label="Move up"
                  disabled={index === 0}
                  onClick={() => move(index, index - 1)}
                >
                  <IconArrowUp />
                </Button>
                <Button
                  type="button"
                  variant="ghost"
                  size="icon-sm"
                  aria-label="Move down"
                  disabled={index === value.length - 1}
                  onClick={() => move(index, index + 1)}
                >
                  <IconArrowDown />
                </Button>
                <Button
                  type="button"
                  variant="ghost"
                  size="icon-sm"
                  aria-label="Remove step"
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
        Most preferred first. Only read when the routing strategy is failover.
      </p>
    </div>
  )
}
