import { zodResolver } from "@hookform/resolvers/zod"
import {
  useMutation,
  useQueryClient,
  useSuspenseQuery,
} from "@tanstack/react-query"
import { createFileRoute, Link } from "@tanstack/react-router"
import { ArrowLeft, ChevronDown, ChevronRight, Plus } from "lucide-react"
import { Fragment, useState } from "react"
import { useForm } from "react-hook-form"
import { z } from "zod"

import { type AgentPublic, AgentsService } from "@/client"
import RunAgent from "@/components/Runs/RunAgent"
import ToolSelection from "@/components/Tools/ToolSelection"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { Checkbox } from "@/components/ui/checkbox"
import {
  Dialog,
  DialogClose,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog"
import {
  Form,
  FormControl,
  FormField,
  FormItem,
  FormLabel,
  FormMessage,
} from "@/components/ui/form"
import { Input } from "@/components/ui/input"
import { LoadingButton } from "@/components/ui/loading-button"
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table"
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs"
import { Textarea } from "@/components/ui/textarea"
import useCustomToast from "@/hooks/useCustomToast"
import { handleError } from "@/utils"

export const Route = createFileRoute("/_layout/agents/$agentId")({
  component: AgentDetail,
  head: () => ({ meta: [{ title: "Agent - AgentHub" }] }),
})

const configurationSchema = z.object({
  tool_ids: z.array(z.string()),
  name: z.string().min(1, { message: "Name is required" }),
  description: z.string().optional(),
  system_prompt: z.string(),
  llm_model: z.string().min(1, { message: "Model is required" }),
  temperature: z.number().min(0).max(2),
  max_tokens: z.number().int().min(1),
  max_iterations: z.number().int().min(1).max(50),
  timeout_seconds: z.number().int().min(1).max(3600),
  is_active: z.boolean(),
})

type ConfigurationData = z.infer<typeof configurationSchema>

function numberSetting(
  settings: AgentPublic["llm_settings"],
  key: string,
  fallback: number,
) {
  const value = settings?.[key]
  return typeof value === "number" ? value : fallback
}

function ConfigurationForm({ agent }: { agent: AgentPublic }) {
  const queryClient = useQueryClient()
  const { showSuccessToast, showErrorToast } = useCustomToast()
  const form = useForm<ConfigurationData>({
    resolver: zodResolver(configurationSchema),
    mode: "onBlur",
    defaultValues: {
      tool_ids: agent.tool_ids ?? [],
      name: agent.name,
      description: agent.description ?? "",
      system_prompt: agent.system_prompt ?? "",
      llm_model: agent.llm_model ?? "",
      temperature: numberSetting(agent.llm_settings, "temperature", 0.7),
      max_tokens: numberSetting(agent.llm_settings, "max_tokens", 1024),
      max_iterations: agent.max_iterations ?? 10,
      timeout_seconds: agent.timeout_seconds ?? 300,
      is_active: agent.is_active ?? true,
    },
  })
  const mutation = useMutation({
    mutationFn: (data: ConfigurationData) =>
      AgentsService.updateAgent({
        path: { id: agent.id },
        body: {
          tool_ids: data.tool_ids,
          name: data.name,
          description: data.description,
          system_prompt: data.system_prompt,
          llm_model: data.llm_model,
          llm_settings: {
            temperature: data.temperature,
            max_tokens: data.max_tokens,
          },
          max_iterations: data.max_iterations,
          timeout_seconds: data.timeout_seconds,
          is_active: data.is_active,
        },
      }),
    onSuccess: () => showSuccessToast("Agent configuration saved"),
    onError: handleError.bind(showErrorToast),
    onSettled: () => {
      queryClient.invalidateQueries({ queryKey: ["agents"] })
      queryClient.invalidateQueries({ queryKey: ["agents", agent.id] })
    },
  })

  return (
    <Card>
      <CardHeader>
        <CardTitle>Configuration</CardTitle>
      </CardHeader>
      <CardContent>
        <Form {...form}>
          <form
            className="grid gap-5"
            onSubmit={form.handleSubmit((data) => mutation.mutate(data))}
          >
            <div className="grid gap-4 md:grid-cols-2">
              <FormField
                control={form.control}
                name="name"
                render={({ field }) => (
                  <FormItem>
                    <FormLabel>Name</FormLabel>
                    <FormControl>
                      <Input
                        data-testid="configuration-name-input"
                        {...field}
                      />
                    </FormControl>
                    <FormMessage />
                  </FormItem>
                )}
              />
              <FormField
                control={form.control}
                name="llm_model"
                render={({ field }) => (
                  <FormItem>
                    <FormLabel>Model</FormLabel>
                    <FormControl>
                      <Input
                        data-testid="configuration-model-input"
                        {...field}
                      />
                    </FormControl>
                    <FormMessage />
                  </FormItem>
                )}
              />
            </div>
            <FormField
              control={form.control}
              name="description"
              render={({ field }) => (
                <FormItem>
                  <FormLabel>Description</FormLabel>
                  <FormControl>
                    <Input {...field} />
                  </FormControl>
                  <FormMessage />
                </FormItem>
              )}
            />
            <FormField
              control={form.control}
              name="system_prompt"
              render={({ field }) => (
                <FormItem>
                  <FormLabel>System prompt</FormLabel>
                  <FormControl>
                    <Textarea
                      className="min-h-40"
                      data-testid="configuration-system-prompt-input"
                      {...field}
                    />
                  </FormControl>
                  <FormMessage />
                </FormItem>
              )}
            />
            <div className="grid gap-4 md:grid-cols-2 lg:grid-cols-4">
              {(
                [
                  ["temperature", "Temperature", 0.1],
                  ["max_tokens", "Max tokens", 1],
                  ["max_iterations", "Max iterations", 1],
                  ["timeout_seconds", "Timeout seconds", 1],
                ] as const
              ).map(([name, label, step]) => (
                <FormField
                  key={name}
                  control={form.control}
                  name={name}
                  render={({ field }) => (
                    <FormItem>
                      <FormLabel>{label}</FormLabel>
                      <FormControl>
                        <Input
                          type="number"
                          step={step}
                          value={field.value}
                          onBlur={field.onBlur}
                          onChange={(event) =>
                            field.onChange(Number(event.target.value))
                          }
                          name={field.name}
                          ref={field.ref}
                        />
                      </FormControl>
                      <FormMessage />
                    </FormItem>
                  )}
                />
              ))}
            </div>
            <FormField
              control={form.control}
              name="is_active"
              render={({ field }) => (
                <FormItem className="flex items-center gap-2">
                  <FormControl>
                    <Checkbox
                      checked={field.value}
                      onCheckedChange={field.onChange}
                    />
                  </FormControl>
                  <FormLabel>Active</FormLabel>
                </FormItem>
              )}
            />
            <FormField
              control={form.control}
              name="tool_ids"
              render={({ field }) => (
                <FormItem>
                  <ToolSelection
                    value={field.value}
                    onChange={field.onChange}
                  />
                  <FormMessage />
                </FormItem>
              )}
            />
            <div>
              <LoadingButton
                type="submit"
                loading={mutation.isPending}
                data-testid="save-configuration-button"
              >
                Save configuration
              </LoadingButton>
            </div>
          </form>
        </Form>
      </CardContent>
    </Card>
  )
}

function PublishVersion({ agentId }: { agentId: string }) {
  const [isOpen, setIsOpen] = useState(false)
  const queryClient = useQueryClient()
  const { showSuccessToast, showErrorToast } = useCustomToast()
  const form = useForm<{ changelog: string }>({
    mode: "onBlur",
    defaultValues: { changelog: "" },
  })
  const mutation = useMutation({
    mutationFn: ({ changelog }: { changelog: string }) =>
      AgentsService.publishVersion({
        path: { id: agentId },
        body: { changelog: changelog || undefined },
      }),
    onSuccess: () => {
      showSuccessToast("Agent version published")
      form.reset()
      setIsOpen(false)
    },
    onError: handleError.bind(showErrorToast),
    onSettled: () => {
      queryClient.invalidateQueries({ queryKey: ["agents"] })
      queryClient.invalidateQueries({ queryKey: ["agents", agentId] })
      queryClient.invalidateQueries({ queryKey: ["agent-versions", agentId] })
    },
  })

  return (
    <Dialog open={isOpen} onOpenChange={setIsOpen}>
      <DialogTrigger asChild>
        <Button data-testid="publish-version-button">
          <Plus /> Publish new version
        </Button>
      </DialogTrigger>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Publish new version</DialogTitle>
          <DialogDescription>
            Freeze the current configuration as an immutable snapshot.
          </DialogDescription>
        </DialogHeader>
        <form onSubmit={form.handleSubmit((data) => mutation.mutate(data))}>
          <label
            className="grid gap-2 text-sm font-medium"
            htmlFor="version-changelog"
          >
            Changelog
            <Textarea
              id="version-changelog"
              data-testid="version-changelog-input"
              {...form.register("changelog")}
            />
          </label>
          <DialogFooter className="mt-4">
            <DialogClose asChild>
              <Button variant="outline" disabled={mutation.isPending}>
                Cancel
              </Button>
            </DialogClose>
            <LoadingButton
              type="submit"
              loading={mutation.isPending}
              data-testid="publish-version-confirm"
            >
              Publish
            </LoadingButton>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  )
}

function Versions({ agentId }: { agentId: string }) {
  const [expandedId, setExpandedId] = useState<string | null>(null)
  const { data: versions } = useSuspenseQuery({
    queryFn: async () =>
      (await AgentsService.readVersions({ path: { id: agentId } })).data,
    queryKey: ["agent-versions", agentId],
  })

  return (
    <Card>
      <CardHeader className="flex-row items-center justify-between">
        <CardTitle>Versions</CardTitle>
        <PublishVersion agentId={agentId} />
      </CardHeader>
      <CardContent>
        {versions.data.length === 0 ? (
          <p className="py-8 text-center text-muted-foreground">
            No versions published yet.
          </p>
        ) : (
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Version</TableHead>
                <TableHead>Changelog</TableHead>
                <TableHead>Published</TableHead>
                <TableHead>
                  <span className="sr-only">Snapshot</span>
                </TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {versions.data.map((version) => {
                const expanded = expandedId === version.id
                return (
                  <Fragment key={version.id}>
                    <TableRow>
                      <TableCell>
                        <Badge variant="secondary">
                          v{version.version_number}
                        </Badge>
                        <p className="mt-1 text-xs text-muted-foreground">
                          {version.tool_names?.join(", ") || "No tools"}
                        </p>
                      </TableCell>
                      <TableCell>{version.changelog || "—"}</TableCell>
                      <TableCell>
                        {version.created_at
                          ? new Date(version.created_at).toLocaleString()
                          : "—"}
                      </TableCell>
                      <TableCell className="text-right">
                        <RunAgent
                          versionId={version.id}
                          versionNumber={version.version_number}
                        />
                        <Button
                          variant="ghost"
                          size="sm"
                          onClick={() =>
                            setExpandedId(expanded ? null : version.id)
                          }
                        >
                          {expanded ? <ChevronDown /> : <ChevronRight />}
                          Snapshot
                        </Button>
                      </TableCell>
                    </TableRow>
                    {expanded && (
                      <TableRow key={`${version.id}-snapshot`}>
                        <TableCell colSpan={4}>
                          <pre className="max-h-96 overflow-auto rounded-md bg-muted p-4 text-xs">
                            {JSON.stringify(version.snapshot, null, 2)}
                          </pre>
                        </TableCell>
                      </TableRow>
                    )}
                  </Fragment>
                )
              })}
            </TableBody>
          </Table>
        )}
      </CardContent>
    </Card>
  )
}

function AgentDetail() {
  const { agentId } = Route.useParams()
  const { data: agent } = useSuspenseQuery({
    queryFn: async () =>
      (await AgentsService.readAgent({ path: { id: agentId } })).data,
    queryKey: ["agents", agentId],
  })

  return (
    <div className="flex flex-col gap-6">
      <div>
        <Button variant="ghost" asChild className="mb-2 -ml-3">
          <Link to="/agents">
            <ArrowLeft /> Back to agents
          </Link>
        </Button>
        <div className="flex items-center gap-3">
          <h1 className="text-2xl font-bold tracking-tight">{agent.name}</h1>
          <Badge variant={agent.is_active ? "default" : "secondary"}>
            {agent.is_active ? "Active" : "Inactive"}
          </Badge>
        </div>
        <p className="text-muted-foreground">
          {agent.description || "No description"}
        </p>
      </div>
      <Tabs defaultValue="configuration">
        <TabsList>
          <TabsTrigger value="configuration">Configuration</TabsTrigger>
          <TabsTrigger value="versions" data-testid="versions-tab">
            Versions
          </TabsTrigger>
        </TabsList>
        <TabsContent value="configuration">
          <ConfigurationForm agent={agent} />
        </TabsContent>
        <TabsContent value="versions">
          <Versions agentId={agentId} />
        </TabsContent>
      </Tabs>
    </div>
  )
}
