import { useMutation, useQueryClient } from "@tanstack/react-query"
import type { ColumnDef } from "@tanstack/react-table"
import { ShieldCheck } from "lucide-react"
import { useState } from "react"

import { type ToolPublic, ToolsService } from "@/client"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog"
import { LoadingButton } from "@/components/ui/loading-button"
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip"
import useCustomToast from "@/hooks/useCustomToast"
import { handleError } from "@/utils"
import TestTool from "./TestTool"
import ToolForm from "./ToolForm"

export function ApprovalMark() {
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <span role="img" aria-label="Approval required">
          <ShieldCheck className="size-4" />
        </span>
      </TooltipTrigger>
      <TooltipContent>
        The agent pauses for your approval before each call.
      </TooltipContent>
    </Tooltip>
  )
}

function DeleteTool({ tool }: { tool: ToolPublic }) {
  const [open, setOpen] = useState(false)
  const queryClient = useQueryClient()
  const { showSuccessToast, showErrorToast } = useCustomToast()
  const mutation = useMutation({
    mutationFn: () => ToolsService.deleteTool({ path: { id: tool.id } }),
    onSuccess: () => {
      showSuccessToast("Tool deleted successfully")
      setOpen(false)
    },
    onError: handleError.bind(showErrorToast),
    onSettled: () => queryClient.invalidateQueries({ queryKey: ["tools"] }),
  })
  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger asChild>
        <Button size="sm" variant="ghost">
          Delete
        </Button>
      </DialogTrigger>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Delete {tool.name}?</DialogTitle>
          <DialogDescription>
            Tools bound to published versions cannot be deleted. Deactivate them
            using Edit.
          </DialogDescription>
        </DialogHeader>
        <LoadingButton
          variant="destructive"
          loading={mutation.isPending}
          onClick={() => mutation.mutate()}
        >
          Delete tool
        </LoadingButton>
      </DialogContent>
    </Dialog>
  )
}

export const columns: ColumnDef<ToolPublic>[] = [
  { accessorKey: "name", header: "Name" },
  {
    accessorKey: "tool_type",
    header: "Type",
    cell: ({ row }) => (
      <Badge variant="outline">{row.original.tool_type}</Badge>
    ),
  },
  {
    accessorKey: "description",
    header: "Description",
    cell: ({ row }) => (
      <span
        className="block max-w-80 truncate"
        title={row.original.description}
      >
        {row.original.description}
      </span>
    ),
  },
  {
    accessorKey: "requires_approval",
    header: "Approval",
    cell: ({ row }) =>
      row.original.requires_approval ? <ApprovalMark /> : "—",
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
    header: "Actions",
    cell: ({ row }) => (
      <div className="flex gap-2">
        <TestTool tool={row.original} />
        <ToolForm tool={row.original} />
        <DeleteTool tool={row.original} />
      </div>
    ),
  },
]
