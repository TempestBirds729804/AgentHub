import { useSuspenseQuery } from "@tanstack/react-query"
import { createFileRoute } from "@tanstack/react-router"
import { Suspense } from "react"

import ApprovalGroups, {
  approvalsQuery,
} from "@/components/Approvals/ApprovalGroups"
import { Skeleton } from "@/components/ui/skeleton"

export const Route = createFileRoute("/_layout/approvals")({
  component: Approvals,
  head: () => ({ meta: [{ title: "Approvals - AgentHub" }] }),
})

function Requests() {
  const { data } = useSuspenseQuery(approvalsQuery())
  return data.length ? (
    <ApprovalGroups requests={data} />
  ) : (
    <p className="text-muted-foreground">No pending approvals.</p>
  )
}

function Approvals() {
  return (
    <div className="flex flex-col gap-6">
      <div>
        <h1 className="text-2xl font-bold tracking-tight">Approvals</h1>
        <p className="text-muted-foreground">
          Review tool calls before agents continue.
        </p>
      </div>
      <Suspense fallback={<Skeleton className="h-64" />}>
        <Requests />
      </Suspense>
    </div>
  )
}
