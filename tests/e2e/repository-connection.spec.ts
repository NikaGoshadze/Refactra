import { USER_REPOSITORIES, chooseRepository, test, expect } from "../fixtures/auth-proxy";

const metadata = {
  repository_id: 1001132861, full_name: "fixture/example", default_branch: "main", access: "read-only",
  branches: ["feature/cart", "main"], branches_truncated: false,
};
const listUrl = "**/api/v1/user/repositories";
const repositoryUrl = "**/api/v1/repositories/1001132861";

test.beforeEach(async ({ page, authProxy }) => {
  authProxy.calls.length = 0;
  await page.route("**/api/v1/capabilities", (route) => route.fulfill({ json: { mock_enabled: true } }));
});

async function signIn(context: import("@playwright/test").BrowserContext, origin: string) {
  await context.addCookies([{ name: "refactra_session", value: "fixture-session", url: origin, httpOnly: true }]);
}

test("signed-out visitors are asked to sign in and nothing is requested", async ({ page, authProxy }) => {
  await page.goto(authProxy.origin);
  await expect(page.getByRole("region", { name: "GitHub account" }).getByText("Not signed in", { exact: true })).toBeVisible();
  const panel = page.getByRole("region", { name: "Repository connection" });
  await expect(panel.getByRole("status")).toHaveText("Sign in with GitHub to choose a repository.");
  await expect(panel.getByLabel("Repository", { exact: true })).toHaveCount(0);
  expect(authProxy.calls.some((call) => call.url.includes("repositor"))).toBe(false);
});

test("a signed-in user picks a writable repository and sign-out clears it", async ({ page, context, authProxy }, testInfo) => {
  await signIn(context, authProxy.origin);
  await page.setViewportSize({ width: 1280, height: 1000 });
  await page.goto(authProxy.origin);
  const panel = page.getByRole("region", { name: "Repository connection" });
  await expect(panel.getByRole("status")).toHaveText("Choose a repository to refactor.");
  const select = panel.getByLabel("Repository", { exact: true });
  await expect(select.locator("option")).toHaveText(["Choose a repository…", "fixture/example", "fixture/read-only (read-only)"]);
  await expect(select.locator("option", { hasText: "fixture/read-only" })).toBeDisabled();
  await expect(panel.getByRole("link", { name: "Install Refactra on more repositories" }))
    .toHaveAttribute("href", USER_REPOSITORIES.install_url);
  await expect(panel.getByRole("link", { name: "Choose which fixture repositories Refactra can access" }))
    .toHaveAttribute("href", "https://github.com/settings/installations/7");
  await chooseRepository(page);
  await expect(panel.locator(".repository-target")).toHaveText("fixture/example");
  await expect(panel.getByRole("definition").filter({ hasText: "main" })).toBeVisible();
  await expect(page.getByRole("region", { name: "Repository analysis" })).toBeVisible();
  expect(authProxy.calls.filter((call) => call.url === "/api/v1/repositories/1001132861")).toHaveLength(1);
  expect(await page.evaluate(() => document.cookie)).not.toContain("refactra_session");
  await page.screenshot({ path: testInfo.outputPath("repository-desktop.png"), fullPage: true });
  await page.getByRole("button", { name: "Sign out", exact: true }).click();
  await expect(panel.getByRole("status")).toHaveText("Sign in with GitHub to choose a repository.");
  await expect(panel.getByText("fixture/example", { exact: true })).toHaveCount(0);
  await expect(page.getByRole("region", { name: "Repository analysis" })).toHaveCount(0);
});

test("the filter narrows the repository list", async ({ page, context, authProxy }) => {
  await signIn(context, authProxy.origin);
  await page.goto(authProxy.origin);
  const panel = page.getByRole("region", { name: "Repository connection" });
  await panel.getByLabel("Filter repositories").fill("READ");
  await expect(panel.getByLabel("Repository", { exact: true }).locator("option"))
    .toHaveText(["Choose a repository…", "fixture/read-only (read-only)"]);
});

test("a failed list can be refreshed and the page stays usable on mobile", async ({ page, context, authProxy }, testInfo) => {
  await signIn(context, authProxy.origin);
  await page.setViewportSize({ width: 390, height: 844 });
  let attempts = 0;
  await page.route(listUrl, (route) => ++attempts === 1
    ? route.fulfill({ status: 502, json: { error: { message: "Your repositories could not be loaded from GitHub. Try again." } } })
    : route.fulfill({ json: USER_REPOSITORIES }));
  await page.goto(authProxy.origin);
  const panel = page.getByRole("region", { name: "Repository connection" });
  await expect(panel.getByRole("alert")).toHaveText("Your repositories could not be loaded from GitHub. Try again.");
  await panel.getByRole("button", { name: "Refresh list" }).click();
  await expect(panel.getByRole("status")).toHaveText("Choose a repository to refactor.");
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.screenshot({ path: testInfo.outputPath("repository-mobile.png"), fullPage: true });
});

test("users without usable repositories are pointed to the install link", async ({ page, context, authProxy }) => {
  await signIn(context, authProxy.origin);
  await page.route(listUrl, (route) => route.fulfill({ json: { ...USER_REPOSITORIES, installations: [], repositories: [] } }));
  await page.goto(authProxy.origin);
  const panel = page.getByRole("region", { name: "Repository connection" });
  await expect(panel.getByRole("status")).toContainText("Install Refactra on the repositories you want to refactor.");
  await expect(panel.getByLabel("Repository", { exact: true })).toHaveCount(0);
  await expect(panel.getByRole("link", { name: "Install Refactra on more repositories" })).toBeVisible();
});

test("a refused access check is explained and another choice can follow", async ({ page, context, authProxy }) => {
  await signIn(context, authProxy.origin);
  let attempts = 0;
  await page.route(repositoryUrl, (route) => ++attempts === 1
    ? route.fulfill({ status: 403, json: { error: { message: "Refactra is not installed on this repository. Install it on GitHub first." } } })
    : route.fulfill({ json: metadata }));
  await page.goto(authProxy.origin);
  const panel = page.getByRole("region", { name: "Repository connection" });
  const select = panel.getByLabel("Repository", { exact: true });
  await select.selectOption({ label: "fixture/example" });
  await expect(panel.getByRole("alert")).toHaveText("Refactra is not installed on this repository. Install it on GitHub first.");
  await expect(page.getByRole("region", { name: "Repository analysis" })).toHaveCount(0);
  await select.selectOption("");
  await chooseRepository(page);
});

test("access checks can outlast the shared client's seven-second timeout", async ({ page, context, authProxy }) => {
  await signIn(context, authProxy.origin);
  await page.route(repositoryUrl, async (route) => {
    await new Promise((resolve) => setTimeout(resolve, 7500));
    await route.fulfill({ json: metadata });
  });
  await page.goto(authProxy.origin);
  await chooseRepository(page);
});

test("sign-out cancels a pending check and a late result does not come back", async ({ page, context, authProxy }) => {
  await signIn(context, authProxy.origin);
  let release!: () => void;
  let delivered!: () => void;
  const gate = new Promise<void>((resolve) => { release = resolve; });
  const lateResponse = new Promise<void>((resolve) => { delivered = resolve; });
  await page.route(repositoryUrl, async (route) => {
    await gate;
    await route.fulfill({ json: metadata });
    delivered();
  });
  await page.goto(authProxy.origin);
  const panel = page.getByRole("region", { name: "Repository connection" });
  const started = page.waitForRequest((request) => request.url().endsWith("/api/v1/repositories/1001132861"));
  await panel.getByLabel("Repository", { exact: true }).selectOption({ label: "fixture/example" });
  await started;
  await expect(panel.getByRole("status")).toHaveText("Checking your access…");
  const cancelled = page.waitForEvent("requestfailed", { predicate: (request) => request.url().endsWith("/api/v1/repositories/1001132861") });
  await page.getByRole("button", { name: "Sign out", exact: true }).click();
  await cancelled;
  release();
  await lateResponse;
  await expect(panel.getByRole("status")).toHaveText("Sign in with GitHub to choose a repository.");
  await expect(panel.getByText("fixture/example", { exact: true })).toHaveCount(0);
});

test("a repository with too many branches to list takes a typed branch", async ({ page, context, authProxy }) => {
  await signIn(context, authProxy.origin);
  await page.route("**/api/v1/capabilities", (route) => route.fulfill({
    json: { mock_enabled: true, repository_analysis_enabled: true },
  }));
  await page.route(repositoryUrl, (route) => route.fulfill({ json: { ...metadata, branches_truncated: true } }));
  await page.goto(authProxy.origin);
  await chooseRepository(page);
  const branch = page.getByRole("region", { name: "Repository analysis" }).getByLabel("Base branch", { exact: true });
  await expect(branch).toHaveJSProperty("tagName", "INPUT");
  await expect(branch).toHaveValue("main");
});

test("the analysis form explains when no analysis worker is running", async ({ page, context, authProxy }) => {
  await signIn(context, authProxy.origin);
  await page.route("**/api/v1/capabilities", (route) => route.fulfill({
    json: { mock_enabled: true, repository_analysis_enabled: true, analysis_worker_available: false },
  }));
  await page.goto(authProxy.origin);
  await chooseRepository(page);
  const analysis = page.getByRole("region", { name: "Repository analysis" });
  await expect(analysis.getByText("No analysis worker is running.", { exact: false })).toBeVisible();
  await expect(analysis.getByRole("button", { name: "Start analysis" })).toBeVisible();
});

test("a waitlisted user sees the waitlist notice instead of repositories", async ({ page, context, authProxy }) => {
  await signIn(context, authProxy.origin);
  await page.route("**/api/v1/auth/session", (route) => route.fulfill({
    json: { authenticated: true, user: { user_id: 777, login: "newcomer" }, access: "waitlisted" },
  }));
  await page.goto(authProxy.origin);
  const panel = page.getByRole("region", { name: "Repository connection" });
  await expect(panel.getByRole("status")).toHaveText(
    "You're on the Refactra waitlist. You'll get access once the owner invites you.",
  );
  await expect(panel.getByLabel("Repository", { exact: true })).toHaveCount(0);
  await page.goto(`${authProxy.origin}/history`);
  await expect(page.getByText("You're on the Refactra waitlist.", { exact: false })).toBeVisible();
  expect(authProxy.calls.some((call) => call.url.startsWith("/api/v1/user/repositories")
    || call.url.startsWith("/api/v1/runs") || call.url === "/api/v1/usage")).toBe(false);
});
