import {
  useMutation,
  useQueryClient,
  useSuspenseQuery,
} from "@tanstack/react-query"
import { createFileRoute, Link } from "@tanstack/react-router"
import { Suspense } from "react"
import { EvalsService } from "@/client"
import { money, Summary } from "@/components/Evals/Summary"
import { Button } from "@/components/ui/button"
import { Skeleton } from "@/components/ui/skeleton"
import useCustomToast from "@/hooks/useCustomToast"
import { handleError } from "@/utils"

export const Route = createFileRoute("/_layout/evals/$evalRunId")({
  component: Detail,
})
function Content() {
  const { evalRunId: id } = Route.useParams()
  const queryClient = useQueryClient()
  const { showErrorToast, showSuccessToast } = useCustomToast()
  const { data: batch } = useSuspenseQuery({
    queryKey: ["evals", id],
    queryFn: async () =>
      (await EvalsService.readEvalRun({ path: { id } })).data,
    refetchInterval: 3000,
  })
  const { data: results } = useSuspenseQuery({
    queryKey: ["eval-results", id],
    queryFn: async () =>
      (await EvalsService.readResults({ path: { id } })).data,
    refetchInterval: 3000,
  })
  const cancel = useMutation({
    mutationFn: () => EvalsService.cancelEvalRun({ path: { id } }),
    onError: handleError.bind(showErrorToast),
    onSuccess: () => showSuccessToast("Evaluation cancelled"),
    onSettled: () => queryClient.invalidateQueries({ queryKey: ["evals"] }),
  })
  return (
    <div className="space-y-4">
      <Link to="/evals" className="underline">
        Back to evaluations
      </Link>
      <h1 className="text-2xl font-bold">Evaluation details</h1>
      <Summary batch={batch} />
      <p className="text-sm text-muted-foreground">
        Costs exclude LLM judge calls. Unknown prices are not counted as zero.
      </p>
      {["running", "queued"].includes(batch.status) && (
        <Button disabled={cancel.isPending} onClick={() => cancel.mutate()}>
          Cancel evaluation
        </Button>
      )}
      {results.data.map((r) => (
        <details
          key={r.id}
          className={`rounded border p-4 ${r.passed ? "" : "border-destructive"}`}
        >
          <summary className="cursor-pointer">
            {r.passed ? "✓" : "✗"} {r.case_name} · Score {r.score ?? "—"} ·{" "}
            {r.duration_ms ?? "—"} ms · {money(r.cost_usd)}
          </summary>
          <div className="mt-3 space-y-3">
            {r.error && (
              <p role="alert" className="text-destructive">
                {r.error}
              </p>
            )}
            <pre className="whitespace-pre-wrap break-words">
              {r.actual_output}
            </pre>
            {r.assertion_results.map((a, i) => (
              <pre
                key={`${String(a.type)}-${i}`}
                className={`overflow-auto rounded p-3 text-xs ${a.passed ? "bg-muted" : "bg-destructive/10 text-destructive"}`}
              >
                {JSON.stringify(a, null, 2)}
              </pre>
            ))}
            {r.run_id && (
              <Link
                className="underline"
                to="/runs/$runId"
                params={{ runId: r.run_id }}
              >
                View run trace
              </Link>
            )}
          </div>
        </details>
      ))}
    </div>
  )
}
function Detail() {
  return (
    <Suspense fallback={<Skeleton className="h-48 w-full" />}>
      <Content />
    </Suspense>
  )
}
