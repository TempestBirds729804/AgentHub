import { useSuspenseQuery } from "@tanstack/react-query"
import { createFileRoute, Link } from "@tanstack/react-router"
import { Suspense, useState } from "react"
import { EvalsService } from "@/client"
import { Summary } from "@/components/Evals/Summary"
import { Skeleton } from "@/components/ui/skeleton"

export const Route = createFileRoute("/_layout/evals/compare")({
  component: Compare,
  validateSearch: (s: Record<string, unknown>) => ({
    left: typeof s.left === "string" ? s.left : "",
    right: typeof s.right === "string" ? s.right : "",
  }),
})
function Content({ left, right }: { left: string; right: string }) {
  const [only, setOnly] = useState(true)
  const { data } = useSuspenseQuery({
    queryKey: ["eval-comparison", left, right],
    queryFn: async () =>
      (
        await EvalsService.compareEvalRuns({
          query: { left_id: left, right_id: right },
        })
      ).data,
  })
  const deltas = [
    [
      "Pass rate",
      data.left.pass_rate == null || data.right.pass_rate == null
        ? null
        : (data.right.pass_rate - data.left.pass_rate) * 100,
      "pp",
      true,
    ],
    [
      "Average latency",
      data.left.avg_duration_ms == null || data.right.avg_duration_ms == null
        ? null
        : data.right.avg_duration_ms - data.left.avg_duration_ms,
      "ms",
      false,
    ],
    [
      "Cost",
      data.left.total_cost_usd == null || data.right.total_cost_usd == null
        ? null
        : Number(data.right.total_cost_usd) - Number(data.left.total_cost_usd),
      "USD",
      false,
    ],
  ] as const
  return (
    <div className="space-y-4">
      <h1 className="text-2xl font-bold">Version comparison</h1>
      <div className="grid gap-3 lg:grid-cols-2">
        <Summary batch={data.left} />
        <Summary batch={data.right} />
      </div>
      <div className="flex flex-wrap gap-4">
        {deltas.map(([label, value, unit, higher]) => (
          <p
            key={label}
            className={
              value == null || value === 0
                ? ""
                : (higher ? value > 0 : value < 0)
                  ? "text-green-700"
                  : "text-destructive"
            }
          >
            {label}:{" "}
            {value == null
              ? "Unknown"
              : `${value > 0 ? "+" : ""}${value.toFixed(4)} ${unit}`}
          </p>
        ))}
      </div>
      <div className="flex flex-wrap gap-6">
        <strong className="text-destructive">
          Regressions: {data.regression_count}
        </strong>
        <strong className="text-green-700">
          Improvements: {data.improvement_count}
        </strong>
        <span>
          Unchanged:{" "}
          {data.items.length - data.regression_count - data.improvement_count}
        </span>
      </div>
      <label className="flex gap-2">
        <input
          type="checkbox"
          checked={only}
          onChange={(e) => setOnly(e.target.checked)}
        />
        Only regressions
      </label>
      {data.items
        .filter((i) => !only || i.verdict === "regression")
        .map((i) => (
          <details
            key={i.case_id}
            className={`rounded border p-4 ${i.verdict === "regression" ? "bg-destructive/10" : i.verdict === "improvement" ? "bg-green-500/10" : ""}`}
          >
            <summary className="cursor-pointer">
              {i.case_name} · {i.verdict} ·{" "}
              {i.left == null ? "Missing" : i.left.passed ? "Pass" : "Fail"} →{" "}
              {i.right == null ? "Missing" : i.right.passed ? "Pass" : "Fail"}
            </summary>
            <div className="mt-3 grid gap-4 lg:grid-cols-2">
              {([i.left, i.right] as const).map((r, index) => (
                <section key={index === 0 ? "left" : "right"}>
                  <h3>
                    {index === 0 ? "Before" : "After"} · Score {r?.score ?? "—"}
                  </h3>
                  <pre className="whitespace-pre-wrap break-words">
                    {r?.actual_output ?? "Missing result"}
                  </pre>
                  {r?.run_id && (
                    <Link
                      className="underline"
                      to="/runs/$runId"
                      params={{ runId: r.run_id }}
                    >
                      View run trace
                    </Link>
                  )}
                </section>
              ))}
            </div>
          </details>
        ))}
    </div>
  )
}
function Compare() {
  const { left, right } = Route.useSearch()
  return (
    <>
      <Link to="/evals" className="underline">
        Back to evaluations
      </Link>
      {left && right ? (
        <Suspense fallback={<Skeleton className="h-48 w-full" />}>
          <Content left={left} right={right} />
        </Suspense>
      ) : (
        <p>Select two evaluations from the list.</p>
      )}
    </>
  )
}
