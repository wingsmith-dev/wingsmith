import {
  Sidebar,
  SidebarContent,
  SidebarFooter,
  SidebarGroup,
  SidebarGroupLabel,
  SidebarHeader,
  SidebarMenu,
  SidebarMenuButton,
  SidebarMenuItem,
  SidebarRail,
} from "@/components/ui/sidebar"
import { Badge } from "@/components/ui/badge"
import { transport } from "@/lib/api"
import {
  ResourceIcon,
  type ResourceKind,
} from "@/components/layout/resource-icon"
import { HangarMark } from "@/components/layout/hangar-mark"
import { IconEye, IconLogout, IconSettings } from "@tabler/icons-react"
import { Link, useRouterState } from "@tanstack/react-router"
import { useAuthStore } from "@/modules/auth/store"
import { useReadOnly } from "@/modules/settings/store"
import { useUndecidedCount } from "@/modules/discovered/queries"

type NavItem = {
  label: string
  to: string
  kind: ResourceKind
  /** Draws the count of sightings nobody has decided on yet. */
  badge?: "undecided"
}

const overviewItems: NavItem[] = [
  { label: "Overview", to: "/overview", kind: "overview" },
]

/**
 * Grouped by what an operator came to do, not by the domain model. Supply,
 * demand, binding and machinery are the right vocabulary in the code and the
 * docs; in a sidebar they ask the reader to translate before they can navigate.
 * Within each group the operated thing comes before the thing that defines it —
 * an endpoint has a URL and users, a recipe is something you wrote last week.
 */
const servingItems: NavItem[] = [
  { label: "Endpoints", to: "/endpoints", kind: "endpoint" },
  { label: "Deployments", to: "/deployments", kind: "deployment" },
  {
    label: "Discovered",
    to: "/discovered",
    kind: "discovered",
    badge: "undecided",
  },
]

const catalogItems: NavItem[] = [
  { label: "Recipes", to: "/recipes", kind: "recipe" },
  { label: "Artifacts", to: "/artifacts", kind: "artifact" },
]

const fleetItems: NavItem[] = [
  { label: "Clusters", to: "/clusters", kind: "cluster" },
  { label: "Pools", to: "/pools", kind: "pool" },
  { label: "Nodes", to: "/nodes", kind: "node" },
  { label: "Devices", to: "/devices", kind: "device" },
]

const accessItems: NavItem[] = [
  { label: "Projects", to: "/projects", kind: "project" },
  { label: "Quotas", to: "/quotas", kind: "quota" },
  {
    label: "Priority classes",
    to: "/priority-classes",
    kind: "priority-class",
  },
  { label: "Policies", to: "/policies", kind: "policy" },
]

const activityItems: NavItem[] = [
  { label: "Operations", to: "/operations", kind: "operation" },
  { label: "Events", to: "/events", kind: "event" },
  { label: "Allocations", to: "/allocations", kind: "allocation" },
]

const activeClass =
  "data-[active=true]:bg-primary/10 data-[active=true]:text-primary data-[active=true]:font-semibold"

/**
 * Adopted clusters arrive with workloads already running on them. Until someone
 * decides about each one it is GPUs held by something the plane cannot see, so
 * the count is carried in the sidebar rather than waiting on the page.
 */
function UndecidedBadge() {
  const count = useUndecidedCount()
  if (count === 0) return null
  return (
    <Badge
      variant="outline"
      className="ml-auto border-amber-500/30 bg-amber-500/10 font-mono text-2xs text-amber-600 group-data-[collapsible=icon]:hidden dark:text-amber-400"
    >
      {count}
    </Badge>
  )
}

function NavGroup({
  label,
  items,
  pathname,
}: {
  label?: string
  items: NavItem[]
  pathname: string
}) {
  return (
    <SidebarGroup>
      {label && (
        <SidebarGroupLabel className="font-mono text-2xs font-semibold tracking-[0.15em] text-muted-foreground/50 uppercase">
          {label}
        </SidebarGroupLabel>
      )}
      <SidebarMenu>
        {items.map((item) => (
          <SidebarMenuItem key={item.to}>
            <SidebarMenuButton
              asChild
              isActive={pathname.startsWith(item.to)}
              tooltip={item.label}
              className={activeClass}
            >
              <Link to={item.to}>
                <ResourceIcon kind={item.kind} />
                <span>{item.label}</span>
                {item.badge === "undecided" && <UndecidedBadge />}
              </Link>
            </SidebarMenuButton>
          </SidebarMenuItem>
        ))}
      </SidebarMenu>
    </SidebarGroup>
  )
}

export function ConsoleSidebar({
  ...props
}: React.ComponentProps<typeof Sidebar>) {
  const readOnly = useReadOnly()
  const pathname = useRouterState().location.pathname
  const signOut = useAuthStore((s) => s.signOut)
  const operator = useAuthStore((s) => s.operator)

  return (
    <Sidebar collapsible="icon" {...props}>
      <SidebarHeader className="gap-3 p-4">
        <div className="flex items-center gap-2">
          <div className="flex size-7 shrink-0 items-center justify-center rounded-md bg-white text-[#0b1220]">
            <HangarMark className="size-4" />
          </div>
          <span className="truncate text-sm leading-none font-semibold tracking-[0.14em] uppercase group-data-[collapsible=icon]:hidden">
            HANGAR
          </span>
        </div>
        {transport.kind === "fixtures" && (
          <Badge
            variant="outline"
            className="justify-center border-amber-500/30 bg-amber-500/10 font-mono text-2xs tracking-[0.18em] text-amber-600 uppercase group-data-[collapsible=icon]:hidden dark:text-amber-400"
          >
            Fixtures
          </Badge>
        )}
      </SidebarHeader>

      <SidebarContent>
        <NavGroup items={overviewItems} pathname={pathname} />
        <NavGroup label="Serving" items={servingItems} pathname={pathname} />
        <NavGroup label="Catalog" items={catalogItems} pathname={pathname} />
        <NavGroup label="Fleet" items={fleetItems} pathname={pathname} />
        <NavGroup label="Access" items={accessItems} pathname={pathname} />
        <NavGroup label="Activity" items={activityItems} pathname={pathname} />
      </SidebarContent>

      <SidebarFooter className="p-2">
        {readOnly && <ReadOnlyNotice />}
        <SidebarMenu>
          <SidebarMenuItem>
            <SidebarMenuButton
              asChild
              isActive={pathname.startsWith("/settings")}
              tooltip="Settings"
              className={activeClass}
            >
              <Link to="/settings">
                <IconSettings />
                <span>Settings</span>
              </Link>
            </SidebarMenuButton>
          </SidebarMenuItem>
          <SidebarMenuItem>
            <SidebarMenuButton
              tooltip={operator?.email ?? "Sign out"}
              onClick={signOut}
            >
              <IconLogout />
              <span className="truncate">{operator?.email ?? "Sign out"}</span>
            </SidebarMenuButton>
          </SidebarMenuItem>
        </SidebarMenu>
      </SidebarFooter>

      <SidebarRail />
    </Sidebar>
  )
}

/**
 * Read-only is a mode, not an event: a banner over every page shouts a fact that
 * never changes. It sits by Settings because that is where it is turned off, and
 * it names its own limit — the hub keeps reconciling regardless of this console.
 */
function ReadOnlyNotice() {
  return (
    <Link
      to="/settings"
      title="Writes are blocked from this console. Connected clusters still reconcile."
      className="mb-1 flex items-center gap-2 rounded-md border border-amber-500/30 bg-amber-500/10 px-2 py-1.5 text-amber-700 transition-colors group-data-[collapsible=icon]:justify-center group-data-[collapsible=icon]:px-0 hover:bg-amber-500/15 dark:text-amber-400"
    >
      <IconEye className="size-4 shrink-0" />
      <span className="min-w-0 flex-1 truncate font-mono text-2xs tracking-[0.14em] uppercase group-data-[collapsible=icon]:hidden">
        read-only
      </span>
    </Link>
  )
}
