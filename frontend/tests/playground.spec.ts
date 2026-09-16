import { expect, test } from "@playwright/test"

test("published agent streams, remembers and restores a conversation", async ({
  page,
}, testInfo) => {
  test.skip(!process.env.LLM_API_KEY, "Requires LLM_API_KEY")
  test.setTimeout(180000)
  await page.goto("/playground")
  const token = await page.evaluate(() => localStorage.getItem("access_token"))
  const api = process.env.VITE_API_URL || new URL(page.url()).origin
  const headers = { Authorization: `Bearer ${token}` }
  const name = `Playground E2E ${Date.now()}`
  const created = await page.request.post(`${api}/api/v1/agents/`, {
    headers,
    data: {
      name,
      llm_model: process.env.LLM_MODEL || "gpt-4o-mini",
      system_prompt:
        "Answer in Chinese. Remember user facts. Keep responses under 150 words.",
    },
  })
  expect(created.ok()).toBeTruthy()
  const agent = await created.json()
  try {
    const published = await page.request.post(
      `${api}/api/v1/agents/${agent.id}/versions`,
      { headers, data: {} },
    )
    expect(published.ok()).toBeTruthy()
    await page.reload()
    await page.getByTestId("playground-agent").click()
    await page.getByRole("option", { name, exact: true }).click()
    await page.getByTestId("new-conversation").click()
    await expect(page).toHaveURL(/conversation=/)
    await page.evaluate(() => {
      const observed = window as unknown as {
        replyLengths: number[]
        replyObserver: MutationObserver
      }
      observed.replyLengths = []
      observed.replyObserver = new MutationObserver(() => {
        const text = document.querySelector(
          '[data-testid="assistant-message"]',
        )?.textContent
        if (text && !text.includes("…")) observed.replyLengths.push(text.length)
      })
      observed.replyObserver.observe(document.body, {
        subtree: true,
        childList: true,
        characterData: true,
      })
    })
    await page
      .getByTestId("playground-message")
      .fill(
        "我叫张三。请记住我的名字，再用一小段文字介绍你能如何帮助我学习编程。",
      )
    await page.getByTestId("send-message").click()
    await expect(page.getByTestId("current-node")).toContainText("call_model", {
      timeout: 60000,
    })
    const partial = page.getByTestId("assistant-message")
    await expect(partial).not.toContainText("…", { timeout: 60000 })
    await expect(page.getByTestId("send-message")).toBeVisible({
      timeout: 60000,
    })
    await expect(partial).not.toHaveText("Assistant")
    const finalText = await partial.innerText()
    const lengths = await page.evaluate(() => {
      const observed = window as unknown as {
        replyLengths: number[]
        replyObserver: MutationObserver
      }
      observed.replyObserver.disconnect()
      return observed.replyLengths
    })
    expect(new Set(lengths).size).toBeGreaterThan(1)
    await page
      .locator("summary")
      .filter({ hasText: "Execution events" })
      .click()
    await expect(page.getByTestId("run-usage")).toBeVisible()
    await page.evaluate(() => window.scrollTo(0, 0))
    await page.screenshot({
      path: testInfo.outputPath("playground.png"),
      fullPage: true,
    })
    await page.reload()
    await expect(page.getByTestId("user-message")).toHaveCount(1)
    await expect(page.getByTestId("assistant-message")).toHaveText(finalText, {
      useInnerText: true,
    })
    await page
      .getByTestId("playground-message")
      .fill("我叫什么名字？只回答名字。")
    await page.getByTestId("send-message").click()
    await expect(page.getByTestId("send-message")).toBeVisible({
      timeout: 60000,
    })
    await expect(page.getByTestId("user-message")).toHaveCount(2)
    await expect(page.getByTestId("assistant-message")).toHaveCount(2)
    await expect(page.getByTestId("assistant-message").last()).toContainText(
      "张三",
    )

    await page
      .getByTestId("playground-message")
      .fill("写一篇很长的教程，详细解释计算机网络的所有协议。")
    await page.getByTestId("send-message").click()
    await expect(page.getByTestId("current-node")).toContainText("call_model", {
      timeout: 60000,
    })
    const runLink = page.locator('a[href^="/runs/"]')
    const runPath = await runLink.getAttribute("href")
    expect(runPath).toBeTruthy()
    await page.close()
    await expect
      .poll(
        async () => {
          const response = await page.request.get(`${api}/api/v1${runPath}`, {
            headers,
          })
          return (await response.json()).status
        },
        { timeout: 15000 },
      )
      .toBe("cancelled")
  } finally {
    await page.request.delete(`${api}/api/v1/agents/${agent.id}`, { headers })
  }
})
