import { expect, test } from "@playwright/test"

test("submit async and replay real MCP progress after reopening Run details", async ({
  page,
}) => {
  test.skip(
    process.env.ASYNC_LIVE_TEST !== "1",
    "Requires a running worker and real model",
  )
  test.setTimeout(180000)
  await page.goto("/agents")
  const token = await page.evaluate(() => localStorage.getItem("access_token"))
  const headers = { Authorization: `Bearer ${token}` }
  const api = process.env.VITE_API_URL ?? "http://127.0.0.1:8000"
  const suffix = Date.now()
  const toolIds: string[] = []
  let agentId: string | undefined
  try {
    for (const [name, field] of [
      ["search_dev_docs", "query"],
      ["read_dev_doc", "doc_id"],
    ]) {
      const response = await page.request.post(`${api}/api/v1/tools/`, {
        headers,
        data: {
          name: `${name}_${suffix}`,
          description: name,
          tool_type: "mcp",
          config: {
            transport: "streamable_http",
            url: "http://mcp-docs:3001/mcp",
            tool_name: name,
          },
          parameters_schema: {
            type: "object",
            properties: { [field]: { type: "string" } },
            required: [field],
          },
        },
      })
      expect(response.ok(), await response.text()).toBeTruthy()
      toolIds.push((await response.json()).id)
    }
    const created = await page.request.post(`${api}/api/v1/agents/`, {
      headers,
      data: {
        name: `Async MCP ${suffix}`,
        llm_model: process.env.LLM_MODEL ?? "deepseek-flash",
        tool_ids: toolIds,
        system_prompt: `First call search_dev_docs_${suffix} with query Checkpoint. Then call read_dev_doc_${suffix} with doc_id 06-async-worker. Call both tools sequentially. Summarize recovery responsibilities in Chinese in about 150 words and cite the document ID.`,
      },
    })
    expect(created.ok()).toBeTruthy()
    agentId = (await created.json()).id
    expect(
      (
        await page.request.post(`${api}/api/v1/agents/${agentId}/versions`, {
          headers,
          data: {},
        })
      ).ok(),
    ).toBeTruthy()
    await page.goto(`/agents/${agentId}`)
    await page.getByTestId("versions-tab").click()
    await page.getByTestId("run-version-button").click()
    await page
      .getByTestId("run-message-input")
      .fill("先搜索Checkpoint，再读取06阶段文档，解释恢复职责。")
    const submitted = page.waitForResponse(
      (r) => r.url().endsWith("/runs/async") && r.request().method() === "POST",
    )
    const stream = page.waitForResponse((r) =>
      /\/runs\/[^/]+\/stream$/.test(r.url()),
    )
    await page.getByTestId("run-async-submit-button").click()
    const accepted = await submitted
    expect(accepted.status()).toBe(202)
    const run = await accepted.json()
    expect(run.status).toBe("queued")
    await page.waitForURL(`/runs/${run.id}`)
    const subscribed = await stream
    expect(subscribed.request().method()).toBe("GET")
    expect(subscribed.request().headers().authorization).toContain("Bearer ")
    await expect(page.getByTestId("tool-call").first()).toBeVisible({
      timeout: 90000,
    })
    await page.screenshot({
      path: test.info().outputPath("async-live.png"),
      fullPage: true,
    })
    await page.reload()
    await expect(page.getByTestId("tool-call").first()).toBeVisible({
      timeout: 30000,
    })
    await expect(page.getByText("succeeded", { exact: true })).toBeVisible({
      timeout: 90000,
    })
    await expect(page.getByTestId("tool-call")).toHaveCount(2)
    await expect(page.locator("details summary").first()).toContainText(
      "run_started",
    )
    await expect(page.locator("details summary").last()).toContainText(
      "run_finished",
    )
    const detail = await (
      await page.request.get(`${api}/api/v1/runs/${run.id}`, { headers })
    ).json()
    const events = await (
      await page.request.get(`${api}/api/v1/runs/${run.id}/events`, { headers })
    ).json()
    expect(detail.checkpoint_id).toBeTruthy()
    expect(
      events.data.filter(
        (e: { event_type: string; payload: { ok?: boolean } }) =>
          e.event_type === "tool_result" && e.payload.ok,
      ),
    ).toHaveLength(2)
    await test
      .info()
      .attach("async-run.json", {
        body: JSON.stringify({ run: detail, events: events.data }, null, 2),
        contentType: "application/json",
      })
    await page.screenshot({
      path: test.info().outputPath("async-finished.png"),
      fullPage: true,
    })
  } finally {
    if (agentId)
      await page.request.delete(`${api}/api/v1/agents/${agentId}`, { headers })
    for (const id of toolIds)
      await page.request.delete(`${api}/api/v1/tools/${id}`, { headers })
  }
})
