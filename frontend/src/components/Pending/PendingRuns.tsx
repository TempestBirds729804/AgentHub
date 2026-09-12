import { Skeleton } from "@/components/ui/skeleton"

export default function PendingRuns() {
  return (
    <div role="status" aria-label="Loading runs" className="space-y-4">
      {Array.from({ length: 5 }, (_, index) => (
        <Skeleton key={index} className="h-12 w-full" />
      ))}
    </div>
  )
}
