import {
  useMutation,
  useQueryClient,
  useSuspenseQuery,
} from "@tanstack/react-query"
import {
  createFileRoute,
  Link,
  Outlet,
  useRouterState,
} from "@tanstack/react-router"
import { Suspense } from "react"
import { KnowledgeService } from "@/client"
import KnowledgeForm from "@/components/Knowledge/KnowledgeForm"
import { Button } from "@/components/ui/button"
import { Skeleton } from "@/components/ui/skeleton"
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table"
import useCustomToast from "@/hooks/useCustomToast"
import { handleError } from "@/utils"

export const Route = createFileRoute("/_layout/knowledge")({
  component: Knowledge,
  head: () => ({ meta: [{ title: "Knowledge - AgentHub" }] }),
})

function Knowledge() {
  const path = useRouterState({ select: (state) => state.location.pathname })
  if (path.startsWith("/knowledge/")) return <Outlet />
  return (
    <div className="space-y-6">
      <div className="flex justify-between items-center">
        <div>
          <h1 className="text-2xl font-bold">Knowledge</h1>
          <p className="text-muted-foreground">
            Private documents for grounded answers.
          </p>
        </div>
        <KnowledgeForm />
      </div>
      <Suspense fallback={<Skeleton className="h-60" />}>
        <KnowledgeTable />
      </Suspense>
    </div>
  )
}

function KnowledgeTable() {
  const cache = useQueryClient()
  const { showErrorToast, showSuccessToast } = useCustomToast()
  const { data } = useSuspenseQuery({
    queryKey: ["knowledge"],
    queryFn: async () => (await KnowledgeService.readKnowledgeBases()).data,
  })
  const remove = useMutation({
    mutationFn: (id: string) =>
      KnowledgeService.deleteKnowledge({ path: { id } }),
    onSuccess: () => showSuccessToast("Knowledge base deleted"),
    onError: handleError.bind(showErrorToast),
    onSettled: () => cache.invalidateQueries({ queryKey: ["knowledge"] }),
  })
  return (
    <Table>
      <TableHeader>
        <TableRow>
          {[
            "Name",
            "Description",
            "Documents",
            "Ready",
            "Created",
            "Actions",
          ].map((label) => (
            <TableHead key={label}>{label}</TableHead>
          ))}
        </TableRow>
      </TableHeader>
      <TableBody>
        {data.data.map((kb) => (
          <TableRow key={kb.id}>
            <TableCell>
              <Link
                className="font-medium underline"
                to="/knowledge/$kbId"
                params={{ kbId: kb.id }}
              >
                {kb.name}
              </Link>
            </TableCell>
            <TableCell>{kb.description}</TableCell>
            <TableCell>{kb.document_count}</TableCell>
            <TableCell>{kb.ready_document_count}</TableCell>
            <TableCell>{new Date(kb.created_at).toLocaleString()}</TableCell>
            <TableCell>
              <Button
                variant="destructive"
                size="sm"
                disabled={remove.isPending}
                onClick={() => {
                  if (
                    window.confirm(`Delete ${kb.name} and all its documents?`)
                  )
                    remove.mutate(kb.id)
                }}
              >
                Delete
              </Button>
            </TableCell>
          </TableRow>
        ))}
        {!data.count && (
          <TableRow>
            <TableCell colSpan={6}>
              Create a knowledge base to upload documents.
            </TableCell>
          </TableRow>
        )}
      </TableBody>
    </Table>
  )
}
