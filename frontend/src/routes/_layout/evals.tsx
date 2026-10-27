import { useSuspenseQuery } from "@tanstack/react-query"
import {
  createFileRoute,
  Link,
  Outlet,
  useRouterState,
} from "@tanstack/react-router"
import { Suspense, useState } from "react"
import { EvalsService } from "@/client"
import NewEvaluation from "@/components/Evals/NewEvaluation"
import { money, rate } from "@/components/Evals/Summary"
import { Button } from "@/components/ui/button"
import { Skeleton } from "@/components/ui/skeleton"

export const Route = createFileRoute("/_layout/evals")({
  component: Evaluations,
  head: () => ({ meta: [{ title: "Evaluations - AgentHub" }] }),
})
function List() {
  const [page, setPage] = useState(0)
  const [selected, setSelected] = useState<string[]>([])
  const { data } = useSuspenseQuery({
    queryKey: ["evals", page],
    queryFn: async () =>
      (
        await EvalsService.readEvalRuns({
          query: { skip: page * 20, limit: 20 },
        })
      ).data,
    refetchInterval: 3000,
  })
  return (
    <>
      <div className="flex gap-3">
        <NewEvaluation />
        {selected.length === 2 && (
          <Button asChild variant="outline">
            <Link
              to="/evals/compare"
              search={{ left: selected[0], right: selected[1] }}
            >
              Compare
            </Link>
          </Button>
        )}
      </div>
      <div className="overflow-auto">
        <table className="w-full text-left text-sm">
          <thead>
            <tr>
              {[
                "Select",
                "Agent / version",
                "Note",
                "Status / progress",
                "Pass rate",
                "Avg latency",
                "Cost",
                "Created",
              ].map((h) => (
                <th className="p-3" key={h}>
                  {h}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {data.data.map((b) => (
              <tr key={b.id} className="border-t">
                <td className="p-3">
                  <input
                    aria-label={`Compare ${b.note || b.id}`}
                    type="checkbox"
                    checked={selected.includes(b.id)}
                    disabled={selected.length === 2 && !selected.includes(b.id)}
                    onChange={(e) =>
                      setSelected(
                        e.target.checked
                          ? [...selected, b.id]
                          : selected.filter((id) => id !== b.id),
                      )
                    }
                  />
                </td>
                <td className="p-3">
                  <Link
                    className="underline"
                    to="/evals/$evalRunId"
                    params={{ evalRunId: b.id }}
                  >
                    {b.agent_name} v{b.agent_version_number}
                  </Link>
                </td>
                <td>{b.note}</td>
                <td>
                  {b.status} {b.completed_count}/{b.case_count}
                  <progress
                    aria-label="Evaluation progress"
                    value={b.completed_count}
                    max={b.case_count}
                  />
                </td>
                <td>{rate(b.pass_rate)}</td>
                <td>{b.avg_duration_ms ?? "—"} ms</td>
                <td>{money(b.total_cost_usd)}</td>
                <td>
                  {b.created_at ? new Date(b.created_at).toLocaleString() : "—"}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {!data.count && (
        <p>No evaluations yet. Add test cases on an Agent's Test Cases tab.</p>
      )}
      <div className="flex gap-3">
        <Button
          variant="outline"
          disabled={!page}
          onClick={() => setPage((p) => p - 1)}
        >
          Previous
        </Button>
        <span>{data.count} evaluations</span>
        <Button
          variant="outline"
          disabled={(page + 1) * 20 >= data.count}
          onClick={() => setPage((p) => p + 1)}
        >
          Next
        </Button>
      </div>
    </>
  )
}
function Evaluations() {
  const path = useRouterState({ select: (s) => s.location.pathname })
  if (path.startsWith("/evals/")) return <Outlet />
  return (
    <div className="space-y-6">
      <h1 className="text-2xl font-bold">Evaluations</h1>
      <p>
        Compare quality, latency and estimated cost across versions. Costs
        exclude LLM judge calls.
      </p>
      <Suspense fallback={<Skeleton className="h-48 w-full" />}>
        <List />
      </Suspense>
    </div>
  )
}
