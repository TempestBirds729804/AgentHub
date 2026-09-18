import { zodResolver } from "@hookform/resolvers/zod"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { createFileRoute, Link } from "@tanstack/react-router"
import { AxiosError } from "axios"
import { useState } from "react"
import { useForm } from "react-hook-form"
import { z } from "zod"
import { KnowledgeService } from "@/client"
import KnowledgeForm from "@/components/Knowledge/KnowledgeForm"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
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
import useCustomToast from "@/hooks/useCustomToast"
import { handleError } from "@/utils"

export const Route = createFileRoute("/_layout/knowledge/$kbId")({
  component: KnowledgeDetail,
  head: () => ({ meta: [{ title: "Knowledge base - AgentHub" }] }),
})
const searchSchema = z.object({
  query: z.string().trim().min(1).max(2000),
  top_k: z.number().int().min(1).max(20),
})

function KnowledgeDetail() {
  const { kbId } = Route.useParams()
  const cache = useQueryClient()
  const { showErrorToast, showSuccessToast } = useCustomToast()
  const [page, setPage] = useState(0)
  const kb = useQuery({
    queryKey: ["knowledge", kbId],
    queryFn: async () =>
      (await KnowledgeService.readKnowledge({ path: { id: kbId } })).data,
  })
  const documents = useQuery({
    queryKey: ["documents", kbId, page],
    queryFn: async () =>
      (
        await KnowledgeService.readDocuments({
          path: { id: kbId },
          query: { skip: page * 20, limit: 20 },
        })
      ).data,
    refetchInterval: (query) =>
      query.state.data?.data.some(
        (d) => d.status === "pending" || d.status === "processing",
      )
        ? 3000
        : false,
  })
  const refresh = () => {
    void cache.invalidateQueries({ queryKey: ["documents", kbId] })
    void cache.invalidateQueries({ queryKey: ["knowledge"] })
  }
  const upload = useMutation({
    mutationFn: async (file: File) => {
      if (file.size > 20 * 1024 * 1024)
        throw new AxiosError("File exceeds the 20 MB limit")
      const body = new FormData()
      body.append("file", file)
      const response = await fetch(
        `${import.meta.env.VITE_API_URL ?? ""}/api/v1/knowledge/${kbId}/documents`,
        {
          method: "POST",
          headers: {
            Authorization: `Bearer ${localStorage.getItem("access_token")}`,
          },
          body,
        },
      )
      if (!response.ok) {
        const body = await response.json()
        throw new AxiosError(
          typeof body.detail === "string"
            ? body.detail
            : "Document upload failed",
        )
      }
      return response.json()
    },
    onSuccess: () => {
      showSuccessToast("Document queued")
      setPage(0)
    },
    onError: handleError.bind(showErrorToast),
    onSettled: refresh,
  })
  const remove = useMutation({
    mutationFn: (doc_id: string) =>
      KnowledgeService.deleteDocument({ path: { id: kbId, doc_id } }),
    onSuccess: () => showSuccessToast("Document deleted"),
    onError: handleError.bind(showErrorToast),
    onSettled: refresh,
  })
  const retry = useMutation({
    mutationFn: (doc_id: string) =>
      KnowledgeService.reprocessDocument({ path: { id: kbId, doc_id } }),
    onSuccess: () => showSuccessToast("Document queued"),
    onError: handleError.bind(showErrorToast),
    onSettled: refresh,
  })
  const form = useForm<z.infer<typeof searchSchema>>({
    resolver: zodResolver(searchSchema),
    mode: "onBlur",
    defaultValues: { query: "", top_k: 5 },
  })
  const search = useMutation({
    mutationFn: (data: z.infer<typeof searchSchema>) =>
      KnowledgeService.searchKnowledge({ path: { id: kbId }, body: data }),
    onError: handleError.bind(showErrorToast),
  })
  return (
    <div className="space-y-6">
      <Link to="/knowledge" className="underline">
        Back to Knowledge
      </Link>
      <div className="flex justify-between">
        <div>
          <h1 className="text-2xl font-bold">
            {kb.data?.name ?? "Knowledge base"}
          </h1>
          <p>{kb.data?.description}</p>
        </div>
        {kb.data && <KnowledgeForm knowledge={kb.data} />}
      </div>
      {kb.isError && <p role="alert">Unable to load this knowledge base.</p>}
      <Tabs defaultValue="documents">
        <TabsList>
          <TabsTrigger value="documents">Documents</TabsTrigger>
          <TabsTrigger value="search">Search</TabsTrigger>
        </TabsList>
        <TabsContent value="documents" className="space-y-4">
          <section
            aria-label="Document upload"
            className="rounded-lg border-2 border-dashed p-6"
            onDragOver={(event) => event.preventDefault()}
            onDrop={(event) => {
              event.preventDefault()
              const file = event.dataTransfer.files[0]
              if (file && !upload.isPending) upload.mutate(file)
            }}
          >
            <label htmlFor="document-upload" className="block font-medium">
              Drop a document here or choose a file
            </label>
            <p className="text-sm text-muted-foreground">
              PDF (text layer required), TXT or Markdown. Maximum 20 MB.
            </p>
            <Input
              id="document-upload"
              type="file"
              accept=".pdf,.txt,.md,text/plain,text/markdown,application/pdf"
              disabled={upload.isPending}
              onChange={(event) => {
                const file = event.target.files?.[0]
                if (file) upload.mutate(file)
                event.target.value = ""
              }}
            />
            {upload.isPending && <p role="status">Uploading…</p>}
          </section>
          {documents.isError && <p role="alert">Unable to load documents.</p>}
          <Table>
            <TableHeader>
              <TableRow>
                {[
                  "Filename",
                  "Status",
                  "Size",
                  "Chunks",
                  "Created",
                  "Actions",
                ].map((label) => (
                  <TableHead key={label}>{label}</TableHead>
                ))}
              </TableRow>
            </TableHeader>
            <TableBody>
              {documents.data?.data.map((doc) => (
                <TableRow key={doc.id}>
                  <TableCell>{doc.filename}</TableCell>
                  <TableCell>
                    <Badge
                      variant={
                        doc.status === "failed"
                          ? "destructive"
                          : doc.status === "ready"
                            ? "default"
                            : "secondary"
                      }
                    >
                      {doc.status}
                    </Badge>
                    {doc.error && (
                      <span
                        role="img"
                        title={doc.error}
                        className="ml-2 text-destructive"
                        aria-label={doc.error}
                      >
                        ⚠
                      </span>
                    )}
                  </TableCell>
                  <TableCell>{(doc.size_bytes / 1024).toFixed(1)} KB</TableCell>
                  <TableCell>{doc.chunk_count}</TableCell>
                  <TableCell>
                    {new Date(doc.created_at).toLocaleString()}
                  </TableCell>
                  <TableCell className="space-x-2">
                    {doc.status === "failed" && (
                      <Button
                        size="sm"
                        variant="outline"
                        disabled={retry.isPending}
                        onClick={() => retry.mutate(doc.id)}
                      >
                        Reprocess
                      </Button>
                    )}
                    <Button
                      size="sm"
                      variant="destructive"
                      disabled={remove.isPending}
                      onClick={() => {
                        if (window.confirm(`Delete ${doc.filename}?`))
                          remove.mutate(doc.id)
                      }}
                    >
                      Delete
                    </Button>
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
          <div className="flex gap-3">
            <Button
              variant="outline"
              disabled={page === 0}
              onClick={() => setPage(page - 1)}
            >
              Previous
            </Button>
            <span>
              Page {page + 1} · {documents.data?.count ?? 0} documents
            </span>
            <Button
              variant="outline"
              disabled={(page + 1) * 20 >= (documents.data?.count ?? 0)}
              onClick={() => setPage(page + 1)}
            >
              Next
            </Button>
          </div>
        </TabsContent>
        <TabsContent value="search" className="space-y-4">
          <Form {...form}>
            <form
              className="space-y-4"
              onSubmit={form.handleSubmit((data) => search.mutate(data))}
            >
              <FormField
                control={form.control}
                name="query"
                render={({ field }) => (
                  <FormItem>
                    <FormLabel>Query</FormLabel>
                    <FormControl>
                      <Input {...field} />
                    </FormControl>
                    <FormMessage />
                  </FormItem>
                )}
              />
              <FormField
                control={form.control}
                name="top_k"
                render={({ field }) => (
                  <FormItem>
                    <FormLabel>Maximum results</FormLabel>
                    <FormControl>
                      <Input
                        type="number"
                        min={1}
                        max={20}
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
              <LoadingButton loading={search.isPending} type="submit">
                Search
              </LoadingButton>
            </form>
          </Form>
          {search.data?.data.data.map((result) => (
            <article key={result.chunk_id} className="rounded-lg border p-4">
              <h3 className="font-medium">
                {result.filename} · Chunk {result.seq}
              </h3>
              <div className="flex gap-3 items-center">
                <meter
                  min={0}
                  max={1}
                  value={Math.max(0, result.score)}
                  aria-label="Similarity"
                />
                {result.score.toFixed(3)}
              </div>
              <p className="whitespace-pre-wrap mt-3">{result.content}</p>
            </article>
          ))}
          {search.isSuccess && !search.data.data.count && (
            <p>No matching context found.</p>
          )}
        </TabsContent>
      </Tabs>
    </div>
  )
}
