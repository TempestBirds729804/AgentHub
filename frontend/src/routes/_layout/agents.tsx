import { useSuspenseQuery } from "@tanstack/react-query"
import { createFileRoute, Outlet, useRouterState } from "@tanstack/react-router"
import { Bot } from "lucide-react"
import { Suspense } from "react"

import { AgentsService } from "@/client"
import AddAgent from "@/components/Agents/AddAgent"
import { columns } from "@/components/Agents/columns"
import { DataTable } from "@/components/Common/DataTable"
import PendingAgents from "@/components/Pending/PendingAgents"

function getAgentsQueryOptions() {
  return {
    queryFn: async () =>
      (await AgentsService.readAgents({ query: { skip: 0, limit: 100 } })).data,
    queryKey: ["agents"],
  }
}

export const Route = createFileRoute("/_layout/agents")({
  component: Agents,
  head: () => ({ meta: [{ title: "Agents - AgentHub" }] }),
})

function AgentsTableContent() {
  const { data: agents } = useSuspenseQuery(getAgentsQueryOptions())
  if (agents.data.length === 0) {
    return (
      <div className="flex flex-col items-center justify-center text-center py-12">
        <div className="rounded-full bg-muted p-4 mb-4">
          <Bot className="h-8 w-8 text-muted-foreground" />
        </div>
        <h3 className="text-lg font-semibold">You don't have any agents yet</h3>
        <p className="text-muted-foreground">Add a new agent to get started</p>
      </div>
    )
  }
  return <DataTable columns={columns} data={agents.data} />
}

function Agents() {
  const pathname = useRouterState({
    select: (state) => state.location.pathname,
  })
  if (pathname.startsWith("/agents/")) {
    return <Outlet />
  }

  return (
    <div className="flex flex-col gap-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold tracking-tight">Agents</h1>
          <p className="text-muted-foreground">Create and manage your agents</p>
        </div>
        <AddAgent />
      </div>
      <Suspense fallback={<PendingAgents />}>
        <AgentsTableContent />
      </Suspense>
    </div>
  )
}
