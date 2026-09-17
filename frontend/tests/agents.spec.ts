import { expect, test } from "@playwright/test"

test("manage an agent and its published versions", async ({ page }) => {
  const agentName = `Agent ${Math.random().toString(36).substring(7)}`
  const updatedPrompt = `Updated prompt ${Math.random().toString(36).substring(7)}`

  await page.goto("/agents")
  await expect(page).toHaveTitle("Agents - AgentHub")
  await expect(page.getByRole("heading", { name: "Agents" })).toBeVisible()
  await expect(
    page.getByRole("link", { name: "Items", exact: true }),
  ).toHaveCount(0)

  await page.getByTestId("add-agent-button").click()
  await page.getByTestId("agent-name-input").fill(agentName)
  await page
    .getByTestId("agent-description-input")
    .fill("Created by the Agent management E2E test")
  await page
    .getByTestId("agent-system-prompt-input")
    .fill("You are a helpful assistant.")
  await page.getByTestId("agent-model-input").fill("gpt-4o-mini")
  await page.getByTestId("save-agent-button").click()

  await expect(page.getByText("Agent created successfully")).toBeVisible()
  const agentRow = page.getByRole("row").filter({ hasText: agentName })
  await expect(agentRow).toBeVisible()

  await agentRow.getByRole("button", { name: "Agent actions" }).click()
  await page.getByRole("menuitem", { name: "Open" }).click()
  await expect(page.getByRole("heading", { name: agentName })).toBeVisible()

  await page
    .getByTestId("configuration-system-prompt-input")
    .fill(updatedPrompt)
  await page.getByTestId("save-configuration-button").click()
  await expect(page.getByText("Agent configuration saved")).toBeVisible()
  await page.reload()
  await expect(
    page.getByTestId("configuration-system-prompt-input"),
  ).toHaveValue(updatedPrompt)

  await page.getByTestId("versions-tab").click()
  await page.getByTestId("publish-version-button").click()
  await page.getByTestId("version-changelog-input").fill("Initial version")
  await page.getByTestId("publish-version-confirm").click()
  await expect(page.getByText("Agent version published")).toBeVisible()
  await expect(page.getByText("v1", { exact: true })).toBeVisible()

  const secondPrompt = `${updatedPrompt} Revised for version two.`
  await page.getByRole("tab", { name: "Configuration" }).click()
  await page.getByTestId("configuration-system-prompt-input").fill(secondPrompt)
  await page.getByTestId("save-configuration-button").click()
  await expect(page.getByText("Agent configuration saved")).toBeVisible()
  await page.getByTestId("versions-tab").click()
  await page.getByTestId("publish-version-button").click()
  await page.getByTestId("version-changelog-input").fill("Second version")
  await page.getByTestId("publish-version-confirm").click()
  await expect(page.getByText("v2", { exact: true })).toBeVisible()

  const firstVersion = page
    .getByRole("row")
    .filter({ has: page.getByText("v1", { exact: true }) })
  await firstVersion.getByRole("button", { name: "Snapshot" }).click()
  await expect(page.locator("pre")).toContainText(updatedPrompt)
  await expect(page.locator("pre")).not.toContainText(secondPrompt)
  await firstVersion.getByRole("button", { name: "Snapshot" }).click()
  const secondVersion = page
    .getByRole("row")
    .filter({ has: page.getByText("v2", { exact: true }) })
  await secondVersion.getByRole("button", { name: "Snapshot" }).click()
  await expect(page.locator("pre")).toContainText(secondPrompt)

  await page.getByRole("link", { name: "Back to agents" }).click()
  const updatedAgentRow = page.getByRole("row").filter({ hasText: agentName })
  await updatedAgentRow.getByRole("button", { name: "Agent actions" }).click()
  await page.getByRole("menuitem", { name: "Delete Agent" }).click()
  await page.getByTestId("delete-agent-confirm").click()

  await expect(
    page.getByText("The agent was deleted successfully"),
  ).toBeVisible()
  await expect(
    page.getByRole("row").filter({ hasText: agentName }),
  ).not.toBeVisible()
})
