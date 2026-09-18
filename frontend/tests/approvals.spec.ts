import { expect, test } from "@playwright/test"
import "./config"

for (const decision of ["approve", "reject", "cancel"] as const) {
  test(`real MCP Playground approval: ${decision}`, async ({ page }) => {
    test.skip(
      process.env.APPROVAL_LIVE_TEST !== "1",
      "Requires isolated API, worker, MCP service and model",
    )
    test.setTimeout(180000)
    await page.goto("/agents")
    const token = await page.evaluate(() =>
      localStorage.getItem("access_token"),
    )
    const headers = { Authorization: `Bearer ${token}` }
    const api = process.env.VITE_API_URL ?? "http://127.0.0.1:8000"
    const suffix = `${Date.now()}_${decision}`
    const toolName = `read_docs_${suffix}`
    const toolResponse = await page.request.post(`${api}/api/v1/tools/`, {
      headers,
      data: {
        name: toolName,
        description: "Read the AgentHub development document by ID",
        tool_type: "mcp",
        requires_approval: true,
        config: {
          transport: "streamable_http",
          url: process.env.MCP_SERVICE_URL ?? "http://127.0.0.1:3007/mcp",
          tool_name: "read_dev_doc",
        },
        parameters_schema: {
          type: "object",
          properties: { doc_id: { type: "string" } },
          required: ["doc_id"],
        },
      },
    })
    expect(toolResponse.ok(), await toolResponse.text()).toBeTruthy()
    const tool = await toolResponse.json()
    const agentResponse = await page.request.post(`${api}/api/v1/agents/`, {
      headers,
      data: {
        name: `Approval ${suffix}`,
        llm_model: process.env.LLM_MODEL ?? "deepseek-flash",
        tool_ids: [tool.id],
        timeout_seconds: 120,
        system_prompt: `Call ${toolName} exactly once with doc_id 06-async-worker before answering. If the tool call is rejected, do not retry or call any tool: explain the refusal and offer an alternative in Chinese. Otherwise summarize the document in 2 short Chinese sentences.`,
      },
    })
    expect(agentResponse.ok(), await agentResponse.text()).toBeTruthy()
    const agent = await agentResponse.json()
    const version = await page.request.post(
      `${api}/api/v1/agents/${agent.id}/versions`,
      { headers, data: {} },
    )
    expect(version.ok()).toBeTruthy()
    const conversationResponse = await page.request.post(
      `${api}/api/v1/conversations/`,
      { headers, data: { agent_id: agent.id } },
    )
    const conversation = await conversationResponse.json()
    await page.goto(`/playground?conversation=${conversation.id}`)
    await page
      .getByTestId("playground-message")
      .fill("请调用工具读取06阶段文档，并简短说明恢复职责。")
    await page.getByTestId("send-message").click()
    const group = page.getByTestId("approval-group")
    await expect(group).toBeVisible({ timeout: 60000 })
    await expect(page.getByTestId("send-message")).toBeDisabled()
    await expect(page.getByTestId("approval-count")).toHaveText("1")
    const pending = await (
      await page.request.get(`${api}/api/v1/approvals/`, { headers })
    ).json()
    const request = pending.data.find(
      (r: { tool_name: string }) => r.tool_name === toolName,
    )
    expect(request).toBeTruthy()
    const runId = request.run_id
    const before = await (
      await page.request.get(`${api}/api/v1/runs/${runId}/events`, { headers })
    ).json()
    expect(
      before.data.filter(
        (e: { event_type: string }) => e.event_type === "tool_called",
      ),
    ).toHaveLength(0)
    await group.getByText("Tool arguments", { exact: true }).click()
    await expect(
      group.getByText('"doc_id": "06-async-worker"', { exact: false }),
    ).toBeVisible()
    await page.screenshot({
      path: test.info().outputPath(`${decision}-pending.png`),
      fullPage: true,
    })
    if (decision === "cancel") {
      await page.goto(`/runs/${runId}`)
      await expect(
        page.getByText("Waiting for approval", { exact: true }),
      ).toBeVisible()
      await page.getByRole("button", { name: "Cancel", exact: true }).click()
      await expect
        .poll(
          async () =>
            (
              await (
                await page.request.get(
                  `${api}/api/v1/approvals/${request.id}`,
                  { headers },
                )
              ).json()
            ).status,
        )
        .toBe("expired")
    } else {
      const cursors: string[] = []
      page.on("request", (r) => {
        if (r.url().includes(`/runs/${runId}/stream`))
          cursors.push(
            new URL(r.url()).searchParams.get("last_event_id") ?? "missing",
          )
      })
      if (decision === "approve") {
        await group
          .getByRole("button", { name: "Approve", exact: true })
          .click()
      } else {
        // Pending state and inline actions survive a page reload.
        await page.reload()
        await expect(group).toBeVisible()
        await group.getByRole("button", { name: "Reject", exact: true }).click()
        await page
          .getByLabel("Rejection reason")
          .fill("too risky，请不要读取文档")
        await page.getByRole("button", { name: "Confirm rejection" }).click()
      }
      await expect(page.getByTestId("assistant-message").last()).toBeVisible({
        timeout: 90000,
      })
      await expect(page.getByTestId("send-message")).toBeEnabled({
        timeout: 90000,
      })
      await expect(group.getByRole("status")).toContainText(
        decision === "approve" ? "approved" : "rejected",
      )
      await expect(page.getByTestId("approval-count")).toHaveCount(0)
      expect(cursors.length).toBeGreaterThan(0)
      if (decision === "approve") expect(cursors[0]).not.toBe("0")
      await page.getByText(/Execution events/).click()
      const trace = page
        .locator("details")
        .filter({ has: page.getByText(/Execution events/) })
        .first()
      if (decision === "approve")
        await expect(
          trace.locator("pre").filter({ hasText: /^tool_called:/ }),
        ).toHaveCount(1)
      await page.screenshot({
        path: test.info().outputPath(`${decision}-finished.png`),
        fullPage: true,
      })
    }
    const run = await (
      await page.request.get(`${api}/api/v1/runs/${runId}`, { headers })
    ).json()
    const events = await (
      await page.request.get(`${api}/api/v1/runs/${runId}/events`, { headers })
    ).json()
    expect(run.status).toBe(decision === "cancel" ? "cancelled" : "succeeded")
    expect(
      events.data.filter(
        (e: { event_type: string }) => e.event_type === "tool_called",
      ),
    ).toHaveLength(decision === "approve" ? 1 : 0)
    if (decision === "reject")
      expect(run.output.content).toMatch(/拒绝|批准|授权|无法|不能|未.*读取/)
    await test.info().attach("run.json", {
      body: JSON.stringify({ run, events: events.data }, null, 2),
      contentType: "application/json",
    })
  })
}

test("approval center groups calls and resumes only after the final decision", async ({
  page,
}) => {
  const now = new Date().toISOString()
  const requests = [0, 1].map((i) => ({
    id: `request-${i}`,
    run_id: "run-group",
    owner_id: "owner",
    tool_name: `tool_${i}`,
    tool_call_id: `call-${i}`,
    tool_args: { value: i },
    reason: "Requires approval",
    status: "pending",
    created_at: now,
    agent_name: "Parallel agent",
  }))
  await page.route("**/api/v1/approvals/**", async (route) => {
    const url = new URL(route.request().url())
    if (url.pathname.endsWith("/decide")) {
      const request = requests.find((r) => url.pathname.includes(r.id))!
      request.status = route.request().postDataJSON().approved
        ? "approved"
        : "rejected"
      return route.fulfill({ json: request })
    }
    const data = requests.filter((r) => r.status === "pending")
    return route.fulfill({
      json: {
        data: data.slice(0, Number(url.searchParams.get("limit") ?? 100)),
        count: data.length,
      },
    })
  })
  await page.goto("/approvals")
  await expect(page.getByTestId("approval-group")).toHaveCount(1)
  await expect(page.getByTestId("approval-count")).toHaveText("2")
  await page
    .getByRole("button", { name: "Approve", exact: true })
    .first()
    .click()
  await expect(
    page.getByText("Waiting for the remaining 1 decisions before resuming."),
  ).toBeVisible()
  await expect(page.getByTestId("approval-count")).toHaveText("1")
  await page.getByRole("button", { name: "Reject", exact: true }).click()
  await page.getByRole("button", { name: "Confirm rejection" }).click()
  await expect(page.getByText("A rejection reason is required")).toBeVisible()
  await page.getByLabel("Rejection reason").fill("too risky")
  await page.getByRole("button", { name: "Confirm rejection" }).click()
  await expect(page.getByText("No pending approvals.")).toBeVisible()
  await expect(page.getByTestId("approval-count")).toHaveCount(0)
  for (const bulk of ["approve", "reject"]) {
    for (const request of requests) request.status = "pending"
    await page.reload()
    await expect(page.getByTestId("approval-count")).toHaveText("2")
    if (bulk === "approve")
      await page
        .getByRole("button", { name: "Approve all", exact: true })
        .click()
    else {
      await page
        .getByRole("button", { name: "Reject all", exact: true })
        .click()
      await page.getByLabel("Rejection reason").fill("Reject both calls")
      await page.getByRole("button", { name: "Confirm rejection" }).click()
    }
    await expect(page.getByText("No pending approvals.")).toBeVisible()
    expect(
      requests.every(
        (request) =>
          request.status === (bulk === "approve" ? "approved" : "rejected"),
      ),
    ).toBeTruthy()
  }
})

test("real parallel calls wait for every decision in the approval center", async ({
  page,
}) => {
  test.skip(
    process.env.APPROVAL_LIVE_TEST !== "1",
    "Requires isolated API, worker and model",
  )
  test.setTimeout(120000)
  await page.goto("/agents")
  const token = await page.evaluate(() => localStorage.getItem("access_token"))
  const headers = { Authorization: `Bearer ${token}` }
  const api = process.env.VITE_API_URL ?? "http://127.0.0.1:8000"
  const name = `parallel_calc_${Date.now()}`
  const toolResponse = await page.request.post(`${api}/api/v1/tools/`, {
    headers,
    data: {
      name,
      description: "Calculate arithmetic",
      tool_type: "function",
      config: { function_name: "calculator" },
      requires_approval: true,
      parameters_schema: {
        type: "object",
        properties: { expression: { type: "string" } },
        required: ["expression"],
      },
    },
  })
  expect(toolResponse.ok()).toBeTruthy()
  const tool = await toolResponse.json()
  const agentResponse = await page.request.post(`${api}/api/v1/agents/`, {
    headers,
    data: {
      name,
      llm_model: process.env.LLM_MODEL ?? "deepseek-flash",
      tool_ids: [tool.id],
      system_prompt: `In your first response issue exactly two parallel tool calls to ${name}: one with expression 20+22 and one with expression 30+12. Both must be in the same response, do not wait for the first result. After receiving both results, give a short answer in Chinese. Never retry a rejected call and never call tools in later responses.`,
    },
  })
  expect(agentResponse.ok()).toBeTruthy()
  const agent = await agentResponse.json()
  const version = await (
    await page.request.post(`${api}/api/v1/agents/${agent.id}/versions`, {
      headers,
      data: {},
    })
  ).json()
  const run = await (
    await page.request.post(`${api}/api/v1/runs/async`, {
      headers,
      data: {
        agent_version_id: version.id,
        input: {
          message: "请在同一轮并行调用两次工具，分别计算20+22和30+12。",
        },
      },
    })
  ).json()
  await expect
    .poll(
      async () =>
        (
          await (
            await page.request.get(`${api}/api/v1/runs/${run.id}`, { headers })
          ).json()
        ).status,
      { timeout: 60000 },
    )
    .toBe("waiting_approval")
  await page.goto("/approvals")
  const group = page.getByTestId("approval-group").filter({ hasText: run.id })
  await expect(group.getByTestId("approval-request")).toHaveCount(2)
  await page.screenshot({
    path: test.info().outputPath("parallel-pending.png"),
    fullPage: true,
  })
  await group
    .getByRole("button", { name: "Approve", exact: true })
    .first()
    .click()
  await expect(
    group.getByText("Waiting for the remaining 1 decisions before resuming."),
  ).toBeVisible()
  expect(
    (
      await (
        await page.request.get(`${api}/api/v1/runs/${run.id}`, { headers })
      ).json()
    ).status,
  ).toBe("waiting_approval")
  const before = await (
    await page.request.get(`${api}/api/v1/runs/${run.id}/events`, { headers })
  ).json()
  expect(
    before.data.filter(
      (e: { event_type: string }) => e.event_type === "tool_called",
    ),
  ).toHaveLength(0)
  await group.getByRole("button", { name: "Reject", exact: true }).click()
  await page
    .getByLabel("Rejection reason")
    .fill("Only approve the first calculation")
  await page.getByRole("button", { name: "Confirm rejection" }).click()
  await expect
    .poll(
      async () =>
        (
          await (
            await page.request.get(`${api}/api/v1/runs/${run.id}`, { headers })
          ).json()
        ).status,
      { timeout: 60000 },
    )
    .toBe("succeeded")
  await expect(page.getByTestId("approval-count")).toHaveCount(0)
  const events = await (
    await page.request.get(`${api}/api/v1/runs/${run.id}/events`, { headers })
  ).json()
  expect(
    events.data.filter(
      (e: { event_type: string }) => e.event_type === "tool_called",
    ),
  ).toHaveLength(1)
  await page.goto(`/runs/${run.id}`)
  await expect(
    page.getByText("approval_requested", { exact: true }),
  ).toBeVisible()
  await page.screenshot({
    path: test.info().outputPath("parallel-finished.png"),
    fullPage: true,
  })
})
