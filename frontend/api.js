/**
 * The one place that talks to the control plane.
 *
 * Split out of app.js because the previous single file mixed transport,
 * error shaping and rendering, and the bugs were all in the seams: a rejected
 * promise from a form handler went nowhere, and a FastAPI 422 -- whose `detail`
 * is a *list of objects*, not a string -- was rendered as `[object Object]` or
 * a JSON blob.
 */

const TOKEN_KEY = "maludb.sessionToken";

/**
 * Where the control plane is, and it is not configurable.
 *
 * There was a "control-plane API base" field on the page. It came from this
 * frontend's first life as a developer console pointed at localhost:8112, and
 * it had no business on a page whose job is selling: internal vocabulary in
 * front of a visitor, a second and worse way to do what `dev-server.py --api`
 * already does, and an invitation to aim the page at an arbitrary origin.
 *
 * `/api` is same-origin by necessity rather than by preference -- the control
 * plane ships no CORS middleware, so the page and the API must share an origin
 * either way. Development gets there through `dev-server.py`'s proxy and
 * production through the reverse proxy in docs/DEPLOYMENT.md.
 */
const API_BASE = "/api";

export class ApiError extends Error {
  constructor(message, { status, retryAfter = null, fields = {} } = {}) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.retryAfter = retryAfter;
    // field name -> message, so a form can put the error next to the input
    // that caused it rather than in a banner that says "422".
    this.fields = fields;
  }
}

export const session = {
  get token() {
    try {
      return localStorage.getItem(TOKEN_KEY) || "";
    } catch {
      return ""; // private mode, or site data blocked
    }
  },
  set token(value) {
    try {
      if (value) localStorage.setItem(TOKEN_KEY, value);
      else localStorage.removeItem(TOKEN_KEY);
    } catch {
      /* the session still works for this page load */
    }
  },
  get base() {
    return API_BASE;
  },
};

/**
 * Turn whatever the control plane returned into one readable sentence, plus
 * per-field messages where the shape gives them.
 *
 * FastAPI/Pydantic validation answers 422 with
 *   {"detail": [{"loc": ["body", "password"], "msg": "String should have at
 *    least 12 characters", "type": "string_too_short"}]}
 * which is the case the old client stringified wholesale.
 */
function describe(status, statusText, payload) {
  const fields = {};
  const detail = payload && payload.detail;

  if (Array.isArray(detail)) {
    const parts = [];
    for (const item of detail) {
      const loc = Array.isArray(item.loc) ? item.loc : [];
      // Drop the leading "body"/"query"/"path" segment; what a person needs is
      // the field name.
      const field = loc.filter((s) => s !== "body" && s !== "query" && s !== "path").join(".");
      const msg = item.msg || "is not valid";
      if (field) {
        fields[field] = msg;
        parts.push(`${field}: ${msg}`);
      } else {
        parts.push(msg);
      }
    }
    return { message: parts.join("; ") || "That request was rejected.", fields };
  }

  if (typeof detail === "string" && detail) return { message: detail, fields };
  if (payload && typeof payload.message === "string") return { message: payload.message, fields };
  return { message: `${status} ${statusText}`.trim(), fields };
}

export async function api(path, { method = "GET", body, headers = {}, auth = true } = {}) {
  const head = new Headers(headers);
  if (body !== undefined && !head.has("Content-Type")) {
    head.set("Content-Type", "application/json");
  }
  if (auth && session.token) head.set("Authorization", `Bearer ${session.token}`);

  let response;
  try {
    response = await fetch(`${session.base}${path}`, {
      method,
      headers: head,
      body: body === undefined ? undefined : JSON.stringify(body),
    });
  } catch (cause) {
    // A network-level failure is the single most common thing to hit while
    // setting this up (wrong API base, control plane not running, CORS), and
    // "Failed to fetch" tells a person nothing about which.
    throw new ApiError(
      `Could not reach the control plane at ${session.base}. Check that it is running and that the reverse proxy forwards ${session.base} to it.`,
      { status: 0 },
    );
  }

  if (response.status === 204) return null;

  const text = await response.text();
  let payload = null;
  if (text) {
    try {
      payload = JSON.parse(text);
    } catch {
      payload = null; // an HTML error page from a proxy, most likely
    }
  }

  if (!response.ok) {
    const { message, fields } = describe(response.status, response.statusText, payload);
    const retryAfter = Number(response.headers.get("Retry-After")) || null;
    throw new ApiError(message, { status: response.status, retryAfter, fields });
  }
  return payload;
}

/* ------------------------------------------------------------------ *
 * Auth
 * ------------------------------------------------------------------ */

/**
 * Create the account and return a signed-in session.
 *
 * `POST /v1/auth/signup` answers 201 with the user and **no token** -- so a
 * client that stops there leaves the person staring at a sign-in form, retyping
 * the credentials they just chose. That was the old flow. Signing in
 * immediately afterwards is what makes signup a funnel rather than a form.
 */
export async function signUp({ email, password, displayName, captchaToken }) {
  const body = { email, password };
  if (displayName) body.display_name = displayName;
  // Sent only when there is one. The route reads `captcha_token` and checks it
  // only when the deployment requires a challenge.
  if (captchaToken) body.captcha_token = captchaToken;

  await api("/v1/auth/signup", { method: "POST", body, auth: false });
  return signIn({ email, password });
}

export async function signIn({ email, password }) {
  const out = await api("/v1/auth/signin", {
    method: "POST",
    body: { email, password },
    auth: false,
  });
  session.token = out.token;
  return out;
}

export async function signOut() {
  try {
    await api("/v1/auth/signout", { method: "POST" });
  } catch {
    // An expired or already-revoked token still has to leave this browser.
  }
  session.token = "";
}

export const me = () => api("/v1/auth/me");
export const listOrganizations = () => api("/v1/organizations");
export const listPlans = () => api("/v1/plans");
export const listProjects = (orgId) => api(`/v1/organizations/${orgId}/projects`);
export const getProject = (ref) => api(`/v1/projects/${encodeURIComponent(ref)}`);

/**
 * `crypto.randomUUID` exists only in a secure context, so it is present on
 * https and on localhost and absent if this is ever served over plain http
 * from another host. The fallback does not need to be cryptographically
 * strong -- this value only has to be unique per attempt.
 */
function idempotencyKey() {
  if (globalThis.crypto?.randomUUID) return crypto.randomUUID();
  return `k-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 12)}`;
}

export const createProject = (orgId, { displayName, planCode }) =>
  api(`/v1/organizations/${orgId}/projects`, {
    method: "POST",
    body: { display_name: displayName, plan_code: planCode },
    // A retried create must not make a second database on a node. The route
    // reads this header and replays the first result.
    headers: { "Idempotency-Key": idempotencyKey() },
  });

/* ------------------------------------------------------------------ *
 * Plan and usage (launch slice 2)
 * ------------------------------------------------------------------ */

const projectPath = (ref) => `/v1/projects/${encodeURIComponent(ref)}`;

/** What a project has used against its plan, and its billing period. Members may read it. */
export const getUsage = (ref) => api(`${projectPath(ref)}/usage`);

/**
 * Open a hosted Stripe Checkout for `planCode` and return where to send the customer.
 * Owner or admin only (403 otherwise); 503 when this deployment takes no payments.
 */
export const startCheckout = (ref, planCode) =>
  api(`${projectPath(ref)}/billing/checkout`, { method: "POST", body: { plan_code: planCode } });

/** Ask an operator to move the project -- the path when billing is not configured. */
export const requestUpgrade = (ref, planCode) =>
  api(`${projectPath(ref)}/upgrade-request`, {
    method: "POST",
    body: { requested_plan_code: planCode },
  });

export const getUpgradeRequest = (ref) => api(`${projectPath(ref)}/upgrade-request`);

/* ------------------------------------------------------------------ *
 * API keys (Phase 07 slice 2)
 *
 * A publishable key is listed with its value, because it ships in a client
 * bundle and a dashboard shows it every time. A secret key is stored as a
 * verifier only: its value comes back once, in the answer to creating it, and
 * no route can return it again.
 * ------------------------------------------------------------------ */

/** The project's keys: identifiers for all, the value for live publishable keys only. Members may read it. */
export const listApiKeys = (ref) => api(`${projectPath(ref)}/api-keys`);

/** Owner or admin. The answer carries the key; for a secret key it is the only time it ever will. */
export const createApiKey = (ref, { keyType, name }) =>
  api(`${projectPath(ref)}/api-keys`, { method: "POST", body: { key_type: keyType, name } });

/** Owner or admin. Revoking is how a key is reset: create the replacement first, then revoke. */
export const revokeApiKey = (ref, keyId) =>
  api(`${projectPath(ref)}/api-keys/${encodeURIComponent(keyId)}`, { method: "DELETE" });

/* ------------------------------------------------------------------ *
 * Memory spaces (ADR-079)
 *
 * The control-plane half of memory: spaces, their models, and the project's
 * provider keys. Ingest and search run on the project's own host with its
 * secret key, not here -- a person's session never writes a memory.
 * ------------------------------------------------------------------ */

/** The project's spaces and its plan's memory limits. Members may read it. */
export const listMemorySpaces = (ref) => api(`${projectPath(ref)}/maludb/memory/spaces`);

/** Owner or admin. 202 while it is built; 200 when it already exists. */
export const createMemorySpace = (ref, name) =>
  api(`${projectPath(ref)}/maludb/memory/spaces`, { method: "POST", body: { name } });

/** Owner or admin. Irreversible: the space and every memory in it are removed. */
export const deleteMemorySpace = (ref, name) =>
  api(`${projectPath(ref)}/maludb/memory/spaces/${encodeURIComponent(name)}`, { method: "DELETE" });

/** Owner or admin. Model names may be empty, which means the provider's default. */
export const setMemoryModels = (ref, name, models) =>
  api(`${projectPath(ref)}/maludb/memory/spaces/${encodeURIComponent(name)}/models`, { method: "PUT", body: models });

/** Which providers have a key set, and each key's last four characters. Never a key. */
export const listProviderKeys = (ref) => api(`${projectPath(ref)}/maludb/memory/provider-keys`);

/** Owner or admin. Write-only: no route returns the key afterwards. */
export const setProviderKey = (ref, provider, apiKey) =>
  api(`${projectPath(ref)}/maludb/memory/provider-keys/${encodeURIComponent(provider)}`, {
    method: "PUT",
    body: { api_key: apiKey },
  });

export const removeProviderKey = (ref, provider) =>
  api(`${projectPath(ref)}/maludb/memory/provider-keys/${encodeURIComponent(provider)}`, { method: "DELETE" });
