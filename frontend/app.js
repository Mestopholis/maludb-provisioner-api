/**
 * MaluDB console — signup funnel first.
 *
 * What this rewrite fixes, all of it observable rather than stylistic:
 *
 *  1. **Signup did not sign you in.** `POST /v1/auth/signup` returns the user
 *     and no token, so the old flow toasted "sign in with the same
 *     credentials" and made a new customer retype them. `signUp()` now chains
 *     the signin.
 *  2. **Nothing caught a rejected promise.** Form handlers were `async`
 *     functions wired straight to `submit`, so a 401, a 409 or a dead control
 *     plane produced an unhandled rejection and a page that visibly did
 *     nothing. Every submit now goes through `submit()`.
 *  3. **No challenge token.** `captcha_required` defaults to true when
 *     `MALUDB_ENV=production`, and the old client hard-coded
 *     `captcha_token: null` -- so signup worked in development and failed on
 *     the day it mattered. Turnstile is mounted when a site key is configured.
 *  4. **422s were unreadable.** See `describe()` in api.js.
 *  5. **No client-side password rule**, so "too short" cost a round trip and
 *     came back as a Pydantic blob. The API's minimum is 12.
 */

import {
  ApiError,
  api,
  createApiKey,
  createMemorySpace,
  createProject,
  deleteMemorySpace,
  getUpgradeRequest,
  getUsage,
  listOrganizations,
  listApiKeys,
  listPlans,
  listMemorySpaces,
  listProjects,
  listProviderKeys,
  me,
  removeProviderKey,
  revokeApiKey,
  session,
  setMemoryModels,
  setProviderKey,
  signIn,
  signOut,
  signUp,
  requestUpgrade,
  startCheckout,
} from "./api.js";

const PASSWORD_MIN = 12; // services/control_plane/api/auth.py: SignupIn

/**
 * The public pricing view.
 *
 * Deliberately not `/v1/plans`. ADR-037 keeps that endpoint authenticated
 * because it returns `plans.config_json.limits` verbatim -- `work_mem_mb`,
 * `temp_file_limit_mb`, `postgrest_pool_size`, statement and lock timeouts --
 * and publishing those tells anyone designing a workload precisely where every
 * threshold sits. The ADR says the public view should be "a curated projection
 * with prices in it", which is this.
 *
 * So these are customer-facing quotas, not internal tuning knobs. What is
 * published is what a customer needs to choose a plan -- storage, egress,
 * projects, connections, recovery window, Realtime and email volume. What is
 * withheld is what ADR-037 names: `work_mem_mb`, `temp_file_limit_mb`,
 * `postgrest_pool_size` and the statement, lock and idle-transaction timeouts,
 * which describe where a workload would have to sit to stay under them.
 *
 * Every entry carries the `key` and `value` it publishes, so
 * `tests/test_public_pricing.py` can prove this page matches
 * `entitlements.DEFAULTS` instead of trusting it. That test is the reason this
 * is no longer "the one place nothing checks" -- an earlier version of this
 * file advertised 1 GB of database storage on Free, which is the *object*
 * storage limit; the database limit is 500 MB.
 */
const PUBLIC_PLANS = [
  {
    code: "free",
    name: "Developer",
    price: "$0",
    // No cadence. "$0" needs no qualifier, and "forever" would commit the
    // platform to a permanence nobody has decided on.
    cadence: "",
    lede: "A real database, not a sandbox.",
    specs: [
      { key: "database_storage_bytes", value: 524288000, label: "500 MB database" },
      { key: "object_storage_bytes", value: 1073741824, label: "1 GB file storage" },
      { key: "egress_bytes_per_month", value: 5368709120, label: "5 GB egress a month" },
      { key: "max_projects", value: 2, label: "2 projects" },
      { key: "api_requests_per_window", value: 300, label: "300 API requests a minute" },
      { key: "database_connections", value: 10, label: "10 pooled connections" },
      { key: "emails_per_month", value: 1000, label: "1,000 emails a month" },
      { key: "backup_retention_days", value: 7, label: "7 days of backups" },
    ],
    // Free is what it is; saying so on the card is cheaper than a support
    // ticket from somebody who found out after building on it.
    excludes: [
      "No direct database connection — API access only",
      "No point-in-time recovery",
      "No Realtime subscriptions",
    ],
  },
  {
    code: "starter",
    name: "Builder",
    price: "$49",
    cadence: "per project / month",
    lede: "When you need to connect to it yourself.",
    featured: true,
    specs: [
      { key: "database_storage_bytes", value: 8589934592, label: "8 GB database" },
      { key: "object_storage_bytes", value: 26843545600, label: "25 GB file storage" },
      { key: "egress_bytes_per_month", value: 107374182400, label: "100 GB egress a month" },
      { key: "max_projects", value: 3, label: "3 projects" },
      { key: "api_requests_per_window", value: 3000, label: "3,000 API requests a minute" },
      { key: "database_connections", value: 30, label: "30 direct connections" },
      { key: "realtime_connections", value: 200, label: "200 Realtime connections" },
      { key: "emails_per_month", value: 50000, label: "50,000 emails a month" },
      { key: "pitr_window_hours", value: 168, label: "7-day point-in-time recovery" },
      { key: "backup_retention_days", value: 14, label: "14 days of backups" },
    ],
    includes: ["Direct PostgreSQL connection", "Send email from your own domain"],
  },
  {
    code: "production",
    name: "Professional",
    price: "$149",
    cadence: "per project / month",
    lede: "For the ones that page you.",
    specs: [
      { key: "database_storage_bytes", value: 53687091200, label: "50 GB database" },
      { key: "object_storage_bytes", value: 107374182400, label: "100 GB file storage" },
      { key: "egress_bytes_per_month", value: 268435456000, label: "250 GB egress a month" },
      { key: "max_projects", value: 25, label: "25 projects" },
      { key: "api_requests_per_window", value: 30000, label: "30,000 API requests a minute" },
      { key: "database_connections", value: 90, label: "90 direct connections" },
      { key: "realtime_connections", value: 2000, label: "2,000 Realtime connections" },
      { key: "emails_per_month", value: 1000000, label: "1,000,000 emails a month" },
      { key: "pitr_window_hours", value: 336, label: "14-day point-in-time recovery" },
      { key: "backup_retention_days", value: 30, label: "30 days of backups" },
    ],
    includes: ["Everything in Builder", "Higher resource limits per query"],
  },
];

/** Signups are closed until the platform is deployed. One line to flip. */
const signupsOpen = () => window.MALUDB_SIGNUPS_OPEN === true;

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

const state = {
  me: null,
  orgs: [],
  plans: [],
  projects: [],
  // project_ref -> the last /usage answer, and which panel is open. Kept across
  // the dashboard's polling re-render so an open panel does not snap shut.
  usage: {},
  upgradeRequests: {},
  openUsage: null,
  // project_ref -> {spaces, keys} or {error}, and which memory panel is open.
  memory: {},
  openMemory: null,
  // project_ref -> the key listing or {error}, and which keys panel is open.
  apiKeys: {},
  openKeys: null,
  // project_ref -> a key just created, while it is on screen. The only place a
  // secret key's value is ever held: dropped when dismissed, when its panel is
  // closed, and on sign-out, and never written to storage.
  issuedKey: {},
};

/* ------------------------------------------------------------------ *
 * Rendering helpers
 * ------------------------------------------------------------------ */

const escapeHtml = (value) =>
  String(value ?? "").replace(
    /[&<>"']/g,
    (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c],
  );

function toast(message, kind = "info") {
  const node = $("#toast");
  node.textContent = message;
  node.dataset.kind = kind;
  node.classList.add("show");
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => node.classList.remove("show"), 5000);
}

/** Put an error where the person is looking: on the form they just submitted. */
function showFormError(form, error) {
  clearFormErrors(form);
  const banner = $(".form-error", form);
  if (banner) {
    banner.textContent = error.message;
    banner.hidden = false;
  }
  for (const [field, message] of Object.entries(error.fields || {})) {
    const input = form.elements[field];
    if (!input) continue;
    input.setAttribute("aria-invalid", "true");
    const hint = form.querySelector(`[data-error-for="${field}"]`);
    if (hint) {
      hint.textContent = message;
      hint.hidden = false;
    }
  }
}

function clearFormErrors(form) {
  const banner = $(".form-error", form);
  if (banner) {
    banner.hidden = true;
    banner.textContent = "";
  }
  $$("[data-error-for]", form).forEach((n) => {
    n.hidden = true;
    n.textContent = "";
  });
  $$("[aria-invalid]", form).forEach((n) => n.removeAttribute("aria-invalid"));
}

/**
 * Wire a form so a failure is always visible and the button cannot be
 * double-fired. This is the piece whose absence made the old console feel
 * broken: every one of these paths can fail, and none of them said so.
 */
function submit(form, handler) {
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    runForm(form, handler);
  });
}

/** The body of `submit`, for forms rendered after wiring -- the memory panel's. */
async function runForm(form, handler) {
  clearFormErrors(form);
  const button = form.querySelector('button[type="submit"]');
  const label = button?.textContent;
  if (button) {
    button.disabled = true;
    button.textContent = button.dataset.busy || "Working…";
  }
  try {
    await handler(new FormData(form), form);
  } catch (error) {
    if (error instanceof ApiError) {
      showFormError(form, error);
      if (error.status === 429 && error.retryAfter) {
        toast(`Too many attempts. Try again in ${error.retryAfter}s.`, "error");
      } else {
        toast(error.message, "error");
      }
    } else {
      showFormError(form, { message: "Something went wrong.", fields: {} });
      toast("Something went wrong.", "error");
      console.error(error);
    }
  } finally {
    if (button) {
      button.disabled = false;
      button.textContent = label;
    }
  }
}

/* ------------------------------------------------------------------ *
 * Cloudflare Turnstile
 *
 * Loaded only when a site key is configured, so a development deployment --
 * where `captcha_required` is false -- has no third-party script at all.
 * `window.MALUDB_TURNSTILE_SITE_KEY` is set by a one-line <script> in
 * index.html that a deployment edits; there is no build step to inject it.
 * ------------------------------------------------------------------ */

const turnstile = {
  siteKey: null,
  widgetId: null,

  init() {
    this.siteKey = (window.MALUDB_TURNSTILE_SITE_KEY || "").trim() || null;
    const mount = $("#captcha-mount");
    if (!this.siteKey) {
      // Say so rather than failing silently at submit time. Signup still works
      // against a control plane that does not require a challenge.
      mount.hidden = true;
      return;
    }
    mount.hidden = false;
    const script = document.createElement("script");
    script.src = "https://challenges.cloudflare.com/turnstile/v0/api.js?onload=onTurnstileReady";
    script.async = true;
    script.defer = true;
    window.onTurnstileReady = () => {
      this.widgetId = window.turnstile.render(mount, { sitekey: this.siteKey });
    };
    document.head.appendChild(script);
  },

  token() {
    if (!this.siteKey || !window.turnstile || this.widgetId === null) return null;
    return window.turnstile.getResponse(this.widgetId) || null;
  },

  reset() {
    if (this.widgetId !== null && window.turnstile) window.turnstile.reset(this.widgetId);
  },
};

/* ------------------------------------------------------------------ *
 * Views
 * ------------------------------------------------------------------ */

/**
 * Signed in, the page *becomes* the console: the sales sections go, the projects
 * take the page, and who you are sits in the header with the way out. It used to
 * reveal the dashboard inside the signup section, under the hero and the plans,
 * which read as nothing having happened.
 */
function renderSession() {
  const signedIn = Boolean(state.me);
  const wasSignedIn = !$("#console").hidden;
  $("#top").hidden = signedIn;
  $("#console").hidden = !signedIn;
  $("#nav-links").hidden = signedIn;
  $("#nav-account").hidden = !signedIn;
  $("#auth-panel").hidden = signedIn;
  document.title = signedIn ? "Projects · MaluDB" : "MaluDB";

  if (signedIn !== wasSignedIn) {
    // Arriving in the console, or back on the sales page, starts at the top of it.
    // The address's #start fragment is dropped so a reload does not scroll to a
    // section that is no longer shown.
    if (window.location.hash) window.history.replaceState(null, "", window.location.pathname + window.location.search);
    window.scrollTo(0, 0);
  }
  if (!signedIn) {
    $("#account-email").textContent = "";
    $("#account-name").textContent = "";
    return;
  }
  $("#account-email").textContent = state.me.email;
  $("#account-name").textContent = state.me.display_name || "";
}

function renderPlans() {
  const grid = $("#plan-grid");

  // Signed out -- every visitor to the sales page -- gets the curated view.
  // Signed in, the live limits replace it: by then the reader is a customer
  // deciding whether to upgrade rather than a stranger.
  const live = state.plans.length
    ? Object.fromEntries(state.plans.map((p) => [p.code, p]))
    : null;

  grid.innerHTML = PUBLIC_PLANS.map((plan) => {
    const specs = (plan.specs || [])
      .map((s) => {
        // When signed in, show the deployment's actual number rather than the
        // marketing copy -- a deployment may have overridden it.
        const actual = live?.[plan.code]?.limits?.[s.key];
        const label =
          actual !== undefined && actual !== s.value
            ? `${escapeHtml(s.label)} <em>(this deployment: ${escapeHtml(actual)})</em>`
            : escapeHtml(s.label);
        return `<li>${label}</li>`;
      })
      .join("");
    const includes = (plan.includes || [])
      .map((t) => `<li>${escapeHtml(t)}</li>`)
      .join("");
    const excludes = (plan.excludes || [])
      .map((t) => `<li class="excluded">${escapeHtml(t)}</li>`)
      .join("");

    return `
      <article class="plan-card${plan.featured ? " featured" : ""}">
        ${plan.featured ? '<p class="plan-badge">Most popular</p>' : ""}
        <h3>${escapeHtml(plan.name)}</h3>
        <p class="plan-price">${escapeHtml(plan.price)}${
          plan.cadence ? `<span>${escapeHtml(plan.cadence)}</span>` : ""
        }</p>
        <p class="plan-lede">${escapeHtml(plan.lede)}</p>
        <ul class="plan-limits">${specs}${includes}</ul>
        ${excludes ? `<ul class="plan-limits plan-excludes">${excludes}</ul>` : ""}
      </article>`;
  }).join("");
}

function renderOrgs() {
  const select = $("#org-select");
  select.innerHTML = state.orgs
    .map((o) => `<option value="${escapeHtml(o.org_id)}">${escapeHtml(o.name)}</option>`)
    .join("");

  // Creating a project is a manager privilege; the route answers 403 otherwise,
  // so the form is hidden rather than offered and then refused.
  const canCreate = state.orgs.some((o) => o.role === "owner" || o.role === "admin");
  $("#create-project-form").hidden = !canCreate;
  $("#create-project-note").hidden = canCreate;
}

/**
 * What a project's status means to the person reading it.
 *
 * The raw value is the provisioning state machine's (`projects_status_check`), and
 * showing it verbatim made a ready project look unfinished: `PROVISIONED` means the
 * database is built and its API starts on the first request -- the same thing to a
 * customer as `ACTIVE`, which the gateway sets once it has started it. Both are
 * served (the gateway's `SERVING_STATUSES`), memory can be enabled on both
 * (`maludb.ENABLEABLE_STATUSES`), and keys and usage have no status gate at all, so
 * both get the panels. The raw value stays on the badge's title.
 */
const STATUS = (() => {
  const setup = ["REQUESTED", "PLACEMENT_RESERVED", "ROLES_CREATING", "DATABASE_CREATING", "EXECUTOR_CREATING",
    "CLIENT_CREATING", "STORAGE_ROLE_CREATING", "BOOTSTRAPPING", "KEYS_CONFIGURING", "VALIDATING",
    "API_CONFIGURING", "ROUTING_CONFIGURING"];
  const table = Object.fromEntries(setup.map((s) => [s, { label: "Setting up", tone: "working", moving: true }]));
  return Object.assign(table, {
    RETRY_WAIT: { label: "Setting up — retrying", tone: "working", moving: true },
    PROVISIONED: { label: "Ready", tone: "ready", serving: true },
    ACTIVE: { label: "Ready", tone: "ready", serving: true },
    PAUSING: { label: "Pausing", tone: "working", moving: true },
    PAUSED: { label: "Paused", tone: "idle" },
    RESUMING: { label: "Resuming", tone: "working", moving: true },
    SUSPENDING: { label: "Suspending", tone: "working", moving: true },
    SUSPENDED: { label: "Suspended", tone: "failed" },
    UPGRADING: { label: "Changing plan", tone: "working", moving: true },
    MOVING: { label: "Moving", tone: "working", moving: true },
    DELETING: { label: "Deleting", tone: "working", moving: true },
    DELETED: { label: "Deleted", tone: "idle" },
    FAILED: { label: "Setup failed", tone: "failed" },
  });
})();

const statusOf = (p) => STATUS[p.status] || { label: p.status, tone: "idle" };

function renderProjects() {
  const grid = $("#project-grid");
  if (!state.projects.length) {
    grid.innerHTML = `<p class="empty-state">No projects yet. Create one above.</p>`;
    return;
  }
  grid.innerHTML = state.projects
    .map(
      (p) => `
      <article class="project-card" data-status="${escapeHtml(p.status)}" data-tone="${escapeHtml(statusOf(p).tone)}">
        <header>
          <h4>${escapeHtml(p.display_name)}</h4>
          <span class="badge" title="${escapeHtml(p.status)}">${escapeHtml(statusOf(p).label)}</span>
        </header>
        <p class="project-ref">${escapeHtml(p.project_ref)}</p>
        <p class="project-url"><code>${escapeHtml(p.api_url)}</code></p>
        ${statusOf(p).moving ? `<p class="usage-note">This usually takes under a minute; the page updates itself.</p>` : ""}
        ${p.status === "FAILED" ? `<p class="usage-state">Setup did not finish. Contact support with the project ref above.</p>` : ""}
        ${
          statusOf(p).serving
            ? `<button class="button secondary small" type="button" data-keys-ref="${escapeHtml(p.project_ref)}"
                 aria-expanded="${state.openKeys === p.project_ref}">API keys</button>
               <div class="usage-panel keys-panel" data-keys-for="${escapeHtml(p.project_ref)}"
                 ${state.openKeys === p.project_ref ? "" : "hidden"}>${keysPanel(p)}</div>
               <button class="button secondary small" type="button" data-usage-ref="${escapeHtml(p.project_ref)}"
                 aria-expanded="${state.openUsage === p.project_ref}">Plan &amp; usage</button>
               <div class="usage-panel" data-usage-for="${escapeHtml(p.project_ref)}"
                 ${state.openUsage === p.project_ref ? "" : "hidden"}>${usagePanel(p)}</div>
               <button class="button secondary small" type="button" data-memory-ref="${escapeHtml(p.project_ref)}"
                 aria-expanded="${state.openMemory === p.project_ref}">Memory</button>
               <div class="usage-panel memory-panel" data-memory-for="${escapeHtml(p.project_ref)}"
                 ${state.openMemory === p.project_ref ? "" : "hidden"}>${memoryPanel(p)}</div>`
            : ""
        }
      </article>`,
    )
    .join("");
}

/* ------------------------------------------------------------------ *
 * Plan and usage (launch slice 2)
 *
 * Everything here renders what `GET /v1/projects/{ref}/usage` says and
 * nothing it does not. Two rules from the decisions behind that route:
 *
 *  - No amount is rendered from the platform (ADR-052). The only prices on
 *    this page are the published list in PUBLIC_PLANS; what a customer was
 *    charged is Stripe's to state, on Stripe's receipt.
 *  - `grace_ends_at` is the earliest the restriction can arrive, not the
 *    moment it will (ADR-051), and is worded that way.
 * ------------------------------------------------------------------ */

const PLAN_ORDER = PUBLIC_PLANS.map((p) => p.code);
const planName = (code) =>
  PUBLIC_PLANS.find((p) => p.code === code)?.name ||
  state.plans.find((p) => p.code === code)?.name ||
  code;
const planPrice = (code) => PUBLIC_PLANS.find((p) => p.code === code)?.price || "";

const canManage = (orgId) =>
  state.orgs.some((o) => o.org_id === orgId && (o.role === "owner" || o.role === "admin"));

function formatBytes(bytes) {
  if (bytes === null || bytes === undefined) return "—";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let value = Number(bytes);
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit += 1;
  }
  return `${value >= 10 || unit === 0 ? Math.round(value) : value.toFixed(1)} ${units[unit]}`;
}

const formatDate = (iso) =>
  iso ? new Date(iso).toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" }) : "—";

// What each state means to the person reading it, and what they can do.
const STATE_TEXT = {
  ok: null,
  warning: "Nearing this plan's limit.",
  restricted: "Over the limit: new writes are refused. Reads and deletes still work.",
  exceeded: "At the limit: further use is refused until the next period or a larger plan.",
};

function meter({ label, used, limit, state: meterState, bytes = true, note = "" }) {
  const measured = used !== null && used !== undefined;
  const pct = measured && limit > 0 ? Math.min(100, Math.round((used / limit) * 100)) : 0;
  const show = (v) => (bytes ? formatBytes(v) : Number(v).toLocaleString());
  const message = STATE_TEXT[meterState];
  return `
    <div class="usage-meter" data-state="${escapeHtml(meterState || "ok")}">
      <div class="usage-meter-head">
        <span>${escapeHtml(label)}</span>
        <span>${measured ? `${escapeHtml(show(used))} of ${escapeHtml(show(limit))}` : `— of ${escapeHtml(show(limit))}`}</span>
      </div>
      <div class="usage-bar" role="img" aria-label="${escapeHtml(label)}: ${measured ? `${pct}% used` : "not measured yet"}">
        <span style="width: ${pct}%"></span>
      </div>
      ${message ? `<p class="usage-state">${escapeHtml(message)}</p>` : ""}
      ${!measured ? `<p class="usage-note">Not measured yet — figures appear after the next maintenance pass.</p>` : ""}
      ${note ? `<p class="usage-note">${escapeHtml(note)}</p>` : ""}
    </div>`;
}

function billingSummary(usage) {
  const b = usage.billing;
  if (!b.subscribed) {
    return `<p class="usage-note">No subscription — this project is on ${escapeHtml(planName(usage.plan_code))}.</p>`;
  }
  const parts = [];
  if (b.period_end) parts.push(`Current period ends ${escapeHtml(formatDate(b.period_end))}.`);
  if (b.plan_code && b.plan_code !== usage.plan_code) {
    // ADR-048: what is paid for is recorded first and applied by the
    // maintenance pass, so a just-bought plan is visible before it is in force.
    parts.push(`${escapeHtml(planName(b.plan_code))} is paid for and is being applied — usually within a minute.`);
  }
  if (b.state === "past_due") {
    parts.push(
      `<strong>A payment failed.</strong> Service is unchanged for now; if it is not resolved, ` +
        `writes can be restricted from ${escapeHtml(formatDate(b.grace_ends_at))} at the earliest. ` +
        `Your data is never deleted.`,
    );
  } else if (b.state === "trialing") {
    parts.push("On a trial.");
  } else if (b.state === "incomplete") {
    parts.push("Checkout was started but not completed.");
  }
  return `<p class="usage-note">${parts.join(" ")}</p>`;
}

function upgradeActions(project, usage) {
  const current = PLAN_ORDER.indexOf(usage.plan_code);
  const higher = PLAN_ORDER.filter((code, i) => i > current);
  if (!higher.length) return `<p class="usage-note">This is the largest self-serve plan.</p>`;
  if (!canManage(project.org_id)) {
    // The route answers 403 to members; say who can, rather than hide the option.
    return `<p class="usage-note">An organization owner or admin can move this project to a larger plan.</p>`;
  }
  const pending = state.upgradeRequests[project.project_ref];
  const requested = pending
    ? `<p class="usage-note">You asked for ${escapeHtml(planName(pending.requested_plan_code))} on ${escapeHtml(
        formatDate(pending.requested_at),
      )}; it is with an operator.</p>`
    : "";
  return `${requested}
    <div class="usage-actions">
      ${higher
        .map(
          (code) => `<button class="button primary small" type="button"
             data-upgrade-ref="${escapeHtml(project.project_ref)}" data-upgrade-plan="${escapeHtml(code)}">
             Move to ${escapeHtml(planName(code))}${planPrice(code) ? ` · ${escapeHtml(planPrice(code))}/mo` : ""}
           </button>`,
        )
        .join("")}
    </div>`;
}

function usagePanel(project) {
  const usage = state.usage[project.project_ref];
  if (!usage) return `<p class="usage-note">Loading…</p>`;
  if (usage.error) return `<p class="form-error">${escapeHtml(usage.error)}</p>`;
  return `
    <h5>${escapeHtml(planName(usage.plan_code))}</h5>
    ${billingSummary(usage)}
    ${meter({ label: "Database", used: usage.storage.used_bytes, limit: usage.storage.limit_bytes, state: usage.storage.state })}
    ${meter({ label: "File storage", used: usage.object_storage.used_bytes, limit: usage.object_storage.limit_bytes, state: usage.object_storage.state })}
    ${meter({ label: "Egress this month", used: usage.egress.used_bytes, limit: usage.egress.limit_bytes, state: usage.egress.state })}
    ${meter({ label: "Emails this month", used: usage.email.used, limit: usage.email.limit, state: usage.email.used >= usage.email.limit ? "exceeded" : "ok", bytes: false })}
    <dl class="usage-limits">
      <div><dt>API requests</dt><dd>${escapeHtml(Number(usage.api_requests.limit).toLocaleString())}${
        usage.api_requests.window_seconds ? ` per ${escapeHtml(usage.api_requests.window_seconds)}s` : ""
      }</dd></div>
      <div><dt>Database connections</dt><dd>${escapeHtml(usage.database_connections.limit)}</dd></div>
      <div><dt>Realtime connections</dt><dd>${
        usage.realtime.enabled ? escapeHtml(usage.realtime.connection_limit) : "Not on this plan"
      }</dd></div>
    </dl>
    ${upgradeActions(project, usage)}`;
}

async function loadUsage(ref) {
  const project = state.projects.find((p) => p.project_ref === ref);
  try {
    state.usage[ref] = await getUsage(ref);
  } catch (error) {
    state.usage[ref] = { error: error instanceof ApiError ? error.message : "Could not load usage." };
  }
  if (project && canManage(project.org_id)) {
    // Managers only: the route answers 403 to members, and a member has no
    // request to see. A failure here costs a note, never the panel.
    state.upgradeRequests[ref] = await getUpgradeRequest(ref).catch(() => null);
  }
  const panel = $(`[data-usage-for="${CSS.escape(ref)}"]`);
  if (project && panel) panel.innerHTML = usagePanel(project);
}

async function toggleUsage(ref) {
  state.openUsage = state.openUsage === ref ? null : ref;
  renderProjects();
  if (state.openUsage) await loadUsage(ref);
}

/**
 * Send a manager to Stripe, or -- on a deployment that takes no payments --
 * record the request for an operator instead of failing.
 */
async function upgrade(ref, planCode, button) {
  const label = button.textContent;
  button.disabled = true;
  button.textContent = "Opening checkout…";
  try {
    const checkout = await startCheckout(ref, planCode);
    const target = new URL(checkout.checkout_url);
    // The route documents a URL on Stripe's domain, always. Held to that here,
    // so a misbehaving response cannot turn this button into a redirect anywhere.
    if (target.protocol !== "https:" || !(target.hostname === "stripe.com" || target.hostname.endsWith(".stripe.com"))) {
      throw new ApiError("The checkout link was not a Stripe address; nothing was opened.", { status: 0 });
    }
    window.location.assign(target.href);
  } catch (error) {
    if (error instanceof ApiError && error.status === 503) {
      try {
        const request = await requestUpgrade(ref, planCode);
        state.upgradeRequests[ref] = request;
        loadUsage(ref);
        toast(
          `Requested ${planName(request.requested_plan_code)}. Online payment is not available here yet; we will be in touch.`,
          "success",
        );
      } catch (inner) {
        toast(inner instanceof ApiError ? inner.message : "Could not send the request.", "error");
      }
    } else {
      toast(error instanceof ApiError ? error.message : "Could not start checkout.", "error");
      if (!(error instanceof ApiError)) console.error(error);
    }
    button.disabled = false;
    button.textContent = label;
  }
}

/** Stripe sends the customer back to `/?checkout=complete&project=<ref>`. */
function handleCheckoutReturn() {
  const params = new URLSearchParams(window.location.search);
  const outcome = params.get("checkout");
  const ref = params.get("project");
  if (!outcome) return;
  // Take the query off the address so a reload does not announce it again.
  window.history.replaceState(null, "", window.location.pathname + window.location.hash);
  if (outcome === "complete") {
    toast("Payment received. The new plan applies within a minute.", "success");
  } else if (outcome === "cancelled") {
    toast("Checkout cancelled. Nothing was charged and the plan is unchanged.");
  }
  if (ref && state.projects.some((p) => p.project_ref === ref)) {
    state.openUsage = ref;
    renderProjects();
    loadUsage(ref);
    // A completed checkout is applied by the next maintenance pass; look again
    // shortly so the panel shows the plan in force rather than the one paid for.
    if (outcome === "complete") setTimeout(() => loadUsage(ref), 45000);
  }
}


/* ------------------------------------------------------------------ *
 * API keys (Phase 07 slice 2)
 *
 * How a customer gets the keys their project is used with, which the dashboard
 * had no way to do: the routes existed and only a hand-written request reached
 * them.
 *
 * - A publishable key is shown with its value, and a copy button: it is meant
 *   for a browser bundle, and the API returns it on every listing.
 * - A secret key's value is shown **once**, straight after it is created, with
 *   a plain statement that it cannot be retrieved again. It is held only in
 *   `state.issuedKey` while on screen, and dropped when dismissed, when the
 *   panel closes, or on sign-out. Listing never carries it.
 * - Revoking asks first, because every client using the key stops at once.
 *   Members see the keys; owners and admins create and revoke them.
 * ------------------------------------------------------------------ */

const KEY_TYPE_TEXT = {
  publishable: "Publishable — safe in a browser; row-level security applies.",
  secret: "Secret — server only; bypasses row-level security.",
};

function issuedKeyNotice(ref, issued) {
  const secret = issued.key_type === "secret";
  return `
    <div class="key-issued" data-state="${secret ? "secret" : "publishable"}" role="status">
      <p><strong>${secret ? "Copy this secret key now." : "Publishable key created."}</strong>
        ${secret ? "It is shown once and cannot be retrieved again. If it is lost, create another and revoke this one." : ""}</p>
      <code class="key-value" id="issued-key-${escapeHtml(ref)}">${escapeHtml(issued.key)}</code>
      <div class="usage-actions">
        <button class="button primary small" type="button" data-key-copy="issued-key-${escapeHtml(ref)}">Copy</button>
        <button class="button secondary small" type="button" data-key-dismiss="${escapeHtml(ref)}">${secret ? "I have saved it" : "Done"}</button>
      </div>
    </div>`;
}

function keyRow(project, key, manager) {
  const ref = escapeHtml(project.project_ref);
  const id = escapeHtml(key.id);
  const value =
    key.key_type === "publishable" && key.key
      ? `<code class="key-value" id="key-${id}">${escapeHtml(key.key)}</code>
         <button class="button secondary small" type="button" data-key-copy="key-${id}">Copy</button>`
      : `<span class="usage-note">Not shown — a secret key is only visible when it is created.</span>`;
  return `
    <div class="api-key" data-type="${escapeHtml(key.key_type)}">
      <header>
        <strong>${escapeHtml(key.name || key.key_identifier)}</strong>
        <span class="badge">${escapeHtml(key.key_type)}</span>
      </header>
      <p class="usage-note">${escapeHtml(KEY_TYPE_TEXT[key.key_type] || "")}</p>
      <p class="usage-note"><code>${escapeHtml(key.key_identifier)}</code> · created ${escapeHtml(formatDate(key.created_at))} ·
        ${key.last_used_at ? `last used ${escapeHtml(formatDate(key.last_used_at))}` : "never used"}</p>
      <div class="key-line">${value}</div>
      ${
        manager
          ? `<button class="button secondary small danger" type="button" data-key-revoke="${id}" data-ref="${ref}"
               data-key-label="${escapeHtml(key.name || key.key_identifier)}">Revoke</button>`
          : ""
      }
    </div>`;
}

function keysPanel(project) {
  const ref = project.project_ref;
  const listing = state.apiKeys[ref];
  if (!listing) return `<p class="usage-note">Loading…</p>`;
  if (listing.error) return `<p class="form-error">${escapeHtml(listing.error)}</p>`;
  const manager = canManage(project.org_id);
  const live = listing.filter((k) => !k.revoked_at);
  const revoked = listing.length - live.length;
  const issued = state.issuedKey[ref];
  return `
    <h5>API keys</h5>
    <p class="usage-note">Send one in the <code>apikey</code> header to <code>${escapeHtml(project.api_url)}</code>.
      Use the publishable key in browsers and apps, and the secret key only on your servers.</p>
    ${issued ? issuedKeyNotice(ref, issued) : ""}
    ${live.map((key) => keyRow(project, key, manager)).join("") || `<p class="usage-note">No keys yet.</p>`}
    ${revoked ? `<p class="usage-note">${escapeHtml(revoked)} revoked key${revoked === 1 ? "" : "s"} not shown.</p>` : ""}
    ${
      manager
        ? `<form class="inline-form compact" data-keys-form="create" data-ref="${escapeHtml(ref)}" novalidate>
             <p class="form-error" role="alert" hidden></p>
             <label>Type <select name="key_type">
               <option value="publishable">Publishable</option>
               <option value="secret">Secret</option>
             </select></label>
             <label>Name <span class="optional">optional</span>
               <input name="name" type="text" maxlength="100" placeholder="web app">
               <small class="field-error" data-error-for="name" hidden></small></label>
             <button class="button primary small" type="submit" data-busy="Creating…">Create key</button>
           </form>`
        : `<p class="usage-note">An organization owner or admin can create and revoke keys.</p>`
    }`;
}

async function loadKeys(ref) {
  const project = state.projects.find((p) => p.project_ref === ref);
  try {
    state.apiKeys[ref] = await listApiKeys(ref);
  } catch (error) {
    state.apiKeys[ref] = { error: error instanceof ApiError ? error.message : "Could not load API keys." };
  }
  const panel = $(`[data-keys-for="${CSS.escape(ref)}"]`);
  if (project && panel) panel.innerHTML = keysPanel(project);
}

async function toggleKeys(ref) {
  const closing = state.openKeys === ref;
  // Closing a panel drops a key still on screen: the next time it opens, a
  // secret must not be sitting there for whoever looks next.
  if (state.openKeys) delete state.issuedKey[state.openKeys];
  state.openKeys = closing ? null : ref;
  renderProjects();
  if (state.openKeys) await loadKeys(ref);
}

async function keysForm(form) {
  const ref = form.dataset.ref;
  await runForm(form, async (data) => {
    const keyType = String(data.get("key_type"));
    const name = String(data.get("name") || "").trim() || null;
    const issued = await createApiKey(ref, { keyType, name });
    state.issuedKey[ref] = { key_type: issued.key_type, key: issued.key };
    toast(issued.key_type === "secret" ? "Secret key created. Copy it now." : "Publishable key created.", "success");
    await loadKeys(ref);
  });
}

/** Copy a key from the element that shows it, or select it where the clipboard is unavailable. */
async function copyKey(elementId) {
  const node = document.getElementById(elementId);
  if (!node) return;
  try {
    // Only offered in a secure context (https, or localhost).
    await navigator.clipboard.writeText(node.textContent);
    toast("Copied.", "success");
  } catch {
    const range = document.createRange();
    range.selectNodeContents(node);
    const selection = window.getSelection();
    selection.removeAllRanges();
    selection.addRange(range);
    toast("Selected — press Ctrl+C (or ⌘C) to copy.");
  }
}

async function keysAction(button) {
  if (button.dataset.keyCopy) {
    await copyKey(button.dataset.keyCopy);
    return;
  }
  if (button.dataset.keyDismiss) {
    const ref = button.dataset.keyDismiss;
    delete state.issuedKey[ref];
    const project = state.projects.find((p) => p.project_ref === ref);
    const panel = $(`[data-keys-for="${CSS.escape(ref)}"]`);
    if (project && panel) panel.innerHTML = keysPanel(project);
    return;
  }
  if (button.dataset.keyRevoke) {
    const ref = button.dataset.ref;
    if (!window.confirm(`Revoke "${button.dataset.keyLabel}"? Every client using it stops working immediately.`)) return;
    await revokeApiKey(ref, button.dataset.keyRevoke);
    toast("Key revoked.", "success");
    await loadKeys(ref);
  }
}

/* ------------------------------------------------------------------ *
 * Memory spaces (ADR-079)
 *
 * Renders what the control plane's memory routes say, and offers only what the
 * caller may do: members see spaces and which provider keys exist; owners and
 * admins also create and delete spaces, name models, and set or remove keys.
 *
 * A provider key goes in through a password field, is sent once, and the field
 * is cleared -- no route returns it, so nothing here ever displays more than its
 * last four characters. Deleting a space asks for its name typed back, because
 * it removes every memory in it and cannot be undone.
 * ------------------------------------------------------------------ */

const EXTRACTION_PROVIDERS = ["anthropic", "openai"];
const EMBEDDING_PROVIDERS = ["openai", "voyage"];
const PROVIDER_NAMES = { anthropic: "Anthropic", openai: "OpenAI", voyage: "Voyage" };
const SPACE_BUSY = new Set(["pending", "deleting"]);

const options = (values, selected) =>
  values
    .map((v) => `<option value="${escapeHtml(v)}"${v === selected ? " selected" : ""}>${escapeHtml(PROVIDER_NAMES[v] || v)}</option>`)
    .join("");

function spaceModels(space) {
  if (!space.embedding_provider) {
    return `<p class="usage-note">No models: stores your own embeddings and is searched with a vector.</p>`;
  }
  return `<p class="usage-note">Extraction ${escapeHtml(PROVIDER_NAMES[space.extraction_provider])} · <code>${escapeHtml(
    space.extraction_model,
  )}</code><br>Embeddings ${escapeHtml(PROVIDER_NAMES[space.embedding_provider])} · <code>${escapeHtml(
    space.embedding_model,
  )}</code></p>`;
}

/*
 * The model picker. The API's catalog (`models` on the spaces listing) lists each
 * provider's suggested models, default first -- suggestions, not an allowlist, so
 * "Other model…" takes any name and the provider judges it. An embedding model
 * shows its dimensions, and once a space holds memories its embedding choice is
 * shown rather than offered: the API refuses a change (409), because search
 * compares only vectors from one model.
 */
const OTHER_MODEL = "__other__";

function modelOptions(catalog, kind, provider, current) {
  const offer = catalog?.[kind]?.[provider];
  if (!offer) return `<option value="${OTHER_MODEL}" selected>Other model…</option>`;
  const listed = offer.models.some((m) => m.model === current);
  const chosen = current && !listed ? OTHER_MODEL : current || offer.default;
  const choices = offer.models
    .map((m) => {
      const notes = [m.model === offer.default ? "default" : "", m.dimensions ? `${m.dimensions} dimensions` : ""]
        .filter(Boolean)
        .join(", ");
      return `<option value="${escapeHtml(m.model)}"${m.model === chosen ? " selected" : ""}>${escapeHtml(m.model)}${
        notes ? ` (${escapeHtml(notes)})` : ""
      }</option>`;
    })
    .join("");
  return `${choices}<option value="${OTHER_MODEL}"${chosen === OTHER_MODEL ? " selected" : ""}>Other model…</option>`;
}

function modelField(catalog, kind, provider, current) {
  const offer = catalog?.[kind]?.[provider];
  const other = Boolean(current) && !(offer?.models || []).some((m) => m.model === current);
  return `
    <label>Model
      <select name="${kind}_model" data-model-kind="${kind}">${modelOptions(catalog, kind, provider, current)}</select>
    </label>
    <label data-other-for="${kind}" ${other || !offer ? "" : "hidden"}>Model name
      <input name="${kind}_model_other" type="text" maxlength="100" placeholder="exactly as the provider names it"
        value="${other ? escapeHtml(current) : ""}">
    </label>`;
}

function spaceCard(project, space, manager, catalog) {
  const ref = escapeHtml(project.project_ref);
  const name = escapeHtml(space.name);
  const busy = SPACE_BUSY.has(space.state);
  // Fixed once the space holds memories: the API answers 409 to a change.
  const embeddingLocked = Number(space.item_count || 0) > 0 && Boolean(space.embedding_provider);
  const embedding = embeddingLocked
    ? `<p class="usage-note">Embeddings ${escapeHtml(PROVIDER_NAMES[space.embedding_provider] || space.embedding_provider)} ·
         <code>${escapeHtml(space.embedding_model)}</code> — fixed, because this space holds memories and search compares
         only vectors from one model. To embed with another model, create a new space.</p>`
    : `<label>Embeddings
         <select name="embedding_provider" data-model-provider="embedding">${options(EMBEDDING_PROVIDERS, space.embedding_provider)}</select>
       </label>
       ${modelField(catalog, "embedding", space.embedding_provider || EMBEDDING_PROVIDERS[0], space.embedding_model)}`;
  const actions =
    manager && space.state === "active"
      ? `<details>
           <summary>Models</summary>
           <form class="inline-form compact" data-memory-form="models" data-ref="${ref}" data-space="${name}"
             ${embeddingLocked ? `data-embedding-provider="${escapeHtml(space.embedding_provider)}" data-embedding-model="${escapeHtml(space.embedding_model)}"` : ""} novalidate>
             <p class="form-error" role="alert" hidden></p>
             <label>Extraction
               <select name="extraction_provider" data-model-provider="extraction">${options(EXTRACTION_PROVIDERS, space.extraction_provider)}</select>
             </label>
             ${modelField(catalog, "extraction", space.extraction_provider || EXTRACTION_PROVIDERS[0], space.extraction_model)}
             ${embedding}
             <button class="button primary small" type="submit" data-busy="Saving…">Save models</button>
           </form>
         </details>
         <button class="button secondary small danger" type="button" data-memory-delete="${name}"
           data-ref="${ref}">Delete space</button>`
      : "";
  return `
    <div class="memory-space" data-state="${escapeHtml(space.state)}">
      <header>
        <strong>${name}</strong>
        <span class="badge">${escapeHtml(space.state)}${busy ? "…" : ""}</span>
      </header>
      <p class="usage-note">${escapeHtml(Number(space.item_count || 0).toLocaleString())} memories</p>
      ${space.state === "active" ? spaceModels(space) : ""}
      ${space.detail ? `<p class="usage-state">${escapeHtml(space.detail)}</p>` : ""}
      ${actions}
    </div>`;
}

function providerKeys(project, keys, manager) {
  const ref = escapeHtml(project.project_ref);
  const set = new Map(keys.keys.map((k) => [k.provider, k]));
  return `
    <h5>Provider keys</h5>
    <p class="usage-note">Used to extract and embed with your own account. Stored encrypted and never shown again.</p>
    <dl class="usage-limits">
      ${keys.providers
        .map((provider) => {
          const key = set.get(provider);
          return `<div><dt>${escapeHtml(PROVIDER_NAMES[provider] || provider)}</dt><dd>${
            key ? `…${escapeHtml(key.hint)} <span class="usage-note">set ${escapeHtml(formatDate(key.created_at))}</span>` : "Not set"
          }${
            key && manager
              ? ` <button class="button secondary small" type="button" data-memory-remove-key="${escapeHtml(provider)}"
                   data-ref="${ref}">Remove</button>`
              : ""
          }</dd></div>`;
        })
        .join("")}
    </dl>
    ${
      manager
        ? `<form class="inline-form compact" data-memory-form="key" data-ref="${ref}" novalidate autocomplete="off">
             <p class="form-error" role="alert" hidden></p>
             <label>Provider <select name="provider">${options(keys.providers, keys.providers[0])}</select></label>
             <label>API key <input name="api_key" type="password" autocomplete="new-password" required>
               <small class="field-error" data-error-for="api_key" hidden></small></label>
             <button class="button primary small" type="submit" data-busy="Saving…">Set key</button>
           </form>`
        : ""
    }`;
}

function memoryPanel(project) {
  const memory = state.memory[project.project_ref];
  if (!memory) return `<p class="usage-note">Loading…</p>`;
  if (memory.error) return `<p class="form-error">${escapeHtml(memory.error)}</p>`;
  const { spaces, keys } = memory;
  if (!spaces.entitled) {
    return `<p class="usage-note">This project's plan does not include memory spaces.</p>`;
  }
  const manager = canManage(project.org_id);
  const held = spaces.spaces.length;
  const create =
    manager && held < spaces.max_spaces
      ? `<form class="inline-form compact" data-memory-form="create" data-ref="${escapeHtml(project.project_ref)}" novalidate>
           <p class="form-error" role="alert" hidden></p>
           <label>New space <input name="name" type="text" placeholder="support_bot" pattern="[a-z][a-z0-9_]{0,39}"
             maxlength="40" required><small class="field-error" data-error-for="name" hidden></small></label>
           <button class="button primary small" type="submit" data-busy="Creating…">Create space</button>
         </form>`
      : manager
        ? `<p class="usage-note">This plan's ${escapeHtml(spaces.max_spaces)} space${spaces.max_spaces === 1 ? " is" : "s are"} in use.</p>`
        : `<p class="usage-note">An organization owner or admin can create and delete spaces and set keys.</p>`;
  return `
    <h5>Memory spaces</h5>
    <dl class="usage-limits">
      <div><dt>Spaces</dt><dd>${escapeHtml(held)} of ${escapeHtml(spaces.max_spaces)}</dd></div>
      <div><dt>Stored memories</dt><dd>up to ${escapeHtml(Number(spaces.max_items).toLocaleString())}</dd></div>
      <div><dt>Ingest requests</dt><dd>${escapeHtml(Number(spaces.ingests_per_hour).toLocaleString())} an hour</dd></div>
    </dl>
    ${spaces.spaces.map((space) => spaceCard(project, space, manager, spaces.models)).join("") || `<p class="usage-note">No spaces yet.</p>`}
    ${create}
    <p class="usage-note">Store and search from your server with the project's secret key at
      <code>${escapeHtml(project.api_url)}/memory/v1/spaces/&lt;name&gt;/ingest</code> and <code>…/search</code>.</p>
    ${providerKeys(project, keys, manager)}`;
}

async function loadMemory(ref) {
  const project = state.projects.find((p) => p.project_ref === ref);
  try {
    const [spaces, keys] = await Promise.all([listMemorySpaces(ref), listProviderKeys(ref)]);
    state.memory[ref] = { spaces, keys };
  } catch (error) {
    state.memory[ref] = { error: error instanceof ApiError ? error.message : "Could not load memory spaces." };
  }
  const panel = $(`[data-memory-for="${CSS.escape(ref)}"]`);
  if (project && panel) panel.innerHTML = memoryPanel(project);

  // A space is built and deleted asynchronously; follow it while the panel is open.
  clearTimeout(loadMemory.timer);
  const spaces = state.memory[ref]?.spaces?.spaces || [];
  if (state.openMemory === ref && spaces.some((s) => SPACE_BUSY.has(s.state))) {
    loadMemory.timer = setTimeout(() => loadMemory(ref).catch(() => {}), 3000);
  }
}

async function toggleMemory(ref) {
  state.openMemory = state.openMemory === ref ? null : ref;
  renderProjects();
  if (state.openMemory) await loadMemory(ref);
}

async function memoryForm(form) {
  const ref = form.dataset.ref;
  await runForm(form, async (data) => {
    const kind = form.dataset.memoryForm;
    if (kind === "create") {
      const name = String(data.get("name") || "").trim();
      await createMemorySpace(ref, name);
      toast(`Building ${name}. It will show as active in a moment.`, "success");
    } else if (kind === "models") {
      // A picked model, or the name typed under "Other model…"; empty means the default.
      const model = (which) => {
        const choice = String(data.get(`${which}_model`) || "");
        const typed = choice === OTHER_MODEL ? String(data.get(`${which}_model_other`) || "") : choice;
        return typed.trim() || null;
      };
      // A space holding memories keeps its embedding model: its fields are not in the
      // form, so what the space already uses is sent back unchanged.
      const locked = Boolean(form.dataset.embeddingProvider);
      await setMemoryModels(ref, form.dataset.space, {
        extraction_provider: String(data.get("extraction_provider")),
        extraction_model: model("extraction"),
        embedding_provider: locked ? form.dataset.embeddingProvider : String(data.get("embedding_provider")),
        embedding_model: locked ? form.dataset.embeddingModel : model("embedding"),
      });
      toast(`Models saved for ${form.dataset.space}.`, "success");
    } else if (kind === "key") {
      const provider = String(data.get("provider"));
      const key = String(data.get("api_key") || "");
      form.elements.api_key.value = ""; // never left in the page, whatever happens next
      await setProviderKey(ref, provider, key);
      toast(`${PROVIDER_NAMES[provider] || provider} key saved.`, "success");
    }
    await loadMemory(ref);
  });
}

async function memoryAction(button) {
  const ref = button.dataset.ref;
  if (button.dataset.memoryDelete) {
    const name = button.dataset.memoryDelete;
    const typed = window.prompt(
      `Delete the space "${name}" and every memory in it? This cannot be undone.\n\nType its name to confirm.`,
    );
    if (typed === null) return;
    if (typed.trim() !== name) {
      toast("The name did not match; nothing was deleted.", "error");
      return;
    }
    await deleteMemorySpace(ref, name);
    toast(`Deleting ${name}.`, "success");
  } else if (button.dataset.memoryRemoveKey) {
    const provider = button.dataset.memoryRemoveKey;
    if (!window.confirm(`Remove the ${PROVIDER_NAMES[provider] || provider} key? Ingests and searches that need it will fail.`)) {
      return;
    }
    await removeProviderKey(ref, provider);
    toast("Key removed.", "success");
  }
  await loadMemory(ref);
}

async function loadDashboard() {
  state.me = await me();
  const [orgs, plans] = await Promise.all([listOrganizations(), listPlans()]);
  state.orgs = orgs;
  state.plans = plans;

  const perOrg = await Promise.all(orgs.map((o) => listProjects(o.org_id)));
  // A project response does not carry its organization; the listing does.
  state.projects = perOrg.flatMap((projects, i) =>
    projects.map((p) => ({ ...p, org_id: orgs[i].org_id })),
  );

  renderSession();
  renderPlans();
  renderOrgs();
  renderProjects();

  const planSelect = $("#project-plan");
  planSelect.innerHTML = state.plans
    .map((p) => `<option value="${escapeHtml(p.code)}">${escapeHtml(p.name)}</option>`)
    .join("");

  // A project is created asynchronously (202), so the dashboard follows it while
  // anything is changing -- and only then. It used to poll until every project was
  // ACTIVE, which a ready PROVISIONED project may never become until something
  // calls its API: every four seconds, for as long as the page stayed open.
  clearTimeout(loadDashboard.timer);
  if (state.projects.some((p) => statusOf(p).moving)) {
    loadDashboard.timer = setTimeout(() => loadDashboard().catch(() => {}), 4000);
  }
}

/* ------------------------------------------------------------------ *
 * Wiring
 * ------------------------------------------------------------------ */

/**
 * Close the signup form until the platform is deployed.
 *
 * Sign-in stays open on purpose: the operator has an account before the public
 * does, and a sales page that locked its own author out would be a nuisance
 * with no upside. What closes is account *creation*, which is the thing that
 * would otherwise hand somebody a project on a platform with no node behind it
 * -- a 503 on their first action, which is a worse first impression than an
 * honest "not yet".
 */
function applySignupGate() {
  const open = signupsOpen();
  $("#signup-form").hidden = !open;
  $("#signup-closed").hidden = open;
  $("#signup-tab").textContent = open ? "Create account" : "Get notified";
  for (const el of $$("[data-cta]")) {
    el.textContent = open ? "Create a free project" : "Join the waitlist";
  }
}

function wire() {
  submit($("#signup-form"), async (data, form) => {
    // Belt and braces: the form is hidden when signups are closed, but hidden
    // is a CSS state and this is a real request against a real control plane.
    if (!signupsOpen()) {
      throw new ApiError("Signups are not open yet.", { status: 0 });
    }
    const password = String(data.get("password") || "");
    if (password.length < PASSWORD_MIN) {
      throw new ApiError(`Password must be at least ${PASSWORD_MIN} characters.`, {
        status: 0,
        fields: { password: `At least ${PASSWORD_MIN} characters.` },
      });
    }
    const captchaToken = turnstile.token();
    if (turnstile.siteKey && !captchaToken) {
      throw new ApiError("Complete the challenge first.", { status: 0 });
    }
    await signUp({
      email: String(data.get("email") || "").trim(),
      password,
      displayName: String(data.get("display_name") || "").trim() || null,
      captchaToken,
    });
    form.reset();
    turnstile.reset();
    toast("Welcome. Your account is ready.", "success");
    await loadDashboard();
  });

  submit($("#signin-form"), async (data, form) => {
    await signIn({
      email: String(data.get("email") || "").trim(),
      password: String(data.get("password") || ""),
    });
    form.reset();
    toast("Signed in.", "success");
    await loadDashboard();
  });

  submit($("#create-project-form"), async (data, form) => {
    const project = await createProject(String(data.get("org_id")), {
      displayName: String(data.get("display_name") || "").trim(),
      planCode: String(data.get("plan_code") || "") || null,
    });
    form.reset();
    toast(`Creating ${project.display_name}. It will show as Ready in a minute or so.`, "success");
    await loadDashboard();
  });

  $("#signout").addEventListener("click", async () => {
    await signOut();
    state.me = null;
    state.orgs = [];
    state.projects = [];
    state.usage = {};
    state.openUsage = null;
    state.memory = {};
    state.openMemory = null;
    state.apiKeys = {};
    state.openKeys = null;
    state.issuedKey = {}; // a secret still on screen does not survive sign-out
    clearTimeout(loadMemory.timer);
    clearTimeout(loadDashboard.timer);
    renderSession();
    renderProjects();
    toast("Signed out.");
  });

  $("#project-grid").addEventListener("submit", (event) => {
    const keys = event.target.closest("[data-keys-form]");
    if (keys) {
      event.preventDefault();
      keysForm(keys);
      return;
    }
    const form = event.target.closest("[data-memory-form]");
    if (!form) return;
    event.preventDefault();
    memoryForm(form);
  });

  // The model picker: a new provider lists its own models, with its default chosen;
  // "Other model…" reveals the name field.
  $("#project-grid").addEventListener("change", (event) => {
    const form = event.target.closest('[data-memory-form="models"]');
    if (!form) return;
    const catalog = state.memory[form.dataset.ref]?.spaces?.models;
    const kind = event.target.dataset.modelProvider || event.target.dataset.modelKind;
    if (!kind) return;
    const select = form.querySelector(`[data-model-kind="${kind}"]`);
    if (event.target.dataset.modelProvider) {
      select.innerHTML = modelOptions(catalog, kind, event.target.value, null);
    }
    form.querySelector(`[data-other-for="${kind}"]`).hidden = select.value !== OTHER_MODEL;
  });

  $("#project-grid").addEventListener("click", (event) => {
    const keys = event.target.closest("[data-keys-ref]");
    if (keys) {
      toggleKeys(keys.dataset.keysRef).catch((e) => toast(e.message, "error"));
      return;
    }
    const keyAction = event.target.closest("[data-key-copy], [data-key-dismiss], [data-key-revoke]");
    if (keyAction) {
      keysAction(keyAction).catch((e) => toast(e instanceof ApiError ? e.message : "Something went wrong.", "error"));
      return;
    }
    const memory = event.target.closest("[data-memory-ref]");
    if (memory) {
      toggleMemory(memory.dataset.memoryRef).catch((e) => toast(e.message, "error"));
      return;
    }
    const action = event.target.closest("[data-memory-delete], [data-memory-remove-key]");
    if (action) {
      memoryAction(action).catch((e) => toast(e instanceof ApiError ? e.message : "Something went wrong.", "error"));
      return;
    }
    const toggle = event.target.closest("[data-usage-ref]");
    if (toggle) {
      toggleUsage(toggle.dataset.usageRef).catch((e) => toast(e.message, "error"));
      return;
    }
    const move = event.target.closest("[data-upgrade-ref]");
    if (move) upgrade(move.dataset.upgradeRef, move.dataset.upgradePlan, move);
  });

  $("#refresh").addEventListener("click", () => {
    loadDashboard().catch((e) => toast(e.message, "error"));
  });

  // Tabs between sign in and create account.
  $$("[data-tab]").forEach((button) => {
    button.addEventListener("click", () => {
      const target = button.dataset.tab;
      $$("[data-tab]").forEach((b) => b.classList.toggle("active", b === button));
      $$("[data-tab-panel]").forEach((p) => {
        p.hidden = p.dataset.tabPanel !== target;
      });
    });
  });
}

async function start() {
  wire();
  applySignupGate();
  // Only mount a third-party challenge when signups can actually happen.
  if (signupsOpen()) turnstile.init();
  renderPlans();

  if (!session.token) {
    renderSession();
    return;
  }
  try {
    await loadDashboard();
    handleCheckoutReturn();
  } catch (error) {
    // A stored token that no longer works must not leave the console stuck on
    // a spinner; drop it and show the signed-out view.
    if (error instanceof ApiError && (error.status === 401 || error.status === 403)) {
      session.token = "";
      state.me = null;
      renderSession();
      toast("Your session expired. Sign in again.");
    } else {
      renderSession();
      toast(error.message, "error");
    }
  }
}

start();
