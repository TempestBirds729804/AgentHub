import type { RunStatus } from "@/client"
import { Badge } from "@/components/ui/badge"

const statusVariant: Record<
  RunStatus,
  "default" | "secondary" | "destructive" | "outline"
> = {
  queued: "outline",
  running: "secondary",
  waiting_approval: "outline",
  succeeded: "default",
  failed: "destructive",
  cancelled: "secondary",
}

export default function RunStatusBadge({ status }: { status: RunStatus }) {
  return (
    <Badge variant={statusVariant[status]}>{status.replace(/_/g, " ")}</Badge>
  )
}
