import { PLAN, PLAN_PATH, RUN_DETAIL, RUN_HISTORY, RUN_ID, RUN_PATH, test, expect } from "../fixtures/auth-proxy";

const TIMELINE = `**${RUN_PATH}/timeline`;

test.beforeEach(async ({ context, page, authProxy }) => {
  authProxy.calls.length = 0;
  await page.route("**/api/v1/capabilities", (route) => route.fulfill({
    json: { mock_enabled: true, repository_analysis_enabled: true },
  }));
  if (!test.info().title.startsWith("signed-out")) {
    await context.addCookies([{ name: "refactra_session", value: "fixture-session", url: authProxy.origin, httpOnly: true }]);
  }
});

test("signed-out visitors are asked to sign in and nothing is requested", async ({ page, authProxy }) => {
  await page.goto(`${authProxy.origin}/runs/${RUN_ID}`);
  await expect(page.getByText("Sign in with GitHub to see this run.")).toBeVisible();
  expect(authProxy.calls.some((call) => call.url.startsWith(RUN_PATH))).toBe(false);
});

test("a run page shows its summary, timeline and saved plan", async ({ page, authProxy }, testInfo) => {
  await page.setViewportSize({ width: 1280, height: 1000 });
  await page.route(`**${PLAN_PATH}`, (route) => route.fulfill({ json: PLAN }));
  await page.goto(`${authProxy.origin}/history`);
  await page.getByRole("link", { name: "Extract the cart calculation" }).first().click();
  await expect(page).toHaveURL(new RegExp(`/runs/${RUN_ID}$`));
  const run = page.getByRole("region", { name: "Run", exact: true });
  await expect(run.getByText("fixture/example")).toBeVisible();
  await expect(run.getByText("into a helper.", { exact: false })).toBeVisible();
  await expect(run.getByText("Published")).toBeVisible();
  await expect(run.getByRole("link", { name: "Draft PR #7" })).toHaveAttribute("href", "https://github.com/fixture/example/pull/7");
  const similar = new URL(await run.getByRole("link", { name: "Start a similar task" }).getAttribute("href") ?? "", authProxy.origin);
  expect(Object.fromEntries(similar.searchParams)).toEqual({
    repository: "1001132861", branch: "main", task: RUN_DETAIL.task_text,
  });
  const timeline = page.getByRole("region", { name: "Timeline" });
  await expect(timeline.getByRole("listitem")).toHaveCount(3);
  await expect(timeline.getByRole("link", { name: "Draft PR #7 opened" })).toHaveAttribute("href", "https://github.com/fixture/example/pull/7");
  // Two plan revisions exist, so the saved plan panel continues the run here.
  await expect(page.getByRole("region", { name: "Refactoring plan" })).toBeVisible();
  await page.screenshot({ path: testInfo.outputPath("run-detail-desktop.png"), fullPage: true });
});

test("an analyzing run keeps checking until its plan is ready", async ({ page, authProxy }) => {
  let reads = 0;
  await page.route(TIMELINE, (route) => route.fulfill({ json: {
    ...RUN_DETAIL,
    summary: { ...RUN_DETAIL.summary, ...(++reads === 1
      ? { stage: "analyzing", plan_revisions: 0, pull_request: null }
      : { stage: "plan_ready", plan_revisions: 1, pull_request: null }) },
  } }));
  await page.route(`**${PLAN_PATH}`, (route) => route.fulfill({ json: PLAN }));
  await page.goto(`${authProxy.origin}/runs/${RUN_ID}`);
  await expect(page.getByText("Analyzing", { exact: true })).toBeVisible();
  await expect(page.getByRole("region", { name: "Refactoring plan" })).toHaveCount(0);
  await expect(page.getByText("Plan ready for review", { exact: true })).toBeVisible({ timeout: 10000 });
  await expect(page.getByRole("region", { name: "Refactoring plan" })).toBeVisible();
});

test("a failed load can be retried and the page stays usable on mobile", async ({ page, authProxy }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  let reads = 0;
  await page.route(TIMELINE, (route) => ++reads === 1
    ? route.fulfill({ status: 502, json: { error: { message: "Repository access could not be verified." } } })
    : route.fulfill({ json: { ...RUN_DETAIL, summary: { ...RUN_DETAIL.summary, plan_revisions: 0 } } }));
  await page.goto(`${authProxy.origin}/runs/${RUN_ID}`);
  // Next.js adds an empty route-announcer alert, so match this one by its text.
  await expect(page.getByRole("alert").filter({ hasText: "Repository access" }))
    .toHaveText("Repository access could not be verified.");
  await page.getByRole("button", { name: "Retry" }).click();
  await expect(page.getByRole("region", { name: "Timeline" })).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
});

test("a malformed run address shows the not-found page", async ({ page, authProxy }) => {
  const response = await page.goto(`${authProxy.origin}/runs/not-a-run`);
  expect(response?.status()).toBe(404);
});

test("a similar-task link preselects the repository, branch and task", async ({ page, authProxy }) => {
  const params = new URLSearchParams({ repository: "1001132861", branch: "feature/cart", task: "Extract the helper" });
  await page.goto(`${authProxy.origin}/?${params}`);
  const picker = page.getByRole("region", { name: "Repository connection" });
  await expect(picker.getByRole("status")).toHaveText("Write access verified.");
  await expect(picker.getByLabel("Repository", { exact: true })).toHaveValue("1001132861");
  const analysis = page.getByRole("region", { name: "Repository analysis" });
  await expect(analysis.getByLabel("Base branch", { exact: true })).toHaveValue("feature/cart");
  await expect(analysis.getByLabel("What would you like to refactor?")).toHaveValue("Extract the helper");
});

test("a similar-task link for an unavailable repository changes nothing", async ({ page, authProxy }) => {
  await page.goto(`${authProxy.origin}/?repository=1001132862&branch=main&task=Hidden`);
  const picker = page.getByRole("region", { name: "Repository connection" });
  await expect(picker.getByRole("status")).toHaveText("Choose a repository to refactor.");
  await expect(page.getByRole("region", { name: "Repository analysis" })).toHaveCount(0);
});

test("the proxy forwards timeline reads with only the session cookie", async ({ authProxy, request }) => {
  const response = await request.get(`${authProxy.origin}${RUN_PATH}/timeline`, {
    headers: { Cookie: "refactra_session=fixture-session; analytics=private", Authorization: "Bearer must-not-pass" },
  });
  expect(response.status()).toBe(200);
  expect(response.headers()["cache-control"]).toBe("no-store");
  expect((await response.json()).summary.run_id).toBe(RUN_HISTORY.runs[0].run_id);
  expect(authProxy.calls[0].headers.cookie).toBe("refactra_session=fixture-session");
  expect(authProxy.calls[0].headers.authorization).toBeUndefined();
});
