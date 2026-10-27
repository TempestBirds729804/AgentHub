import { zodResolver } from "@hookform/resolvers/zod"
import {
  useMutation,
  useQuery,
  useQueryClient,
  useSuspenseQuery,
} from "@tanstack/react-query"
import { Suspense, useState } from "react"
import { useFieldArray, useForm } from "react-hook-form"
import { z } from "zod"
import {
  type AgentPublic,
  type EvalCasePublic,
  EvalsService,
  ToolsService,
} from "@/client"
import { Button } from "@/components/ui/button"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
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
import { Skeleton } from "@/components/ui/skeleton"
import { Textarea } from "@/components/ui/textarea"
import useCustomToast from "@/hooks/useCustomToast"
import { handleError } from "@/utils"

const kinds = [
  "exact",
  "contains",
  "not_contains",
  "regex",
  "json_path",
  "tool_called",
  "tool_not_called",
  "llm_judge",
  "max_duration_ms",
  "max_cost_usd",
] as const
const schema = z.object({
  name: z.string().min(1).max(255),
  description: z.string().max(1000),
  message: z.string().min(1).max(20000),
  expected: z.string().max(20000),
  tags: z.string(),
  active: z.boolean(),
  assertions: z.array(
    z.object({
      type: z.enum(kinds),
      value: z.string(),
      path: z.string(),
      threshold: z.number().min(0).max(1),
    }),
  ),
})
type CaseForm = z.infer<typeof schema>

function Editor({
  agent,
  current,
  close,
}: {
  agent: AgentPublic
  current?: EvalCasePublic
  close: () => void
}) {
  const queryClient = useQueryClient()
  const { showSuccessToast, showErrorToast } = useCustomToast()
  const tools = useQuery({
    queryKey: ["tools"],
    queryFn: async () => (await ToolsService.readTools()).data,
  })
  const form = useForm<CaseForm>({
    resolver: zodResolver(schema),
    mode: "onBlur",
    defaultValues: {
      name: current?.name ?? "",
      description: current?.description ?? "",
      message: String(current?.input?.message ?? ""),
      expected: current?.expected_output ?? "",
      tags: current?.tags?.join(", ") ?? "",
      active: current?.is_active ?? true,
      assertions: (current?.assertions ?? []).map((a) => ({
        type: a.type as (typeof kinds)[number],
        value:
          a.type === "json_path"
            ? JSON.stringify(a.value)
            : String(a.value ?? ""),
        path: String(a.path ?? ""),
        threshold: Number(a.threshold ?? 0.7),
      })),
    },
  })
  const rows = useFieldArray({ control: form.control, name: "assertions" })
  const mutation = useMutation({
    mutationFn: (values: CaseForm) => {
      const body = {
        name: values.name,
        description: values.description,
        input: { message: values.message },
        expected_output: values.expected || null,
        tags: values.tags
          .split(",")
          .map((t) => t.trim())
          .filter(Boolean),
        is_active: values.active,
        assertions: values.assertions.map((a) => ({
          type: a.type,
          value: a.type === "json_path" ? JSON.parse(a.value) : a.value,
          ...(a.type === "json_path" ? { path: a.path } : {}),
          ...(a.type === "llm_judge"
            ? { threshold: a.threshold, expected_output: values.expected }
            : {}),
        })),
      }
      return current
        ? EvalsService.updateCase({ path: { id: current.id }, body })
        : EvalsService.createCase({ query: { agent_id: agent.id }, body })
    },
    onSuccess: () => {
      showSuccessToast("Test case saved")
      close()
    },
    onError: handleError.bind(showErrorToast),
    onSettled: () =>
      queryClient.invalidateQueries({ queryKey: ["eval-cases", agent.id] }),
  })
  return (
    <Dialog
      open
      onOpenChange={(open) => {
        if (!open) close()
      }}
    >
      <DialogContent className="max-h-[90vh] overflow-auto sm:max-w-2xl">
        <DialogHeader>
          <DialogTitle>
            {current ? "Edit test case" : "New test case"}
          </DialogTitle>
          <DialogDescription>
            All assertions must pass. JSON path values must be valid JSON.
          </DialogDescription>
        </DialogHeader>
        <Form {...form}>
          <form
            className="space-y-4"
            onSubmit={form.handleSubmit((v) => mutation.mutate(v))}
          >
            {(
              [
                ["name", "Name"],
                ["description", "Description"],
                ["message", "Message"],
                ["expected", "Expected output"],
                ["tags", "Tags (comma separated)"],
              ] as const
            ).map(([name, label]) => (
              <FormField
                key={name}
                control={form.control}
                name={name}
                render={({ field }) => (
                  <FormItem>
                    <FormLabel>{label}</FormLabel>
                    <FormControl>
                      {name === "message" || name === "expected" ? (
                        <Textarea {...field} />
                      ) : (
                        <Input {...field} />
                      )}
                    </FormControl>
                    <FormMessage />
                  </FormItem>
                )}
              />
            ))}
            <FormField
              control={form.control}
              name="active"
              render={({ field }) => (
                <FormItem>
                  <FormLabel>
                    <input
                      type="checkbox"
                      checked={field.value}
                      onChange={(e) => field.onChange(e.target.checked)}
                    />{" "}
                    Active
                  </FormLabel>
                  <FormMessage />
                </FormItem>
              )}
            />
            <h3 className="font-semibold">Assertions</h3>
            {rows.fields.map((row, index) => {
              const kind = form.watch(`assertions.${index}.type`)
              return (
                <fieldset
                  key={row.id}
                  className="grid gap-3 rounded border p-3"
                >
                  <FormField
                    control={form.control}
                    name={`assertions.${index}.type`}
                    render={({ field }) => (
                      <FormItem>
                        <FormLabel>Type</FormLabel>
                        <FormControl>
                          <select
                            className="w-full rounded border p-2"
                            {...field}
                          >
                            {kinds.map((k) => (
                              <option key={k}>{k}</option>
                            ))}
                          </select>
                        </FormControl>
                        <FormMessage />
                      </FormItem>
                    )}
                  />
                  <FormField
                    control={form.control}
                    name={`assertions.${index}.value`}
                    render={({ field }) => (
                      <FormItem>
                        <FormLabel>
                          {kind === "llm_judge"
                            ? "Criterion"
                            : "Expected value"}
                        </FormLabel>
                        <FormControl>
                          {kind.startsWith("tool_") ? (
                            <select
                              className="w-full rounded border p-2"
                              {...field}
                            >
                              <option value="">Select tool</option>
                              {tools.data?.data
                                .filter((t) => agent.tool_ids?.includes(t.id))
                                .map((t) => (
                                  <option key={t.id} value={t.name}>
                                    {t.name}
                                  </option>
                                ))}
                            </select>
                          ) : (
                            <Input
                              type={kind.startsWith("max_") ? "number" : "text"}
                              step="any"
                              {...field}
                            />
                          )}
                        </FormControl>
                        <FormMessage />
                      </FormItem>
                    )}
                  />
                  {kind === "json_path" && (
                    <FormField
                      control={form.control}
                      name={`assertions.${index}.path`}
                      render={({ field }) => (
                        <FormItem>
                          <FormLabel>JSON path (e.g. items.0.name)</FormLabel>
                          <FormControl>
                            <Input {...field} />
                          </FormControl>
                          <FormMessage />
                        </FormItem>
                      )}
                    />
                  )}
                  {kind === "llm_judge" && (
                    <FormField
                      control={form.control}
                      name={`assertions.${index}.threshold`}
                      render={({ field }) => (
                        <FormItem>
                          <FormLabel>Threshold: {field.value}</FormLabel>
                          <FormControl>
                            <Input
                              type="range"
                              min="0"
                              max="1"
                              step="0.05"
                              {...field}
                              onChange={(e) =>
                                field.onChange(Number(e.target.value))
                              }
                            />
                          </FormControl>
                          <FormMessage />
                        </FormItem>
                      )}
                    />
                  )}
                  <Button
                    type="button"
                    variant="outline"
                    onClick={() => rows.remove(index)}
                  >
                    Remove assertion
                  </Button>
                </fieldset>
              )
            })}
            <Button
              type="button"
              variant="outline"
              onClick={() =>
                rows.append({
                  type: "contains",
                  value: "",
                  path: "",
                  threshold: 0.7,
                })
              }
            >
              Add assertion
            </Button>
            <LoadingButton
              className="ml-3"
              type="submit"
              loading={mutation.isPending}
            >
              Save test case
            </LoadingButton>
          </form>
        </Form>
      </DialogContent>
    </Dialog>
  )
}

function Cases({ agent }: { agent: AgentPublic }) {
  const [editing, setEditing] = useState<EvalCasePublic | "new" | null>(null)
  const [importing, setImporting] = useState(false)
  const [json, setJson] = useState("")
  const [page, setPage] = useState(0)
  const queryClient = useQueryClient()
  const { showSuccessToast, showErrorToast } = useCustomToast()
  const { data } = useSuspenseQuery({
    queryKey: ["eval-cases", agent.id, page],
    queryFn: async () =>
      (
        await EvalsService.readCases({
          query: { agent_id: agent.id, skip: page * 20, limit: 20 },
        })
      ).data,
  })
  const remove = useMutation({
    mutationFn: (id: string) => EvalsService.deleteCase({ path: { id } }),
    onError: handleError.bind(showErrorToast),
    onSuccess: () => showSuccessToast("Test case deleted"),
    onSettled: () =>
      queryClient.invalidateQueries({ queryKey: ["eval-cases", agent.id] }),
  })
  const upload = useMutation({
    mutationFn: () =>
      EvalsService.importCases({
        query: { agent_id: agent.id },
        body: JSON.parse(json),
      }),
    onError: handleError.bind(showErrorToast),
    onSuccess: () => {
      setImporting(false)
      setJson("")
      showSuccessToast("Test cases imported")
    },
    onSettled: () =>
      queryClient.invalidateQueries({ queryKey: ["eval-cases", agent.id] }),
  })
  return (
    <section className="space-y-4">
      <div className="flex gap-2">
        <Button onClick={() => setEditing("new")}>New test case</Button>
        <Button variant="outline" onClick={() => setImporting(true)}>
          Import JSON
        </Button>
      </div>
      {data.data.map((c) => (
        <div
          key={c.id}
          className="flex flex-wrap items-center gap-3 rounded border p-4"
        >
          <strong>{c.name}</strong>
          <span>{c.tags?.join(", ")}</span>
          <span>
            {c.assertions?.length ?? 0} assertions ·{" "}
            {c.is_active ? "Active" : "Inactive"}
          </span>
          <Button variant="outline" onClick={() => setEditing(c)}>
            Edit
          </Button>
          <Button
            variant="destructive"
            disabled={remove.isPending}
            onClick={() => {
              if (window.confirm(`Delete ${c.name}?`)) remove.mutate(c.id)
            }}
          >
            Delete
          </Button>
        </div>
      ))}
      {!data.count && <p>No test cases yet.</p>}
      <div className="flex gap-3">
        <Button
          variant="outline"
          disabled={!page}
          onClick={() => setPage((p) => p - 1)}
        >
          Previous
        </Button>
        <span>{data.count} cases</span>
        <Button
          variant="outline"
          disabled={(page + 1) * 20 >= data.count}
          onClick={() => setPage((p) => p + 1)}
        >
          Next
        </Button>
      </div>
      {editing && (
        <Editor
          agent={agent}
          current={editing === "new" ? undefined : editing}
          close={() => setEditing(null)}
        />
      )}
      <Dialog open={importing} onOpenChange={setImporting}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Import test cases</DialogTitle>
            <DialogDescription>
              Paste a JSON array of case objects with name, input.message and
              assertions.
            </DialogDescription>
          </DialogHeader>
          <Textarea
            aria-label="Case JSON"
            value={json}
            onChange={(e) => setJson(e.target.value)}
          />
          <LoadingButton
            loading={upload.isPending}
            onClick={() => upload.mutate()}
          >
            Import
          </LoadingButton>
        </DialogContent>
      </Dialog>
    </section>
  )
}

export default function TestCases({ agent }: { agent: AgentPublic }) {
  return (
    <Suspense fallback={<Skeleton className="h-48 w-full" />}>
      <Cases agent={agent} />
    </Suspense>
  )
}
