import { useNavigate } from "@tanstack/react-router"
import { EllipsisVertical, ExternalLink } from "lucide-react"
import { useState } from "react"

import type { AgentPublic } from "@/client"
import { Button } from "@/components/ui/button"
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu"
import DeleteAgent from "./DeleteAgent"
import EditAgent from "./EditAgent"

export const AgentActionsMenu = ({ agent }: { agent: AgentPublic }) => {
  const [open, setOpen] = useState(false)
  const navigate = useNavigate()

  return (
    <DropdownMenu open={open} onOpenChange={setOpen}>
      <DropdownMenuTrigger asChild>
        <Button
          variant="ghost"
          size="icon"
          data-testid={`agent-actions-${agent.id}`}
        >
          <EllipsisVertical />
          <span className="sr-only">Agent actions</span>
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end">
        <DropdownMenuItem
          onClick={() =>
            navigate({
              to: "/agents/$agentId",
              params: { agentId: agent.id },
            })
          }
        >
          <ExternalLink /> Open
        </DropdownMenuItem>
        <EditAgent agent={agent} onSuccess={() => setOpen(false)} />
        <DeleteAgent id={agent.id} onSuccess={() => setOpen(false)} />
      </DropdownMenuContent>
    </DropdownMenu>
  )
}
