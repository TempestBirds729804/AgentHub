import { useQuery } from "@tanstack/react-query"
import {
  Activity,
  BookOpen,
  Bot,
  Home,
  MessageSquare,
  ShieldCheck,
  Users,
  Wrench,
} from "lucide-react"
import { ApprovalsService } from "@/client"

import { SidebarAppearance } from "@/components/Common/Appearance"
import { Logo } from "@/components/Common/Logo"
import {
  Sidebar,
  SidebarContent,
  SidebarFooter,
  SidebarHeader,
} from "@/components/ui/sidebar"
import useAuth from "@/hooks/useAuth"
import { type Item, Main } from "./Main"
import { User } from "./User"

const baseItems: Item[] = [
  { icon: Home, title: "Dashboard", path: "/" },
  { icon: Bot, title: "Agents", path: "/agents" },
  { icon: BookOpen, title: "Knowledge", path: "/knowledge" },
  { icon: Wrench, title: "Tools", path: "/tools" },
  { icon: MessageSquare, title: "Playground", path: "/playground" },
  { icon: Activity, title: "Runs", path: "/runs" },
]

export function AppSidebar() {
  const { user: currentUser } = useAuth()
  const { data: pendingCount } = useQuery({
    queryKey: ["approvals", "pending-count"],
    queryFn: async () =>
      (
        await ApprovalsService.readApprovals({
          query: { status: "pending", limit: 1 },
        })
      ).data.count,
    refetchInterval: 30000,
  })
  const navigation: Item[] = [
    ...baseItems,
    {
      icon: ShieldCheck,
      title: "Approvals",
      path: "/approvals",
      badge: pendingCount,
    },
  ]

  const items = currentUser?.is_superuser
    ? [...navigation, { icon: Users, title: "Admin", path: "/admin" }]
    : navigation

  return (
    <Sidebar collapsible="icon">
      <SidebarHeader className="px-4 py-6 group-data-[collapsible=icon]:px-0 group-data-[collapsible=icon]:items-center">
        <Logo variant="responsive" />
      </SidebarHeader>
      <SidebarContent>
        <Main items={items} />
      </SidebarContent>
      <SidebarFooter>
        <SidebarAppearance />
        <User user={currentUser} />
      </SidebarFooter>
    </Sidebar>
  )
}

export default AppSidebar
