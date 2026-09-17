import { useSuspenseQuery } from "@tanstack/react-query"
import { createFileRoute, Outlet, useRouterState } from "@tanstack/react-router"
import { Activity } from "lucide-react"
import { Suspense } from "react"

import { RunsService } from "@/client"
import { DataTable } from "@/components/Common/DataTable"
import PendingRuns from "@/components/Pending/PendingRuns"
import { columns } from "@/components/Runs/columns"

export const Route = createFileRoute("/_layout/runs")({
  component: Runs,
  head: () => ({ meta: [{ title: "Runs - AgentHub" }] }),
})

function RunsTable() {
  const { data: runs } = useSuspenseQuery({
    queryKey: ["runs"],
    queryFn: async () =>
      (await RunsService.readRuns({ query: { skip: 0, limit: 100 } })).data,
    refetchInterval: (query) =>
      query.state.data?.data.some(
        (run) => run.status === "queued" || run.status === "running",
      )
        ? 3000
        : false,
  })
  if (!runs.data.length)
    return (
      <div className="flex flex-col items-center gap-3 py-12 text-center">
        <Activity className="h-8 w-8 text-muted-foreground" />
        <h3 className="text-lg font-semibold">No runs yet</h3>
        <p className="text-muted-foreground">
          Open an agent's published version and select Run to get started.
        </p>
      </div>
    )
  return <DataTable columns={columns} data={runs.data} />
}

function Runs() {
  const pathname = useRouterState({
    select: (state) => state.location.pathname,
  })
  if (pathname.startsWith("/runs/")) return <Outlet />
  return (
    <div className="flex flex-col gap-6">
      <div>
        <h1 className="text-2xl font-bold tracking-tight">Runs</h1>
        <p className="text-muted-foreground">
          Inspect recent executions, usage and results
        </p>
      </div>
      <Suspense fallback={<PendingRuns />}>
        <RunsTable />
      </Suspense>
    </div>
  )
}
