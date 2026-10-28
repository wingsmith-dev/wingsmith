import { Panel } from "@/components/layout/panel"
import { Field, FieldGrid } from "@/components/layout/field"
import { Badge } from "@/components/ui/badge"
import { Label } from "@/components/ui/label"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import { Switch } from "@/components/ui/switch"
import { useTheme } from "@/components/theme-provider"
import { BASE_URL } from "@/lib/api/http"
import { transport } from "@/lib/api"
import { useAuthStore } from "@/modules/auth/store"
import { useSignOut } from "@/modules/auth/session"
import { useSettingsStore } from "@/modules/settings/store"

/**
 * This browser's own settings, in three panels: read-only is a
 * safety switch, appearance is taste, and the plane endpoint is deployment
 * configuration nobody can change from here. Reading them as one list invites
 * the last to be mistaken for a setting.
 */
export function PreferencesSection() {
  return (
    <>
      <ReadOnlyPanel />
      <AppearancePanel />
      <PlanePanel />
    </>
  )
}

function ReadOnlyPanel() {
  const readOnly = useSettingsStore((s) => s.readOnly)
  const setReadOnly = useSettingsStore((s) => s.setReadOnly)

  return (
    <Panel
      id="preferences"
      title="Read-only mode"
      description="what this console is allowed to write"
    >
      <div className="space-y-4">
        <div className="flex items-start justify-between gap-4 rounded-md border px-3 py-2.5">
          <div className="space-y-0.5">
            <Label htmlFor="read-only">Read-only mode</Label>
            <p className="text-2xs text-muted-foreground">
              Refuses every write before it leaves the browser, and hides the
              affordances that would start one.
            </p>
          </div>
          <Switch
            id="read-only"
            checked={readOnly}
            onCheckedChange={setReadOnly}
          />
        </div>

        <p className="rounded-md border bg-muted/40 px-3 py-2 text-xs text-muted-foreground">
          This stops <em>this console</em> writing. It does not stop anyone
          else, and it does not stop the hub reconciling a cluster toward
          declared state. Connecting a live cluster safely is a separate,
          per-cluster decision.
        </p>
      </div>
    </Panel>
  )
}

function AppearancePanel() {
  const { theme, setTheme } = useTheme()

  return (
    <Panel
      id="appearance"
      title="Appearance"
      description="held in this browser, never written to the plane"
    >
      <div className="space-y-4">
        <div className="space-y-1.5">
          <Label htmlFor="theme">Theme</Label>
          <Select
            value={theme}
            onValueChange={(v) => setTheme(v as typeof theme)}
          >
            <SelectTrigger id="theme" className="w-56 font-mono text-xs">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              {["system", "light", "dark"].map((value) => (
                <SelectItem
                  key={value}
                  value={value}
                  className="font-mono text-xs"
                >
                  {value}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>
      </div>
    </Panel>
  )
}

/**
 * An empty {@link BASE_URL} is a setting, not a missing one: every request goes
 * to the origin that served the console. Naming that origin is the only way the
 * field reads as configured instead of blank.
 */
function PlanePanel() {
  const origin = typeof window === "undefined" ? null : window.location.origin

  return (
    <Panel
      id="plane"
      title="Plane"
      description="where this console sends its requests"
    >
      <div className="space-y-1.5">
        <FieldGrid>
          <Field label="Base URL">
            {BASE_URL || (
              <span>
                same origin
                {origin && (
                  <span className="ml-1.5 text-muted-foreground">{origin}</span>
                )}
              </span>
            )}
          </Field>
          <Field label="Transport">
            <Badge variant="outline" className="font-mono text-2xs">
              {transport.kind}
            </Badge>
          </Field>
        </FieldGrid>
        <p className="font-mono text-2xs text-muted-foreground">
          Set by the deployment: WINGSMITH_API_BASE_URL on the console image, or
          VITE_API_BASE_URL for a dev server.
        </p>
      </div>
    </Panel>
  )
}

/** Who the plane thinks you are, and the way out. */
export function AccessSection() {
  const operator = useAuthStore((s) => s.operator)
  const kind = useAuthStore((s) => s.kind)
  const signOut = useSignOut()

  return (
    <Panel
      id="access"
      title="Access & tokens"
      description="who the plane thinks you are"
    >
      <div className="space-y-4">
        <FieldGrid>
          <Field label="Name">{operator?.name ?? "—"}</Field>
          <Field label="Access">{kind === "tenant" ? "Tenant" : "Admin"}</Field>
        </FieldGrid>
        {kind === "admin" && (
          <p className="text-2xs text-muted-foreground">
            The console has one admin login, set on the plane. Everyone who
            signs in as admin shares it and has full access.
          </p>
        )}
        <button
          onClick={signOut}
          className="text-xs text-destructive hover:underline"
        >
          Sign out
        </button>
      </div>
    </Panel>
  )
}
