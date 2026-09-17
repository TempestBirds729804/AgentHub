import { Loader2, Wrench } from "lucide-react"

export type TraceEvent = { event: string; data: unknown }
type Call = {
  index: number
  tool: string
  args: unknown
  result?: string
  ok?: boolean
  error?: string | null
  duration_ms?: number
}

export default function ToolTrace({
  events,
  running = false,
}: {
  events: TraceEvent[]
  running?: boolean
}) {
  const calls: Call[] = []
  for (const event of events) {
    const payload = event.data as Partial<Call>
    if (event.event === "tool_called")
      calls.push({
        index: payload.index ?? calls.length,
        tool: payload.tool ?? "Tool",
        args: payload.args,
      })
    if (event.event === "tool_result") {
      const call = calls.find((c) => c.index === payload.index)
      if (call) Object.assign(call, payload)
    }
  }
  return (
    <div className="space-y-2">
      {calls.map((call) => (
        <div
          key={call.index}
          data-testid="tool-call"
          className="rounded-lg border bg-muted/30 p-3 text-sm"
        >
          <div className="flex items-center gap-2 font-medium">
            <Wrench className="size-4" />
            {call.tool}
            {call.ok == null && running && (
              <Loader2 className="size-3 animate-spin" />
            )}
            <span className="ml-auto text-xs font-normal text-muted-foreground">
              {call.duration_ms != null
                ? `${call.duration_ms} ms`
                : running
                  ? "Running…"
                  : "No result recorded"}
            </span>
          </div>
          <pre className="mt-2 max-h-32 overflow-auto whitespace-pre-wrap break-all text-xs">
            Arguments: {JSON.stringify(call.args)}
          </pre>
          {call.result != null && (
            <pre
              className={`mt-2 max-h-64 overflow-auto whitespace-pre-wrap break-all ${call.ok === false ? "text-destructive" : ""}`}
            >
              {call.ok === false ? call.error || call.result : call.result}
            </pre>
          )}
        </div>
      ))}
    </div>
  )
}
