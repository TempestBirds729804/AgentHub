import { useQuery } from "@tanstack/react-query"
import { ToolsService } from "@/client"
import { Badge } from "@/components/ui/badge"
import { Checkbox } from "@/components/ui/checkbox"
import { ApprovalMark } from "./columns"

export default function ToolSelection({
  value,
  onChange,
}: {
  value: string[]
  onChange: (ids: string[]) => void
}) {
  const query = useQuery({
    queryKey: ["tools", "active"],
    queryFn: async () =>
      (
        await ToolsService.readTools({
          query: { skip: 0, limit: 100, is_active: true },
        })
      ).data,
  })
  return (
    <fieldset className="space-y-3 rounded-lg border p-4">
      <legend className="px-1 text-sm font-medium">Tools</legend>
      <p className="text-xs text-muted-foreground">
        Save the configuration and publish a new version to apply tool
        selections.
      </p>
      {query.isPending && <p>Loading tools…</p>}
      {query.isError && <p role="alert">Unable to load tools.</p>}
      {query.data?.data.map((tool) => (
        <label
          key={tool.id}
          htmlFor={`tool-${tool.id}`}
          className="flex items-center gap-3 text-sm"
        >
          <Checkbox
            id={`tool-${tool.id}`}
            checked={value.includes(tool.id)}
            onCheckedChange={(checked) =>
              onChange(
                checked
                  ? [...value, tool.id]
                  : value.filter((id) => id !== tool.id),
              )
            }
          />
          <span>{tool.name}</span>
          <Badge variant="outline">{tool.tool_type}</Badge>
          {tool.requires_approval && <ApprovalMark />}
        </label>
      ))}
      {query.data && !query.data.count && (
        <p className="text-sm text-muted-foreground">
          Create an active tool on the Tools page.
        </p>
      )}
      {query.data &&
        value
          .filter((id) => !query.data.data.some((tool) => tool.id === id))
          .map((id) => (
            <label
              key={id}
              htmlFor={`tool-${id}`}
              className="flex items-center gap-3 text-sm text-muted-foreground"
            >
              <Checkbox
                id={`tool-${id}`}
                checked
                onCheckedChange={() =>
                  onChange(value.filter((selected) => selected !== id))
                }
              />
              Unavailable or inactive tool ({id.slice(0, 8)})
            </label>
          ))}
    </fieldset>
  )
}
