import { zodResolver } from "@hookform/resolvers/zod"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { useState } from "react"
import { useForm } from "react-hook-form"
import { z } from "zod"
import { AgentsService, EvalsService } from "@/client"
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
import useCustomToast from "@/hooks/useCustomToast"
import { handleError } from "@/utils"

const schema = z.object({
  agent: z.string().min(1),
  version: z.string().min(1),
  mode: z.enum(["all", "tags", "manual"]),
  tags: z.string(),
  cases: z.array(z.string()),
  note: z.string().max(500),
})
type Values = z.infer<typeof schema>
export default function NewEvaluation() {
  const [open, setOpen] = useState(false)
  const queryClient = useQueryClient()
  const { showSuccessToast, showErrorToast } = useCustomToast()
  const form = useForm<Values>({
    resolver: zodResolver(schema),
    mode: "onBlur",
    defaultValues: {
      agent: "",
      version: "",
      mode: "all",
      tags: "",
      cases: [],
      note: "",
    },
  })
  const agent = form.watch("agent")
  const mode = form.watch("mode")
  const agents = useQuery({
    queryKey: ["agents"],
    queryFn: async () => (await AgentsService.readAgents()).data,
    enabled: open,
  })
  const versions = useQuery({
    queryKey: ["agent-versions", agent],
    queryFn: async () =>
      (await AgentsService.readVersions({ path: { id: agent } })).data,
    enabled: !!agent && open,
  })
  const cases = useQuery({
    queryKey: ["eval-cases", agent, "selection"],
    queryFn: async () =>
      (await EvalsService.readCases({ query: { agent_id: agent } })).data,
    enabled: !!agent && open,
  })
  const mutation = useMutation({
    mutationFn: (v: Values) =>
      EvalsService.createEvalRun({
        body: {
          agent_version_id: v.version,
          note: v.note,
          case_ids: v.mode === "manual" ? v.cases : null,
          tags:
            v.mode === "tags"
              ? v.tags
                  .split(",")
                  .map((t) => t.trim())
                  .filter(Boolean)
              : null,
        },
      }),
    onError: handleError.bind(showErrorToast),
    onSuccess: () => {
      showSuccessToast("Evaluation queued")
      setOpen(false)
      form.reset()
    },
    onSettled: () => queryClient.invalidateQueries({ queryKey: ["evals"] }),
  })
  return (
    <>
      <Button onClick={() => setOpen(true)}>New evaluation</Button>
      <Dialog open={open} onOpenChange={setOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>New evaluation</DialogTitle>
            <DialogDescription>
              Run active test cases against an immutable version. Costs exclude
              LLM judge calls; unknown prices remain unknown.
            </DialogDescription>
          </DialogHeader>
          <Form {...form}>
            <form
              className="space-y-4"
              onSubmit={form.handleSubmit((v) => mutation.mutate(v))}
            >
              <FormField
                control={form.control}
                name="agent"
                render={({ field }) => (
                  <FormItem>
                    <FormLabel>Agent</FormLabel>
                    <FormControl>
                      <select
                        className="w-full rounded border p-2"
                        {...field}
                        onChange={(e) => {
                          field.onChange(e)
                          form.setValue("version", "")
                          form.setValue("cases", [])
                        }}
                      >
                        <option value="">Select agent</option>
                        {agents.data?.data.map((a) => (
                          <option key={a.id} value={a.id}>
                            {a.name}
                          </option>
                        ))}
                      </select>
                    </FormControl>
                    <FormMessage />
                  </FormItem>
                )}
              />
              <FormField
                control={form.control}
                name="version"
                render={({ field }) => (
                  <FormItem>
                    <FormLabel>Version</FormLabel>
                    <FormControl>
                      <select className="w-full rounded border p-2" {...field}>
                        <option value="">Select version</option>
                        {versions.data?.data.map((v) => (
                          <option key={v.id} value={v.id}>
                            v{v.version_number}
                          </option>
                        ))}
                      </select>
                    </FormControl>
                    <FormMessage />
                  </FormItem>
                )}
              />
              <FormField
                control={form.control}
                name="mode"
                render={({ field }) => (
                  <FormItem>
                    <FormLabel>Cases</FormLabel>
                    <FormControl>
                      <select className="w-full rounded border p-2" {...field}>
                        <option value="all">All active cases</option>
                        <option value="tags">By tag</option>
                        <option value="manual">Manual selection</option>
                      </select>
                    </FormControl>
                  </FormItem>
                )}
              />
              {mode === "tags" && (
                <FormField
                  control={form.control}
                  name="tags"
                  render={({ field }) => (
                    <FormItem>
                      <FormLabel>Tags (comma separated)</FormLabel>
                      <FormControl>
                        <Input {...field} />
                      </FormControl>
                    </FormItem>
                  )}
                />
              )}
              {mode === "manual" && (
                <FormField
                  control={form.control}
                  name="cases"
                  render={({ field }) => (
                    <FormItem>
                      <FormLabel>Selected cases</FormLabel>
                      {cases.data?.data
                        .filter((c) => c.is_active)
                        .map((c) => (
                          <label key={c.id} className="flex gap-2">
                            <input
                              type="checkbox"
                              checked={field.value.includes(c.id)}
                              onChange={(e) =>
                                field.onChange(
                                  e.target.checked
                                    ? [...field.value, c.id]
                                    : field.value.filter((id) => id !== c.id),
                                )
                              }
                            />
                            {c.name}
                          </label>
                        ))}
                    </FormItem>
                  )}
                />
              )}
              <FormField
                control={form.control}
                name="note"
                render={({ field }) => (
                  <FormItem>
                    <FormLabel>Note</FormLabel>
                    <FormControl>
                      <Input {...field} />
                    </FormControl>
                    <FormMessage />
                  </FormItem>
                )}
              />
              <LoadingButton loading={mutation.isPending} type="submit">
                Start evaluation
              </LoadingButton>
            </form>
          </Form>
        </DialogContent>
      </Dialog>
    </>
  )
}
