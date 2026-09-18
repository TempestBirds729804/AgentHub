import { useQuery } from "@tanstack/react-query"
import { KnowledgeService } from "@/client"
import { Checkbox } from "@/components/ui/checkbox"

export default function KnowledgeSelection({
  value,
  onChange,
}: {
  value: string[]
  onChange: (ids: string[]) => void
}) {
  const query = useQuery({
    queryKey: ["knowledge"],
    queryFn: async () => (await KnowledgeService.readKnowledgeBases()).data,
  })
  return (
    <fieldset className="space-y-3 rounded-lg border p-4">
      <legend className="px-1 text-sm font-medium">Knowledge Bases</legend>
      <p className="text-xs text-muted-foreground">
        Save and publish a new version to apply selections.
      </p>
      {query.isPending && <p>Loading knowledge bases…</p>}
      {query.isError && <p role="alert">Unable to load knowledge bases.</p>}
      {query.data?.data.map((kb) => (
        <label
          key={kb.id}
          htmlFor={`kb-${kb.id}`}
          className="flex items-center gap-3 text-sm"
        >
          <Checkbox
            id={`kb-${kb.id}`}
            checked={value.includes(kb.id)}
            onCheckedChange={(checked) =>
              onChange(
                checked
                  ? [...value, kb.id]
                  : value.filter((id) => id !== kb.id),
              )
            }
          />
          <span>
            {kb.name} — {kb.ready_document_count} ready documents
          </span>
          {!kb.ready_document_count && (
            <span className="text-amber-700">No searchable content</span>
          )}
        </label>
      ))}
      {query.data &&
        value
          .filter((id) => !query.data.data.some((kb) => kb.id === id))
          .map((id) => (
            <label key={id} htmlFor={`kb-missing-${id}`} className="flex gap-3">
              <Checkbox
                id={`kb-missing-${id}`}
                checked
                onCheckedChange={() => onChange(value.filter((v) => v !== id))}
              />
              Unavailable knowledge base ({id.slice(0, 8)})
            </label>
          ))}
    </fieldset>
  )
}
