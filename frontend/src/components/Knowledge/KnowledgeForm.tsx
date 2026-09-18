import { zodResolver } from "@hookform/resolvers/zod"
import { useMutation, useQueryClient } from "@tanstack/react-query"
import { useState } from "react"
import { useForm } from "react-hook-form"
import { z } from "zod"
import { type KnowledgeBasePublic, KnowledgeService } from "@/client"
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

const schema = z
  .object({
    name: z.string().trim().min(1).max(255),
    description: z.string().max(1000),
    chunk_size: z.number().int().min(100).max(4000),
    chunk_overlap: z.number().int().min(0).max(1000),
  })
  .refine((v) => v.chunk_overlap < v.chunk_size, {
    path: ["chunk_overlap"],
    message: "Overlap must be smaller than chunk size",
  })

export default function KnowledgeForm({
  knowledge,
}: {
  knowledge?: KnowledgeBasePublic
}) {
  const [open, setOpen] = useState(false)
  const cache = useQueryClient()
  const { showSuccessToast, showErrorToast } = useCustomToast()
  const form = useForm<z.infer<typeof schema>>({
    resolver: zodResolver(schema),
    mode: "onBlur",
    defaultValues: {
      name: knowledge?.name ?? "",
      description: knowledge?.description ?? "",
      chunk_size: knowledge?.chunk_size ?? 1000,
      chunk_overlap: knowledge?.chunk_overlap ?? 200,
    },
  })
  const mutation = useMutation({
    mutationFn: (data: z.infer<typeof schema>) =>
      knowledge
        ? KnowledgeService.updateKnowledge({
            path: { id: knowledge.id },
            body: { name: data.name, description: data.description },
          })
        : KnowledgeService.createKnowledge({ body: data }),
    onSuccess: () => {
      showSuccessToast("Knowledge base saved")
      setOpen(false)
      if (!knowledge) form.reset()
    },
    onError: handleError.bind(showErrorToast),
    onSettled: () => cache.invalidateQueries({ queryKey: ["knowledge"] }),
  })
  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger asChild>
        <Button variant={knowledge ? "outline" : "default"}>
          {knowledge ? "Edit knowledge base" : "Add Knowledge Base"}
        </Button>
      </DialogTrigger>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>
            {knowledge ? "Edit knowledge base" : "Add Knowledge Base"}
          </DialogTitle>
          <DialogDescription>
            Chunk size is measured in characters. Chunk settings cannot be
            changed after creation.
          </DialogDescription>
        </DialogHeader>
        <Form {...form}>
          <form
            className="space-y-4"
            onSubmit={form.handleSubmit((data) => mutation.mutate(data))}
          >
            {(["name", "description"] as const).map((name) => (
              <FormField
                key={name}
                control={form.control}
                name={name}
                render={({ field }) => (
                  <FormItem>
                    <FormLabel>
                      {name === "name" ? "Name" : "Description"}
                    </FormLabel>
                    <FormControl>
                      <Input {...field} />
                    </FormControl>
                    <FormMessage />
                  </FormItem>
                )}
              />
            ))}
            {!knowledge &&
              (["chunk_size", "chunk_overlap"] as const).map((name) => (
                <FormField
                  key={name}
                  control={form.control}
                  name={name}
                  render={({ field }) => (
                    <FormItem>
                      <FormLabel>
                        {name === "chunk_size" ? "Chunk size" : "Chunk overlap"}
                      </FormLabel>
                      <FormControl>
                        <Input
                          type="number"
                          {...field}
                          onChange={(event) =>
                            field.onChange(Number(event.target.value))
                          }
                        />
                      </FormControl>
                      <FormMessage />
                    </FormItem>
                  )}
                />
              ))}
            <LoadingButton type="submit" loading={mutation.isPending}>
              Save
            </LoadingButton>
          </form>
        </Form>
      </DialogContent>
    </Dialog>
  )
}
