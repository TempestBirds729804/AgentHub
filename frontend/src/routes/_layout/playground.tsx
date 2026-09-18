import { zodResolver } from "@hookform/resolvers/zod"
import {
  useMutation,
  useQuery,
  useQueryClient,
  useSuspenseQuery,
} from "@tanstack/react-query"
import { createFileRoute, Link } from "@tanstack/react-router"
import { Loader2, MessageSquare, Plus, Send, Square } from "lucide-react"
import {
  Fragment,
  Suspense,
  useEffect,
  useEffectEvent,
  useRef,
  useState,
} from "react"
import { useForm } from "react-hook-form"
import { z } from "zod"

import {
  AgentsService,
  ConversationsService,
  type ConvMessagePublic,
  RunsService,
} from "@/client"
import ApprovalGroups, {
  approvalsQuery,
} from "@/components/Approvals/ApprovalGroups"
import Citations, { type CitationChunk } from "@/components/Knowledge/Citations"
import ToolTrace from "@/components/Tools/ToolTrace"
import { Button } from "@/components/ui/button"
import {
  Form,
  FormControl,
  FormField,
  FormItem,
  FormMessage,
} from "@/components/ui/form"
import { LoadingButton } from "@/components/ui/loading-button"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import { Skeleton } from "@/components/ui/skeleton"
import { Textarea } from "@/components/ui/textarea"
import useCustomToast from "@/hooks/useCustomToast"
import { type SSEEvent, streamSSE } from "@/lib/sse"
import { handleError } from "@/utils"

export const Route = createFileRoute("/_layout/playground")({
  component: Playground,
  validateSearch: (
    search: Record<string, unknown>,
  ): { conversation?: string } => ({
    conversation:
      typeof search.conversation === "string" ? search.conversation : undefined,
  }),
  head: () => ({ meta: [{ title: "Playground - AgentHub" }] }),
})

const schema = z.object({
  message: z.string().trim().min(1, "Enter a message").max(20000),
})

function PendingPlayground() {
  return (
    <div className="grid gap-4 md:grid-cols-[240px_1fr]">
      <Skeleton className="h-[65vh]" />
      <Skeleton className="h-[65vh]" />
    </div>
  )
}

function Playground() {
  return (
    <div className="flex flex-col gap-6">
      <div>
        <h1 className="text-2xl font-bold tracking-tight">Playground</h1>
        <p className="text-muted-foreground">
          Test published agents with persistent conversations and live execution
          events.
        </p>
      </div>
      <Suspense fallback={<PendingPlayground />}>
        <Workspace />
      </Suspense>
    </div>
  )
}

function Workspace() {
  const { conversation: conversationId } = Route.useSearch()
  const navigate = Route.useNavigate()
  const queryClient = useQueryClient()
  const { showErrorToast } = useCustomToast()
  const [agentId, setAgentId] = useState("")
  const [isStreaming, setIsStreaming] = useState(false)
  const { data: agents } = useSuspenseQuery({
    queryKey: ["agents", "published"],
    queryFn: async () => {
      const list = (
        await AgentsService.readAgents({ query: { skip: 0, limit: 100 } })
      ).data.data
      const published = await Promise.all(
        list
          .filter((agent) => agent.latest_version_number)
          .map(async (agent) => {
            const versions = (
              await AgentsService.readVersions({ path: { id: agent.id } })
            ).data.data
            return versions.some((version) => !version.is_draft) ? agent : null
          }),
      )
      return published.filter((agent) => agent !== null)
    },
  })
  const { data: conversations } = useSuspenseQuery({
    queryKey: ["conversations"],
    queryFn: async () =>
      (
        await ConversationsService.readConversations({
          query: { skip: 0, limit: 100 },
        })
      ).data,
  })
  const chosenAgent = agentId || agents[0]?.id || ""
  const mutation = useMutation({
    mutationFn: () =>
      ConversationsService.createConversation({
        body: { agent_id: chosenAgent },
      }),
    onSuccess: ({ data }) => {
      void navigate({ search: { conversation: data.id } })
    },
    onError: handleError.bind(showErrorToast),
    onSettled: () =>
      queryClient.invalidateQueries({ queryKey: ["conversations"] }),
  })
  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-wrap gap-3">
        <Select
          value={chosenAgent}
          onValueChange={setAgentId}
          disabled={isStreaming || mutation.isPending}
        >
          <SelectTrigger
            className="w-72"
            aria-label="Agent"
            data-testid="playground-agent"
          >
            <SelectValue placeholder="Select a published agent" />
          </SelectTrigger>
          <SelectContent>
            {agents.map((agent) => (
              <SelectItem key={agent.id} value={agent.id}>
                {agent.name}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
        <LoadingButton
          onClick={() => mutation.mutate()}
          loading={mutation.isPending}
          disabled={!chosenAgent || isStreaming}
          data-testid="new-conversation"
        >
          <Plus />
          New conversation
        </LoadingButton>
      </div>
      {!agents.length && (
        <p className="text-sm text-muted-foreground">
          Publish a version on the Agents page to start a conversation.
        </p>
      )}
      <div className="grid min-h-[65vh] gap-4 md:grid-cols-[240px_minmax(0,1fr)]">
        <aside
          className="max-h-[70vh] space-y-1 overflow-y-auto rounded-xl border p-2"
          aria-label="Conversations"
        >
          <h2 className="px-3 py-2 text-sm font-semibold">Conversations</h2>
          {!conversations.data.length && (
            <p className="p-3 text-sm text-muted-foreground">
              No conversations yet.
            </p>
          )}
          {conversations.data.map((conv) => (
            <button
              type="button"
              key={conv.id}
              disabled={isStreaming}
              onClick={() => {
                setAgentId(conv.agent_id)
                void navigate({ search: { conversation: conv.id } })
              }}
              className={`w-full rounded-lg px-3 py-3 text-left hover:bg-muted disabled:opacity-50 ${conversationId === conv.id ? "bg-muted" : ""}`}
            >
              <span className="block truncate text-sm font-medium">
                {conv.title || "New conversation"}
              </span>
              <span className="text-xs text-muted-foreground">
                {conv.message_count ?? 0} messages
              </span>
            </button>
          ))}
        </aside>
        {conversationId ? (
          <Chat
            key={conversationId}
            id={conversationId}
            onStreaming={setIsStreaming}
          />
        ) : (
          <div className="flex flex-col items-center justify-center gap-3 rounded-xl border text-muted-foreground">
            <MessageSquare className="size-10" />
            <p>Select or create a conversation.</p>
          </div>
        )}
      </div>
    </div>
  )
}

async function readAllMessages(id: string) {
  const messages: ConvMessagePublic[] = []
  while (true) {
    const { data } = await ConversationsService.readMessages({
      path: { id },
      query: { skip: messages.length, limit: 100 },
    })
    messages.push(...data.data)
    if (messages.length >= data.count || !data.data.length) return messages
  }
}

function Chat({
  id,
  onStreaming,
}: {
  id: string
  onStreaming: (value: boolean) => void
}) {
  const queryClient = useQueryClient()
  const { showErrorToast } = useCustomToast()
  const form = useForm<z.infer<typeof schema>>({
    resolver: zodResolver(schema),
    mode: "onBlur",
    defaultValues: { message: "" },
  })
  const [streaming, setStreaming] = useState(false)
  const [optimisticUser, setOptimisticUser] = useState("")
  const [text, setText] = useState("")
  const [chunks, setChunks] = useState<CitationChunk[]>([])
  const [node, setNode] = useState<string | null>(null)
  const [events, setEvents] = useState<SSEEvent[]>([])
  const [runId, setRunId] = useState<string | null>(null)
  const [waitingApproval, setWaitingApproval] = useState(false)
  const lastEventId = useRef("0")
  const approvals = useQuery({
    ...approvalsQuery(runId ?? ""),
    enabled: !!runId,
  })
  const latestRun = useQuery({
    queryKey: ["runs", "conversation", id],
    queryFn: async () =>
      (await RunsService.readRuns({ query: { conversation_id: id, limit: 1 } }))
        .data.data[0] ?? null,
    refetchInterval: 3000,
    enabled: !streaming,
  })
  const [truncated, setTruncated] = useState(false)
  const [usage, setUsage] = useState<{
    prompt_tokens: number
    completion_tokens: number
    cost_usd: string | null
  } | null>(null)
  const abortRef = useRef<AbortController | null>(null)
  const messagesRef = useRef<HTMLDivElement>(null)
  const history = useQuery({
    queryKey: ["conversations", id, "messages"],
    queryFn: () => readAllMessages(id),
  })
  const detail = useQuery({
    queryKey: ["conversations", id],
    queryFn: async () =>
      (await ConversationsService.readConversation({ path: { id } })).data,
  })
  const recoverRun = useEffectEvent((run: typeof latestRun.data) => {
    if (!run || abortRef.current) return
    setRunId(run.id)
    if (run.status === "waiting_approval") {
      setRunId(run.id)
      setWaitingApproval(true)
    } else if (
      run.status === "running" &&
      run.thread_id?.startsWith(`run-${run.id}-`)
    ) {
      setRunId(run.id)
      void followRun(run.id)
    } else if (["succeeded", "failed", "cancelled"].includes(run.status)) {
      setWaitingApproval(false)
      void queryClient.invalidateQueries({
        queryKey: ["conversations", id, "messages"],
      })
      void queryClient.invalidateQueries({ queryKey: ["approvals"] })
    }
  })
  useEffect(() => {
    recoverRun(latestRun.data)
  }, [latestRun.data])
  useEffect(
    () => () => {
      abortRef.current?.abort()
      onStreaming(false)
    },
    [onStreaming],
  )
  useEffect(() => {
    if ((text || history.data?.length || optimisticUser) && messagesRef.current)
      messagesRef.current.scrollTop = messagesRef.current.scrollHeight
  }, [text, history.data?.length, optimisticUser])

  function receive(evt: SSEEvent) {
    const data = evt.data as Record<string, unknown>
    if (typeof data.event_id === "string") lastEventId.current = data.event_id
    if (evt.event !== "model_chunk") setEvents((previous) => [...previous, evt])
    if (evt.event === "run_started") {
      setRunId(String(data.run_id))
      if (!data.resumed) setChunks([])
    }
    if (evt.event === "context_retrieved" && Array.isArray(data.chunks))
      setChunks(data.chunks as CitationChunk[])
    if (evt.event === "model_chunk")
      setText((previous) => previous + String(data.text ?? ""))
    if (evt.event === "node_started") {
      setNode(String(data.node))
      if (data.node === "call_model") setText("")
    }
    if (evt.event === "node_finished") setNode(null)
    if (evt.event === "approval_requested") {
      setRunId(String(data.run_id))
      setWaitingApproval(true)
      void queryClient.invalidateQueries({ queryKey: ["approvals"] })
      return true
    }
    if (evt.event === "run_finished") {
      setWaitingApproval(false)
      if (
        typeof data.prompt_tokens === "number" &&
        typeof data.completion_tokens === "number"
      )
        setUsage({
          prompt_tokens: data.prompt_tokens,
          completion_tokens: data.completion_tokens,
          cost_usd: typeof data.cost_usd === "string" ? data.cost_usd : null,
        })
      setTruncated(data.truncated_by_max_iterations === true)
      return true
    }
    if (evt.event === "run_failed") {
      setWaitingApproval(false)
      showErrorToast(String(data.error ?? "Run failed"))
      return true
    }
    return false
  }

  async function finishStream() {
    await queryClient.invalidateQueries({ queryKey: ["conversations"] })
    await queryClient.invalidateQueries({ queryKey: ["runs"] })
    await queryClient.invalidateQueries({ queryKey: ["approvals"] })
    setOptimisticUser("")
    setText("")
    setNode(null)
    setStreaming(false)
    onStreaming(false)
    abortRef.current = null
  }

  async function followRun(targetRunId: string) {
    if (abortRef.current) return
    const controller = new AbortController()
    abortRef.current = controller
    setWaitingApproval(false)
    setStreaming(true)
    onStreaming(true)
    try {
      for await (const evt of streamSSE(
        `${import.meta.env.VITE_API_URL ?? ""}/api/v1/runs/${targetRunId}/stream?last_event_id=${encodeURIComponent(lastEventId.current)}`,
        { method: "GET", signal: controller.signal },
      ))
        receive(evt)
    } catch (error) {
      if (!controller.signal.aborted)
        showErrorToast(error instanceof Error ? error.message : "Stream failed")
    } finally {
      await finishStream()
    }
  }

  async function afterDecision() {
    if (!runId) return
    const { data } = await RunsService.readRun({ path: { id: runId } })
    if (data.status === "running" || data.status === "succeeded")
      await followRun(runId)
  }

  async function send(values: z.infer<typeof schema>) {
    if (abortRef.current) return
    const controller = new AbortController()
    abortRef.current = controller
    setStreaming(true)
    onStreaming(true)
    setOptimisticUser(values.message)
    setText("")
    setEvents([])
    setUsage(null)
    setRunId(null)
    setWaitingApproval(false)
    lastEventId.current = "0"
    setTruncated(false)
    form.reset()
    let terminal = false
    try {
      for await (const evt of streamSSE(
        `${import.meta.env.VITE_API_URL ?? ""}/api/v1/conversations/${id}/stream`,
        { body: values, signal: controller.signal },
      )) {
        terminal = receive(evt) || terminal
      }
      if (!terminal)
        throw new Error(
          "Stream ended before the run finished. Reload the conversation to check its status.",
        )
    } catch (error) {
      if (!controller.signal.aborted)
        showErrorToast(error instanceof Error ? error.message : "Stream failed")
    } finally {
      await finishStream()
    }
  }

  return (
    <section className="flex min-w-0 flex-col rounded-xl border">
      <div className="border-b px-5 py-3 font-medium">
        {detail.data?.title || "New conversation"}
      </div>
      <div
        ref={messagesRef}
        className="flex h-[45vh] flex-col gap-4 overflow-y-auto p-5"
        role="log"
        aria-label="Messages"
      >
        {history.isPending && <Skeleton className="h-20 w-3/4" />}
        {(history.isError || detail.isError) && (
          <p role="alert">Unable to load this conversation.</p>
        )}
        {history.data?.map((message) => (
          <Fragment key={message.id}>
            {!streaming &&
              message.role === "assistant" &&
              message.run_id === runId && <ToolTrace events={events} />}
            <Bubble
              messageRole={message.role}
              content={message.content}
              runId={message.run_id}
            />
          </Fragment>
        ))}
        {optimisticUser && (
          <Bubble messageRole="user" content={optimisticUser} />
        )}
        {streaming && (
          <div className="max-w-[90%] self-start">
            <ToolTrace events={events} running />
            <div
              className="mb-2 flex items-center gap-2 text-xs text-muted-foreground"
              data-testid="current-node"
            >
              <Loader2 className="size-3 animate-spin" />
              {node === "execute_tools"
                ? "executing tools"
                : node || "Processing"}
            </div>
            <Bubble
              messageRole="assistant"
              content={text || "…"}
              chunks={chunks}
            />
          </div>
        )}
        {truncated && (
          <p
            role="status"
            className="text-sm text-amber-700 dark:text-amber-400"
          >
            Stopped at the maximum iteration limit. Some tool calls were not
            executed.
          </p>
        )}
        {!!approvals.data?.length && (
          <ApprovalGroups
            requests={approvals.data}
            onDecision={() => void afterDecision()}
          />
        )}
        {waitingApproval && (
          <p role="status" className="text-sm text-amber-700">
            Waiting for approval before continuing.
          </p>
        )}
      </div>
      <div className="mt-auto border-t p-4">
        <Form {...form}>
          <form
            onSubmit={form.handleSubmit(send)}
            className="flex items-start gap-3"
          >
            <FormField
              control={form.control}
              name="message"
              render={({ field }) => (
                <FormItem className="flex-1">
                  <FormControl>
                    <Textarea
                      {...field}
                      aria-label="Message"
                      placeholder="Send a message…"
                      maxLength={20000}
                      disabled={
                        streaming || waitingApproval || !history.isSuccess
                      }
                      data-testid="playground-message"
                    />
                  </FormControl>
                  <FormMessage />
                </FormItem>
              )}
            />
            {streaming ? (
              <Button
                type="button"
                variant="outline"
                onClick={() => {
                  abortRef.current?.abort()
                  if (runId)
                    void RunsService.cancelRun({ path: { id: runId } }).catch(
                      () => {},
                    )
                }}
              >
                <Square />
                Stop
              </Button>
            ) : (
              <LoadingButton
                type="submit"
                loading={form.formState.isSubmitting}
                disabled={waitingApproval || !history.isSuccess}
                data-testid="send-message"
              >
                <Send />
                Send
              </LoadingButton>
            )}
          </form>
        </Form>
      </div>
      <details className="border-t px-5 py-3">
        <summary className="cursor-pointer text-sm font-medium">
          Execution events {events.length > 0 && `(${events.length})`}
        </summary>
        {runId && (
          <Link
            to="/runs/$runId"
            params={{ runId }}
            className="my-3 block text-sm underline"
          >
            View run
          </Link>
        )}
        {usage && (
          <p className="my-2 text-sm" data-testid="run-usage">
            Input: {usage.prompt_tokens} tokens · Output:{" "}
            {usage.completion_tokens} tokens · Cost:{" "}
            {usage.cost_usd === null ? "Unknown" : `$${usage.cost_usd}`}
          </p>
        )}
        <div className="max-h-48 overflow-auto">
          {events.map((event, index) => (
            <pre
              key={`${event.event}-${index}`}
              className="my-2 whitespace-pre-wrap break-all text-xs"
            >
              {event.event}: {JSON.stringify(event.data)}
            </pre>
          ))}
        </div>
      </details>
    </section>
  )
}

function Bubble({
  messageRole: role,
  content,
  runId,
  chunks,
}: {
  messageRole: string
  content: string
  runId?: string | null
  chunks?: CitationChunk[]
}) {
  return (
    <div
      data-testid={`${role}-message`}
      className={`max-w-[90%] whitespace-pre-wrap break-words rounded-xl px-4 py-3 text-sm ${role === "user" ? "self-end bg-primary text-primary-foreground" : "self-start bg-muted"}`}
    >
      <div className="mb-1 text-xs font-semibold opacity-70">
        {role === "user" ? "You" : role === "assistant" ? "Assistant" : role}
      </div>
      {role === "assistant" ? (
        <Citations content={content} runId={runId} chunks={chunks} />
      ) : (
        content
      )}
    </div>
  )
}
