import { useSuspenseQuery } from "@tanstack/react-query"
import { createFileRoute } from "@tanstack/react-router"
import { Suspense } from "react"

import { ToolsService } from "@/client"
import { DataTable } from "@/components/Common/DataTable"
import { columns } from "@/components/Tools/columns"
import ToolForm from "@/components/Tools/ToolForm"
import { Skeleton } from "@/components/ui/skeleton"

export const Route = createFileRoute("/_layout/tools")({
  component: Tools,
  head: () => ({ meta: [{ title: "Tools - AgentHub" }] }),
})
function ToolsTable() {
  const { data } = useSuspenseQuery({
    queryKey: ["tools"],
    queryFn: async () =>
      (await ToolsService.readTools({ query: { skip: 0, limit: 100 } })).data,
  })
  return data.count ? (
    <DataTable columns={columns} data={data.data} />
  ) : (
    <p className="py-12 text-center text-muted-foreground">
      No tools yet. Add a built-in function, HTTP endpoint or MCP tool.
    </p>
  )
}
function Tools() {
  return (
    <div className="flex flex-col gap-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold tracking-tight">Tools</h1>
          <p className="text-muted-foreground">
            Configure, test and manage the tools your agents can use.
          </p>
        </div>
        <ToolForm />
      </div>
      <Suspense fallback={<Skeleton className="h-64 w-full" />}>
        <ToolsTable />
      </Suspense>
    </div>
  )
}
