import { expect, test } from "@playwright/test"

for (const status of ["succeeded", "failed"] as const) {
  test(`run a published version and inspect ${status} trace`, async ({
    page,
  }) => {
    await page.goto("/agents")
    const token = await page.evaluate(() =>
      localStorage.getItem("access_token"),
    )
    const headers = { Authorization: `Bearer ${token}` }
    const api = process.env.VITE_API_URL ?? "http://localhost:8000"
    const name = `Run E2E ${status} ${Date.now()}`
    const created = await page.request.post(`${api}/api/v1/agents/`, {
      headers,
      data: { name, llm_model: "gpt-4o-mini" },
    })
    expect(created.ok()).toBeTruthy()
    const agent = await created.json()
    const published = await page.request.post(
      `${api}/api/v1/agents/${agent.id}/versions`,
      { headers, data: {} },
    )
    expect(published.ok()).toBeTruthy()
    const version = await published.json()
    const id = crypto.randomUUID()
    const run = {
      id,
      owner_id: agent.owner_id,
      agent_version_id: version.id,
      agent_name: name,
      agent_version_number: 1,
      status,
      trigger: "playground",
      input: { message: "Hello from the browser" },
      output: status === "succeeded" ? { content: "Browser test reply" } : null,
      error: status === "failed" ? "Model unavailable for this test" : null,
      prompt_tokens: 100,
      completion_tokens: 50,
      cost_usd: "0.000045",
      duration_ms: 1250,
      created_at: "2026-09-12T09:00:00Z",
      started_at: "2026-09-12T09:00:00Z",
      finished_at: "2026-09-12T09:00:01.250Z",
    }
    let releaseRun: (() => void) | undefined
    const finishRun = new Promise<void>((resolve) => {
      releaseRun = resolve
    })
    await page.route("**/api/v1/runs/**", async (route) => {
      const url = new URL(route.request().url())
      if (route.request().method() === "POST") {
        expect(route.request().postDataJSON()).toEqual({
          agent_version_id: version.id,
          input: run.input,
          trigger: "playground",
        })
        await finishRun
        await route.fulfill({ json: run })
      } else if (url.pathname.endsWith("/events")) {
        await route.fulfill({
          json: {
            count: 2,
            data: [
              {
                id: crypto.randomUUID(),
                run_id: id,
                seq: 0,
                event_type: "run_started",
                node_name: null,
                created_at: run.started_at,
                payload: { input: run.input },
              },
              {
                id: crypto.randomUUID(),
                run_id: id,
                seq: 1,
                event_type:
                  status === "succeeded" ? "run_finished" : "run_failed",
                node_name: null,
                created_at: run.finished_at,
                payload:
                  status === "succeeded"
                    ? { output: run.output }
                    : { error: run.error },
              },
            ],
          },
        })
      } else if (url.pathname.endsWith(id)) {
        await route.fulfill({ json: run })
      } else {
        await route.fulfill({ json: { count: 1, data: [run] } })
      }
    })
    try {
      await page.goto(`/agents/${agent.id}`)
      await page.getByTestId("versions-tab").click()
      await page.getByTestId("run-version-button").click()
      await page.getByTestId("run-message-input").fill(run.input.message)
      await page.getByTestId("run-submit-button").click()
      await expect(page.getByTestId("run-submit-button")).toBeDisabled()
      await expect(
        page.getByText("Execution in progress. Please keep this dialog open."),
      ).toBeVisible()
      await page.keyboard.press("Escape")
      await expect(page.getByRole("dialog")).toBeVisible()
      releaseRun?.()
      await page.waitForURL(`/runs/${id}`)
      await expect(
        page.getByRole("heading", { name: "Run details" }),
      ).toBeVisible()
      await expect(page.getByText(status, { exact: true })).toBeVisible()
      await expect(
        page.getByText(
          status === "succeeded"
            ? "Browser test reply"
            : "Model unavailable for this test",
          { exact: true },
        ),
      ).toBeVisible()
      const trace = page.locator("details summary")
      await expect(trace).toHaveCount(2)
      await expect(trace.first()).toContainText("run_started")
      await expect(trace.last()).toContainText(
        status === "succeeded" ? "run_finished" : "run_failed",
      )
      await expect(trace.last()).toContainText("+1250 ms")
      await trace.last().click()
      await expect(page.locator("details[open] pre")).toContainText(
        status === "succeeded" ? "Browser test reply" : "Model unavailable",
      )
      await page.screenshot({
        path: test.info().outputPath(`${status}-detail.png`),
        fullPage: true,
      })
      await page.getByRole("link", { name: "Back to runs" }).click()
      await expect(
        page.getByRole("heading", { name: "Runs", exact: true }),
      ).toBeVisible()
      await expect(
        page.getByRole("row").filter({ hasText: name }),
      ).toContainText(status)
      await page.getByRole("link", { name: "View", exact: true }).click()
      await page.waitForURL(`/runs/${id}`)
    } finally {
      releaseRun?.()
      await page.request.delete(`${api}/api/v1/agents/${agent.id}`, { headers })
    }
  })
}
