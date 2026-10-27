import { useQuery } from "@tanstack/react-query"
import { createFileRoute, Link, redirect } from "@tanstack/react-router"
import { MonitoringService, UsersService } from "@/client"
import { money as cost, rate } from "@/components/Evals/Summary"

export const Route = createFileRoute("/_layout/monitoring")({
  component: Monitoring,
  beforeLoad: async () => {
    const { data } = await UsersService.readUserMe()
    if (!data.is_superuser) throw redirect({ to: "/" })
  },
  head: () => ({ meta: [{ title: "Monitoring - AgentHub" }] }),
})

function Monitoring() {
  const overview = useQuery({
    queryKey: ["monitoring", "overview"],
    queryFn: async () => (await MonitoringService.readOverview()).data,
    refetchInterval: 15000,
  })
  const usage = useQuery({
    queryKey: ["monitoring", "usage"],
    queryFn: async () => (await MonitoringService.readUsage()).data,
    refetchInterval: 15000,
  })
  const users = useQuery({
    queryKey: ["monitoring", "users"],
    queryFn: async () => (await MonitoringService.readTopUsers()).data,
    refetchInterval: 15000,
  })
  const failures = useQuery({
    queryKey: ["monitoring", "failures"],
    queryFn: async () => (await MonitoringService.readRecentFailures()).data,
    refetchInterval: 15000,
  })
  const system = useQuery({
    queryKey: ["monitoring", "system"],
    queryFn: async () => (await MonitoringService.readSystem()).data,
    refetchInterval: 15000,
    retry: false,
  })
  const o = overview.data
  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-bold">Monitoring</h1>
        <p className="text-muted-foreground">
          Platform activity and component health · refreshes every 15 seconds
        </p>
      </div>
      {overview.isPending && <p>Loading overview…</p>}
      {overview.isError && <p role="alert">Unable to load overview.</p>}
      {o && (
        <>
          <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
            {[
              ["Runs · 24 hours", o.runs_24h],
              ["Success · 24 hours", rate(o.success_rate_24h)],
              ["Cost · 30 days", cost(o.total_cost_usd_30d)],
              ["Active users · 7 days", o.active_users_7d],
            ].map(([label, value]) => (
              <section className="rounded-lg border p-4" key={label}>
                <h2 className="text-sm text-muted-foreground">{label}</h2>
                <p
                  className={`mt-2 text-2xl font-semibold ${label === "Success · 24 hours" && o.success_rate_24h != null && o.success_rate_24h < 0.9 ? "text-destructive" : ""}`}
                >
                  {value}
                </p>
              </section>
            ))}
          </div>
          <p className="text-sm text-muted-foreground">
            {o.total_users} users · {o.total_agents} agents · {o.total_runs}{" "}
            total runs · {o.total_tokens_30d.toLocaleString()} tokens in 30
            days. Cost is unknown if any run has no price; LLM judge costs are
            not included.
          </p>
          <div className="flex gap-6">
            <Link className="underline" to="/approvals">
              Pending approvals: {o.pending_approvals}
            </Link>
            <Link className="underline" to="/knowledge">
              Documents pending/processing: {o.documents_processing}
            </Link>
          </div>
        </>
      )}
      <section className="rounded-lg border p-4 space-y-4">
        <h2 className="font-semibold">Daily usage · 30 days · UTC</h2>
        {usage.isError && <p role="alert">Unable to load usage.</p>}
        {(["runs", "tokens"] as const).map((kind) => (
          <div key={kind}>
            <h3 className="capitalize text-sm">{kind}</h3>
            <div
              className="flex h-24 items-end gap-1"
              role="img"
              aria-label={`Daily ${kind}`}
            >
              {usage.data?.data.map((d) => (
                <div
                  className="flex-1 bg-primary/75 rounded-t min-h-px"
                  key={d.day}
                  title={`${d.day}: ${d[kind]}`}
                  style={{
                    height: `${(100 * d[kind]) / Math.max(1, ...usage.data.data.map((v) => v[kind]))}%`,
                  }}
                />
              ))}
            </div>
          </div>
        ))}
        <details>
          <summary className="cursor-pointer">View daily values</summary>
          <div className="overflow-auto">
            <table className="w-full text-sm">
              <thead>
                <tr>
                  <th className="text-left">Date (UTC)</th>
                  <th>Runs</th>
                  <th>Tokens</th>
                  <th>Cost</th>
                </tr>
              </thead>
              <tbody>
                {usage.data?.data.map((d) => (
                  <tr key={d.day}>
                    <td>{d.day}</td>
                    <td className="text-center">{d.runs}</td>
                    <td className="text-center">{d.tokens}</td>
                    <td className="text-center">{cost(d.cost)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </details>
      </section>
      <div className="grid gap-4 xl:grid-cols-2">
        <section className="rounded-lg border p-4 overflow-auto">
          <h2 className="font-semibold mb-3">Top users · 30 days</h2>
          {users.isError && <p role="alert">Unable to load users.</p>}
          <table className="w-full text-sm">
            <thead>
              <tr>
                <th className="text-left">Email</th>
                <th>Runs</th>
                <th>Tokens</th>
                <th>Cost</th>
              </tr>
            </thead>
            <tbody>
              {users.data?.data.map((u) => (
                <tr className="border-t" key={u.user_id}>
                  <td className="py-2">{u.email}</td>
                  <td className="text-center">{u.runs}</td>
                  <td className="text-center">{u.tokens}</td>
                  <td className="text-center">{cost(u.cost)}</td>
                </tr>
              ))}
            </tbody>
          </table>
          {users.data?.count === 0 && <p>No activity yet.</p>}
        </section>
        <section className="rounded-lg border p-4">
          <h2 className="font-semibold mb-3">Recent failures</h2>
          {failures.isError && <p role="alert">Unable to load failures.</p>}
          <ul className="max-h-80 overflow-auto space-y-3">
            {failures.data?.data.map((f) => (
              <li key={f.id} className="border-t pt-2 text-sm">
                <Link
                  className="underline font-medium"
                  to="/runs/$runId"
                  params={{ runId: f.id }}
                >
                  {f.agent_name}
                </Link>
                <p className="text-muted-foreground">
                  {f.email} · {new Date(f.created_at).toLocaleString()}
                </p>
                <p className="break-words">{f.error || "Unknown error"}</p>
              </li>
            ))}
          </ul>
          {failures.data?.count === 0 && <p>No failed runs.</p>}
        </section>
      </div>
      <section className="space-y-3">
        <h2 className="font-semibold">System health</h2>
        {system.isPending && <p>Checking components…</p>}
        {system.isError && (
          <p role="alert">
            Unable to check components. Business statistics remain available.
          </p>
        )}
        <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-5">
          {system.data?.components.map((c) => (
            <div key={c.name} className="rounded-lg border p-3">
              <h3 className="text-sm font-medium">{c.name}</h3>
              <p className={c.healthy ? "text-green-600" : "text-destructive"}>
                {c.healthy ? "Healthy" : "Unavailable"} · {c.latency_ms} ms
              </p>
              <p className="text-xs text-muted-foreground">{c.detail}</p>
            </div>
          ))}
        </div>
        <p className="text-sm">
          Queue depth: {system.data?.queue_depth ?? "Unknown"}
        </p>
      </section>
    </div>
  )
}
