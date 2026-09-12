import { zodResolver } from "@hookform/resolvers/zod"
import { useMutation, useQueryClient } from "@tanstack/react-query"
import { useNavigate } from "@tanstack/react-router"
import { useState } from "react"
import { useForm } from "react-hook-form"
import { z } from "zod"

import { RunsService } from "@/client"
import { Button } from "@/components/ui/button"
import {
  Dialog,
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
import { LoadingButton } from "@/components/ui/loading-button"
import { Textarea } from "@/components/ui/textarea"
import useCustomToast from "@/hooks/useCustomToast"
import { handleError } from "@/utils"

const schema = z.object({
  message: z.string().trim().min(1, "Message is required"),
})
type RunForm = z.infer<typeof schema>

export default function RunAgent({
  versionId,
  versionNumber,
}: {
  versionId: string
  versionNumber: number
}) {
  const [isOpen, setIsOpen] = useState(false)
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const { showErrorToast } = useCustomToast()
  const form = useForm<RunForm>({
    resolver: zodResolver(schema),
    mode: "onBlur",
    defaultValues: { message: "" },
  })
  const mutation = useMutation({
    mutationFn: (data: RunForm) =>
      RunsService.createRun({
        body: {
          agent_version_id: versionId,
          input: { message: data.message },
          trigger: "playground",
        },
      }),
    onSuccess: (response) => {
      form.reset()
      setIsOpen(false)
      navigate({ to: "/runs/$runId", params: { runId: response.data.id } })
    },
    onError: handleError.bind(showErrorToast),
    onSettled: () => {
      queryClient.invalidateQueries({ queryKey: ["runs"] })
    },
  })
  return (
    <Dialog
      open={isOpen}
      onOpenChange={(open) => {
        if (!mutation.isPending) setIsOpen(open)
      }}
    >
      <DialogTrigger asChild>
        <Button variant="outline" size="sm" data-testid="run-version-button">
          Run
        </Button>
      </DialogTrigger>
      <DialogContent
        onEscapeKeyDown={(event) => {
          if (mutation.isPending) event.preventDefault()
        }}
        onInteractOutside={(event) => {
          if (mutation.isPending) event.preventDefault()
        }}
      >
        <DialogHeader>
          <DialogTitle>Run version {versionNumber}</DialogTitle>
          <DialogDescription>
            Send a message to this published version. Execution may take several
            seconds.
          </DialogDescription>
        </DialogHeader>
        <Form {...form}>
          <form
            className="space-y-4"
            onSubmit={form.handleSubmit((data) => mutation.mutate(data))}
          >
            <FormField
              control={form.control}
              name="message"
              render={({ field }) => (
                <FormItem>
                  <FormLabel>Message</FormLabel>
                  <FormControl>
                    <Textarea
                      {...field}
                      data-testid="run-message-input"
                      disabled={mutation.isPending}
                    />
                  </FormControl>
                  <FormMessage />
                </FormItem>
              )}
            />
            {mutation.isPending && (
              <p role="status" className="text-sm text-muted-foreground">
                Execution in progress. Please keep this dialog open.
              </p>
            )}
            <DialogFooter>
              <LoadingButton
                type="submit"
                loading={mutation.isPending}
                data-testid="run-submit-button"
              >
                Run
              </LoadingButton>
            </DialogFooter>
          </form>
        </Form>
      </DialogContent>
    </Dialog>
  )
}
