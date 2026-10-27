import type { EvalRunPublic } from "@/client"

export const money = (value: string | null | undefined) =>
  value == null ? "Unknown" : `$${Number(value).toFixed(6)}`
export const rate = (value: number | null | undefined) =>
  value == null ? "—" : `${(value * 100).toFixed(1)}%`
export function Summary({ batch }: { batch: EvalRunPublic }) {
  return (
    <div className="grid gap-3 rounded border p-4 sm:grid-cols-3">
      <strong>
        {batch.agent_name} · v{batch.agent_version_number}
      </strong>
      <span>
        {batch.status} · {batch.completed_count}/{batch.case_count}
        <progress
          aria-label="Evaluation progress"
          className="block w-full"
          max={batch.case_count || 1}
          value={batch.completed_count}
        />
      </span>
      <span>Pass rate: {rate(batch.pass_rate)}</span>
      <span>
        Avg / p95: {batch.avg_duration_ms ?? "—"} /{" "}
        {batch.p95_duration_ms ?? "—"} ms
      </span>
      <span>Cost: {money(batch.total_cost_usd)}</span>
      <span>
        Tokens: {batch.total_prompt_tokens + batch.total_completion_tokens}
      </span>
      {batch.error && (
        <p role="alert" className="text-destructive">
          {batch.error}
        </p>
      )}
    </div>
  )
}
