import type { ColumnDef } from "@tanstack/react-table"

import type { AgentPublic } from "@/client"
import { AgentActionsMenu } from "@/components/Agents/AgentActionsMenu"
import { Badge } from "@/components/ui/badge"

export const columns: ColumnDef<AgentPublic>[] = [
  {
    accessorKey: "name",
    header: "Name",
    cell: ({ row }) => <span className="font-medium">{row.original.name}</span>,
  },
  {
    accessorKey: "llm_model",
    header: "Model",
    cell: ({ row }) => row.original.llm_model || "Not configured",
  },
  {
    accessorKey: "latest_version_number",
    header: "Version",
    cell: ({ row }) =>
      row.original.latest_version_number ? (
        <Badge variant="secondary">v{row.original.latest_version_number}</Badge>
      ) : (
        <Badge variant="outline">Draft</Badge>
      ),
  },
  {
    accessorKey: "is_active",
    header: "Status",
    cell: ({ row }) => (
      <Badge variant={row.original.is_active ? "default" : "secondary"}>
        {row.original.is_active ? "Active" : "Inactive"}
      </Badge>
    ),
  },
  {
    id: "actions",
    header: () => <span className="sr-only">Actions</span>,
    cell: ({ row }) => (
      <div className="flex justify-end">
        <AgentActionsMenu agent={row.original} />
      </div>
    ),
  },
]
