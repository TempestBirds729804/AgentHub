import {
  useMutation,
  useQueryClient,
  useSuspenseQuery,
} from "@tanstack/react-query"
import { createFileRoute, Link } from "@tanstack/react-router"
import { ArrowLeft, ShieldAlert, ShieldCheck, ShieldX } from "lucide-react"
import { useEffect, useState } from "react"

import { type RunEventPublic, RunsService } from "@/client"
import RunStatusBadge from "@/components/Runs/RunStatusBadge"
import ToolTrace from "@/components/Tools/ToolTrace"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { LoadingButton } from "@/components/ui/loading-button"
import useCustomToast from "@/hooks/useCustomToast"
import { streamSSE } from "@/lib/sse"
import { handleError } from "@/utils"

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
  const queryClient = useQueryClient()
  const { showErrorToast } = useCustomToast()
  const [liveEvents, setLiveEvents] = useState<RunEventPublic[]>([])
  const [liveText, setLiveText] = useState("")
  const [streamError, setStreamError] = useState("")
  const { data: run } = useSuspenseQuery({
    queryKey: ["runs", runId],
    queryFn: async () =>
      (await RunsService.readRun({ path: { id: runId } })).data,
    refetchInterval: (query) =>
      query.state.data?.status === "queued" ||
      query.state.data?.status === "running" ||
      query.state.data?.status === "waiting_approval"
        ? 3000
        : false,
  })
  const { data: events } = useSuspenseQuery({
    queryKey: ["runs", runId, "events"],
    queryFn: async () => {
      const data: RunEventPublic[] = []
      let count = 0
      do {
        const page = (
          await RunsService.readEvents({
            path: { id: runId },
            query: { skip: data.length, limit: 100 },
          })
        ).data
        count = page.count
        data.push(...page.data)
        if (!page.data.length) break
      } while (data.length < count)
      return { data, count }
    },
  })
  const active = run.status === "queued" || run.status === "running"
  const asyncRun = run.thread_id?.startsWith(`run-${runId}-`) ?? false
  const action = useMutation({
    mutationFn: (operation: "cancel" | "retry" | "fresh") =>
      operation === "cancel"
        ? RunsService.cancelRun({ path: { id: runId } })
        : RunsService.retryRun({
            path: { id: runId },
            query: { fresh: operation === "fresh" },
          }),
    onSuccess: (response) => {
      queryClient.setQueryData(["runs", runId], response.data)
    },
    onError: handleError.bind(showErrorToast),
    onSettled: () => queryClient.invalidateQueries({ queryKey: ["runs"] }),
  })
  const attempt = run.retry_count ?? 0
  useEffect(() => {
    if (!active || !asyncRun) {
      queryClient.invalidateQueries({ queryKey: ["runs", runId, "events"] })
      return
    }
    const controller = new AbortController()
    setLiveEvents([])
    setLiveText("")
    setStreamError("")
    async function follow() {
      let seq = 0
      try {
        for await (const event of streamSSE(
          `${import.meta.env.VITE_API_URL ?? ""}/api/v1/runs/${runId}/stream`,
          { method: "GET", signal: controller.signal },
        )) {
          const payload = event.data as Record<string, unknown>
          if (event.event === "model_chunk") {
            setLiveText((previous) => previous + String(payload.text ?? ""))
          } else {
            if (event.event === "node_started" && payload.node === "call_model")
              setLiveText("")
            const current: RunEventPublic = {
              id: `${runId}-${attempt}-${seq}`,
              run_id: runId,
              seq: seq++,
              event_type: event.event as RunEventPublic["event_type"],
              node_name: typeof payload.node === "string" ? payload.node : null,
              payload,
              created_at: new Date().toISOString(),
            }
            setLiveEvents((previous) => [...previous, current])
          }
          if (event.event === "run_finished" || event.event === "run_failed")
            break
        }
      } catch (error) {
        if (!controller.signal.aborted)
          setStreamError(
            error instanceof Error ? error.message : "Live updates unavailable",
          )
      } finally {
        if (!controller.signal.aborted)
          queryClient.invalidateQueries({ queryKey: ["runs", runId] })
      }
    }
    void follow()
    return () => controller.abort()
  }, [active, asyncRun, runId, attempt, queryClient])
  const trace = active && liveEvents.length ? liveEvents : events.data
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
          {run.status === "waiting_approval" && (
            <Alert className="border-amber-500">
              <ShieldAlert />
              <AlertTitle>Waiting for approval</AlertTitle>
              <AlertDescription>
                Review the pending tool calls to continue.
                <Button variant="link" asChild>
                  <Link to="/approvals">Review approvals</Link>
                </Button>
              </AlertDescription>
            </Alert>
          )}
          {(active || run.status === "waiting_approval") && asyncRun && (
            <div className="space-y-2">
              <LoadingButton
                loading={action.isPending}
                onClick={() => action.mutate("cancel")}
              >
                {action.isPending ? "Cancelling…" : "Cancel"}
              </LoadingButton>
              <p className="text-sm text-muted-foreground">
                Cancellation takes effect at the next node boundary.
              </p>
            </div>
          )}
          {(run.status === "failed" || run.status === "cancelled") && (
            <div
              className="flex flex-wrap gap-2"
              title={attempt >= 3 ? "Retry limit of 3 reached" : undefined}
            >
              {run.checkpoint_id && (
                <LoadingButton
                  disabled={attempt >= 3 || action.isPending}
                  loading={action.isPending && action.variables === "retry"}
                  onClick={() => action.mutate("retry")}
                >
                  Retry from checkpoint
                </LoadingButton>
              )}
              <LoadingButton
                variant="outline"
                disabled={attempt >= 3 || action.isPending}
                loading={action.isPending && action.variables === "fresh"}
                onClick={() => action.mutate("fresh")}
              >
                Run again from scratch
              </LoadingButton>
              {attempt >= 3 && (
                <p className="text-sm text-muted-foreground">
                  Retry limit of 3 reached.
                </p>
              )}
            </div>
          )}
          {streamError && (
            <Alert>
              <AlertDescription>
                {streamError}. Status continues to refresh.
              </AlertDescription>
            </Alert>
          )}
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
          {run.output?.truncated_by_max_iterations === true && (
            <Alert>
              <AlertTitle>Iteration limit reached</AlertTitle>
              <AlertDescription>
                Some tool calls were not executed because the maximum iteration
                count was reached.
              </AlertDescription>
            </Alert>
          )}
        </CardContent>
      </Card>
      <div className="grid gap-6 lg:grid-cols-2">
        {[
          ["Input", run.input.message],
          ["Output", active && liveText ? liveText : run.output?.content],
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
          <ToolTrace
            events={trace.map((event) => ({
              event: event.event_type,
              data: event.payload,
            }))}
            running={active}
          />
          <ol className="space-y-3">
            {trace.map((event) => (
              <li key={event.id}>
                <details className="rounded-md border p-3">
                  <summary className="flex cursor-pointer flex-wrap items-center gap-3 text-sm">
                    <span className="font-mono text-xs text-muted-foreground">
                      #{event.seq}
                    </span>
                    <span className="font-medium">{event.event_type}</span>
                    {event.event_type === "approval_requested" && (
                      <ShieldAlert className="size-4 text-amber-600" />
                    )}
                    {event.event_type === "approval_resolved" &&
                      (event.payload.approved ? (
                        <ShieldCheck className="size-4 text-green-600" />
                      ) : (
                        <ShieldX className="size-4 text-red-600" />
                      ))}
                    {event.node_name && <span>{event.node_name}</span>}
                    <span className="ml-auto font-mono text-xs text-muted-foreground">
                      {active && liveEvents.length
                        ? "Live"
                        : `+${Math.max(0, new Date(event.created_at).getTime() - startedAt)} ms`}
                    </span>
                  </summary>
                  <pre className="mt-3 max-h-96 overflow-auto rounded-md bg-muted p-3 text-xs">
                    {JSON.stringify(event.payload, null, 2)}
                  </pre>
                </details>
              </li>
            ))}
          </ol>
          {trace.length === 0 && (
            <p className="text-muted-foreground">No events recorded.</p>
          )}
        </CardContent>
      </Card>
    </div>
  )
}
