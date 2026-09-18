import { zodResolver } from "@hookform/resolvers/zod"
import { useMutation, useQueryClient } from "@tanstack/react-query"
import { Link } from "@tanstack/react-router"
import { ShieldCheck } from "lucide-react"
import { useState } from "react"
import { useForm } from "react-hook-form"
import { z } from "zod"

import {
  type ApprovalRequestPublic,
  type ApprovalStatus,
  ApprovalsService,
} from "@/client"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
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
import { LoadingButton } from "@/components/ui/loading-button"
import { Textarea } from "@/components/ui/textarea"
import useCustomToast from "@/hooks/useCustomToast"
import { handleError } from "@/utils"

export function approvalsQuery(runId?: string) {
  return {
    queryKey: ["approvals", runId ?? "pending"],
    queryFn: async () => {
      const results: ApprovalRequestPublic[] = []
      const statuses: ApprovalStatus[] = runId
        ? ["pending", "approved", "rejected", "expired"]
        : ["pending"]
      for (const status of statuses) {
        let skip = 0
        while (true) {
          const { data } = await ApprovalsService.readApprovals({
            query: { status, run_id: runId, skip, limit: 100 },
          })
          results.push(...data.data)
          skip += data.data.length
          if (skip >= data.count || !data.data.length) break
        }
      }
      return results
    },
    refetchInterval: 5000,
  }
}

const schema = z.object({
  reason: z.string().trim().min(1, "A rejection reason is required").max(1000),
})

export default function ApprovalGroups({
  requests,
  onDecision,
}: {
  requests: ApprovalRequestPublic[]
  onDecision?: () => void
}) {
  const queryClient = useQueryClient()
  const { showSuccessToast, showErrorToast } = useCustomToast()
  const [rejectIds, setRejectIds] = useState<string[]>([])
  const form = useForm<z.infer<typeof schema>>({
    resolver: zodResolver(schema),
    mode: "onBlur",
    defaultValues: { reason: "" },
  })
  const mutation = useMutation({
    mutationFn: async ({
      ids,
      approved,
      reason,
    }: {
      ids: string[]
      approved: boolean
      reason?: string
    }) => {
      for (const id of ids)
        await ApprovalsService.decideApproval({
          path: { id },
          body: { approved, rejection_reason: reason },
        })
    },
    onSuccess: () => {
      showSuccessToast("Decision saved")
      setRejectIds([])
      form.reset()
    },
    onError: handleError.bind(showErrorToast),
    onSettled: async () => {
      await queryClient.invalidateQueries({ queryKey: ["approvals"] })
      await queryClient.invalidateQueries({ queryKey: ["runs"] })
      onDecision?.()
    },
  })
  const groups = new Map<string, ApprovalRequestPublic[]>()
  for (const request of requests) {
    const group = groups.get(request.run_id) ?? []
    group.push(request)
    groups.set(request.run_id, group)
  }
  return (
    <div className="space-y-4">
      {[...groups].map(([runId, group]) => {
        const pending = group.filter((request) => request.status === "pending")
        return (
          <Card key={runId} data-testid="approval-group">
            <CardHeader>
              <CardTitle className="flex items-center gap-2">
                <ShieldCheck className="size-4" />
                {group[0].agent_name ?? "Tool approval"}
              </CardTitle>
              <Link
                to="/runs/$runId"
                params={{ runId }}
                className="break-all text-xs underline"
              >
                Run: {runId}
              </Link>
              <p className="text-sm text-muted-foreground">
                {pending.length
                  ? `Waiting for the remaining ${pending.length} decisions before resuming.`
                  : "All requests decided."}
              </p>
            </CardHeader>
            <CardContent className="space-y-4">
              {group.map((request) => (
                <div
                  key={request.id}
                  className="space-y-2 rounded-lg border p-3"
                  data-testid="approval-request"
                >
                  <p className="font-medium">{request.tool_name}</p>
                  <details>
                    <summary className="cursor-pointer text-sm">
                      Tool arguments
                    </summary>
                    <pre className="mt-2 max-h-56 overflow-auto whitespace-pre-wrap break-all rounded bg-muted p-3 text-xs">
                      {JSON.stringify(request.tool_args, null, 2)}
                    </pre>
                  </details>
                  {request.reason && (
                    <p className="text-sm text-amber-700 dark:text-amber-400">
                      {request.reason}
                    </p>
                  )}
                  {request.status === "pending" ? (
                    <div className="flex gap-2">
                      <LoadingButton
                        loading={mutation.isPending}
                        onClick={() =>
                          mutation.mutate({ ids: [request.id], approved: true })
                        }
                      >
                        Approve
                      </LoadingButton>
                      <Button
                        variant="outline"
                        disabled={mutation.isPending}
                        onClick={() => setRejectIds([request.id])}
                      >
                        Reject
                      </Button>
                    </div>
                  ) : (
                    <p className="break-all text-sm" role="status">
                      {request.status}
                      {request.resolved_by_id &&
                        ` · Decided by ${request.resolved_by_id}`}
                      {request.rejection_reason &&
                        ` · ${request.rejection_reason}`}
                    </p>
                  )}
                </div>
              ))}
              {pending.length > 1 && (
                <div className="flex gap-2">
                  <Button
                    disabled={mutation.isPending}
                    onClick={() =>
                      mutation.mutate({
                        ids: pending.map((r) => r.id),
                        approved: true,
                      })
                    }
                  >
                    Approve all
                  </Button>
                  <Button
                    variant="outline"
                    disabled={mutation.isPending}
                    onClick={() => setRejectIds(pending.map((r) => r.id))}
                  >
                    Reject all
                  </Button>
                </div>
              )}
            </CardContent>
          </Card>
        )
      })}
      <Dialog
        open={rejectIds.length > 0}
        onOpenChange={(open) => {
          if (!open && !mutation.isPending) {
            setRejectIds([])
            form.reset()
          }
        }}
      >
        <DialogContent>
          <DialogHeader>
            <DialogTitle>
              Reject tool call{rejectIds.length > 1 ? "s" : ""}
            </DialogTitle>
            <DialogDescription>
              Explain why. The agent receives your reason and can choose another
              approach.
            </DialogDescription>
          </DialogHeader>
          <Form {...form}>
            <form
              onSubmit={form.handleSubmit(({ reason }) =>
                mutation.mutate({ ids: rejectIds, approved: false, reason }),
              )}
              className="space-y-4"
            >
              <FormField
                control={form.control}
                name="reason"
                render={({ field }) => (
                  <FormItem>
                    <FormLabel>Rejection reason</FormLabel>
                    <FormControl>
                      <Textarea {...field} maxLength={1000} />
                    </FormControl>
                    <FormMessage />
                  </FormItem>
                )}
              />
              <LoadingButton type="submit" loading={mutation.isPending}>
                Confirm rejection
              </LoadingButton>
            </form>
          </Form>
        </DialogContent>
      </Dialog>
    </div>
  )
}
