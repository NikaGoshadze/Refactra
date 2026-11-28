import { test, expect } from "../fixtures/auth-proxy";

test.beforeEach(async ({ page, authProxy }) => {
  authProxy.calls.length = 0;
  await page.route("**/api/v1/capabilities", (route) => route.fulfill({ json: { mock_enabled: true } }));
});

async function signIn(context: import("@playwright/test").BrowserContext, origin: string, value = "fixture-session") {
  await context.addCookies([{ name: "refactra_session", value, url: origin, httpOnly: true }]);
}

test("the sign-in page shows a real change and a GitHub-only card at both widths", async ({ page, authProxy }, testInfo) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.goto(`${authProxy.origin}/sign-in`);
  await expect(page).toHaveTitle("Sign in · Refactra");
  await expect(page.getByRole("heading", { level: 1 })).toHaveText("Refactoring you review before it reaches GitHub.");
  const diff = page.getByRole("figure", { name: "Example change from a Refactra run" });
  await expect(diff.getByText("pizza_app/main.py")).toBeVisible();
  await expect(diff.locator(".diff-split")).toBeVisible();
  await expect(page.getByRole("link", { name: "Sign in with GitHub" })).toHaveAttribute("href", "/api/v1/auth/github/login");
  await page.screenshot({ path: testInfo.outputPath("sign-in-desktop.png"), fullPage: true });
  await page.setViewportSize({ width: 390, height: 844 });
  // Phones get the unified view, and the page never scrolls sideways.
  await expect(diff.locator(".diff-unified")).toBeVisible();
  await expect(diff.locator(".diff-split")).toBeHidden();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.screenshot({ path: testInfo.outputPath("sign-in-mobile.png"), fullPage: true });
});

test("only same-site return paths are passed on to GitHub sign-in", async ({ page, authProxy }) => {
  await page.goto(`${authProxy.origin}/sign-in?next=${encodeURIComponent("/runs/abc?tab=plan")}`);
  await expect(page.getByRole("link", { name: "Sign in with GitHub" }))
    .toHaveAttribute("href", `/api/v1/auth/github/login?next=${encodeURIComponent("/runs/abc?tab=plan")}`);
  await page.goto(`${authProxy.origin}/sign-in?next=${encodeURIComponent("//evil.example/")}`);
  await expect(page.getByRole("link", { name: "Sign in with GitHub" })).toHaveAttribute("href", "/api/v1/auth/github/login");
});

test("a signed-in visitor on the sign-in page goes on to the requested page", async ({ page, context, authProxy }) => {
  await signIn(context, authProxy.origin);
  await page.goto(`${authProxy.origin}/sign-in?next=%2Fhistory`);
  await expect(page).toHaveURL(`${authProxy.origin}/history`);
});

test("an expired session cookie still ends on the sign-in page with the return path", async ({ page, context, authProxy }) => {
  await signIn(context, authProxy.origin, "expired-session");
  await page.goto(`${authProxy.origin}/history`);
  await expect(page).toHaveURL(`${authProxy.origin}/sign-in?next=%2Fhistory`);
  expect(authProxy.calls.some((call) => call.url.startsWith("/api/v1/runs"))).toBe(false);
});

test("the waitlist page needs a session and sends invited users to the workspace", async ({ page, context, authProxy }) => {
  await page.goto(`${authProxy.origin}/waitlist`);
  await expect(page).toHaveURL(`${authProxy.origin}/sign-in?next=%2Fwaitlist`);
  await signIn(context, authProxy.origin);
  await page.goto(`${authProxy.origin}/waitlist`);
  await expect(page).toHaveURL(`${authProxy.origin}/`);
  await expect(page.getByRole("heading", { name: "Workspace" })).toBeVisible();
});

test("a removed account sees that it has no access", async ({ page, context, authProxy }, testInfo) => {
  await signIn(context, authProxy.origin);
  await page.route("**/api/v1/auth/session", (route) => route.fulfill({
    json: { authenticated: true, user: { user_id: 778, login: "former" }, access: "denied" },
  }));
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto(`${authProxy.origin}/runs/367dfc44-100b-4af2-9ed7-ddf97ae24d52`);
  await expect(page).toHaveURL(`${authProxy.origin}/waitlist`);
  await expect(page.getByRole("heading", { name: "Your account does not have access" })).toBeVisible();
  await expect(page.getByRole("link", { name: "delete your account and data" })).toHaveAttribute("href", "/privacy");
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.screenshot({ path: testInfo.outputPath("waitlist-denied-mobile.png"), fullPage: true });
});
