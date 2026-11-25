import { test, expect } from "../fixtures/auth-proxy";

test.beforeEach(async ({ authProxy }) => { authProxy.calls.length = 0; });

for (const cookieName of ["refactra_session", "__Host-refactra_session"]) {
  test(`repository GET forwards only the ${cookieName} session`, async ({ authProxy, request }) => {
    const response = await request.get(`${authProxy.origin}/api/v1/repositories/42?view=summary`, {
      headers: {
        Cookie: `${cookieName}=fixture-session; refactra_oauth=attempt; __Host-refactra_oauth=secure-attempt; analytics=private`,
        Authorization: "Bearer must-not-pass",
      },
    });
    expect(response.status()).toBe(200);
    expect(response.headers()["cache-control"]).toBe("no-store");
    expect(response.headers()["content-type"]).toContain("application/json");
    expect(response.headersArray().filter((header) => header.name.toLowerCase() === "set-cookie")).toEqual([]);
    expect(await response.json()).toEqual({
      repository_id: 42, full_name: "fixture/example", default_branch: "main", access: "read-only",
    });
    expect(authProxy.calls).toHaveLength(1);
    expect(authProxy.calls[0].url).toBe("/api/v1/repositories/42?view=summary");
    expect(authProxy.calls[0].headers.cookie).toBe(`${cookieName}=fixture-session`);
    expect(authProxy.calls[0].headers.authorization).toBeUndefined();
  });
}

test("the configured repository route forwards only the session cookie", async ({ authProxy, request }) => {
  const response = await request.get(`${authProxy.origin}/api/v1/repository`, {
    headers: { Cookie: "refactra_session=fixture-session; analytics=private", Authorization: "Bearer must-not-pass" },
  });
  expect(response.status()).toBe(200);
  expect(response.headers()["cache-control"]).toBe("no-store");
  expect((await response.json()).repository_id).toBe(1001132861);
  expect(authProxy.calls[0].url).toBe("/api/v1/repository");
  expect(authProxy.calls[0].headers.cookie).toBe("refactra_session=fixture-session");
  expect(authProxy.calls[0].headers.authorization).toBeUndefined();
});

test("anonymous repository requests retain the backend 401", async ({ authProxy, request }) => {
  const response = await request.get(`${authProxy.origin}/api/v1/repositories/42`, {
    headers: { Cookie: "refactra_oauth=attempt; analytics=private" },
  });
  expect(response.status()).toBe(401);
  expect(response.headers()["cache-control"]).toBe("no-store");
  expect((await response.json()).error.code).toBe("authentication_required");
  expect(authProxy.calls[0].headers.cookie).toBeUndefined();
});

for (const [id, status, code] of [[43, 403, "repository_access_denied"], [44, 502, "github_unavailable"], [45, 503, "repository_access_unconfigured"]] as const) {
  test(`repository status ${status} and JSON pass through uncached`, async ({ authProxy, request }) => {
    const response = await request.get(`${authProxy.origin}/api/v1/repositories/${id}`, {
      headers: { Cookie: "refactra_session=fixture-session" },
    });
    expect(response.status()).toBe(status);
    expect(response.headers()["cache-control"]).toBe("no-store");
    expect(await response.json()).toEqual({ error: { code, message: "Fixture repository access failed." } });
  });
}

for (const [method, path, status] of [
  ["POST", "42", 404], ["HEAD", "42", 404], ["DELETE", "42", 405],
  ["GET", "0", 404], ["GET", "042", 404], ["GET", "-42", 404],
  ["GET", "42/files", 404], ["GET", "not-an-id", 404],
] as const) {
  test(`blocks repository route ${method} ${path}`, async ({ authProxy, request }) => {
    const response = await request.fetch(`${authProxy.origin}/api/v1/repositories/${path}`, { method });
    expect(response.status()).toBe(status);
    expect(authProxy.calls).toHaveLength(0);
  });
}

test("repository reads can outlast the standard five-second proxy timeout", async ({ authProxy, request }) => {
  const response = await request.get(`${authProxy.origin}/api/v1/repositories/46`, {
    headers: { Cookie: "refactra_session=fixture-session" },
  });
  expect(response.status()).toBe(200);
  expect((await response.json()).repository_id).toBe(46);
});

test("an unreachable repository upstream returns a sanitized uncached 503", async ({ authProxy, request }) => {
  const response = await request.get(`${authProxy.origin}/api/v1/repositories/47`);
  expect(response.status()).toBe(503);
  expect(response.headers()["cache-control"]).toBe("no-store");
  expect((await response.json()).error.code).toBe("api_unavailable");
});
