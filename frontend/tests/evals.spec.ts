import { expect, test } from "@playwright/test"

test("manage evaluation cases from the agent detail page", async ({ page }) => {
  await page.goto("/agents")
  const token = await page.evaluate(() => localStorage.getItem("access_token"))
  const headers = { Authorization: `Bearer ${token}` }
  const api = process.env.VITE_API_URL ?? "http://localhost:8000"
  const created = await page.request.post(`${api}/api/v1/agents/`, {
    headers,
    data: { name: `Eval UI ${Date.now()}`, llm_model: "gpt-4o-mini" },
  })
  expect(created.ok()).toBeTruthy()
  const agent = await created.json()
  try {
    await page.goto(`/agents/${agent.id}`)
    await page.getByRole("tab", { name: "Test Cases" }).click()
    await page
      .getByRole("button", { name: "New test case", exact: true })
      .click()
    await page.getByLabel("Name", { exact: true }).fill("Greeting")
    await page.getByLabel("Message", { exact: true }).fill("Hello")
    await page.getByLabel("Expected output", { exact: true }).fill("Hello")
    await page.getByRole("button", { name: "Add assertion" }).click()
    await page.getByLabel("Expected value").fill("hello")
    await page.getByRole("button", { name: "Save test case" }).click()
    await expect(page.getByText("Test case saved")).toBeVisible()
    await expect(page.getByText("Greeting", { exact: true })).toBeVisible()
    await page.getByRole("button", { name: "Edit", exact: true }).click()
    await page.getByLabel("Name", { exact: true }).fill("Updated greeting")
    await page.getByRole("button", { name: "Save test case" }).click()
    await expect(
      page.getByText("Updated greeting", { exact: true }),
    ).toBeVisible()
    await page.getByRole("button", { name: "Import JSON" }).click()
    await page.getByLabel("Case JSON").fill(
      JSON.stringify([
        {
          name: "Imported case",
          input: { message: "hi" },
          expected_output: "hello",
        },
      ]),
    )
    await page.getByRole("button", { name: "Import", exact: true }).click()
    await expect(page.getByText("Imported case", { exact: true })).toBeVisible()
    await page.goto("/evals")
    await expect(
      page.getByRole("heading", { name: "Evaluations", exact: true }),
    ).toBeVisible()
  } finally {
    await page.request.delete(`${api}/api/v1/agents/${agent.id}`, { headers })
  }
})

test("live evaluation detects version regressions and opens the run trace", async ({
  page,
}) => {
  test.skip(
    process.env.EVAL_LIVE_MODEL_TEST !== "1",
    "Requires explicit paid-model opt-in",
  )
  test.setTimeout(240000)
  await page.goto("/agents")
  const token = await page.evaluate(() => localStorage.getItem("access_token"))
  const headers = { Authorization: `Bearer ${token}` }
  const api = process.env.VITE_API_URL ?? "http://localhost:8000"
  const name = `Eval live ${Date.now()}`
  const created = await page.request.post(`${api}/api/v1/agents/`, {
    headers,
    data: {
      name,
      llm_model: process.env.LLM_MODEL,
      system_prompt: "Always respond with exactly 你好 and nothing else.",
      llm_settings: { temperature: 0 },
      timeout_seconds: 60,
    },
  })
  expect(created.ok()).toBeTruthy()
  const agent = await created.json()
  const cases = [
    {
      name: "Greeting",
      input: { message: "Hello" },
      assertions: [{ type: "contains", value: "你好" }],
    },
    {
      name: "Exact",
      input: { message: "Say hello" },
      assertions: [{ type: "exact", value: "你好" }],
    },
    {
      name: "Judge",
      input: { message: "Greet me" },
      assertions: [
        {
          type: "llm_judge",
          value: "The response is a greeting written in Chinese, not English",
          threshold: 0.7,
        },
      ],
    },
  ]
  expect(
    (
      await page.request.post(
        `${api}/api/v1/evals/cases/import?agent_id=${agent.id}`,
        { headers, data: cases },
      )
    ).ok(),
  ).toBeTruthy()
  const ids: string[] = []
  for (const versionNumber of [1, 2]) {
    if (versionNumber === 2)
      expect(
        (
          await page.request.patch(`${api}/api/v1/agents/${agent.id}`, {
            headers,
            data: {
              system_prompt:
                "Always respond with exactly GOODBYE and nothing else.",
            },
          })
        ).ok(),
      ).toBeTruthy()
    const published = await page.request.post(
      `${api}/api/v1/agents/${agent.id}/versions`,
      { headers, data: {} },
    )
    expect(published.ok()).toBeTruthy()
    const version = await published.json()
    const response = await page.request.post(`${api}/api/v1/evals/runs`, {
      headers,
      data: { agent_version_id: version.id, note: `live-v${versionNumber}` },
    })
    expect(response.status()).toBe(202)
    const batch = await response.json()
    ids.push(batch.id)
    await page.goto(`/evals/${batch.id}`)
    await expect
      .poll(
        async () =>
          (
            await page.request.get(`${api}/api/v1/evals/runs/${batch.id}`, {
              headers,
            })
          ).json(),
        { timeout: 100000 },
      )
      .toMatchObject({ status: "succeeded", completed_count: 3 })
    await expect(page.getByText("succeeded · 3/3")).toBeVisible()
    await expect(
      page.getByRole("progressbar", { name: "Evaluation progress" }),
    ).toHaveAttribute("value", "3")
    await page.screenshot({
      path: `test-results/eval-live-v${versionNumber}.png`,
      fullPage: true,
    })
  }
  const compared = await page.request.get(
    `${api}/api/v1/evals/compare?left_id=${ids[0]}&right_id=${ids[1]}`,
    { headers },
  )
  const comparison = await compared.json()
  expect(comparison.regression_count).toBeGreaterThanOrEqual(2)
  expect(comparison.left.passed_count).toBe(3)
  await page.goto(`/evals/compare?left=${ids[0]}&right=${ids[1]}`)
  await expect(
    page.getByRole("heading", { name: "Version comparison" }),
  ).toBeVisible()
  await page.locator("summary").first().click()
  await expect(
    page.locator("details").first().getByText("你好", { exact: true }),
  ).toBeVisible()
  await expect(
    page.locator("details").first().getByText("GOODBYE", { exact: true }),
  ).toBeVisible()
  await page.screenshot({
    path: "test-results/eval-live-comparison.png",
    fullPage: true,
  })
  await page
    .locator("details")
    .first()
    .getByRole("link", { name: "View run trace" })
    .first()
    .click()
  await expect(page).toHaveURL(/\/runs\//)
})
