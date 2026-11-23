import { PLAN, PLAN_APPROVAL_PATH, PLAN_PATH, PUBLICATION_APPROVAL_PATH, RESULT, RESULT_PATH, REVISED_ARTIFACT, RUN_ID, RUN_PATH, expect, test } from "../fixtures/auth-proxy";

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
  await page.route(`**${PLAN_PATH}`, (route) => route.fulfill({
    json: { ...PLAN, content: { ...PLAN.content, blocking_questions: [] },
      readiness: { has_required_supported_check: true, unsupported_check_ids: ["VC1"], scope_errors: [] },
    },
  }));
  await panel.getByRole("button", { name: "Start analysis" }).click();
  await expect(panel.getByRole("status")).toHaveText("Preparing plan…");
  await expect(panel.getByText(receipt.base_sha, { exact: true })).toBeVisible();
  await expect(panel.locator("p").filter({ hasText: PLAN.content.summary })).toBeVisible({ timeout: 10000 });
  await expect(panel.getByRole("button", { name: "Approve plan revision 1" })).toBeEnabled();
  await expect(panel.getByText("No runner available for VC1")).toBeVisible();
  await expect(panel.getByText(analysis, { exact: true })).toHaveCount(0);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.getByRole("button", { name: "Sign out", exact: true }).click();
  await expect(panel).toHaveCount(0);
});

test("a missing deletion target shows a saved result without an approval action", async ({ page }) => {
  const summary = "src/legacy-cart.py is absent from the pinned source snapshot. There is nothing to delete.";
  await page.route(`**${PLAN_PATH}`, (route) => route.fulfill({
    json: { ...PLAN, content: { ...PLAN.content, summary, changes: [], blocking_questions: [],
      acceptance_criteria: [], validation_checks: [], preserve_behavior: [] },
    readiness: { outcome: "unsupported", unsupported_check_ids: [], scope_errors: [] } },
  }));
  await page.getByText("Open a saved run", { exact: true }).click();
  await page.getByLabel("Run ID", { exact: true }).fill(RUN_ID);
  await page.getByRole("button", { name: "Open run", exact: true }).click();
  const plan = page.getByRole("region", { name: "Refactoring plan" });
  await expect(plan.getByText(summary, { exact: true })).toBeVisible();
  await expect(plan.getByText("No changes proposed.")).toBeVisible();
  await expect(plan.getByText("Unsupported", { exact: true })).toHaveCount(0);
  await expect(plan.getByRole("button", { name: /Approve plan/ })).toHaveCount(0);
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
  const plan = page.getByRole("region", { name: "Refactoring plan" });
  await page.route(`**${PLAN_PATH}`, (route) => route.fulfill({
    status: 404, json: { error: { code: "plan_pending" } },
  }), { times: 1 });
  await page.getByRole("button", { name: "Open run", exact: true }).click();
  await expect(plan.getByRole("status")).toHaveText("Preparing plan…");
  await expect(plan.locator("p").filter({ hasText: PLAN.content.summary })).toBeVisible();
  await expect(plan.getByText(PLAN.artifact.sha256, { exact: true })).toBeVisible();
  await expect(plan.getByText("src/cart.py", { exact: true })).toBeVisible();
  await expect(plan.getByText("No runner available for VC1")).toBeVisible();
  await expect(plan.getByText("Resolve the blocking questions.")).toBeVisible();
  const answer = plan.getByLabel(PLAN.content.blocking_questions[0], { exact: true });
  await expect(answer).toBeVisible();
  await expect(plan.getByRole("button", { name: "Approve plan revision 1" })).toBeDisabled();
  await expect(plan.getByText("Extract the cart calculation", { exact: true })).toHaveCount(0);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  const runCalls = authProxy.calls.filter((call) => call.url.startsWith("/api/v1/runs"));
  expect(runCalls.slice(-2).map(({ method, url }) => [method, url])).toEqual([
    ["GET", RUN_PATH], ["GET", PLAN_PATH],
  ]);

  await expect(plan.getByText("Edit plan", { exact: true })).toHaveCount(0);
  const feedback = plan.getByLabel("Changes you want");
  const update = plan.getByRole("button", { name: "Update plan" });
  await feedback.fill("Keep the Python test.");
  await page.route(`**${PLAN_PATH}`, (route) => {
    expect(route.request().postDataJSON()).toEqual({
      expected_revision: 1, notes: "Keep the Python test.", answers: [""],
    });
    return route.fulfill({ status: 502,
      json: { error: { code: "github_unavailable", message: "Repository access could not be verified." } },
    });
  }, { times: 1 });
  await update.click();
  await expect(plan.getByRole("alert")).toContainText("Repository access could not be verified.");
  await expect(feedback).toHaveValue("Keep the Python test.");

  const revisedContent = { ...PLAN.content, blocking_questions: [],
    preserve_behavior: [...PLAN.content.preserve_behavior, "Round half to even."],
    validation_checks: [{ ...PLAN.content.validation_checks[0], check_id: "python.pytest" }],
  };
  await page.route(`**${PLAN_PATH}`, (route) => {
    if (route.request().method() === "POST") {
      expect(route.request().postDataJSON()).toEqual({
        expected_revision: 1, notes: "Keep the Python test.", answers: ["Round half to even."],
      });
      return route.fulfill({ json: { ...PLAN, feedback_status: "pending" } });
    }
    return route.fulfill({ json: { ...PLAN, content: revisedContent, artifact: REVISED_ARTIFACT,
      readiness: { has_required_supported_check: true, unsupported_check_ids: [], scope_errors: [] },
    } });
  });
  await answer.fill("Round half to even.");
  await update.click();
  await expect(plan.getByText("Updating plan…")).toBeVisible();
  await expect(update).toBeDisabled();
  await expect(plan.getByRole("button", { name: "Approve plan revision 1" })).toBeDisabled();
  await expect(plan.getByText("Round half to even.", { exact: true })).toBeVisible();
  await expect(plan.getByText(REVISED_ARTIFACT.sha256, { exact: true })).toBeVisible();
  await expect(plan.getByText("No runner available for VC1")).toHaveCount(0);

  const approve = plan.getByRole("button", { name: "Approve plan revision 2" });
  await expect(approve).toBeEnabled();
  await expect(feedback).toHaveValue("");
  await expect(plan.getByText("Save your edits before approving.")).toHaveCount(0);
  const result = page.getByRole("region", { name: "Candidate result" });
  const link = "https://github.com/fixture/example/pull/7";
  let phase: "validating" | "preparing" | "review" | "draft" = "validating";
  await page.route(`**${RESULT_PATH}?progress=true`, async (route) => {
    if (phase === "validating") {
      await route.fulfill({ json: { status: phase, message: null, result: null } });
      return;
    }
    const response = await route.fetch();
    const saved = await response.json();
    const status = phase === "preparing" ? phase : "idle";
    if (phase === "preparing") saved.publication_target = null;
    if (phase === "draft") saved.publication = { status: "draft_opened", number: 7, url: link };
    await route.fulfill({ response, json: { status, message: null, result: saved } });
  });
  await approve.click();
  await expect(plan.getByText("Approval recorded for revision 2.")).toBeVisible();
  await expect(plan.getByText("Edit plan", { exact: true })).toHaveCount(0);
  const approval = authProxy.calls.findLast((call) => call.method === "POST" && call.url === PLAN_APPROVAL_PATH)!;
  expect(approval.headers.cookie).toBe("refactra_session=fixture-session");
  expect(JSON.parse(approval.body!)).toEqual({
    plan_id: REVISED_ARTIFACT.id, revision: 2, sha256: REVISED_ARTIFACT.sha256,
  });

  await expect(result.getByRole("status")).toHaveText("Running validation checks.");
  phase = "preparing";
  await expect(result.getByRole("status")).toHaveText("Preparing the commit for review.", { timeout: 10000 });
  await expect(result.getByRole("button", { name: "Approve publication" })).toHaveCount(0);
  phase = "review";
  await expect(result.getByText(RESULT.sha256, { exact: true })).toBeVisible();
  await expect(result.getByText("3 passed", { exact: true })).toBeVisible();
  await expect(result.getByText("Exit code 0", { exact: false })).toBeVisible();
  await expect(result.getByText(RESULT.diff)).toBeVisible();
  await expect(result.locator("pre strong")).toHaveCount(0);
  await expect(result.getByText(RESULT.publication_target.commit_sha, { exact: true })).toBeVisible({ timeout: 10000 });
  await expect(result.getByText(RESULT.publication_target.destination.head_branch, { exact: true })).toBeVisible();
  await expect(result.getByRole("status")).toHaveText("Candidate ready for review.");
  await result.getByRole("button", { name: "Approve publication" }).click();
  await expect(result.getByRole("status")).toHaveText("Publishing the draft PR.");
  const publication = authProxy.calls.findLast((call) => call.method === "POST" && call.url === PUBLICATION_APPROVAL_PATH)!;
  expect(publication.headers.cookie).toBe("refactra_session=fixture-session");
  expect(JSON.parse(publication.body!)).toEqual({
    result_id: RESULT.id, target_sha256: RESULT.publication_target.sha256,
  });
  phase = "draft";
  await expect(result.getByRole("link", { name: "View draft PR #7 on GitHub" })).toHaveAttribute("href", link, { timeout: 10000 });

  await page.getByRole("button", { name: "Sign out", exact: true }).click();
  await expect(plan).toHaveCount(0);
});

test("shows a worker failure when reopening an approved run", async ({ page }) => {
  await page.route(`**${PLAN_PATH}`, (route) => route.fulfill({
    json: { ...PLAN, approval: { revision: 1 } },
  }));
  await page.route(`**${RESULT_PATH}?progress=true`, (route) => route.fulfill({
    json: { status: "failed", message: "Candidate validation failed.", result: null },
  }));
  await page.getByText("Open a saved run", { exact: true }).click();
  await page.getByLabel("Run ID", { exact: true }).fill(RUN_ID);
  await page.getByRole("button", { name: "Open run", exact: true }).click();
  const result = page.getByRole("region", { name: "Candidate result" });
  await expect(result.getByRole("alert")).toHaveText("Candidate validation failed.");
  await expect(result.getByRole("button", { name: "Approve publication" })).toHaveCount(0);
});
