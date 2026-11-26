import { readFileSync, writeFileSync } from "node:fs";
import { expect, test, type Locator, type Page } from "@playwright/test";

// Live run against the real stack, GitHub and OpenAI. Start it with scripts/run-live-e2e.ps1,
// which prepares the base branch, session, worker and cleanup.
const sessionFile = process.env.REFACTRA_E2E_SESSION_FILE;
const baseBranch = process.env.REFACTRA_E2E_BASE_BRANCH;
const resultFile = process.env.REFACTRA_E2E_RESULT_FILE;
const allowWaivers = process.env.REFACTRA_E2E_ALLOW_WAIVERS === "1";
// The README's demo task: small, fully specified and covered by an explicit test file.
const task = process.env.REFACTRA_E2E_TASK || [
  "Extract the `!summary` display logic from `pizza_app/main.py` into a pure `summary_lines` helper",
  "in `pizza_app/chat_summary.py` that returns a list of strings to print. Preserve the exact Georgian",
  "messages, leading newlines, and summary text. Keep command detection and both script and package",
  "startup intact. Treat a missing or empty summary the same way. Add offline tests in",
  "`pizza_app/test_chat_summary.py` for missing, empty, and populated summaries. Use `python.pytest`",
  "on that test file.",
].join(" ");

type Outcome = Record<string, unknown>;

/** Wait until `ready` is visible, failing at once with the text of any visible failure. */
async function waitFor(page: Page, ready: Locator, failures: Locator[], timeout: number,
  refresh?: { when: Locator; button: Locator }) {
  const deadline = Date.now() + timeout;
  while (Date.now() < deadline) {
    if (await ready.isVisible()) return;
    for (const failure of failures) {
      if (await failure.first().isVisible()) {
        throw new Error(`The workflow stopped: ${(await failure.first().innerText()).trim()}`);
      }
    }
    // The candidate panel stops polling after three minutes; long stages need a refresh.
    if (refresh && await refresh.when.isVisible() && await refresh.button.isEnabled()) {
      await refresh.button.click();
    }
    await page.waitForTimeout(5000);
  }
  throw new Error(`Timed out after ${timeout / 60_000} minutes waiting for ${ready}.`);
}

async function connect(page: Page, outcome: Outcome) {
  const repository = page.getByRole("region", { name: "Repository connection" });
  await repository.getByRole("button", { name: "Check repository access" }).click();
  await expect(repository.getByRole("status")).toHaveText("Read-only connection verified.",
    { timeout: 90_000 });
  outcome.repository = (await repository.locator(".repository-target").innerText()).trim();
}

async function analyseAndApprovePlan(page: Page, outcome: Outcome) {
  const analysis = page.getByRole("region", { name: "Repository analysis" });
  await analysis.getByLabel("Base branch", { exact: true }).fill(baseBranch!);
  await analysis.getByLabel("What would you like to refactor?").fill(task);
  const submitted = page.waitForResponse((response) =>
    response.url().endsWith("/api/v1/runs") && response.request().method() === "POST");
  await analysis.getByRole("button", { name: "Start analysis" }).click();
  const response = await submitted;
  const receipt = await response.json();
  expect(response.status(), JSON.stringify(receipt)).toBe(202);
  outcome.runId = receipt.run_id;
  outcome.baseSha = receipt.base_sha;

  const plan = page.getByRole("region", { name: "Refactoring plan" });
  const approve = plan.getByRole("button", { name: /^Approve plan revision \d+$/ });
  await waitFor(page, approve, [analysis.getByText("Analysis failed"), analysis.getByRole("alert")],
    10 * 60_000);
  await expect(approve, "The plan has blockers; see the trace.").toBeEnabled({ timeout: 30_000 });
  await approve.click();
  await expect(plan.getByText(/Approval recorded for revision \d+\./)).toBeVisible({ timeout: 60_000 });
}

async function validateAndPublish(page: Page, outcome: Outcome) {
  const result = page.getByRole("region", { name: "Candidate result" });
  const refresh = {
    when: result.getByText("Still waiting for the worker. Refresh to check again."),
    button: result.getByRole("button", { name: "Refresh saved result" }),
  };
  const failure = result.locator(".error-box").filter({ hasNotText: "Still waiting" });

  await test.step("candidate generation and validation", async () => {
    await waitFor(page, result.getByText("Candidate ready for review."), [failure], 20 * 60_000, refresh);
    const waive = result.getByRole("button", { name: /^Waive check \d+$/ });
    outcome.waivedChecks = await waive.count();
    if (outcome.waivedChecks && !allowWaivers) {
      throw new Error(`${outcome.waivedChecks} required check(s) could not run; rerun with -AllowWaivers to publish anyway.`);
    }
    while (await waive.count()) {
      const name = await waive.first().innerText();
      await result.getByLabel(`Reason to publish without check ${name.replace("Waive check ", "")}`)
        .fill("Live end-to-end test run with -AllowWaivers.");
      await waive.first().click();
      await expect(result.getByRole("button", { name })).toHaveCount(0, { timeout: 60_000 });
    }
  });

  await test.step("publication", async () => {
    await result.getByRole("button", { name: "Approve publication" }).click();
    const link = result.getByRole("link", { name: /^View draft PR #\d+ on GitHub$/ });
    await waitFor(page, link, [failure], 10 * 60_000, refresh);
    outcome.pullRequestUrl = await link.getAttribute("href");
  });
}

test("a real task goes from analysis to a draft pull request", async ({ page, context, baseURL }) => {
  test.skip(!sessionFile || !baseBranch || !resultFile, "Run through scripts/run-live-e2e.ps1.");
  const outcome: Outcome = { baseBranch, task };
  await context.addCookies([{
    name: "refactra_session", value: readFileSync(sessionFile!, "ascii").trim(),
    url: baseURL!, httpOnly: true, sameSite: "Lax",
  }]);
  try {
    await page.goto("/");
    await test.step("repository access", () => connect(page, outcome));
    await test.step("analysis and plan", () => analyseAndApprovePlan(page, outcome));
    await validateAndPublish(page, outcome);
  } finally {
    // Written even on failure, so the runner can report the run and clean up what was published.
    writeFileSync(resultFile!, JSON.stringify(outcome, null, 2));
  }
});
