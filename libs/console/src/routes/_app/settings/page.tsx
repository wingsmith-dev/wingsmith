import { Field } from "@/components/layout/field"
import { PageHeader } from "@/components/layout/page-header"
import { Badge } from "@/components/ui/badge"
import { Card } from "@/components/ui/card"
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
import {
  ACCENTS,
  useSettingsStore,
  type Accent,
} from "@/modules/settings/store"

function Section({
  title,
  description,
  children,
}: React.PropsWithChildren<{ title: string; description: string }>) {
  return (
    <Card className="overflow-hidden p-0">
      <div className="border-b bg-muted/30 px-4 py-3">
        <h2 className="text-sm leading-none font-semibold">{title}</h2>
        <p className="mt-1 font-mono text-2xs text-muted-foreground/60">
          {description}
        </p>
      </div>
      <div className="space-y-4 p-4">{children}</div>
    </Card>
  )
}

/**
 * Settings that belong to this browser, not to the plane. Nothing here is
 * written over the API, so two operators can hold different views of the same
 * fleet — which is the point of a read-only session.
 */
export function SettingsPage() {
  const { theme, setTheme } = useTheme()
  const readOnly = useSettingsStore((s) => s.readOnly)
  const setReadOnly = useSettingsStore((s) => s.setReadOnly)
  const accent = useSettingsStore((s) => s.accent)
  const setAccent = useSettingsStore((s) => s.setAccent)
  const operator = useAuthStore((s) => s.operator)
  const signOut = useSignOut()

  return (
    <div className="flex flex-col">
      <PageHeader title="Settings" description="this console · not the plane" />

      <div className="max-w-3xl space-y-4 p-4">
        <Section
          title="Safety"
          description="what this browser is allowed to do"
        >
          <div className="flex items-start justify-between gap-4 rounded-md border px-3 py-2.5">
            <div className="space-y-0.5">
              <Label htmlFor="read-only">Read-only mode</Label>
              <p className="text-2xs text-muted-foreground/70">
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
        </Section>

        <Section title="Plane" description="where this console is pointed">
          <div className="grid gap-3 sm:grid-cols-2">
            <Field label="Base URL">{BASE_URL}</Field>
            <Field label="Transport">
              <Badge variant="outline" className="font-mono text-2xs">
                {transport.kind}
              </Badge>
            </Field>
          </div>
          <p className="font-mono text-2xs text-muted-foreground/60">
            Set with VITE_API_BASE_URL and VITE_API_MODE at build time.
          </p>
        </Section>

        <Section title="Appearance" description="how it looks on this machine">
          <div className="space-y-1.5">
            <Label>Accent</Label>
            <p className="text-2xs text-muted-foreground/70">
              Drives the active nav item, resource glyphs, buttons and focus
              rings. Green, amber and red are reserved for ok, warn and danger.
            </p>
            <div className="flex flex-wrap gap-2 pt-1">
              {ACCENTS.map((value) => (
                <button
                  key={value}
                  type="button"
                  onClick={() => setAccent(value as Accent)}
                  data-accent={value}
                  aria-pressed={accent === value}
                  className={
                    accent === value
                      ? "flex items-center gap-2 rounded-md border-2 border-primary bg-primary/10 px-2.5 py-1.5 font-mono text-2xs"
                      : "flex items-center gap-2 rounded-md border px-2.5 py-1.5 font-mono text-2xs hover:bg-accent"
                  }
                >
                  <span className="size-3.5 rounded-full bg-primary" />
                  {value}
                </button>
              ))}
            </div>
          </div>

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
        </Section>

        <Section title="Operator" description="who the plane thinks you are">
          <div className="grid gap-3 sm:grid-cols-2">
            <Field label="Name">{operator?.name ?? "—"}</Field>
            <Field label="Email">{operator?.email ?? "—"}</Field>
          </div>
          <button
            onClick={signOut}
            className="text-xs text-destructive hover:underline"
          >
            Sign out
          </button>
        </Section>
      </div>
    </div>
  )
}
