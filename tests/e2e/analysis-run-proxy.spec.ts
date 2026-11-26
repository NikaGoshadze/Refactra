import { PLAN, PLAN_APPROVAL_PATH, PLAN_PATH, PUBLICATION_APPROVAL_PATH, RESULT, RESULT_PATH, RUN_ID, RUN_PATH, SAVED_RUN, WAIVER_PATH, expect, test } from "../fixtures/auth-proxy";

const SUBMIT = "/api/v1/runs";
const IDENTITY = "fd55f548-7593-46cc-ae08-9c651d1a2c3a";
const BODY = { repository_id: 42, base_branch: "feature/cart", task_text: "Review cart" };
const OTHER_COOKIES = "refactra_oauth=attempt; __Host-refactra_oauth=secure-attempt; analytics=private";

test.beforeEach(async ({ authProxy }) => { authProxy.calls.length = 0; });

test("submission forwards its body and session, then returns the receipt and status location", async ({ authProxy, request }) => {
  // The fixture takes more than the default five seconds to prepare the run.
  const response = await request.post(authProxy.origin + SUBMIT, {
    data: BODY,
    headers: {
      Cookie: `refactra_session=fixture-session; ${OTHER_COOKIES}`,
      Authorization: "Bearer must-not-pass",
      Origin: authProxy.origin,
      "X-Refactra-CSRF": "1",
      "Idempotency-Key": IDENTITY,
    },
  });
  expect(response.status()).toBe(202);
  expect(await response.json()).toEqual({ run_id: RUN_ID });
  expect(response.headers().location).toBe(RUN_PATH);
  expect(response.headers()["cache-control"]).toBe("no-store");
  expect(authProxy.calls).toHaveLength(1);
  const [call] = authProxy.calls;
  expect([call.method, call.url]).toEqual(["POST", SUBMIT]);
  expect(JSON.parse(call.body!)).toEqual(BODY);
  expect(call.headers.cookie).toBe("refactra_session=fixture-session");
  expect(call.headers.authorization).toBeUndefined();
  expect(call.headers.origin).toBe(authProxy.origin);
  expect(call.headers["x-refactra-csrf"]).toBe("1");
  expect(call.headers["idempotency-key"]).toBe(IDENTITY);
});

for (const origin of [undefined, "https://untrusted.example"]) {
  test(`submission sends the original Origin (${origin ?? "missing"})`, async ({ authProxy, request }) => {
    const response = await request.post(authProxy.origin + SUBMIT, {
      data: BODY,
      headers: { "X-Refactra-CSRF": "1", ...(origin ? { Origin: origin } : {}) },
    });
    expect(response.status()).toBe(403);
    expect(await response.json()).toEqual({ error: { code: "csrf_rejected" } });
    expect(authProxy.calls[0].headers.origin).toBe(origin);
  });
}

const WRITE_HEADERS = { "Content-Type": "application/json", "X-Refactra-CSRF": "1" };

function streamed(chunks: string[], gapMs = 0) {
  const encoder = new TextEncoder();
  let first = true;
  return new ReadableStream<Uint8Array>({
    async pull(controller) {
      const next = chunks.shift();
      if (next === undefined) return controller.close();
      // The first chunk starts the request; later chunks arrive after the gap.
      if (gapMs && !first) await new Promise((resolve) => setTimeout(resolve, gapMs));
      first = false;
      controller.enqueue(encoder.encode(next));
    },
  });
}

async function postStream(url: string, body: ReadableStream<Uint8Array>) {
  // A streamed body has no Content-Length, like a chunked upload.
  return fetch(url, { method: "POST", body, duplex: "half", headers: WRITE_HEADERS } as RequestInit);
}

test("an oversized submission is rejected before reaching the API", async ({ authProxy, request }) => {
  const response = await request.post(authProxy.origin + SUBMIT, {
    data: JSON.stringify({ task_text: "x".repeat(17 * 1024) }), headers: { ...WRITE_HEADERS, Origin: authProxy.origin },
  });
  expect(response.status()).toBe(413);
  expect((await response.json()).error.code).toBe("request_too_large");
  expect(response.headers()["cache-control"]).toBe("no-store");
  expect(authProxy.calls).toHaveLength(0);
});

test("a chunked body without Content-Length is still bounded", async ({ authProxy }) => {
  const response = await postStream(authProxy.origin + SUBMIT, streamed(Array(5).fill("x".repeat(8192))));
  expect(response.status).toBe(413);
  expect(authProxy.calls).toHaveLength(0);
});

test("a slow body is cut off at the read deadline", async ({ authProxy }) => {
  const started = Date.now();
  const response = await postStream(authProxy.origin + SUBMIT, streamed(['{"repository_id": 42,', '"task_text": "late"}'], 2500));
  expect(response.status).toBe(408);
  expect((await response.json()).error.code).toBe("request_timeout");
  expect(Date.now() - started).toBeLessThan(2500);
  expect(authProxy.calls).toHaveLength(0);
});

test("a large plan edit within its limit still reaches the API", async ({ authProxy, request }) => {
  const proposal = { ...PLAN.content, summary: "x".repeat(40 * 1024) };
  const response = await request.post(authProxy.origin + PLAN_PATH, {
    data: { expected_revision: 1, proposal },
    headers: { ...WRITE_HEADERS, Origin: authProxy.origin, Cookie: "refactra_session=fixture-session" },
  });
  expect(response.status()).toBe(200);
  expect(authProxy.calls).toHaveLength(1);
});

test("check waivers forward the body, session, Origin and CSRF header", async ({ authProxy, request }) => {
  const waiver = { result_id: RESULT.id, result_sha256: RESULT.sha256, check_index: 0, reason: "Reviewed manually." };
  const response = await request.post(authProxy.origin + WAIVER_PATH, {
    data: waiver,
    headers: {
      Cookie: `refactra_session=fixture-session; ${OTHER_COOKIES}`,
      Authorization: "Bearer must-not-pass",
      Origin: authProxy.origin,
      "X-Refactra-CSRF": "1",
    },
  });
  expect(response.status()).toBe(200);
  expect(await response.json()).toEqual({ ...waiver, check_id: "python.pytest", waived_by: 12345 });
  expect(response.headers()["cache-control"]).toBe("no-store");
  const [call] = authProxy.calls;
  expect([call.method, call.url]).toEqual(["POST", WAIVER_PATH]);
  expect(JSON.parse(call.body!)).toEqual(waiver);
  expect(call.headers.cookie).toBe("refactra_session=fixture-session");
  expect(call.headers.authorization).toBeUndefined();
  expect(call.headers.origin).toBe(authProxy.origin);
  expect(call.headers["x-refactra-csrf"]).toBe("1");
});

test("status forwards the HTTPS session and returns uncached analysis", async ({ authProxy, request }) => {
  const response = await request.get(authProxy.origin + RUN_PATH, {
    headers: { Cookie: `__Host-refactra_session=fixture-session; ${OTHER_COOKIES}` },
  });
  expect(response.status()).toBe(200);
  expect(await response.json()).toEqual(SAVED_RUN);
  expect(response.headers()["cache-control"]).toBe("no-store");
  expect(response.headers()["set-cookie"]).toBeUndefined();
  expect(authProxy.calls[0].headers.cookie).toBe("__Host-refactra_session=fixture-session");
});

for (const [route, artifact] of [[PLAN_PATH, PLAN], [RESULT_PATH, RESULT]] as const) {
  test(`forwards the revision and only the session cookie for ${route}`, async ({ authProxy, request }) => {
    const path = `${route}?revision=1`;
    const response = await request.get(authProxy.origin + path, {
      headers: { Cookie: `refactra_session=fixture-session; ${OTHER_COOKIES}` },
    });
    expect(response.status()).toBe(200);
    expect(await response.json()).toEqual(artifact);
    expect(response.headers()["cache-control"]).toBe("no-store");
    expect(response.headers()["set-cookie"]).toBeUndefined();
    expect(authProxy.calls[0]).toMatchObject({ url: path, headers: { cookie: "refactra_session=fixture-session" } });
  });
}

for (const path of [RUN_PATH, PLAN_PATH, RESULT_PATH]) {
  test(`preserves the upstream authentication error for ${path}`, async ({ authProxy, request }) => {
    const response = await request.get(authProxy.origin + path, { headers: { Cookie: OTHER_COOKIES } });
    expect(response.status()).toBe(401);
    expect(await response.json()).toEqual({ error: { code: "authentication_required" } });
    expect(authProxy.calls[0].headers.cookie).toBeUndefined();
  });
}

for (const [method, route, status] of [
  // GET SUBMIT (/api/v1/runs) is the run history now; run-history.spec.ts covers it.
  ["POST", RUN_PATH, 404], ["DELETE", PLAN_PATH, 405], ["DELETE", RUN_PATH, 405],
  ["GET", PLAN_APPROVAL_PATH, 404], ["POST", `${PLAN_APPROVAL_PATH}/extra`, 404],
  ["POST", RESULT_PATH, 404], ["GET", `${RESULT_PATH}/extra`, 404],
  ["GET", PUBLICATION_APPROVAL_PATH, 404], ["POST", `${PUBLICATION_APPROVAL_PATH}/extra`, 404],
  ["GET", WAIVER_PATH, 404], ["POST", `${WAIVER_PATH}/extra`, 404],
  ["GET", `${RUN_PATH}/events`, 404], ["GET", `${SUBMIT}/${RUN_ID.replaceAll("-", "")}`, 404],
] as const) {
  test(`blocks ${method} ${route}`, async ({ authProxy, request }) => {
    const response = await request.fetch(authProxy.origin + route, { method });
    expect(response.status()).toBe(status);
    expect(authProxy.calls).toHaveLength(0);
  });
}
