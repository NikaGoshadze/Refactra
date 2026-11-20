import { PLAN, PLAN_APPROVAL_PATH, PLAN_PATH, PLAN_PROPOSAL, REVISED_ARTIFACT, RUN_ID, RUN_PATH, expect, test } from "../fixtures/auth-proxy";

const REPOSITORY_ID = 1001132861;
const receipt = { run_id: RUN_ID, base_branch: "feature/cart", base_sha: "a".repeat(40) };
const analysis = "<strong>Review cart extraction</strong>";

test.beforeEach(async ({ context, page, authProxy }) => {
  await context.addCookies([{ name: "refactra_session", value: "fixture-session", url: authProxy.origin, httpOnly: true }]);
  await page.route("**/api/v1/capabilities", (route) => route.fulfill({
    json: { mock_enabled: true, repository_analysis_enabled: true },
  }));
  await page.goto(authProxy.origin);
  await expect(page.getByRole("region", { name: "Repository analysis" })).toHaveCount(0);
  await page.getByRole("button", { name: "Check repository access" }).click();
});

test("verified repository submits a selected branch and shows the saved read-only analysis", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  const panel = page.getByRole("region", { name: "Repository analysis" });
  const branch = panel.getByLabel("Base branch", { exact: true });
  await expect(branch).toHaveValue("main");
  await expect(panel.getByRole("button", { name: "Start analysis" })).toBeDisabled();
  await branch.fill(receipt.base_branch);
  await panel.getByLabel("What would you like to refactor?").fill("  Review cart  ");
  await page.route("**/api/v1/runs", async (route) => {
    expect(route.request().postDataJSON()).toEqual({
      repository_id: REPOSITORY_ID, base_branch: receipt.base_branch, task_text: "Review cart",
    });
    expect(route.request().headers()["x-refactra-csrf"]).toBe("1");
    expect(route.request().headers()["idempotency-key"]).toMatch(/^[a-f0-9-]{36}$/);
    await route.fulfill({ status: 202, json: receipt });
  });
  let reads = 0;
  await page.route(`**${RUN_PATH}`, (route) => route.fulfill({
    json: { ...receipt, repository_id: REPOSITORY_ID, stop_reason: null, ...(++reads === 1
      ? { status: "ANALYZING", analysis_text: null }
      : { status: "NEEDS_HUMAN_REVIEW", analysis_text: analysis }) },
  }));
  await panel.getByRole("button", { name: "Start analysis" }).click();
  await expect(panel.getByRole("status")).toHaveText("Analyzing");
  await expect(panel.getByText(receipt.base_sha, { exact: true })).toBeVisible();
  await expect(panel.getByRole("status")).toHaveText("Ready for review", { timeout: 10000 });
  await expect(panel.locator("pre")).toHaveText(analysis);
  await expect(panel.locator("pre strong")).toHaveCount(0);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.getByRole("button", { name: "Sign out", exact: true }).click();
  await expect(panel).toHaveCount(0);
});

test("submission retry reuses its key and a failed run shows the backend reason", async ({ page }) => {
  const panel = page.getByRole("region", { name: "Repository analysis" });
  const start = panel.getByRole("button", { name: "Start analysis" });
  const keys: string[] = [];
  await panel.getByLabel("What would you like to refactor?").fill("Review cart");
  await page.route("**/api/v1/runs", (route) => {
    keys.push(route.request().headers()["idempotency-key"]);
    return keys.length === 1
      ? route.fulfill({ status: 502, json: { error: { message: "Source download failed." } } })
      : route.fulfill({ status: 202, json: receipt });
  });
  let reads = 0;
  await page.route(`**${RUN_PATH}`, (route) => ++reads === 1
    ? route.fulfill({ status: 502, json: { error: { message: "Could not verify repository access." } } })
    : route.fulfill({ json: { ...receipt, repository_id: REPOSITORY_ID, status: "FAILED", analysis_text: null,
      stop_reason: { message: "The run reached an analysis budget limit." } } }));
  await start.click();
  await expect(panel.getByRole("alert")).toHaveText("Source download failed.");
  await start.click();
  await expect(panel.getByRole("alert")).toHaveText("Could not verify repository access.");
  expect(keys).toHaveLength(2);
  expect(keys[1]).toBe(keys[0]);
  await panel.getByRole("button", { name: "Retry status" }).click();
  await expect(panel.getByRole("status")).toHaveText("Analysis failed");
  await expect(panel.getByRole("alert")).toHaveText("The run reached an analysis budget limit.");
});

test("opens, revises, and approves a saved plan", async ({ page, authProxy }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.getByText("Open a saved run", { exact: true }).click();
  await page.getByLabel("Run ID", { exact: true }).fill(RUN_ID);
  await page.getByRole("button", { name: "Open run", exact: true }).click();
  await expect(page.getByText("Fixture analysis")).toBeVisible();

  const plan = page.getByRole("region", { name: "Refactoring plan" });
  await page.route(`**${PLAN_PATH}`, (route) => route.fulfill({
    status: 404, json: { error: { code: "plan_not_found" } },
  }), { times: 1 });
  await plan.getByRole("button", { name: "Load saved plan" }).click();
  await expect(plan.getByRole("status")).toHaveText("No plan has been saved for this run yet.");
  await plan.getByRole("button", { name: "Refresh saved plan" }).click();
  await expect(plan.getByText(PLAN.content.summary, { exact: true })).toBeVisible();
  await expect(plan.getByText(PLAN.artifact.sha256, { exact: true })).toBeVisible();
  await expect(plan.getByText("src/cart.py", { exact: true })).toBeVisible();
  await expect(plan.getByRole("heading", { name: "Unrecognized validation IDs" })).toBeVisible();
  await expect(plan.getByText(PLAN.content.blocking_questions[0], { exact: true })).toBeVisible();
  await expect(plan.getByRole("button", { name: "Approve plan revision 1" })).toBeDisabled();
  await expect(plan.getByText("Extract the cart calculation", { exact: true })).toHaveCount(0);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  const runCalls = authProxy.calls.filter((call) => call.url.startsWith("/api/v1/runs"));
  expect(runCalls.slice(-2).map(({ method, url }) => [method, url])).toEqual([
    ["GET", RUN_PATH], ["GET", PLAN_PATH],
  ]);

  await plan.getByText("Revise plan draft").click();
  const draft = plan.getByLabel("Plan draft JSON");
  const proposal = JSON.parse(await draft.inputValue());
  expect(proposal).toEqual(PLAN_PROPOSAL);
  proposal.blocking_questions = [];
  proposal.preserve_behavior.push("Round half to even.");
  proposal.validation_checks[0].check_id = "python.pytest";
  const edited = JSON.stringify(proposal);
  await draft.fill(edited);
  const saveButton = plan.getByRole("button", { name: "Save plan revision" });
  await page.route(`**${PLAN_PATH}`, (route) => route.fulfill({ status: 502,
    json: { error: { code: "github_unavailable", message: "Repository access could not be verified." } },
  }), { times: 1 });
  await saveButton.click();
  await expect(plan.getByRole("alert")).toContainText("Repository access could not be verified.");
  await expect(draft).toHaveValue(edited);
  await saveButton.click();
  await expect(plan.getByText("Round half to even.", { exact: true })).toBeVisible();
  await expect(plan.getByText(REVISED_ARTIFACT.sha256, { exact: true })).toBeVisible();
  await expect(plan.getByRole("heading", { name: "Unrecognized validation IDs" })).toHaveCount(0);
  const save = authProxy.calls.findLast((call) => call.method === "POST" && call.url === PLAN_PATH)!;
  expect(save.headers.cookie).toBe("refactra_session=fixture-session");
  expect(JSON.parse(save.body!)).toEqual({ expected_revision: 1, proposal });

  const approve = plan.getByRole("button", { name: "Approve plan revision 2" });
  await expect(approve).toBeEnabled();
  await plan.getByText("Revise plan draft").click();
  const savedDraft = await draft.inputValue();
  await draft.fill(`${savedDraft}\n`);
  await expect(approve).toBeDisabled();
  await draft.fill(savedDraft);
  await approve.click();
  await expect(plan.getByRole("status")).toHaveText("Approval recorded for revision 2.");
  await expect(plan.getByText("Revise plan draft")).toHaveCount(0);
  const approval = authProxy.calls.findLast((call) => call.method === "POST" && call.url === PLAN_APPROVAL_PATH)!;
  expect(approval.headers.cookie).toBe("refactra_session=fixture-session");
  expect(JSON.parse(approval.body!)).toEqual({
    plan_id: REVISED_ARTIFACT.id, revision: 2, sha256: REVISED_ARTIFACT.sha256,
  });

  await page.getByRole("button", { name: "Sign out", exact: true }).click();
  await expect(plan).toHaveCount(0);
});
