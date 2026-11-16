import { test, expect } from "@playwright/test";

test("a mock task reaches the worker through PostgreSQL and survives a page reload", async ({ page }) => {
  await page.goto("/");
  await expect(page.getByRole("heading", { name: "A clearer path from task to change." })).toBeVisible();
  await expect(page.getByRole("button", { name: "Start mock run" })).toBeEnabled();
  await page.getByLabel("What would you like to refactor?").fill("Browser smoke: extract duplicated calculation logic.");
  const saved = page.waitForResponse((response) => response.url().endsWith("/api/v1/mock/runs") && response.request().method() === "POST");
  await page.getByRole("button", { name: "Start mock run" }).click();
  const response = await saved;
  expect(response.status()).toBe(202);
  const receipt = await response.json();
  expect(receipt.mode).toBe("mock");
  await expect(page.getByText("needs human review", { exact: true })).toBeVisible({ timeout: 15000 });
  await expect(page.getByText("worker claimed", { exact: true })).toBeVisible();
  await expect(page.getByText("mock analysis", { exact: true })).toBeVisible();
  await page.reload();
  await expect(page.getByText("needs human review", { exact: true })).toBeVisible();
  await expect(page.getByTitle(receipt.run_id)).toBeVisible();
  const stored = await page.request.get(`/api/v1/mock/runs/${receipt.run_id}`);
  expect((await stored.json()).pull_request).toBeNull();
});

test("the local page remains usable at mobile width", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/");
  await expect(page.getByRole("button", { name: "Start mock run" })).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
});
