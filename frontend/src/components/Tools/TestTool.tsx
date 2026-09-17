import { zodResolver } from "@hookform/resolvers/zod"
import { useMutation } from "@tanstack/react-query"
import { useState } from "react"
import { useForm } from "react-hook-form"
import { z } from "zod"

import { type ToolPublic, ToolsService } from "@/client"
import { Button } from "@/components/ui/button"
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

export default function TestTool({ tool }: { tool: ToolPublic }) {
  const [open, setOpen] = useState(false)
  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger asChild>
        <Button variant="outline" size="sm">
          Test
        </Button>
      </DialogTrigger>
      <DialogContent className="max-h-[85vh] overflow-y-auto">
        <DialogHeader>
          <DialogTitle>Test {tool.name}</DialogTitle>
          <DialogDescription>
            Run the tool with these arguments and inspect its result.
          </DialogDescription>
        </DialogHeader>
        {open && <TestForm tool={tool} />}
      </DialogContent>
    </Dialog>
  )
}

function TestForm({ tool }: { tool: ToolPublic }) {
  const { showErrorToast } = useCustomToast()
  const properties = (tool.parameters_schema?.properties ?? {}) as Record<
    string,
    { type?: string; description?: string; default?: unknown }
  >
  const required = (tool.parameters_schema?.required ?? []) as string[]
  const form = useForm<Record<string, string>>({
    resolver: zodResolver(z.record(z.string(), z.string())),
    mode: "onBlur",
    defaultValues: Object.fromEntries(
      Object.entries(properties).map(([name, p]) => [
        name,
        p.default == null
          ? ""
          : typeof p.default === "object"
            ? JSON.stringify(p.default)
            : String(p.default),
      ]),
    ),
  })
  const mutation = useMutation({
    mutationFn: (arguments_: Record<string, unknown>) =>
      ToolsService.testTool({
        path: { id: tool.id },
        body: { arguments: arguments_ },
      }),
    onError: handleError.bind(showErrorToast),
  })
  function submit(values: Record<string, string>) {
    const arguments_: Record<string, unknown> = {}
    for (const [name, property] of Object.entries(properties)) {
      const value = values[name] ?? ""
      if (!value && !required.includes(name)) continue
      try {
        if (!value && required.includes(name)) throw new Error("Required")
        if (property.type === "number" || property.type === "integer") {
          const parsed = Number(value)
          if (
            !Number.isFinite(parsed) ||
            (property.type === "integer" && !Number.isInteger(parsed))
          )
            throw new Error("Enter a valid number")
          arguments_[name] = parsed
        } else if (property.type === "boolean") {
          if (value !== "true" && value !== "false")
            throw new Error("Select true or false")
          arguments_[name] = value === "true"
        } else if (property.type === "object" || property.type === "array")
          arguments_[name] = JSON.parse(value)
        else arguments_[name] = value
      } catch (error) {
        form.setError(name, {
          message: error instanceof Error ? error.message : "Invalid value",
        })
        return
      }
    }
    mutation.mutate(arguments_)
  }
  return (
    <Form {...form}>
      <form className="grid gap-4" onSubmit={form.handleSubmit(submit)}>
        {Object.entries(properties).map(([name, property]) => (
          <FormField
            key={name}
            control={form.control}
            name={name}
            render={({ field }) => (
              <FormItem>
                <FormLabel>
                  {name}
                  {required.includes(name) ? " *" : ""}
                </FormLabel>
                <FormControl>
                  {property.type === "boolean" ? (
                    <select
                      {...field}
                      className="h-9 rounded-md border bg-background px-3"
                    >
                      <option value="">Select</option>
                      <option value="true">true</option>
                      <option value="false">false</option>
                    </select>
                  ) : (
                    <Input
                      {...field}
                      placeholder={
                        property.type === "object" || property.type === "array"
                          ? "JSON value"
                          : property.description
                      }
                    />
                  )}
                </FormControl>
                <p className="text-xs text-muted-foreground">
                  {property.description}
                </p>
                <FormMessage />
              </FormItem>
            )}
          />
        ))}
        <LoadingButton loading={mutation.isPending} type="submit">
          Run test
        </LoadingButton>
        {mutation.data && (
          <div
            role="status"
            className={`rounded-lg border p-3 text-sm ${mutation.data.data.ok ? "" : "text-destructive"}`}
          >
            <p>
              {mutation.data.data.ok ? "Succeeded" : "Failed"} ·{" "}
              {mutation.data.data.duration_ms} ms
            </p>
            <pre className="mt-2 max-h-64 overflow-auto whitespace-pre-wrap break-all">
              {mutation.data.data.ok
                ? mutation.data.data.content
                : mutation.data.data.error}
            </pre>
          </div>
        )}
      </form>
    </Form>
  )
}
