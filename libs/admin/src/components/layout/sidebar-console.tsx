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
import { ResourceIcon, type ResourceKind } from "@/components/layout/resource-icon"
import { IconLogout, IconTopologyStar3 } from "@tabler/icons-react"
import { Link, useRouterState } from "@tanstack/react-router"
import { useAuthStore } from "@/modules/auth/store"

type NavItem = { label: string; to: string; kind: ResourceKind }

const overviewItems: NavItem[] = [
  { label: "Overview", to: "/overview", kind: "overview" },
]

const supplyItems: NavItem[] = [
  { label: "Clusters", to: "/clusters", kind: "cluster" },
  { label: "Nodes", to: "/nodes", kind: "node" },
  { label: "Devices", to: "/devices", kind: "device" },
  { label: "Pools", to: "/pools", kind: "pool" },
]

const demandItems: NavItem[] = [
  { label: "Artifacts", to: "/artifacts", kind: "artifact" },
  { label: "Recipes", to: "/recipes", kind: "recipe" },
  { label: "Deployments", to: "/deployments", kind: "deployment" },
  { label: "Endpoints", to: "/endpoints", kind: "endpoint" },
]

const bindingItems: NavItem[] = [
  { label: "Projects", to: "/projects", kind: "project" },
  { label: "Quotas", to: "/quotas", kind: "quota" },
  { label: "Priority classes", to: "/priority-classes", kind: "priority-class" },
  { label: "Policies", to: "/policies", kind: "policy" },
]

const machineryItems: NavItem[] = [
  { label: "Operations", to: "/operations", kind: "operation" },
  { label: "Allocations", to: "/allocations", kind: "allocation" },
  { label: "Events", to: "/events", kind: "event" },
]

const activeClass =
  "data-[active=true]:bg-primary/10 data-[active=true]:text-primary data-[active=true]:font-semibold"

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
      {label && <SidebarGroupLabel className="font-mono text-2xs font-semibold uppercase tracking-[0.15em] text-muted-foreground/50">
        {label}
      </SidebarGroupLabel>}
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
              </Link>
            </SidebarMenuButton>
          </SidebarMenuItem>
        ))}
      </SidebarMenu>
    </SidebarGroup>
  )
}

export function ConsoleSidebar({ ...props }: React.ComponentProps<typeof Sidebar>) {
  const pathname = useRouterState().location.pathname
  const signOut = useAuthStore((s) => s.signOut)
  const operator = useAuthStore((s) => s.operator)

  return (
    <Sidebar collapsible="icon" {...props}>
      <SidebarHeader className="gap-3 p-4">
        <div className="flex items-center gap-2">
          <div className="flex size-7 shrink-0 items-center justify-center rounded-md bg-primary text-primary-foreground">
            <IconTopologyStar3 className="size-4" />
          </div>
          <div className="flex min-w-0 flex-col group-data-[collapsible=icon]:hidden">
            <span className="truncate text-sm font-semibold leading-none">
              Hangar
            </span>
            <span className="truncate font-mono text-2xs text-muted-foreground/60">
              control plane
            </span>
          </div>
        </div>
        {transport.kind === "fixtures" && (
          <Badge
            variant="outline"
            className="justify-center border-amber-500/30 bg-amber-500/10 font-mono text-2xs uppercase tracking-[0.18em] text-amber-600 group-data-[collapsible=icon]:hidden dark:text-amber-400"
          >
            Fixtures
          </Badge>
        )}
      </SidebarHeader>

      <SidebarContent>
        <NavGroup items={overviewItems} pathname={pathname} />
        <NavGroup label="Supply" items={supplyItems} pathname={pathname} />
        <NavGroup label="Demand" items={demandItems} pathname={pathname} />
        <NavGroup label="Binding" items={bindingItems} pathname={pathname} />
        <NavGroup label="Machinery" items={machineryItems} pathname={pathname} />
      </SidebarContent>

      <SidebarFooter className="p-2">
        <SidebarMenu>
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
