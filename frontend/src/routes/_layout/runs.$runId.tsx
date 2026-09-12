import { useSuspenseQuery } from "@tanstack/react-query"
import { createFileRoute, Link } from "@tanstack/react-router"
import { ArrowLeft } from "lucide-react"

import { RunsService } from "@/client"
import RunStatusBadge from "@/components/Runs/RunStatusBadge"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"

export const Route = createFileRoute("/_layout/runs/$runId")({
  component: RunDetail,
  head: () => ({ meta: [{ title: "Run - AgentHub" }] }),
})

function displayContent(value: unknown) {
  if (value == null) return "No content"
  return typeof value === "string" ? value : JSON.stringify(value, null, 2)
}

function RunDetail() {
  const { runId } = Route.useParams()
  const { data: run } = useSuspenseQuery({
    queryKey: ["runs", runId],
    queryFn: async () =>
      (await RunsService.readRun({ path: { id: runId } })).data,
  })
  const { data: events } = useSuspenseQuery({
    queryKey: ["runs", runId, "events"],
    queryFn: async () =>
      (await RunsService.readEvents({ path: { id: runId } })).data,
  })
  const startedAt = new Date(run.started_at ?? run.created_at).getTime()
  return (
    <div className="flex flex-col gap-6">
      <div>
        <Button variant="ghost" asChild className="mb-2 -ml-3">
          <Link to="/runs">
            <ArrowLeft />
            Back to runs
          </Link>
        </Button>
        <h1 className="text-2xl font-bold tracking-tight">Run details</h1>
        <p className="text-muted-foreground">
          {run.agent_name ?? "Agent"} · v{run.agent_version_number ?? "?"}
        </p>
        <p className="mt-1 break-all font-mono text-xs text-muted-foreground">
          {run.id}
        </p>
      </div>
      <Card>
        <CardHeader>
          <CardTitle>Overview</CardTitle>
        </CardHeader>
        <CardContent className="space-y-4">
          <dl className="grid gap-4 sm:grid-cols-2 lg:grid-cols-5">
            <div>
              <dt className="mb-1 text-sm text-muted-foreground">Status</dt>
              <dd>
                <RunStatusBadge status={run.status} />
              </dd>
            </div>
            <div>
              <dt className="text-sm text-muted-foreground">Trigger</dt>
              <dd>{run.trigger}</dd>
            </div>
            <div>
              <dt className="text-sm text-muted-foreground">Duration</dt>
              <dd>{run.duration_ms == null ? "—" : `${run.duration_ms} ms`}</dd>
            </div>
            <div>
              <dt className="text-sm text-muted-foreground">Tokens</dt>
              <dd>
                {run.prompt_tokens + run.completion_tokens}
                <span className="block text-xs text-muted-foreground">
                  {run.prompt_tokens} input / {run.completion_tokens} output
                </span>
              </dd>
            </div>
            <div>
              <dt className="text-sm text-muted-foreground">
                Estimated cost (USD)
              </dt>
              <dd>
                {run.cost_usd == null
                  ? "Unknown"
                  : `$${Number(run.cost_usd).toFixed(6)}`}
              </dd>
            </div>
          </dl>
          {run.error && (
            <Alert variant="destructive">
              <AlertTitle>Execution failed</AlertTitle>
              <AlertDescription className="whitespace-pre-wrap break-all">
                {run.error}
              </AlertDescription>
            </Alert>
          )}
        </CardContent>
      </Card>
      <div className="grid gap-6 lg:grid-cols-2">
        {[
          ["Input", run.input.message],
          ["Output", run.output?.content],
        ].map(([title, value]) => (
          <Card key={String(title)} className="min-w-0">
            <CardHeader>
              <CardTitle>{String(title)}</CardTitle>
            </CardHeader>
            <CardContent>
              <pre className="whitespace-pre-wrap break-words text-sm">
                {displayContent(value)}
              </pre>
            </CardContent>
          </Card>
        ))}
      </div>
      <Card>
        <CardHeader>
          <CardTitle>Execution trace</CardTitle>
        </CardHeader>
        <CardContent>
          <ol className="space-y-3">
            {events.data.map((event) => (
              <li key={event.id}>
                <details className="rounded-md border p-3">
                  <summary className="flex cursor-pointer flex-wrap items-center gap-3 text-sm">
                    <span className="font-mono text-xs text-muted-foreground">
                      #{event.seq}
                    </span>
                    <span className="font-medium">{event.event_type}</span>
                    {event.node_name && <span>{event.node_name}</span>}
                    <span className="ml-auto font-mono text-xs text-muted-foreground">
                      +
                      {Math.max(
                        0,
                        new Date(event.created_at).getTime() - startedAt,
                      )}{" "}
                      ms
                    </span>
                  </summary>
                  <pre className="mt-3 max-h-96 overflow-auto rounded-md bg-muted p-3 text-xs">
                    {JSON.stringify(event.payload, null, 2)}
                  </pre>
                </details>
              </li>
            ))}
          </ol>
          {events.count === 0 && (
            <p className="text-muted-foreground">No events recorded.</p>
          )}
        </CardContent>
      </Card>
    </div>
  )
}
