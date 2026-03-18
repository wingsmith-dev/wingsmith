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
  IconActivity,
  IconBinaryTree,
  IconBolt,
  IconBox,
  IconCpu,
  IconFolders,
  IconGauge,
  IconLogout,
  IconPackages,
  IconServer,
  IconLayoutGrid,
  IconServer2,
  IconShieldLock,
  IconSortDescending,
  IconStack2,
  IconTimelineEvent,
  IconTopologyStar3,
} from "@tabler/icons-react"
import { Link, useRouterState } from "@tanstack/react-router"
import { useAuthStore } from "@/modules/auth/store"

type NavItem = { label: string; to: string; icon: React.ElementType }

const supplyItems: NavItem[] = [
  { label: "Clusters", to: "/clusters", icon: IconServer2 },
  { label: "Nodes", to: "/nodes", icon: IconServer },
  { label: "Devices", to: "/devices", icon: IconCpu },
  { label: "Pools", to: "/pools", icon: IconStack2 },
]

const demandItems: NavItem[] = [
  { label: "Artifacts", to: "/artifacts", icon: IconPackages },
  { label: "Recipes", to: "/recipes", icon: IconBinaryTree },
  { label: "Deployments", to: "/deployments", icon: IconBox },
  { label: "Endpoints", to: "/endpoints", icon: IconBolt },
]

const bindingItems: NavItem[] = [
  { label: "Projects", to: "/projects", icon: IconFolders },
  { label: "Quotas", to: "/quotas", icon: IconGauge },
  { label: "Priority classes", to: "/priority-classes", icon: IconSortDescending },
  { label: "Policies", to: "/policies", icon: IconShieldLock },
]

const machineryItems: NavItem[] = [
  { label: "Operations", to: "/operations", icon: IconActivity },
  { label: "Allocations", to: "/allocations", icon: IconLayoutGrid },
  { label: "Events", to: "/events", icon: IconTimelineEvent },
]

const activeClass =
  "data-[active=true]:bg-primary/10 data-[active=true]:text-primary data-[active=true]:font-semibold"

function NavGroup({
  label,
  items,
  pathname,
}: {
  label: string
  items: NavItem[]
  pathname: string
}) {
  return (
    <SidebarGroup>
      <SidebarGroupLabel className="font-mono text-2xs font-semibold uppercase tracking-[0.15em] text-muted-foreground/50">
        {label}
      </SidebarGroupLabel>
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
                <item.icon />
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
