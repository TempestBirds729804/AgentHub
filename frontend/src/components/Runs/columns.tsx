import { Link } from "@tanstack/react-router"
import type { ColumnDef } from "@tanstack/react-table"

import type { RunPublic } from "@/client"
import RunStatusBadge from "@/components/Runs/RunStatusBadge"
import { Button } from "@/components/ui/button"

export const columns: ColumnDef<RunPublic>[] = [
  {
    accessorKey: "status",
    header: "Status",
    cell: ({ row }) => <RunStatusBadge status={row.original.status} />,
  },
  {
    accessorKey: "agent_name",
    header: "Agent",
    cell: ({ row }) => (
      <div>
        <div>
          {row.original.agent_name ?? row.original.agent_version_id.slice(0, 8)}
        </div>
        <span className="text-xs text-muted-foreground">
          v{row.original.agent_version_number ?? "?"}
        </span>
      </div>
    ),
  },
  { accessorKey: "trigger", header: "Trigger" },
  {
    accessorKey: "duration_ms",
    header: "Duration",
    cell: ({ row }) =>
      row.original.duration_ms == null ? "—" : `${row.original.duration_ms} ms`,
  },
  {
    id: "tokens",
    header: "Tokens",
    cell: ({ row }) =>
      (
        row.original.prompt_tokens + row.original.completion_tokens
      ).toLocaleString(),
  },
  {
    accessorKey: "cost_usd",
    header: "Est. cost (USD)",
    cell: ({ row }) =>
      row.original.cost_usd == null
        ? "Unknown"
        : `$${Number(row.original.cost_usd).toFixed(6)}`,
  },
  {
    accessorKey: "created_at",
    header: "Created",
    cell: ({ row }) => new Date(row.original.created_at).toLocaleString(),
  },
  {
    id: "actions",
    header: "",
    cell: ({ row }) => (
      <Button variant="ghost" size="sm" asChild>
        <Link to="/runs/$runId" params={{ runId: row.original.id }}>
          View
        </Link>
      </Button>
    ),
  },
]
