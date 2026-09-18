import { useQuery } from "@tanstack/react-query"
import { Fragment, useId } from "react"
import { RunsService, type SearchResultPublic } from "@/client"

export type CitationChunk = SearchResultPublic & { index: number }

export default function Citations({
  content,
  runId,
  chunks,
}: {
  content: string
  runId?: string | null
  chunks?: CitationChunk[]
}) {
  const prefix = useId()
  const events = useQuery({
    queryKey: ["runs", runId, "events"],
    queryFn: async () => {
      const first = (
        await RunsService.readEvents({ path: { id: runId as string } })
      ).data
      while (first.data.length < first.count) {
        const page = (
          await RunsService.readEvents({
            path: { id: runId as string },
            query: { skip: first.data.length, limit: 100 },
          })
        ).data
        if (!page.data.length) break
        first.data.push(...page.data)
      }
      return first
    },
    enabled: !!runId && !chunks,
  })
  const saved = [...(events.data?.data ?? [])]
    .reverse()
    .find((event) => event.event_type === "context_retrieved")?.payload.chunks
  const sources =
    chunks ?? (Array.isArray(saved) ? (saved as CitationChunk[]) : [])
  const files = [...new Set(sources.map((source) => source.filename))]
  return (
    <>
      <div>
        {content.split(/(\[\d+\])/g).map((part, index) => {
          const match = /^\[(\d+)\]$/.exec(part)
          const source = match
            ? sources.find((chunk) => chunk.index === Number(match[1]))
            : undefined
          return source ? (
            <Fragment key={`${part}-${index}`}>
              <button
                type="button"
                popoverTarget={`${prefix}-${index}`}
                className="mx-1 cursor-pointer align-super text-xs text-primary underline"
                aria-label={`Source ${source.index}`}
              >
                {part}
              </button>
              <div
                id={`${prefix}-${index}`}
                popover="auto"
                className="m-auto max-h-[70vh] max-w-md overflow-auto rounded border bg-background p-4 text-foreground shadow-lg"
              >
                <strong>
                  {source.filename} · Chunk {source.seq}
                </strong>
                <p className="whitespace-pre-wrap">{source.content}</p>
              </div>
            </Fragment>
          ) : (
            part
          )
        })}
      </div>
      {files.length > 0 && (
        <p className="mt-3 border-t pt-2 text-xs text-muted-foreground">
          Sources: {files.join(", ")}
        </p>
      )}
    </>
  )
}
