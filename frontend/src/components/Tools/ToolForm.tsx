import { zodResolver } from "@hookform/resolvers/zod"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { useState } from "react"
import { useFieldArray, useForm } from "react-hook-form"
import { z } from "zod"

import { type ToolCreate, type ToolPublic, ToolsService } from "@/client"
import { Button } from "@/components/ui/button"
import { Checkbox } from "@/components/ui/checkbox"
import {
  Dialog,
  DialogContent,
  DialogDescription,
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
import useCustomToast from "@/hooks/useCustomToast"
import { handleError } from "@/utils"

const parameter = z.object({
  name: z
    .string()
    .min(1)
    .regex(/^[a-zA-Z_][a-zA-Z0-9_]*$/),
  type: z.enum(["string", "number", "integer", "boolean", "object", "array"]),
  description: z.string(),
  required: z.boolean(),
})
const schema = z
  .object({
    name: z
      .string()
      .min(1)
      .max(64)
      .regex(
        /^[a-zA-Z0-9_-]+$/,
        "Use letters, numbers, underscores or hyphens",
      ),
    description: z.string().min(1).max(1000),
    tool_type: z.enum(["function", "http", "mcp"]),
    function_name: z.string(),
    url: z.string(),
    method: z.string(),
    tool_name: z.string(),
    transport: z.enum(["sse", "streamable_http"]),
    headers: z.array(z.object({ name: z.string().min(1), value: z.string() })),
    query: z.string(),
    body: z.string(),
    parameters: z.array(parameter),
    timeout_seconds: z.number().int().min(1).max(300),
    requires_approval: z.boolean(),
    is_active: z.boolean(),
  })
  .superRefine((value, ctx) => {
    if (value.tool_type === "function" && !value.function_name)
      ctx.addIssue({
        code: "custom",
        path: ["function_name"],
        message: "Select a built-in function",
      })
    if (value.tool_type !== "function" && !/^https?:\/\//.test(value.url))
      ctx.addIssue({
        code: "custom",
        path: ["url"],
        message: "Enter an HTTP or HTTPS URL",
      })
    if (value.tool_type === "mcp" && !value.tool_name.trim())
      ctx.addIssue({
        code: "custom",
        path: ["tool_name"],
        message: "Enter the remote tool name",
      })
    if (
      new Set(value.parameters.map((p) => p.name)).size !==
      value.parameters.length
    )
      ctx.addIssue({
        code: "custom",
        path: ["parameters"],
        message: "Parameter names must be unique",
      })
  })
type Values = z.infer<typeof schema>
const selectClass = "h-9 rounded-md border bg-background px-3 text-sm"

export default function ToolForm({ tool }: { tool?: ToolPublic }) {
  const [open, setOpen] = useState(false)
  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger asChild>
        <Button
          variant={tool ? "outline" : "default"}
          size={tool ? "sm" : "default"}
        >
          {tool ? "Edit" : "Add Tool"}
        </Button>
      </DialogTrigger>
      <DialogContent className="max-h-[90vh] overflow-y-auto sm:max-w-3xl">
        <DialogHeader>
          <DialogTitle>{tool ? "Edit tool" : "Add Tool"}</DialogTitle>
          <DialogDescription>
            Choose a built-in function or connect an HTTP or MCP tool.
          </DialogDescription>
        </DialogHeader>
        {open && <Editor tool={tool} onSaved={() => setOpen(false)} />}
      </DialogContent>
    </Dialog>
  )
}

function Editor({ tool, onSaved }: { tool?: ToolPublic; onSaved: () => void }) {
  const queryClient = useQueryClient()
  const { showSuccessToast, showErrorToast } = useCustomToast()
  const config = tool?.config ?? {}
  const properties = (tool?.parameters_schema?.properties ?? {}) as Record<
    string,
    { type?: Values["parameters"][number]["type"]; description?: string }
  >
  const required = (tool?.parameters_schema?.required ?? []) as string[]
  const defaults: Values = {
    name: tool?.name ?? "",
    description: tool?.description ?? "",
    tool_type: tool?.tool_type ?? "function",
    function_name: String(config.function_name ?? ""),
    url: String(config.url ?? ""),
    method: String(config.method ?? "GET"),
    tool_name: String(config.tool_name ?? ""),
    transport: config.transport === "sse" ? "sse" : "streamable_http",
    query: ((config.query_from_args ?? []) as string[]).join(", "),
    body: ((config.body_from_args ?? []) as string[]).join(", "),
    headers: Object.entries(config.headers ?? {}).map(([name, value]) => ({
      name,
      value: String(value),
    })),
    parameters: Object.entries(properties).map(([name, prop]) => ({
      name,
      type: prop.type ?? "string",
      description: prop.description ?? "",
      required: required.includes(name),
    })),
    timeout_seconds: tool?.timeout_seconds ?? 30,
    requires_approval: tool?.requires_approval ?? false,
    is_active: tool?.is_active ?? true,
  }
  const form = useForm<Values>({
    resolver: zodResolver(schema),
    mode: "onBlur",
    defaultValues: defaults,
  })
  const params = useFieldArray({ control: form.control, name: "parameters" })
  const headers = useFieldArray({ control: form.control, name: "headers" })
  const kind = form.watch("tool_type")
  const functionName = form.watch("function_name")
  const functions = useQuery({
    queryKey: ["tools", "builtin-functions"],
    queryFn: async () => (await ToolsService.readBuiltinFunctions()).data,
  })
  const selected = functions.data?.data.find((fn) => fn.name === functionName)
  const mutation = useMutation({
    mutationFn: (body: ToolCreate) =>
      tool
        ? ToolsService.updateTool({ path: { id: tool.id }, body })
        : ToolsService.createTool({ body }),
    onSuccess: () => {
      showSuccessToast(
        tool ? "Tool updated successfully" : "Tool created successfully",
      )
      form.reset()
      onSaved()
    },
    onError: handleError.bind(showErrorToast),
    onSettled: () => {
      void queryClient.invalidateQueries({ queryKey: ["tools"] })
      void queryClient.invalidateQueries({ queryKey: ["agent-versions"] })
    },
  })
  function submit(values: Values) {
    const names = (value: string) =>
      value
        .split(",")
        .map((v) => v.trim())
        .filter(Boolean)
    const config =
      values.tool_type === "function"
        ? { function_name: values.function_name }
        : values.tool_type === "mcp"
          ? {
              transport: values.transport,
              url: values.url,
              tool_name: values.tool_name,
            }
          : {
              method: values.method,
              url: values.url,
              headers: Object.fromEntries(
                values.headers.map((h) => [h.name, h.value]),
              ),
              query_from_args: names(values.query),
              body_from_args: names(values.body),
            }
    const parameters_schema =
      values.tool_type === "function"
        ? selected?.parameters_schema
        : tool && !form.formState.dirtyFields.parameters
          ? tool.parameters_schema
          : {
              type: "object",
              properties: Object.fromEntries(
                values.parameters.map((p) => [
                  p.name,
                  {
                    type: p.type,
                    description: p.description,
                    ...(p.type === "array"
                      ? { items: { type: "string" } }
                      : {}),
                  },
                ]),
              ),
              required: values.parameters
                .filter((p) => p.required)
                .map((p) => p.name),
            }
    mutation.mutate({
      name: values.name,
      description: values.description,
      tool_type: values.tool_type,
      config,
      parameters_schema,
      timeout_seconds: values.timeout_seconds,
      requires_approval: values.requires_approval,
      is_active: values.is_active,
    })
  }
  function textField(
    name: "name" | "description" | "url" | "tool_name" | "query" | "body",
    label: string,
    placeholder?: string,
  ) {
    return (
      <FormField
        key={name}
        control={form.control}
        name={name}
        render={({ field }) => (
          <FormItem>
            <FormLabel>{label}</FormLabel>
            <FormControl>
              <Input {...field} placeholder={placeholder} />
            </FormControl>
            <FormMessage />
          </FormItem>
        )}
      />
    )
  }
  return (
    <Form {...form}>
      <form onSubmit={form.handleSubmit(submit)} className="grid gap-4">
        <FormField
          control={form.control}
          name="tool_type"
          render={({ field }) => (
            <FormItem>
              <FormLabel>Tool type</FormLabel>
              <FormControl>
                <select {...field} disabled={!!tool} className={selectClass}>
                  <option value="function">Function</option>
                  <option value="http">HTTP</option>
                  <option value="mcp">MCP</option>
                </select>
              </FormControl>
              <FormMessage />
            </FormItem>
          )}
        />
        {kind === "function" && (
          <>
            <p className="text-sm text-muted-foreground">
              Function tools run built-in functions only. User-submitted Python
              code is not supported. Use HTTP or MCP to extend capabilities.
            </p>
            <FormField
              control={form.control}
              name="function_name"
              render={({ field }) => (
                <FormItem>
                  <FormLabel>Built-in function</FormLabel>
                  <FormControl>
                    <select
                      {...field}
                      className={selectClass}
                      onChange={(e) => {
                        field.onChange(e)
                        const fn = functions.data?.data.find(
                          (f) => f.name === e.target.value,
                        )
                        if (fn) {
                          form.setValue("name", fn.name)
                          form.setValue("description", fn.description)
                        }
                      }}
                    >
                      <option value="">Select a function</option>
                      {functions.data?.data.map((fn) => (
                        <option key={fn.name} value={fn.name}>
                          {fn.name}
                        </option>
                      ))}
                    </select>
                  </FormControl>
                  <FormMessage />
                </FormItem>
              )}
            />
            {functions.isError && (
              <p role="alert">Unable to load built-in functions.</p>
            )}
            {selected && (
              <details>
                <summary className="cursor-pointer text-sm">
                  Parameters (read only)
                </summary>
                <pre className="max-h-48 overflow-auto rounded bg-muted p-3 text-xs">
                  {JSON.stringify(selected.parameters_schema, null, 2)}
                </pre>
              </details>
            )}
          </>
        )}
        <div className="grid gap-4 sm:grid-cols-2">
          {textField("name", "Name")}
          {textField("description", "Description")}
        </div>
        {kind !== "function" && (
          <>
            {kind === "http" ? (
              <FormField
                control={form.control}
                name="method"
                render={({ field }) => (
                  <FormItem>
                    <FormLabel>HTTP method</FormLabel>
                    <FormControl>
                      <select {...field} className={selectClass}>
                        {[
                          "GET",
                          "POST",
                          "PUT",
                          "PATCH",
                          "DELETE",
                          "HEAD",
                          "OPTIONS",
                        ].map((m) => (
                          <option key={m}>{m}</option>
                        ))}
                      </select>
                    </FormControl>
                  </FormItem>
                )}
              />
            ) : (
              <FormField
                control={form.control}
                name="transport"
                render={({ field }) => (
                  <FormItem>
                    <FormLabel>MCP transport</FormLabel>
                    <FormControl>
                      <select {...field} className={selectClass}>
                        <option value="streamable_http">Streamable HTTP</option>
                        <option value="sse">SSE (legacy)</option>
                      </select>
                    </FormControl>
                    <FormMessage />
                  </FormItem>
                )}
              />
            )}
            {textField(
              "url",
              "URL",
              kind === "mcp"
                ? "http://127.0.0.1:3001/mcp"
                : "https://api.github.com/repos/{owner}/{repo}",
            )}
            {kind === "mcp" && (
              <p className="text-sm text-muted-foreground">
                Use http://127.0.0.1:3001/mcp for a local API, or
                http://mcp-docs:3001/mcp when the API runs in Docker Compose.
                The server address must be trusted by the deployment.
              </p>
            )}
            {kind === "mcp" && textField("tool_name", "Remote tool name")}
            {kind === "http" && (
              <>
                <fieldset className="space-y-2">
                  <legend className="text-sm font-medium">Headers</legend>
                  {headers.fields.map((row, index) => (
                    <div key={row.id} className="flex gap-2">
                      <Input
                        aria-label={`Header ${index + 1} name`}
                        placeholder="Header name"
                        {...form.register(`headers.${index}.name`)}
                      />
                      <Input
                        aria-label={`Header ${index + 1} value`}
                        placeholder="Value"
                        {...form.register(`headers.${index}.value`)}
                      />
                      <Button
                        type="button"
                        variant="ghost"
                        onClick={() => headers.remove(index)}
                      >
                        Remove
                      </Button>
                    </div>
                  ))}
                  <Button
                    type="button"
                    variant="outline"
                    size="sm"
                    onClick={() => headers.append({ name: "", value: "" })}
                  >
                    Add header
                  </Button>
                </fieldset>
                <div className="grid gap-4 sm:grid-cols-2">
                  {textField(
                    "query",
                    "Query parameter names",
                    "page, per_page",
                  )}
                  {textField("body", "Body parameter names", "title, content")}
                </div>
                <p className="text-xs text-muted-foreground">
                  Separate parameter names with commas. Path parameters use
                  braces in the URL.
                </p>
              </>
            )}
            <fieldset className="space-y-3">
              <legend className="text-sm font-medium">Parameters</legend>
              {params.fields.map((row, index) => (
                <div
                  key={row.id}
                  className="grid gap-2 rounded-lg border p-3 sm:grid-cols-2"
                >
                  <Input
                    aria-label={`Parameter ${index + 1} name`}
                    placeholder="Name"
                    {...form.register(`parameters.${index}.name`)}
                  />
                  <select
                    aria-label={`Parameter ${index + 1} type`}
                    className={selectClass}
                    {...form.register(`parameters.${index}.type`)}
                  >
                    {[
                      "string",
                      "number",
                      "integer",
                      "boolean",
                      "object",
                      "array",
                    ].map((t) => (
                      <option key={t}>{t}</option>
                    ))}
                  </select>
                  <Input
                    aria-label={`Parameter ${index + 1} description`}
                    placeholder="Description"
                    {...form.register(`parameters.${index}.description`)}
                  />
                  <div className="flex items-center justify-between">
                    <label className="flex items-center gap-2 text-sm">
                      <input
                        type="checkbox"
                        {...form.register(`parameters.${index}.required`)}
                      />
                      Required
                    </label>
                    <Button
                      type="button"
                      variant="ghost"
                      size="sm"
                      onClick={() => params.remove(index)}
                    >
                      Remove parameter
                    </Button>
                  </div>
                  {form.formState.errors.parameters?.[index]?.name && (
                    <p className="text-sm text-destructive">
                      {form.formState.errors.parameters[index]?.name?.message}
                    </p>
                  )}
                </div>
              ))}
              <Button
                type="button"
                variant="outline"
                size="sm"
                onClick={() =>
                  params.append({
                    name: "",
                    type: "string",
                    description: "",
                    required: true,
                  })
                }
              >
                Add parameter
              </Button>
              {form.formState.errors.parameters?.root?.message && (
                <p role="alert">
                  {form.formState.errors.parameters.root.message}
                </p>
              )}
            </fieldset>
          </>
        )}
        <FormField
          control={form.control}
          name="timeout_seconds"
          render={({ field }) => (
            <FormItem>
              <FormLabel>Timeout (seconds)</FormLabel>
              <FormControl>
                <Input
                  type="number"
                  min={1}
                  max={300}
                  {...field}
                  onChange={(e) => field.onChange(Number(e.target.value))}
                />
              </FormControl>
              <FormMessage />
            </FormItem>
          )}
        />
        <div className="flex flex-wrap gap-6">
          {(["is_active", "requires_approval"] as const).map((name) => (
            <FormField
              key={name}
              control={form.control}
              name={name}
              render={({ field }) => (
                <FormItem className="flex items-center gap-2">
                  <FormControl>
                    <Checkbox
                      checked={field.value}
                      onCheckedChange={field.onChange}
                    />
                  </FormControl>
                  <FormLabel>
                    {name === "is_active" ? "Active" : "Requires approval"}
                  </FormLabel>
                </FormItem>
              )}
            />
          ))}
        </div>
        <p className="text-sm text-muted-foreground">
          When approval is required, the agent will pause and wait for your
          approval before each call to this tool.
        </p>
        <LoadingButton type="submit" loading={mutation.isPending}>
          Save tool
        </LoadingButton>
      </form>
    </Form>
  )
}
