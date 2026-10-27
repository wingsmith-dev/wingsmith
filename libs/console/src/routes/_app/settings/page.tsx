import { PageHeader } from "@/components/layout/page-header"
import {
  IconDeviceDesktopAnalytics,
  IconEye,
  IconServerCog,
  IconSettings,
  IconShieldLock,
  IconSortDescending,
} from "@tabler/icons-react"
import { Link, useSearch } from "@tanstack/react-router"
import type { SettingsSection } from "./route"
import {
  GpuModelsSection,
  PoliciesSection,
  PriorityClassesSection,
} from "./Definitions"
import { ConfigurationSection } from "./Configuration"
import { AccessSection, PreferencesSection } from "./ConsoleSettings"

/**
 * Three kinds of thing live here. **Definitions** are reference data — hardware
 * types, scheduling classes and guardrails the rest of the console
 * points at; they are the plane's, read constantly and written rarely. **Plane**
 * is the running process described back to you: nothing under it is editable
 * from anywhere, which is why it is not filed with the definitions it sits
 * beside. Beneath both, **Console** settings belong to this browser and are
 * never written over the API, so two operators can hold different views of the
 * same fleet.
 *
 * One section is on screen at a time and the nav picks it, held in `?section=`
 * so a view is linkable and the back button walks it.
 */
export function SettingsPage() {
  const { section } = useSearch({ from: "/_app/settings" })
  const Section = SECTIONS[section]

  return (
    <div className="flex flex-col">
      <PageHeader
        title="Settings"
        description="definitions · plane · this console"
      />

      <div className="grid gap-6 p-4 md:grid-cols-[12rem_minmax(0,1fr)]">
        <SettingsNav current={section} />

        <div className="flex min-w-0 flex-col gap-4">
          <Section />
        </div>
      </div>
    </div>
  )
}

const SECTIONS: Record<SettingsSection, React.ComponentType> = {
  "gpu-models": GpuModelsSection,
  priority: PriorityClassesSection,
  policies: PoliciesSection,
  configuration: ConfigurationSection,
  preferences: PreferencesSection,
  access: AccessSection,
}

const NAV_ITEMS = [
  { label: "Definitions", kind: "label" as const },
  {
    section: "gpu-models" as const,
    label: "GPU models",
    icon: IconDeviceDesktopAnalytics,
  },
  {
    section: "priority" as const,
    label: "Priority classes",
    icon: IconSortDescending,
  },
  { section: "policies" as const, label: "Policies", icon: IconShieldLock },
  { label: "Plane", kind: "label" as const },
  {
    section: "configuration" as const,
    label: "Configuration",
    icon: IconServerCog,
  },
  { label: "Console", kind: "label" as const },
  {
    section: "preferences" as const,
    label: "Preferences",
    icon: IconSettings,
  },
  { section: "access" as const, label: "Access & tokens", icon: IconEye },
]

function SettingsNav({ current }: { current: SettingsSection }) {
  return (
    <nav className="top-20 flex flex-row flex-wrap gap-0.5 self-start md:sticky md:flex-col">
      {NAV_ITEMS.map((item) =>
        "kind" in item ? (
          <span
            key={item.label}
            className="flex h-8 items-center px-2 font-mono text-2xs tracking-widest text-muted-foreground uppercase"
          >
            {item.label}
          </span>
        ) : (
          <Link
            key={item.section}
            to="/settings"
            search={{ section: item.section }}
            aria-current={current === item.section ? "page" : undefined}
            className={
              current === item.section
                ? "flex h-8 items-center gap-2 rounded-md bg-primary/10 px-2 text-xs font-medium text-primary"
                : "flex h-8 items-center gap-2 rounded-md px-2 text-xs text-muted-foreground hover:bg-accent hover:text-foreground"
            }
          >
            <item.icon className="size-4 shrink-0" />
            {item.label}
          </Link>
        )
      )}
    </nav>
  )
}
