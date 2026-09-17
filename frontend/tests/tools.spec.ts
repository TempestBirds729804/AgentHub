import { writeFile } from "node:fs/promises"
import { expect, test } from "@playwright/test"

test("MCP editor preserves legacy transport and defaults new tools to Streamable HTTP", async ({
  page,
}) => {
  const legacy = {
    id: crypto.randomUUID(),
    name: "legacy_mcp",
    description: "Legacy MCP tool",
    tool_type: "mcp",
    config: {
      transport: "sse",
      url: "https://example.com/sse",
      tool_name: "test",
    },
    parameters_schema: { type: "object", properties: {} },
    timeout_seconds: 30,
    is_active: true,
    requires_approval: false,
  }
  await page.route("**/api/v1/tools/**", async (route) => {
    if (new URL(route.request().url()).pathname.endsWith("/builtin-functions"))
      await route.fulfill({ json: { data: [], count: 0 } })
    else if (route.request().method() === "PATCH") {
      expect(route.request().postDataJSON().config.transport).toBe("sse")
      await route.fulfill({ json: legacy })
    } else await route.fulfill({ json: { data: [legacy], count: 1 } })
  })
  await page.goto("/tools")
  await page.getByRole("button", { name: "Edit", exact: true }).click()
  await expect(page.getByLabel("MCP transport")).toHaveValue("sse")
  await page.getByRole("button", { name: "Save tool" }).click()
  await expect(page.getByRole("dialog")).not.toBeVisible()
  await page.getByRole("button", { name: "Add Tool", exact: true }).click()
  await page.getByLabel("Tool type").selectOption("mcp")
  await expect(page.getByLabel("MCP transport")).toHaveValue("streamable_http")
})

test("real model searches and reads through the independent MCP service", async ({
  page,
}, testInfo) => {
  test.skip(
    !process.env.LLM_API_KEY || !process.env.MCP_SERVICE_URL,
    "Requires real model and MCP_SERVICE_URL",
  )
  test.setTimeout(180000)
  await page.goto("/tools")
  const token = await page.evaluate(() => localStorage.getItem("access_token"))
  const headers = { Authorization: `Bearer ${token}` }
  const api = process.env.VITE_API_URL || new URL(page.url()).origin
  const suffix = Date.now()
  const names = [`search_dev_docs_${suffix}`, `read_dev_doc_${suffix}`]
  const tools: { id: string }[] = []
  let agentId: string | undefined
  try {
    for (const [index, remote] of [
      "search_dev_docs",
      "read_dev_doc",
    ].entries()) {
      await page.getByRole("button", { name: "Add Tool", exact: true }).click()
      const dialog = page.getByRole("dialog")
      await dialog.getByLabel("Tool type").selectOption("mcp")
      await expect(dialog.getByLabel("MCP transport")).toHaveValue(
        "streamable_http",
      )
      await dialog.getByLabel("Name", { exact: true }).fill(names[index])
      await dialog
        .getByLabel("Description", { exact: true })
        .fill(
          index === 0
            ? "Search project development documents and return document IDs. Use before reading."
            : "Read a project development document using its exact document ID.",
        )
      await dialog
        .getByLabel("URL", { exact: true })
        .fill(process.env.MCP_SERVICE_URL!)
      await dialog.getByLabel("Remote tool name").fill(remote)
      await dialog.getByRole("button", { name: "Add parameter" }).click()
      await dialog
        .getByLabel("Parameter 1 name", { exact: true })
        .fill(index === 0 ? "query" : "doc_id")
      const saved = page.waitForResponse(
        (r) =>
          r.request().method() === "POST" &&
          new URL(r.url()).pathname === "/api/v1/tools/",
      )
      await dialog.getByRole("button", { name: "Save tool" }).click()
      tools.push(await (await saved).json())
      await expect(dialog).not.toBeVisible()
      const row = page.getByRole("row").filter({ hasText: names[index] })
      await row.getByRole("button", { name: "Test", exact: true }).click()
      await dialog
        .getByLabel(index === 0 ? "query" : "doc_id")
        .fill(index === 0 ? "异步执行、状态恢复与限流" : "06-async-worker")
      await dialog.getByRole("button", { name: "Run test" }).click()
      await expect(dialog.getByRole("status")).toContainText("06-async-worker")
      await page.screenshot({
        path: testInfo.outputPath(`mcp-test-${index}.png`),
        fullPage: true,
      })
      await page.keyboard.press("Escape")
    }
    expect(tools).toHaveLength(2)
    const name = `MCP live ${suffix}`
    const created = await page.request.post(`${api}/api/v1/agents/`, {
      headers,
      data: {
        name,
        llm_model: process.env.LLM_MODEL,
        tool_ids: tools.map((t) => t.id),
        timeout_seconds: 90,
        system_prompt:
          "Answer briefly in Chinese. Always use the search tool first and then the read tool before answering. Search for 异步执行、状态恢复与限流, read doc_id 06-async-worker, and cite the document ID. If a tool fails, explain it without retrying.",
      },
    })
    expect(created.ok(), await created.text()).toBeTruthy()
    agentId = (await created.json()).id
    expect(
      (
        await page.request.post(`${api}/api/v1/agents/${agentId}/versions`, {
          headers,
          data: {},
        })
      ).ok(),
    ).toBeTruthy()
    await page.goto("/playground")
    await page.getByTestId("playground-agent").click()
    await page.getByRole("option", { name, exact: true }).click()
    await page.getByTestId("new-conversation").click()
    await page
      .getByTestId("playground-message")
      .fill(
        "先搜索项目的异步执行与 Checkpoint 说明，再读取06阶段文档，简述恢复职责，给出文档ID。",
      )
    await page.getByTestId("send-message").click()
    await expect(page.getByTestId("assistant-message")).toHaveCount(1, {
      timeout: 90000,
    })
    await expect(page.getByTestId("send-message")).toBeVisible({
      timeout: 90000,
    })
    await expect(page.getByTestId("tool-call")).toHaveCount(2)
    await expect(page.getByTestId("assistant-message")).toContainText(
      "06-async-worker",
    )
    await page.screenshot({
      path: testInfo.outputPath("mcp-playground.png"),
      fullPage: true,
    })
    await page
      .locator("summary")
      .filter({ hasText: "Execution events" })
      .click()
    const href = await page
      .getByRole("link", { name: "View run" })
      .getAttribute("href")
    const runId = href?.split("/").pop()
    const run = await (
      await page.request.get(`${api}/api/v1/runs/${runId}`, { headers })
    ).json()
    expect(run.status).toBe("succeeded")
    const events = await (
      await page.request.get(`${api}/api/v1/runs/${runId}/events`, { headers })
    ).json()
    const calls = events.data.filter(
      (e: { event_type: string }) => e.event_type === "tool_called",
    )
    const results = events.data.filter(
      (e: { event_type: string }) => e.event_type === "tool_result",
    )
    expect(
      calls.map((e: { payload: { tool: string } }) => e.payload.tool),
    ).toEqual(names)
    expect(
      results.every((e: { payload: { ok: boolean } }) => e.payload.ok),
    ).toBeTruthy()
    await testInfo.attach("mcp-live-run", {
      body: JSON.stringify({ run, events: events.data }, null, 2),
      contentType: "application/json",
    })
    await writeFile(
      testInfo.outputPath("mcp-live-run.json"),
      JSON.stringify({ run, events: events.data }, null, 2),
    )
    await page.goto(href!)
    await expect(page.getByTestId("tool-call")).toHaveCount(2)
    await page.screenshot({
      path: testInfo.outputPath("mcp-run.png"),
      fullPage: true,
    })
  } finally {
    if (agentId)
      expect(
        (
          await page.request.delete(`${api}/api/v1/agents/${agentId}`, {
            headers,
          })
        ).ok(),
      ).toBeTruthy()
    for (const tool of tools)
      expect(
        (
          await page.request.delete(`${api}/api/v1/tools/${tool.id}`, {
            headers,
          })
        ).ok(),
      ).toBeTruthy()
  }
})

test("same-name tool results remain matched when completion order differs", async ({
  page,
}) => {
  const id = crypto.randomUUID()
  await page.route("**/api/v1/runs/**", async (route) => {
    if (new URL(route.request().url()).pathname.endsWith("/events")) {
      const entries = [
        [
          "tool_called",
          { tool: "calculator", index: 0, args: { expression: "2*21" } },
        ],
        [
          "tool_called",
          { tool: "calculator", index: 1, args: { expression: "3*21" } },
        ],
        [
          "tool_result",
          {
            tool: "calculator",
            index: 1,
            result: "63",
            ok: true,
            duration_ms: 2,
          },
        ],
        [
          "tool_result",
          {
            tool: "calculator",
            index: 0,
            result: "42",
            ok: true,
            duration_ms: 5,
          },
        ],
      ]
      await route.fulfill({
        json: {
          count: entries.length,
          data: entries.map(([event_type, payload], seq) => ({
            id: crypto.randomUUID(),
            run_id: id,
            seq,
            event_type,
            payload,
            created_at: "2026-09-17T08:00:00Z",
          })),
        },
      })
    } else
      await route.fulfill({
        json: {
          id,
          agent_name: "Pairing test",
          agent_version_number: 1,
          status: "succeeded",
          trigger: "playground",
          input: { message: "calculate" },
          output: { content: "42, 63", truncated_by_max_iterations: true },
          prompt_tokens: 1,
          completion_tokens: 1,
          duration_ms: 10,
          cost_usd: "0",
          created_at: "2026-09-17T08:00:00Z",
        },
      })
  })
  await page.goto(`/runs/${id}`)
  const blocks = page.getByTestId("tool-call")
  await expect(blocks).toHaveCount(2)
  await expect(blocks.nth(0)).toContainText("2*21")
  await expect(blocks.nth(0)).toContainText("42")
  await expect(blocks.nth(0)).not.toContainText("63")
  await expect(blocks.nth(1)).toContainText("3*21")
  await expect(blocks.nth(1)).toContainText("63")
  await expect(
    page.getByText("Iteration limit reached", { exact: true }),
  ).toBeVisible()
})

test("create, test, bind and deactivate a function tool", async ({
  page,
}, testInfo) => {
  const name = `calculator_${Date.now()}`
  await page.goto("/tools")
  await page.getByRole("button", { name: "Add Tool", exact: true }).click()
  const dialog = page.getByRole("dialog")
  await dialog.getByLabel("Built-in function").selectOption("calculator")
  await dialog.getByLabel("Name", { exact: true }).fill(name)
  await dialog.getByRole("button", { name: "Save tool" }).click()
  await expect(dialog).not.toBeVisible()
  const row = page.getByRole("row").filter({ hasText: name })
  await row.getByRole("button", { name: "Test", exact: true }).click()
  await dialog.getByLabel("expression").fill("123 * 456")
  await dialog.getByRole("button", { name: "Run test" }).click()
  await expect(dialog.getByRole("status")).toContainText("56088")
  await page.screenshot({
    path: testInfo.outputPath("function-test.png"),
    fullPage: true,
  })
  await page.keyboard.press("Escape")

  const token = await page.evaluate(() => localStorage.getItem("access_token"))
  const headers = { Authorization: `Bearer ${token}` }
  const api = process.env.VITE_API_URL || new URL(page.url()).origin
  const listing = await (
    await page.request.get(`${api}/api/v1/tools/`, { headers })
  ).json()
  const tool = listing.data.find((t: { name: string }) => t.name === name)
  const created = await page.request.post(`${api}/api/v1/agents/`, {
    headers,
    data: { name: `Tool test ${Date.now()}`, llm_model: "gpt-4o-mini" },
  })
  const agent = await created.json()
  await page.goto(`/agents/${agent.id}`)
  await page.getByRole("checkbox", { name: new RegExp(name) }).check()
  await page.getByTestId("save-configuration-button").click()
  await expect(page.getByText("Agent configuration saved")).toBeVisible()
  await page.getByTestId("versions-tab").click()
  await page.getByTestId("publish-version-button").click()
  await page.getByTestId("publish-version-confirm").click()
  await expect(page.getByRole("table")).toContainText(name)
  const removed = await page.request.delete(`${api}/api/v1/tools/${tool.id}`, {
    headers,
  })
  expect(removed.status()).toBe(409)
  await page.goto("/tools")
  await row.getByRole("button", { name: "Edit", exact: true }).click()
  await dialog.getByRole("checkbox", { name: "Active", exact: true }).uncheck()
  await dialog.getByRole("button", { name: "Save tool" }).click()
  await expect(row).toContainText("Inactive")
  await page.request.delete(`${api}/api/v1/agents/${agent.id}`, { headers })
  await page.request.delete(`${api}/api/v1/tools/${tool.id}`, { headers })
})

test("HTTP parameter editor and SSRF feedback", async ({ page }, testInfo) => {
  await page.goto("/tools")
  await page.getByRole("button", { name: "Add Tool", exact: true }).click()
  const dialog = page.getByRole("dialog")
  await dialog.getByLabel("Tool type").selectOption("http")
  await dialog.getByLabel("Name", { exact: true }).fill(`http_${Date.now()}`)
  await dialog
    .getByLabel("Description", { exact: true })
    .fill("Look up a repository")
  await dialog
    .getByLabel("URL", { exact: true })
    .fill("http://169.254.169.254/")
  await dialog.getByRole("button", { name: "Add parameter" }).click()
  await dialog.getByLabel("Parameter 1 name", { exact: true }).fill("owner")
  await dialog.getByLabel("Parameter 1 description").fill("Repository owner")
  await dialog.getByRole("button", { name: "Add header" }).click()
  await dialog.getByLabel("Header 1 name").fill("Accept")
  await dialog.getByLabel("Header 1 value").fill("application/vnd.github+json")
  await dialog.getByRole("button", { name: "Save tool" }).click()
  await expect(page.getByText(/non-public address/)).toBeVisible()
  await page.screenshot({
    path: testInfo.outputPath("http-editor.png"),
    fullPage: true,
  })
})

test("real model uses function and HTTP tools and recovers from HTTP failure", async ({
  page,
}, testInfo) => {
  test.skip(!process.env.LLM_API_KEY, "Requires LLM_API_KEY")
  test.setTimeout(240000)
  await page.goto("/playground")
  const token = await page.evaluate(() => localStorage.getItem("access_token"))
  const headers = { Authorization: `Bearer ${token}` }
  const api = process.env.VITE_API_URL || new URL(page.url()).origin
  const suffix = Date.now()
  const definitions = [
    {
      name: `calculator_${suffix}`,
      description:
        "Calculate arithmetic. Always use this tool for multiplication.",
      tool_type: "function",
      config: { function_name: "calculator" },
    },
    {
      name: `repository_${suffix}`,
      description: "Look up a GitHub repository by owner and repo.",
      tool_type: "http",
      config: {
        method: "GET",
        url: "https://api.github.com/repos/{owner}/{repo}",
      },
      parameters_schema: {
        type: "object",
        properties: { owner: { type: "string" }, repo: { type: "string" } },
        required: ["owner", "repo"],
      },
    },
  ]
  const tools = []
  for (const data of definitions) {
    const response = await page.request.post(`${api}/api/v1/tools/`, {
      headers,
      data,
    })
    expect(response.ok(), await response.text()).toBeTruthy()
    tools.push(await response.json())
  }
  const tested = await page.request.post(
    `${api}/api/v1/tools/${tools[1].id}/test`,
    { headers, data: { arguments: { owner: "fastapi", repo: "fastapi" } } },
  )
  expect((await tested.json()).ok).toBeTruthy()
  const name = `Tools live ${suffix}`
  const created = await page.request.post(`${api}/api/v1/agents/`, {
    headers,
    data: {
      name,
      llm_model: process.env.LLM_MODEL,
      tool_ids: tools.map((t) => t.id),
      system_prompt:
        "Answer in Chinese. Always call the relevant tool before answering. If a tool fails, explain the error briefly without retrying. Keep answers under 100 words.",
      timeout_seconds: 90,
    },
  })
  expect(created.ok(), await created.text()).toBeTruthy()
  const agent = await created.json()
  const published = await page.request.post(
    `${api}/api/v1/agents/${agent.id}/versions`,
    { headers, data: {} },
  )
  expect(published.ok()).toBeTruthy()
  await page.reload()
  await page.getByTestId("playground-agent").click()
  await page.getByRole("option", { name, exact: true }).click()
  await page.getByTestId("new-conversation").click()
  for (const [index, message] of [
    "用计算器工具算出123乘456等于多少。",
    "用仓库工具查询 fastapi/fastapi，并告诉我仓库名称和简介。",
    "用仓库工具查询 fastapi/agenthub-nonexistent-repository-xyz-987654321，若工具失败请说明。",
  ].entries()) {
    await page.getByTestId("playground-message").fill(message)
    await page.getByTestId("send-message").click()
    await expect(page.getByTestId("tool-call")).toBeVisible({ timeout: 90000 })
    await expect(page.getByTestId("send-message")).toBeVisible({
      timeout: 90000,
    })
    await expect(page.getByTestId("assistant-message")).toHaveCount(index + 1)
    if (index === 0)
      await expect(page.getByTestId("assistant-message").last()).toContainText(
        "56088",
      )
    if (index === 2)
      await expect(page.getByTestId("tool-call")).toContainText("404")
    await page.screenshot({
      path: testInfo.outputPath(`tools-live-${index}.png`),
      fullPage: true,
    })
    await page
      .locator("summary")
      .filter({ hasText: "Execution events" })
      .click()
    const runLink = page.getByRole("link", { name: "View run" })
    const href = await runLink.getAttribute("href")
    expect(href).toBeTruthy()
    const runId = href?.split("/").pop()
    const run = await (
      await page.request.get(`${api}/api/v1/runs/${runId}`, { headers })
    ).json()
    expect(run.status).toBe("succeeded")
    const events = await (
      await page.request.get(`${api}/api/v1/runs/${runId}/events`, { headers })
    ).json()
    expect(
      events.data.some(
        (e: { event_type: string }) => e.event_type === "tool_called",
      ),
    ).toBeTruthy()
    expect(
      events.data.some(
        (e: { event_type: string }) => e.event_type === "tool_result",
      ),
    ).toBeTruthy()
    await testInfo.attach(`run-${index}`, {
      body: JSON.stringify(
        { id: runId, output: run.output, events: events.data },
        null,
        2,
      ),
      contentType: "application/json",
    })
    await page
      .locator("summary")
      .filter({ hasText: "Execution events" })
      .click()
    if (index === 2 && href) {
      await page.goto(href)
      await expect(
        page.getByText("Execution trace", { exact: true }),
      ).toBeVisible()
      await expect(page.getByTestId("tool-call")).toContainText("404")
      await page.screenshot({
        path: testInfo.outputPath("run-tools.png"),
        fullPage: true,
      })
    }
  }
  // Keep these successful demonstration runs for manual review.
})
