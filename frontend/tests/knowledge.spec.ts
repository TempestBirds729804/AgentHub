import { expect, test } from "@playwright/test"
import "./config"

test("scan PDF failure, retry and deletion are visible", async ({ page }) => {
  test.skip(process.env.RAG_LIVE_TEST !== "1", "Requires a document worker")
  test.setTimeout(90000)
  await page.goto("/knowledge")
  const token = await page.evaluate(() => localStorage.getItem("access_token"))
  const headers = { Authorization: `Bearer ${token}` }
  const response = await page.request.post("/api/v1/knowledge/", {
    headers,
    data: { name: `PDF failure ${Date.now()}` },
  })
  expect(response.ok()).toBeTruthy()
  const kb = await response.json()
  const path = `/api/v1/knowledge/${kb.id}`
  try {
    await page.goto(`/knowledge/${kb.id}`)
    // A pypdf-generated blank page has no text layer, like a scanned PDF without OCR.
    const pdf = Buffer.from(
      "JVBERi0xLjMKJeLjz9MKMSAwIG9iago8PAovUHJvZHVjZXIgKHB5cGRmKQo+PgplbmRvYmoKMiAwIG9iago8PAovVHlwZSAvUGFnZXMKL0NvdW50IDEKL0tpZHMgWyA0IDAgUiBdCj4+CmVuZG9iagozIDAgb2JqCjw8Ci9UeXBlIC9DYXRhbG9nCi9QYWdlcyAyIDAgUgo+PgplbmRvYmoKNCAwIG9iago8PAovVHlwZSAvUGFnZQovUmVzb3VyY2VzIDw8Cj4+Ci9NZWRpYUJveCBbIDAuMCAwLjAgMzAwIDMwMCBdCi9QYXJlbnQgMiAwIFIKPj4KZW5kb2JqCnhyZWYKMCA1CjAwMDAwMDAwMDAgNjU1MzUgZiAKMDAwMDAwMDAxNSAwMDAwMCBuIAowMDAwMDAwMDU0IDAwMDAwIG4gCjAwMDAwMDAxMTMgMDAwMDAgbiAKMDAwMDAwMDE2MiAwMDAwMCBuIAp0cmFpbGVyCjw8Ci9TaXplIDUKL1Jvb3QgMyAwIFIKL0luZm8gMSAwIFIKPj4Kc3RhcnR4cmVmCjI1NgolJUVPRgo=",
      "base64",
    )
    await page.locator("#document-upload").setInputFiles({
      name: "scan.pdf",
      mimeType: "application/pdf",
      buffer: pdf,
    })
    const row = page.getByRole("row").filter({ hasText: "scan.pdf" })
    await expect(row.getByText("failed", { exact: true })).toBeVisible({
      timeout: 30000,
    })
    await expect(row.getByRole("img")).toHaveAttribute("title", /OCR/)
    await page.screenshot({
      path: test.info().outputPath("scan-failed.png"),
      fullPage: true,
    })
    await row.getByRole("button", { name: "Reprocess" }).click()
    await expect(page.getByText("Document queued")).toBeVisible()
    await expect(row.getByText("failed", { exact: true })).toBeVisible({
      timeout: 30000,
    })
    page.once("dialog", (dialog) => dialog.accept())
    await row.getByRole("button", { name: "Delete" }).click()
    await expect(row).toHaveCount(0)
    await page.locator("#document-upload").setInputFiles({
      name: "unsupported.png",
      mimeType: "image/png",
      buffer: Buffer.from("image"),
    })
    await expect(
      page.getByText("Unsupported file type", { exact: true }),
    ).toBeVisible()
  } finally {
    expect((await page.request.delete(path, { headers })).ok()).toBeTruthy()
  }
})

test("knowledge upload, search, binding and persistent citations", async ({
  page,
}) => {
  test.skip(
    process.env.RAG_LIVE_TEST !== "1",
    "Requires real Qwen embeddings and a worker",
  )
  test.setTimeout(180000)
  const name = `RAG acceptance ${Date.now()}`
  const observed: string[] = []
  page.on("response", async (response) => {
    if (
      response.request().method() === "GET" &&
      /\/knowledge\/[^/]+\/documents\?/.test(response.url())
    ) {
      const body = await response.json().catch(() => null)
      if (body?.data)
        observed.push(...body.data.map((doc: { status: string }) => doc.status))
    }
  })
  await page.goto("/knowledge")
  await page.getByRole("button", { name: "Add Knowledge Base" }).click()
  await page.getByLabel("Name", { exact: true }).fill(name)
  await page
    .getByLabel("Description", { exact: true })
    .fill("Live Qwen acceptance")
  await page.getByRole("button", { name: "Save", exact: true }).click()
  await page.getByRole("link", { name, exact: true }).click()
  const kbId = page.url().split("/").pop() as string
  const token = await page.evaluate(() => localStorage.getItem("access_token"))
  const headers = { Authorization: `Bearer ${token}` }
  const document = Buffer.from(
    "AgentHub 的验收暗号是青竹7429，实验室位于紫色月亮站。此文档仅包含暗号和实验室位置。",
  )
  await page.locator("#document-upload").setInputFiles({
    name: "acceptance.txt",
    mimeType: "text/plain",
    buffer: document,
  })
  const row = page.getByRole("row").filter({ hasText: "acceptance.txt" })
  await expect(row.getByText("ready", { exact: true })).toBeVisible({
    timeout: 90000,
  })
  expect(
    observed.some((status) => ["pending", "processing"].includes(status)),
  ).toBeTruthy()
  await page.screenshot({
    path: test.info().outputPath("documents-ready.png"),
    fullPage: true,
  })
  await page.getByRole("tab", { name: "Search", exact: true }).click()
  await page
    .getByLabel("Query", { exact: true })
    .fill("AgentHub 的验收暗号是什么？")
  await page.getByRole("button", { name: "Search", exact: true }).click()
  await expect(page.getByRole("article")).toContainText("青竹7429", {
    timeout: 30000,
  })
  await page.screenshot({
    path: test.info().outputPath("search-result.png"),
    fullPage: true,
  })
  const agentResponse = await page.request.post("/api/v1/agents/", {
    headers,
    data: {
      name,
      llm_model: process.env.LLM_MODEL,
      system_prompt: "请用中文回答。",
    },
  })
  expect(agentResponse.ok()).toBeTruthy()
  const agent = await agentResponse.json()
  await page.goto(`/agents/${agent.id}`)
  await page.getByLabel(`${name} — 1 ready documents`).check()
  await page.getByRole("button", { name: /Save/ }).first().click()
  await expect(page.getByText("Agent configuration saved")).toBeVisible()
  const version = await page.request.post(
    `/api/v1/agents/${agent.id}/versions`,
    { headers, data: {} },
  )
  expect(version.ok()).toBeTruthy()
  const conversation = await page.request.post("/api/v1/conversations/", {
    headers,
    data: { agent_id: agent.id },
  })
  const conv = await conversation.json()
  await page.goto(`/playground?conversation=${conv.id}`)
  await page
    .getByTestId("playground-message")
    .fill("AgentHub 的验收暗号是什么？")
  await page.getByRole("button", { name: "Send", exact: true }).click()
  await expect(page.getByTestId("assistant-message").last()).toContainText(
    "青竹7429",
    { timeout: 60000 },
  )
  await expect(
    page.getByRole("button", { name: "Source 1", exact: true }).first(),
  ).toBeVisible()
  await page
    .getByRole("button", { name: "Source 1", exact: true })
    .first()
    .click()
  await expect(page.locator("[popover]:popover-open")).toContainText(
    "acceptance.txt",
  )
  await expect(page.locator("[popover]:popover-open")).toContainText("青竹7429")
  await page.screenshot({
    path: test.info().outputPath("citation.png"),
    fullPage: true,
  })
  await page.keyboard.press("Escape")
  await expect(page.getByTestId("playground-message")).toBeEnabled()
  await page.reload()
  await expect(
    page.getByRole("button", { name: "Source 1", exact: true }).first(),
  ).toBeVisible()
  await page
    .getByTestId("playground-message")
    .fill(
      "仅根据知识库回答：法国国王路易十四的出生日期是什么？如果资料未提供请明确说明。",
    )
  await page.getByRole("button", { name: "Send", exact: true }).click()
  await expect(page.getByTestId("assistant-message").last()).toContainText(
    /没有|未提供|未包含|未提及|不包含|未找到/,
    { timeout: 60000 },
  )
  await expect(page.getByTestId("playground-message")).toBeEnabled()
  await page.screenshot({
    path: test.info().outputPath("no-answer.png"),
    fullPage: true,
  })
  await test.info().attach("rag-acceptance.json", {
    body: JSON.stringify(
      { kbId, agentId: agent.id, conversationId: conv.id, observed },
      null,
      2,
    ),
    contentType: "application/json",
  })
})
