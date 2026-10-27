import { expect, test } from "@playwright/test"

test("monitoring stays usable during a local Redis outage", async ({
  page,
}) => {
  test.skip(
    process.env.MONITORING_REDIS_OUTAGE !== "1",
    "Requires isolated Redis outage",
  )
  await page.goto("/monitoring")
  await expect(page.getByText("Runs · 24 hours", { exact: true })).toBeVisible()
  const redis = page
    .getByRole("heading", { name: "Redis", exact: true })
    .locator("..")
  await expect(redis.getByText(/Unavailable/).first()).toBeVisible({
    timeout: 10000,
  })
  const postgres = page
    .getByRole("heading", { name: "Postgres", exact: true })
    .locator("..")
  await expect(postgres.getByText(/Healthy/)).toBeVisible()
  await expect(page.getByRole("alert")).toHaveCount(0)
  await page.screenshot({
    path: "test-results/p1-monitoring-outage.png",
    fullPage: true,
  })
})

test("admin monitoring shows usage and isolated component health", async ({
  page,
}) => {
  await page.goto("/monitoring")
  await expect(
    page.getByRole("heading", { name: "Monitoring", exact: true }),
  ).toBeVisible()
  await expect(page.getByText("Runs · 24 hours", { exact: true })).toBeVisible()
  await expect(
    page.getByRole("heading", { name: "Top users · 30 days" }),
  ).toBeVisible()
  await expect(
    page.getByRole("heading", { name: "Postgres", exact: true }),
  ).toBeVisible()
  await expect(
    page.getByRole("heading", { name: "Redis", exact: true }),
  ).toBeVisible()
  await expect(page.getByRole("alert")).toHaveCount(0)
  await page.screenshot({
    path: "test-results/p1-monitoring.png",
    fullPage: true,
  })
})

test("normal users cannot enter monitoring", async ({ page }) => {
  await page.goto("/")
  const token = await page.evaluate(() => localStorage.getItem("access_token"))
  const api = process.env.VITE_API_URL ?? "http://localhost:8000"
  const email = `monitor-${Date.now()}@example.com`
  const password = "test-monitor-password-123"
  const created = await page.request.post(`${api}/api/v1/users/`, {
    headers: { Authorization: `Bearer ${token}` },
    data: { email, password },
  })
  expect(created.ok()).toBeTruthy()
  const user = await created.json()
  try {
    const login = await page.request.post(`${api}/api/v1/login/access-token`, {
      form: { username: email, password },
    })
    const data = await login.json()
    await page.evaluate(
      (value) => localStorage.setItem("access_token", value),
      data.access_token,
    )
    await page.goto("/monitoring")
    await expect(page).toHaveURL(/\/$/)
    await expect(
      page.getByRole("link", { name: "Monitoring", exact: true }),
    ).toHaveCount(0)
    expect(
      (
        await page.request.get(`${api}/api/v1/monitoring/overview`, {
          headers: { Authorization: `Bearer ${data.access_token}` },
        })
      ).status(),
    ).toBe(403)
  } finally {
    await page.request.delete(`${api}/api/v1/users/${user.id}`, {
      headers: { Authorization: `Bearer ${token}` },
    })
  }
})
