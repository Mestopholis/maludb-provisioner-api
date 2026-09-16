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
  acceptInvitation,
  api,
  createApiKey,
  createToken,
  createMemorySpace,
  createProject,
  deleteMemorySpace,
  getDatabaseSchema,
  getUpgradeRequest,
  inviteMember,
  getUsage,
  listOrganizations,
  listApiKeys,
  listPlans,
  listMemorySpaces,
  listMembers,
  listProjects,
  listProviderKeys,
  listTokens,
  me,
  removeMember,
  removeProviderKey,
  revokeApiKey,
  revokeToken,
  runSql,
  session,
  setMemberRole,
  setMemoryModels,
  setProviderKey,
  signIn,
  signOut,
  signUp,
  requestUpgrade,
  startCheckout,
  transferOwnership,
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
  // project_ref -> the last /usage answer. Shown on the Plan & usage page and the overview.
  usage: {},
  upgradeRequests: {},
  // project_ref -> {spaces, keys} or {error}, for the Memory page.
  memory: {},
  // project_ref -> the key listing or {error}, for the API keys page and the overview.
  apiKeys: {},
  // project_ref -> a key just created, while it is on screen. The only place a
  // secret key's value is ever held: dropped when dismissed, when the page changes,
  // and on sign-out, and never written to storage.
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
  // The console's layout -- sidebar, top bar beside it -- hangs off this class.
  document.body.classList.toggle("is-console", signedIn);
  closeNav();
  document.title = signedIn ? "Projects · MaluDB" : "MaluDB";

  if (signedIn !== wasSignedIn) {
    // Arriving in the console, or back on the sales page, starts at the top of it.
    // A sales-page fragment such as #start is dropped so a reload does not scroll to
    // a section that is no longer shown. A console address (#/projects/...) is kept
    // on the way in -- it is where a reload or a shared link means to land -- and
    // dropped on the way out.
    const consoleRoute = window.location.hash.startsWith("#/");
    if (window.location.hash && !(signedIn && consoleRoute)) {
      window.history.replaceState(null, "", window.location.pathname + window.location.search);
    }
    window.scrollTo(0, 0);
  }
  if (!signedIn) {
    $("#account-email").textContent = "";
    $("#account-name").textContent = "";
    $("#account-avatar").textContent = "";
    return;
  }
  $("#account-email").textContent = state.me.email;
  $("#account-name").textContent = state.me.display_name || "";
  $("#account-avatar").textContent = initialOf(state.me.display_name || state.me.email);
}

/** The first letter of a name, for an avatar. Set as text, never as HTML. */
const initialOf = (name) => (String(name || "").trim()[0] || "?").toUpperCase();

/** The sidebar is off-canvas on narrow screens; any navigation closes it. */
function closeNav() {
  document.body.classList.remove("nav-open");
  $("#sidebar-scrim").hidden = true;
  $("#menu-toggle").setAttribute("aria-expanded", "false");
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
  // Opened from the page header's "New project"; open from the start when there is
  // nothing else on the page to look at.
  if (!canCreate) state.creating = false;
  else if (state.creating === undefined) state.creating = state.projects.length === 0;
  $("#create-project-card").hidden = !state.creating;
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

/**
 * The project list: a table, one row a project, each row opening the project's own
 * pages (`#/projects/<ref>`). Keys, usage and memory used to open inside a card here;
 * each is now a page of the project.
 */
function renderProjects() {
  renderProjectStats();
  const grid = $("#project-grid");
  if (!state.projects.length) {
    grid.innerHTML = `
      <div class="empty-projects">
        <span class="stat-icon"><svg class="icon"><use href="#i-db"></use></svg></span>
        <strong>No projects yet</strong>
        <span>A project is a PostgreSQL database of its own, with an API in front of it.</span>
      </div>`;
    return;
  }
  const orgName = (id) => state.orgs.find((o) => o.org_id === id)?.name || "";
  const rows = state.projects
    .map((p) => {
      const page = `#/projects/${encodeURIComponent(p.project_ref)}`;
      return `
      <tr data-status="${escapeHtml(p.status)}" data-tone="${escapeHtml(statusOf(p).tone)}">
        <td>
          <div class="project-cell">
            <span class="project-avatar" aria-hidden="true">${escapeHtml(initialOf(p.display_name))}</span>
            <div>
              <a href="${page}">${escapeHtml(p.display_name)}</a>
              <p class="project-ref">${escapeHtml(p.project_ref)}</p>
            </div>
          </div>
        </td>
        <td>
          <span class="badge" data-tone="${escapeHtml(statusOf(p).tone)}" title="${escapeHtml(p.status)}">${escapeHtml(statusOf(p).label)}</span>
          ${statusOf(p).moving ? `<span class="cell-note">Usually under a minute; this updates itself.</span>` : ""}
          ${p.status === "FAILED" ? `<span class="cell-note">Setup did not finish. Contact support with the project ref.</span>` : ""}
        </td>
        <td class="project-url hide-narrow"><code>${escapeHtml(p.api_url)}</code></td>
        <td class="hide-narrow">${escapeHtml(orgName(p.org_id))}</td>
        <td class="hide-narrow">${escapeHtml(formatDate(p.created_at))}</td>
        <td>
          <div class="row-actions">${
            statusOf(p).serving
              ? `<a class="button secondary small hide-narrow" href="${page}/sql">SQL</a>
                 <a class="button primary small" href="${page}">Open</a>`
              : `<a class="button secondary small" href="${page}">View</a>`
          }</div>
        </td>
      </tr>`;
    })
    .join("");
  grid.innerHTML = `
    <div class="table-scroll">
      <table class="data-table">
        <thead><tr>
          <th>Project</th><th>Status</th><th class="hide-narrow">API URL</th>
          <th class="hide-narrow">Organization</th><th class="hide-narrow">Created</th><th><span hidden>Actions</span></th>
        </tr></thead>
        <tbody>${rows}</tbody>
      </table>
    </div>`;
}

/** Counts above the project list -- what is there, and what is still being built. */
function renderProjectStats() {
  const ready = state.projects.filter((p) => statusOf(p).serving).length;
  const moving = state.projects.filter((p) => statusOf(p).moving).length;
  const card = (id, value, label, note) => `
    <div class="stat-card">
      <div class="stat-top">
        <span class="stat-icon"><svg class="icon"><use href="#${id}"></use></svg></span>
        <div><span class="stat-value">${escapeHtml(value)}</span><span class="stat-label">${escapeHtml(label)}</span></div>
      </div>
      <div class="stat-foot"><span>${escapeHtml(note)}</span></div>
    </div>`;
  $("#project-stats").innerHTML = [
    card("i-grid", state.projects.length, "Projects", "Across your organizations"),
    card("i-db", ready, "Ready", "Serving requests"),
    card("i-clock", moving, "In progress", moving ? "Setting up or changing" : "Nothing changing"),
    card("i-users", state.orgs.length, state.orgs.length === 1 ? "Organization" : "Organizations", "You belong to"),
  ].join("");
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

/** Limits that are enforced but not metered. Shared by the usage page and the overview. */
function usageLimits(usage) {
  return `
    <dl class="usage-limits">
      <div><dt>API requests</dt><dd>${escapeHtml(Number(usage.api_requests.limit).toLocaleString())}${
        usage.api_requests.window_seconds ? ` per ${escapeHtml(usage.api_requests.window_seconds)}s` : ""
      }</dd></div>
      <div><dt>Database connections</dt><dd>${escapeHtml(usage.database_connections.limit)}</dd></div>
      <div><dt>Realtime connections</dt><dd>${
        usage.realtime.enabled ? escapeHtml(usage.realtime.connection_limit) : "Not on this plan"
      }</dd></div>
    </dl>`;
}

function usagePanel(project) {
  const usage = state.usage[project.project_ref];
  if (!usage) return `<div class="card"><p class="usage-note">Loading…</p></div>`;
  if (usage.error) return `<div class="card"><p class="form-error">${escapeHtml(usage.error)}</p></div>`;
  return `
    <div class="grid-main-side">
      <div class="card">
        <div class="card-head"><h2>Usage</h2></div>
        ${meter({ label: "Database", used: usage.storage.used_bytes, limit: usage.storage.limit_bytes, state: usage.storage.state })}
        ${meter({ label: "File storage", used: usage.object_storage.used_bytes, limit: usage.object_storage.limit_bytes, state: usage.object_storage.state })}
        ${meter({ label: "Egress this month", used: usage.egress.used_bytes, limit: usage.egress.limit_bytes, state: usage.egress.state })}
        ${meter({ label: "Emails this month", used: usage.email.used, limit: usage.email.limit, state: usage.email.used >= usage.email.limit ? "exceeded" : "ok", bytes: false })}
      </div>
      <div class="card">
        <div class="card-head"><h2>Plan</h2></div>
        <div class="plan-summary"><strong>${escapeHtml(planName(usage.plan_code))}</strong><span class="usage-note">${escapeHtml(planPrice(usage.plan_code))}</span></div>
        ${billingSummary(usage)}
        ${usageLimits(usage)}
        ${upgradeActions(project, usage)}
      </div>
    </div>`;
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
  refreshOverview(ref);
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
    // The project's Plan & usage page; the route loads it.
    window.location.hash = projectHref(ref, "usage");
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

/**
 * Which key, and how to use it -- filled in with this project's URL and, where one
 * exists, its publishable key (public by design). A secret key is never filled in:
 * the page does not have it, and a snippet is the last place to put one.
 */
function keysHelp(project, live) {
  const url = escapeHtml(project.api_url);
  const publishable = live.find((k) => k.key_type === "publishable" && k.key);
  const pk = escapeHtml(publishable ? publishable.key : "<publishable key>");
  return `
    <details class="help"${live.length ? "" : " open"}>
      <summary>Which key do I use, and how?</summary>
      <ol>
        <li><strong>Publishable key</strong> — for browsers and apps. Row-level security applies, so a request sees only what
          your policies allow. Create one, then connect with the Supabase client:
<pre><code>import { createClient } from '@supabase/supabase-js'
const supabase = createClient('${url}', '${pk}')
const { data } = await supabase.from('todos').select('*')</code></pre></li>
        <li><strong>Secret key</strong> — for your servers only. It bypasses row-level security, and is shown once when
          created, so store it with your other server secrets:
<pre><code>curl '${url}/rest/v1/todos?select=*' -H 'apikey: &lt;secret key&gt;'</code></pre></li>
        <li>An empty result usually means the table has no policy for the publishable key.
          <a href="./docs.html#tables" target="_blank" rel="noopener">Creating tables and policies</a> ·
          <a href="./docs.html#keys" target="_blank" rel="noopener">More about keys</a></li>
      </ol>
    </details>`;
}

function keysPanel(project) {
  const ref = project.project_ref;
  const listing = state.apiKeys[ref];
  if (!listing) return `<div class="card"><p class="usage-note">Loading…</p></div>`;
  if (listing.error) return `<div class="card"><p class="form-error">${escapeHtml(listing.error)}</p></div>`;
  const manager = canManage(project.org_id);
  const live = listing.filter((k) => !k.revoked_at);
  const revoked = listing.length - live.length;
  const issued = state.issuedKey[ref];
  return `
    <div class="grid-main-side">
      <div class="card">
        <div class="card-head">
          <div>
            <h2>API keys</h2>
            <p class="usage-note">Send one in the <code>apikey</code> header to <code>${escapeHtml(project.api_url)}</code>.</p>
          </div>
        </div>
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
        }
      </div>
      <div class="card">
        <div class="card-head"><h2>Using a key</h2></div>
        <p class="usage-note">Use the publishable key in browsers and apps, and the secret key only on your servers.</p>
        ${keysHelp(project, live)}
      </div>
    </div>`;
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
  refreshOverview(ref);
}

/**
 * Leaving the page a key was shown on drops it. `renderRoute` calls this on every change
 * of page: the next time the keys page opens, a secret must not be sitting there for
 * whoever looks next. (It used to be closing the keys panel; the panel is now a page.)
 */
function dropIssuedKeys() {
  for (const ref of Object.keys(state.issuedKey)) delete state.issuedKey[ref];
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

/** How to use memory, in the order it works, with this project's URL and first space. */
function memoryHelp(project, spaces) {
  const url = escapeHtml(project.api_url);
  const first = spaces.spaces.find((s) => s.state === "active");
  const space = escapeHtml(first ? first.name : "<space>");
  return `
    <details class="help"${spaces.spaces.length ? "" : " open"}>
      <summary>How do I use memory?</summary>
      <ol>
        <li><strong>Create a space</strong> below — one per agent or purpose.</li>
        <li><strong>Set provider keys</strong> — your own accounts do the model work: <strong>Anthropic</strong> or
          <strong>OpenAI</strong> to find the statements in a text, and <strong>OpenAI</strong> or <strong>Voyage</strong>
          to embed them. Anthropic has no embeddings; Voyage is its recommendation.</li>
        <li><strong>Choose models</strong> under the space's <em>Models</em>. The embedding model is fixed once the space
          holds memories.</li>
        <li><strong>Store</strong> from your server, with the project's <strong>secret key</strong>:
<pre><code>curl -X POST '${url}/memory/v1/spaces/${space}/ingest' \\
  -H 'apikey: &lt;secret key&gt;' -H 'Content-Type: application/json' \\
  -d '{"items": [{"text": "Carol owns the parser module."}]}'</code></pre>
          It answers with a <code>status_url</code>; the memory is written in the background, usually within seconds.</li>
        <li><strong>Search</strong> by meaning, naming a subject or a verb:
<pre><code>curl -X POST '${url}/memory/v1/spaces/${space}/search' \\
  -H 'apikey: &lt;secret key&gt;' -H 'Content-Type: application/json' \\
  -d '{"text": "who owns the parser?", "subject": "Carol"}'</code></pre></li>
        <li><a href="./docs.html#memory" target="_blank" rel="noopener">Memory spaces in the docs</a></li>
      </ol>
    </details>`;
}

function memoryPanel(project) {
  const memory = state.memory[project.project_ref];
  if (!memory) return `<div class="card"><p class="usage-note">Loading…</p></div>`;
  if (memory.error) return `<div class="card"><p class="form-error">${escapeHtml(memory.error)}</p></div>`;
  const { spaces, keys } = memory;
  if (!spaces.entitled) {
    return `<div class="card"><p class="usage-note">This project's plan does not include memory spaces.</p></div>`;
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
    <div class="grid-main-side">
      <div class="card">
        <div class="card-head">
          <div>
            <h2>Memory spaces</h2>
            <p class="usage-note">Store and search from your server with the project's secret key at
              <code>${escapeHtml(project.api_url)}/memory/v1/spaces/&lt;name&gt;/ingest</code> and <code>…/search</code>.</p>
          </div>
        </div>
        ${spaces.spaces.map((space) => spaceCard(project, space, manager, spaces.models)).join("") || `<p class="usage-note">No spaces yet.</p>`}
        ${create}
      </div>
      <div class="card">
        <div class="card-head"><h2>Limits</h2></div>
        <dl class="usage-limits">
          <div><dt>Spaces</dt><dd>${escapeHtml(held)} of ${escapeHtml(spaces.max_spaces)}</dd></div>
          <div><dt>Stored memories</dt><dd>up to ${escapeHtml(Number(spaces.max_items).toLocaleString())}</dd></div>
          <div><dt>Ingest requests</dt><dd>${escapeHtml(Number(spaces.ingests_per_hour).toLocaleString())} an hour</dd></div>
        </dl>
        ${memoryHelp(project, spaces)}
        ${providerKeys(project, keys, manager)}
      </div>
    </div>`;
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

  // A space is built and deleted asynchronously; follow it while its page is open.
  clearTimeout(loadMemory.timer);
  const spaces = state.memory[ref]?.spaces?.spaces || [];
  if (onProjectPage(ref, "memory") && spaces.some((s) => SPACE_BUSY.has(s.state))) {
    loadMemory.timer = setTimeout(() => loadMemory(ref).catch(() => {}), 3000);
  }
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
  renderRoute();

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
 * Project pages
 *
 * Every project has pages of its own, and the console's frame -- the sidebar and the
 * page header -- follows the address:
 *
 *   #/projects/<ref>          overview: status, how to connect, usage at a glance
 *   #/projects/<ref>/sql      SQL editor        (Phase 08, below)
 *   #/projects/<ref>/tables   table browser     (Phase 08, below)
 *   #/projects/<ref>/keys     API keys          (Phase 07 slice 2, above)
 *   #/projects/<ref>/usage    plan and usage    (launch slice 2, above)
 *   #/projects/<ref>/memory   memory spaces     (ADR-079, above)
 *
 * Keys, usage and memory were panels that opened inside a project's card; each is now a
 * page. Only the overview is offered for a project that is not serving -- the rest follow
 * the gateway's SERVING_STATUSES, as the panels did.
 *
 * - **A value shown once does not survive leaving its page.** Any change of page drops a
 *   secret key still on screen (`dropIssuedKeys`), as closing its panel used to; the
 *   account pages do the same for tokens and invitation links.
 * - **The overview shows a key's value only for a live publishable key**, the one kind
 *   the API lists with its value.
 * - **Everything interpolated is escaped** where it is interpolated, or is an icon id
 *   from this file.
 * ------------------------------------------------------------------ */

const PROJECT_PAGES = [
  { tab: "overview", label: "Overview", icon: "i-home" },
  { tab: "sql", label: "SQL editor", icon: "i-terminal", serving: true, blurb: "Run SQL, as your app's roles too" },
  { tab: "tables", label: "Tables", icon: "i-table", serving: true, blurb: "Columns, policies and indexes" },
  { tab: "keys", label: "API keys", icon: "i-key", serving: true, blurb: "Publishable and secret keys" },
  { tab: "usage", label: "Plan & usage", icon: "i-chart", serving: true, blurb: "Limits, billing and upgrades" },
  { tab: "memory", label: "Memory", icon: "i-spark", serving: true, blurb: "Spaces for agent memory" },
];
const ACCOUNT_PAGES = { tokens: "Access tokens", organization: "Organization", invite: "Invitation" };

state.route = null;

/** An icon from the sprite in index.html. `id` is always a literal from this file. */
const icon = (id) => `<svg class="icon"><use href="#${id}"></use></svg>`;

/** The address of a project page. A ref is untrusted input, so it is encoded. */
const projectHref = (ref, tab = "overview") =>
  "#/projects/" + encodeURIComponent(ref) + (tab === "overview" ? "" : "/" + tab);

const pageOf = (tab) => PROJECT_PAGES.find((page) => page.tab === tab);

/** Whether that project page is the one on screen -- what loaders ask before drawing or polling. */
const onProjectPage = (ref, tab) => Boolean(state.route && state.route.ref === ref && state.route.tab === tab);

function parseRoute() {
  const match = window.location.hash.match(/^#\/projects\/([^/]+)(?:\/([a-z]+))?$/);
  if (!match) return null;
  let ref;
  try {
    ref = decodeURIComponent(match[1]);
  } catch {
    return null; // a malformed escape in a pasted address
  }
  return { ref, tab: pageOf(match[2]) ? match[2] : "overview" };
}

/** Show the page the address names: an account page, a project page, or the project list. */
function renderRoute() {
  if (!state.me) return;
  closeNav();
  const account = parseAccountRoute();
  let route = account ? null : parseRoute();
  const project = route && state.projects.find((p) => p.project_ref === route.ref);
  if (route && !project) {
    toast("No project with that ref in your organizations.", "error");
    window.history.replaceState(null, "", "#/");
    route = null;
  } else if (route && pageOf(route.tab).serving && !statusOf(project).serving) {
    toast("That project is not ready yet.", "error");
    window.history.replaceState(null, "", projectHref(route.ref));
    route = { ref: route.ref, tab: "overview" };
  }

  const same = Boolean(route && state.route && state.route.ref === route.ref && state.route.tab === route.tab);
  // A value shown once does not survive leaving the page it was shown on.
  if (!same) dropIssuedKeys();

  $("#account-view").hidden = !account;
  $("#dashboard").hidden = Boolean(account || route);
  $("#project-view").hidden = !route;
  renderSidebar(account, route, project);
  renderPageHeader(account, route, project);

  if (account) {
    state.route = null;
    renderAccountRoute(account);
    return;
  }
  state.accountRoute = null;
  if (!route) {
    state.route = null;
    document.title = "Projects · MaluDB";
    return;
  }
  state.route = route;
  document.title = `${project.display_name} · ${pageOf(route.tab).label} · MaluDB`;
  if (same) {
    // A dashboard refresh must not wipe what is being typed. The overview holds no
    // input and shows the project's status, so it alone is drawn again.
    if (route.tab === "overview") renderProjectView(project, route.tab);
    return;
  }
  renderProjectView(project, route.tab);
  window.scrollTo(0, 0);
  loadPage(project, route.tab);
}

/** Fetch what a page shows. Each loader draws into its page when it answers. */
function loadPage(project, tab) {
  const ref = project.project_ref;
  const reported = (promise) =>
    promise.catch((error) => toast(error instanceof ApiError ? error.message : "Something went wrong.", "error"));
  if (tab === "tables" && !state.tables[ref]?.schema) loadTables(ref);
  if (tab === "keys" || (tab === "overview" && statusOf(project).serving)) reported(loadKeys(ref));
  if (tab === "usage" || (tab === "overview" && statusOf(project).serving)) reported(loadUsage(ref));
  if (tab === "memory") reported(loadMemory(ref));
}

function renderSidebar(account, route, project) {
  const current = account ? (account.kind === "invite" ? "organization" : account.kind) : route ? null : "projects";
  for (const link of $$("[data-nav]")) {
    const on = link.dataset.nav === current;
    link.classList.toggle("active", on);
    if (on) link.setAttribute("aria-current", "page");
    else link.removeAttribute("aria-current");
  }
  const nav = $("#project-nav");
  nav.hidden = !route;
  if (!route) {
    nav.innerHTML = "";
    return;
  }
  const serving = statusOf(project).serving;
  nav.innerHTML = `
    <p class="project-nav-name" title="${escapeHtml(project.project_ref)}">
      <span class="dot" data-tone="${escapeHtml(statusOf(project).tone)}"></span><span>${escapeHtml(project.display_name)}</span>
    </p>
    ${PROJECT_PAGES.filter((page) => serving || !page.serving)
      .map((page) => `
        <a href="${escapeHtml(projectHref(project.project_ref, page.tab))}"${page.tab === route.tab ? ' class="active" aria-current="page"' : ""}>
          ${icon(page.icon)}<span>${escapeHtml(page.label)}</span></a>`)
      .join("")}`;
}

function renderPageHeader(account, route, project) {
  const sep = '<span class="sep" aria-hidden="true">/</span>';
  let title;
  let crumbs;
  let actions = "";
  if (account) {
    title = ACCOUNT_PAGES[account.kind];
    crumbs = ["<span>Account</span>", `<span>${escapeHtml(title)}</span>`];
  } else if (route) {
    const page = pageOf(route.tab);
    const status = statusOf(project);
    title = route.tab === "overview" ? project.display_name : page.label;
    crumbs = ['<a href="#/">Projects</a>'];
    if (route.tab === "overview") {
      crumbs.push("<span>Overview</span>");
    } else {
      crumbs.push(`<a href="${escapeHtml(projectHref(project.project_ref))}">${escapeHtml(project.display_name)}</a>`);
      crumbs.push(`<span>${escapeHtml(page.label)}</span>`);
    }
    actions = `<span class="badge" data-tone="${escapeHtml(status.tone)}" title="${escapeHtml(project.status)}">${escapeHtml(status.label)}</span>`;
    if (route.tab === "overview" && status.serving) {
      actions += `<a class="button primary small" href="${escapeHtml(projectHref(project.project_ref, "sql"))}">${icon("i-terminal")}SQL editor</a>`;
    }
  } else {
    title = "Projects";
    crumbs = ["<span>Workspace</span>", "<span>Projects</span>"];
    // Creating a project is a manager privilege; the route answers 403 otherwise.
    if (state.orgs.some((o) => o.role === "owner" || o.role === "admin")) {
      actions = `<button class="button primary" type="button" data-new-project aria-controls="create-project-card"
        aria-expanded="${state.creating ? "true" : "false"}">${icon("i-plus")}New project</button>`;
    }
  }
  $("#page-title").textContent = title; // text, not HTML: a project's name is the customer's
  $("#breadcrumb").innerHTML = crumbs.join(sep);
  $("#page-actions").innerHTML = actions;
}

function renderProjectView(project, tab) {
  const ref = escapeHtml(project.project_ref);
  $("#project-view").innerHTML = `<div class="project-view-body" data-view-for="${ref}">${pageBody(project, tab)}</div>`;
}

/** What a page holds. Keys, usage and memory draw into a container their loaders refill. */
function pageBody(project, tab) {
  const ref = escapeHtml(project.project_ref);
  if (tab === "sql") return `<div class="card">${sqlEditor(project)}</div>`;
  if (tab === "tables") return `<div class="card">${tablesBrowser(project)}</div>`;
  if (tab === "keys") return `<div class="usage-panel keys-panel" data-keys-for="${ref}">${keysPanel(project)}</div>`;
  if (tab === "usage") return `<div class="usage-panel" data-usage-for="${ref}">${usagePanel(project)}</div>`;
  if (tab === "memory") return `<div class="usage-panel memory-panel" data-memory-for="${ref}">${memoryPanel(project)}</div>`;
  return projectOverview(project);
}

function refreshView(ref) {
  const project = state.projects.find((p) => p.project_ref === ref);
  const body = $(`[data-view-for="${CSS.escape(ref)}"]`);
  if (!project || !body || !state.route || state.route.ref !== ref) return;
  body.innerHTML = pageBody(project, state.route.tab);
}

/* -- Overview ----------------------------------------------------------- */

function projectOverview(project) {
  const ref = escapeHtml(project.project_ref);
  const status = statusOf(project);
  if (!status.serving) {
    const note = status.moving
      ? "This usually takes under a minute; the page updates itself."
      : project.status === "FAILED"
        ? "Setup did not finish. Contact support with the project ref below."
        : "This project is not serving requests.";
    return `
      <div class="card status-card">
        <span class="stat-icon">${icon("i-db")}</span>
        <div>
          <h2>${escapeHtml(status.label)}</h2>
          <p class="usage-note">${escapeHtml(note)}</p>
          <p class="usage-note">Project ref <code>${ref}</code></p>
        </div>
      </div>`;
  }
  return `
    <div class="stat-grid" data-overview-usage="${ref}">${overviewStats(project)}</div>
    <div class="grid-main-side">
      <div class="card">
        <div class="card-head">
          <h2>Connect</h2>
          <a class="button secondary small" href="${escapeHtml(projectHref(project.project_ref, "keys"))}">${icon("i-key")}API keys</a>
        </div>
        <div class="connect-row"><span>API URL</span>
          <div class="copy-field"><code id="overview-url">${escapeHtml(project.api_url)}</code>
            <button class="button secondary small" type="button" data-key-copy="overview-url">Copy</button></div></div>
        <div class="connect-row"><span>Project ref</span>
          <div class="copy-field"><code id="overview-ref">${ref}</code>
            <button class="button secondary small" type="button" data-key-copy="overview-ref">Copy</button></div></div>
        <div data-overview-keys="${ref}">${overviewKey(project)}</div>
      </div>
      <div class="card" data-overview-plan="${ref}">${overviewPlan(project)}</div>
    </div>
    <div class="shortcut-grid">
      ${PROJECT_PAGES.filter((page) => page.serving)
        .map((page) => `
          <a class="shortcut" href="${escapeHtml(projectHref(project.project_ref, page.tab))}">
            <span class="stat-icon">${icon(page.icon)}</span>
            <span><strong>${escapeHtml(page.label)}</strong><span>${escapeHtml(page.blurb)}</span></span>
          </a>`)
        .join("")}
    </div>`;
}

/** The publishable key, which the API lists with its value. Never a secret: none is listed. */
function overviewKey(project) {
  const listing = state.apiKeys[project.project_ref];
  if (!listing) return `<p class="usage-note">Loading keys…</p>`;
  if (listing.error) return `<p class="form-error">${escapeHtml(listing.error)}</p>`;
  const publishable = listing.find((k) => !k.revoked_at && k.key_type === "publishable" && k.key);
  if (!publishable) {
    return `
      <div class="connect-row"><span>Publishable key</span>
        <p class="usage-note">None yet. <a href="${escapeHtml(projectHref(project.project_ref, "keys"))}">Create one</a>
          to connect from a browser or an app.</p></div>`;
  }
  return `
    <div class="connect-row"><span>Publishable key</span>
      <div class="copy-field"><code id="overview-pk">${escapeHtml(publishable.key)}</code>
        <button class="button secondary small" type="button" data-key-copy="overview-pk">Copy</button></div></div>`;
}

/** Usage at a glance: the same figures as the Plan & usage page, as stat cards. */
function overviewStats(project) {
  const usage = state.usage[project.project_ref];
  if (usage?.error) return `<div class="card"><p class="form-error">${escapeHtml(usage.error)}</p></div>`;
  const stat = (id, label, used, limit, meterState, bytes = true) => {
    const measured = Boolean(usage) && used !== null && used !== undefined;
    const pct = measured && limit > 0 ? Math.min(100, Math.round((used / limit) * 100)) : 0;
    const show = (v) => (bytes ? formatBytes(v) : Number(v).toLocaleString());
    return `
      <div class="stat-card" data-state="${escapeHtml(meterState || "ok")}">
        <div class="stat-top">
          <span class="stat-icon">${icon(id)}</span>
          <div><span class="stat-value">${escapeHtml(measured ? show(used) : "—")}</span><span class="stat-label">${escapeHtml(label)}</span></div>
        </div>
        <div class="stat-foot"><span>${escapeHtml(usage ? `of ${show(limit)}` : "Loading…")}</span><span>${escapeHtml(measured ? `${pct}%` : "")}</span></div>
        <div class="usage-bar" role="img" aria-label="${escapeHtml(label)}: ${escapeHtml(measured ? `${pct}% used` : "not measured yet")}">
          <span style="width: ${Number(pct)}%"></span></div>
      </div>`;
  };
  const u = usage || {};
  return [
    stat("i-db", "Database", u.storage?.used_bytes, u.storage?.limit_bytes, u.storage?.state),
    stat("i-file", "File storage", u.object_storage?.used_bytes, u.object_storage?.limit_bytes, u.object_storage?.state),
    stat("i-egress", "Egress this month", u.egress?.used_bytes, u.egress?.limit_bytes, u.egress?.state),
    stat("i-mail", "Emails this month", u.email?.used, u.email?.limit, u.email && u.email.used >= u.email.limit ? "exceeded" : "ok", false),
  ].join("");
}

function overviewPlan(project) {
  const usage = state.usage[project.project_ref];
  const head = `
    <div class="card-head">
      <h2>Plan</h2>
      <a class="button secondary small" href="${escapeHtml(projectHref(project.project_ref, "usage"))}">${icon("i-chart")}Plan &amp; usage</a>
    </div>`;
  if (!usage) return `${head}<p class="usage-note">Loading…</p>`;
  if (usage.error) return `${head}<p class="form-error">${escapeHtml(usage.error)}</p>`;
  return `${head}
    <div class="plan-summary"><strong>${escapeHtml(planName(usage.plan_code))}</strong><span class="usage-note">${escapeHtml(planPrice(usage.plan_code))}</span></div>
    ${billingSummary(usage)}
    ${usageLimits(usage)}`;
}

/** After a loader answers: redraw the overview's cards, if the overview is on screen. */
function refreshOverview(ref) {
  if (!onProjectPage(ref, "overview")) return;
  const project = state.projects.find((p) => p.project_ref === ref);
  if (!project || !statusOf(project).serving) return;
  const stats = $(`[data-overview-usage="${CSS.escape(ref)}"]`);
  if (stats) stats.innerHTML = overviewStats(project);
  const plan = $(`[data-overview-plan="${CSS.escape(ref)}"]`);
  if (plan) plan.innerHTML = overviewPlan(project);
  const keys = $(`[data-overview-keys="${CSS.escape(ref)}"]`);
  if (keys) keys.innerHTML = overviewKey(project);
}

/* ------------------------------------------------------------------ *
 * SQL editor and table browser (Phase 08 slices 1-3)
 *
 * Two of a project's pages, #/projects/<ref>/sql and #/projects/<ref>/tables, over
 * `POST /v1/projects/{ref}/sql` and `GET /v1/projects/{ref}/database/schema` --
 * on every plan the one way into the project's database.
 *
 * - **Everything a result holds is the customer's data**, and every value, column
 *   name and catalogue string is escaped where it is interpolated. Nothing typed or
 *   returned is written to storage: a statement can carry a secret, and a result can
 *   carry anyone's rows.
 * - **Run as** is the route's own impersonation: the project's admin role (it owns
 *   the tables, so row-level security does not apply to it unless forced), or anon,
 *   authenticated or service_role as the Data API would run a request, with optional
 *   JWT claims. It answers "what would my app see", not "what can I reach".
 * - **Both routes are rate-limited by the plan**; a 429 says when to try again.
 * - **The table browser never runs a statement of its own.** "Query this table" puts
 *   a query in the editor and leaves running it to the person, because on a free plan
 *   the statement budget is one per window.
 * ------------------------------------------------------------------ */

const SQL_ROLES = [
  ["", "Project admin — no row-level security"],
  ["anon", "anon — publishable key, signed out"],
  ["authenticated", "authenticated — signed-in user"],
  ["service_role", "service_role — secret key"],
];

// project_ref -> the editor: {statement, role, claims, result|error, running}
state.sql = {};
// project_ref -> the table browser: {schema|error, loading, selected, showManaged}
state.tables = {};

const STARTER_SQL = `create table public.todos (
  id bigint generated by default as identity primary key,
  title text not null,
  done boolean not null default false
);

-- Without row-level security, the publishable key can read and change every row.
alter table public.todos enable row level security;

create policy "anyone can read todos" on public.todos
  for select to anon, authenticated using (true);`;

/** A SQL identifier, quoted: "name", with any quote in it doubled. */
const quoteIdent = (name) => `"${String(name).replace(/"/g, '""')}"`;

/* -- SQL editor --------------------------------------------------------- */

function sqlEditor(project) {
  const ref = project.project_ref;
  const editor = (state.sql[ref] ||= { statement: "", role: "", claims: "" });
  const roles = SQL_ROLES.map(
    ([value, label]) => `<option value="${escapeHtml(value)}"${value === editor.role ? " selected" : ""}>${escapeHtml(label)}</option>`,
  ).join("");
  return `
    <form class="sql-form" data-sql-form data-ref="${escapeHtml(ref)}" novalidate>
      <p class="form-error" role="alert" hidden></p>
      <label class="sql-statement">SQL
        <textarea name="statement" rows="10" spellcheck="false" autocapitalize="off" autocomplete="off"
          placeholder="select * from public.todos limit 100;">${escapeHtml(editor.statement)}</textarea>
      </label>
      <div class="sql-controls">
        <label>Run as <select name="role" data-sql-role>${roles}</select>
          <small class="hint">The admin role owns your tables, so policies do not apply to it. Pick a role to see what
            your app's requests would.</small></label>
        <label data-sql-claims ${editor.role ? "" : "hidden"}>JWT claims <span class="optional">optional JSON</span>
          <textarea name="claims" rows="2" spellcheck="false"
            placeholder='{"sub": "00000000-0000-0000-0000-000000000000", "email": "user@example.com"}'>${escapeHtml(editor.claims)}</textarea>
          <small class="field-error" data-error-for="claims" hidden></small>
        </label>
        <div class="usage-actions">
          <button class="button primary" type="submit" data-busy="Running…">Run</button>
          <span class="usage-note">Ctrl+Enter or ⌘+Enter</span>
          ${editor.statement ? "" : `<button class="button secondary small" type="button" data-sql-starter="${escapeHtml(ref)}">Insert an example table</button>`}
        </div>
      </div>
    </form>
    <div class="sql-results">${sqlResults(editor)}</div>`;
}

/** One cell: NULL marked, JSON shown as JSON, everything escaped. */
function cell(value) {
  if (value === null || value === undefined) return `<span class="sql-null">NULL</span>`;
  const text = typeof value === "object" ? JSON.stringify(value) : String(value);
  return escapeHtml(text);
}

function sqlResults(editor) {
  if (editor.error) return `<p class="form-error sql-error">${escapeHtml(editor.error)}</p>`;
  const out = editor.result;
  if (!out) {
    return `<p class="usage-note">Results appear here. Statements run in your project's database, one request at a
      time; separate several with semicolons.</p>`;
  }
  const notes = [];
  if (out.requested_role && editor.ranAs) notes.push(`Ran as <code>${escapeHtml(out.requested_role)}</code>.`);
  if (out.storage_restricted) {
    notes.push(`<strong>This project is over its storage limit:</strong> inserts and updates are refused
      (<code>42501</code>) until space is freed or the plan is larger. Reads and deletes still work.`);
  }
  const blocks = out.results.map((r) => {
    const rows = r.rows || [];
    const summary = [
      r.command ? `<code>${escapeHtml(r.command)}</code>` : "",
      r.columns.length ? `${escapeHtml(rows.length.toLocaleString())} row${rows.length === 1 ? "" : "s"}` : "done",
      r.truncated ? `showing the first ${escapeHtml(out.row_limit.toLocaleString())} — the plan's limit for one result` : "",
    ].filter(Boolean).join(" · ");
    const table = r.columns.length
      ? `<div class="sql-table-wrap"><table class="sql-table">
           <thead><tr>${r.columns.map((c) => `<th>${escapeHtml(c)}</th>`).join("")}</tr></thead>
           <tbody>${rows.map((row) => `<tr>${r.columns.map((c) => `<td>${cell(row[c])}</td>`).join("")}</tr>`).join("")}</tbody>
         </table></div>`
      : "";
    return `<div class="sql-result"><p class="usage-note">${summary}</p>${table}</div>`;
  });
  return `${notes.map((n) => `<p class="usage-note">${n}</p>`).join("")}${blocks.join("") || `<p class="usage-note">No results.</p>`}`;
}

async function runSqlForm(form) {
  const ref = form.dataset.ref;
  const editor = state.sql[ref];
  await runForm(form, async (data) => {
    editor.statement = String(data.get("statement") || "");
    editor.role = String(data.get("role") || "");
    editor.claims = String(data.get("claims") || "");
    if (!editor.statement.trim()) {
      throw new ApiError("Write a statement first.", { status: 0 });
    }
    let claims = null;
    if (editor.role && editor.claims.trim()) {
      try {
        claims = JSON.parse(editor.claims);
      } catch {
        throw new ApiError("The JWT claims are not valid JSON.", { status: 0, fields: { claims: "Not valid JSON." } });
      }
      if (!claims || typeof claims !== "object" || Array.isArray(claims)) {
        throw new ApiError("The JWT claims must be a JSON object.", { status: 0, fields: { claims: "A JSON object, like {\"sub\": \"…\"}." } });
      }
    }
    try {
      editor.result = await runSql(ref, { statement: editor.statement, role: editor.role || null, claims });
      editor.ranAs = Boolean(editor.role);
      editor.error = null;
      // A statement may have changed the schema; the table browser reads it again.
      delete state.tables[ref]?.schema;
    } catch (error) {
      editor.result = null;
      editor.error = sqlErrorText(error);
      $(".sql-results", form.parentElement).innerHTML = sqlResults(editor);
      if (error instanceof ApiError && error.status === 400) return; // the statement's own error, shown below it
      throw error;
    }
    $(".sql-results", form.parentElement).innerHTML = sqlResults(editor);
  });
}

function sqlErrorText(error) {
  if (!(error instanceof ApiError)) return "Something went wrong running that.";
  if (error.status === 429) {
    return `This plan runs one statement at a time${error.retryAfter ? `; try again in ${error.retryAfter}s` : "; try again in a moment"}.`;
  }
  if (error.status === 409) return "This project is not ready to run SQL yet.";
  return error.message;
}

/* -- Table browser ------------------------------------------------------ */

async function loadTables(ref) {
  const browser = (state.tables[ref] ||= { showManaged: false, selected: null });
  browser.loading = true;
  browser.error = null; // a retry shows "Reading", not the error it is retrying
  refreshView(ref);
  try {
    browser.schema = await getDatabaseSchema(ref);
    browser.error = null;
  } catch (error) {
    browser.schema = null;
    browser.error = error instanceof ApiError && error.status === 429
      ? `Reading the schema is rate-limited${error.retryAfter ? `; try again in ${error.retryAfter}s` : ""}.`
      : error instanceof ApiError ? error.message : "Could not read the database.";
  }
  browser.loading = false;
  refreshView(ref);
}

const tableKey = (t) => `${t.schema_name}.${t.name}`;

function rlsNote(table) {
  if (!["table", "partitioned_table"].includes(table.kind)) return "";
  if (!table.rls_enabled) {
    return table.schema_name === "public"
      ? `<p class="usage-state"><strong>Row-level security is off.</strong> Anyone with the publishable key can read and
          change every row of this table.</p>`
      : `<p class="usage-note">Row-level security is off.</p>`;
  }
  if (!table.policies.length) {
    return `<p class="usage-note">Row-level security is on and there are no policies, so the publishable key and
      signed-in users see no rows. The secret key still sees everything.</p>`;
  }
  return `<p class="usage-note">Row-level security is on${table.rls_forced ? " and forced" : ""}, with
    ${escapeHtml(table.policies.length)} polic${table.policies.length === 1 ? "y" : "ies"}.</p>`;
}

function tableDetail(project, table) {
  const ref = escapeHtml(project.project_ref);
  const key = escapeHtml(tableKey(table));
  const enableRls = !table.rls_enabled && table.schema_name === "public" && ["table", "partitioned_table"].includes(table.kind);
  const list = (items, empty, render) => (items.length ? items.map(render).join("") : `<p class="usage-note">${empty}</p>`);
  return `
    <header class="table-detail-head">
      <h3><code>${escapeHtml(table.schema_name)}.${escapeHtml(table.name)}</code></h3>
      <span class="badge">${escapeHtml(table.kind.replace("_", " "))}</span>
    </header>
    <p class="usage-note">${
      // -1: PostgreSQL has not analysed the table yet, so it has no estimate to give.
      Number(table.estimated_rows) < 0 ? "Row count not estimated yet" : `About ${escapeHtml(Number(table.estimated_rows).toLocaleString())} rows (an estimate)`
    } ·
      ${escapeHtml(formatBytes(table.size_bytes))}${table.managed ? " · managed by the platform: readable, not alterable" : ""}</p>
    ${table.comment ? `<p class="usage-note">${escapeHtml(table.comment)}</p>` : ""}
    ${rlsNote(table)}
    <div class="usage-actions">
      <button class="button primary small" type="button" data-query-table="${key}" data-ref="${ref}">Query this table</button>
      ${enableRls ? `<button class="button secondary small" type="button" data-enable-rls="${key}" data-ref="${ref}">Turn on row-level security…</button>` : ""}
    </div>

    <h4>Columns</h4>
    <div class="sql-table-wrap"><table class="sql-table">
      <thead><tr><th>Name</th><th>Type</th><th>Nullable</th><th>Default</th></tr></thead>
      <tbody>${table.columns.map((c) => `<tr>
        <td><code>${escapeHtml(c.name)}</code>${c.is_identity ? ` <span class="badge">identity</span>` : ""}${c.is_generated ? ` <span class="badge">generated</span>` : ""}${
          c.comment ? `<br><span class="usage-note">${escapeHtml(c.comment)}</span>` : ""}</td>
        <td><code>${escapeHtml(c.data_type)}</code></td>
        <td>${c.is_nullable ? "yes" : "no"}</td>
        <td>${c.default_expression ? `<code>${escapeHtml(c.default_expression)}</code>` : ""}</td></tr>`).join("")}</tbody>
    </table></div>

    <h4>Policies</h4>
    ${list(table.policies, "None.", (p) => `<div class="table-item">
      <strong>${escapeHtml(p.name)}</strong> <span class="badge">${escapeHtml(p.command)}</span>
      ${p.permissive ? "" : `<span class="badge">restrictive</span>`}
      <p class="usage-note">To ${escapeHtml(p.roles.join(", ") || "public")}</p>
      ${p.using_expression ? `<p class="usage-note">Using <code>${escapeHtml(p.using_expression)}</code></p>` : ""}
      ${p.check_expression ? `<p class="usage-note">With check <code>${escapeHtml(p.check_expression)}</code></p>` : ""}
    </div>`)}

    <h4>Indexes</h4>
    ${list(table.indexes, "None.", (i) => `<div class="table-item"><code>${escapeHtml(i.definition)}</code>${
      i.is_valid ? "" : ` <span class="usage-state">invalid</span>`}</div>`)}

    <h4>Constraints</h4>
    ${list(table.constraints, "None.", (c) => `<div class="table-item"><strong>${escapeHtml(c.name)}</strong>
      <span class="badge">${escapeHtml(c.kind.replace("_", " "))}</span><br><code>${escapeHtml(c.definition)}</code></div>`)}`;
}

function tablesBrowser(project) {
  const ref = project.project_ref;
  const browser = state.tables[ref] || {};
  if (browser.error) {
    return `<p class="form-error">${escapeHtml(browser.error)}</p>
      <button class="button secondary small" type="button" data-tables-reload="${escapeHtml(ref)}">Try again</button>`;
  }
  // No schema means one is being read, or is about to be: a statement run in the editor
  // drops the snapshot so this tab reads the database again. Returning to the tab after
  // that used to read `schema.tables` of nothing and throw, leaving the editor on screen.
  if (!browser.schema) return `<p class="usage-note">Reading the database…</p>`;
  const schema = browser.schema;
  const visible = schema.tables.filter((t) => browser.showManaged || !t.managed);
  if (!browser.selected || !visible.some((t) => tableKey(t) === browser.selected)) {
    browser.selected = visible[0] ? tableKey(visible[0]) : null;
  }
  const bySchema = {};
  for (const t of visible) (bySchema[t.schema_name] ||= []).push(t);
  const functions = schema.functions.filter((f) => browser.showManaged || !f.managed);
  const selected = visible.find((t) => tableKey(t) === browser.selected);
  return `
    <div class="toolbar tables-toolbar">
      <label class="checkbox"><input type="checkbox" data-tables-managed="${escapeHtml(ref)}"${browser.showManaged ? " checked" : ""}>
        Show platform-managed objects</label>
      <button class="button secondary small" type="button" data-tables-reload="${escapeHtml(ref)}">Refresh</button>
    </div>
    ${schema.truncated.length ? `<p class="usage-state">Part of the schema was too large to read in one go
      (${escapeHtml(schema.truncated.join(", "))}); what is listed is not everything.</p>` : ""}
    ${
      visible.length
        ? `<div class="tables-layout">
             <nav class="tables-list" aria-label="Tables">
               ${Object.entries(bySchema).map(([name, tables]) => `
                 <p class="eyebrow">${escapeHtml(name)}</p>
                 <ul>${tables.map((t) => `<li><button type="button" class="table-link${tableKey(t) === browser.selected ? " active" : ""}"
                   data-select-table="${escapeHtml(tableKey(t))}" data-ref="${escapeHtml(ref)}">${escapeHtml(t.name)}${
                   ["table", "partitioned_table"].includes(t.kind) && !t.rls_enabled && t.schema_name === "public" ? ` <span class="rls-off" title="Row-level security is off">RLS off</span>` : ""
                 }${t.kind === "table" ? "" : ` <span class="usage-note">${escapeHtml(t.kind.replace("_", " "))}</span>`}</button></li>`).join("")}</ul>`).join("")}
             </nav>
             <section class="table-detail">${selected ? tableDetail(project, selected) : ""}</section>
           </div>`
        : `<div class="empty-state">
             <p>No tables yet.</p>
             <a class="button primary small" href="#/projects/${encodeURIComponent(ref)}/sql" data-sql-starter="${escapeHtml(ref)}">Create one in the SQL editor</a>
           </div>`
    }
    <details class="help">
      <summary>Functions (${escapeHtml(functions.length)}) and extensions (${escapeHtml(schema.extensions.length)})</summary>
      ${functions.length ? functions.map((f) => `<div class="table-item"><code>${escapeHtml(f.schema_name)}.${escapeHtml(f.name)}(${escapeHtml(f.arguments)})</code>${
        f.returns ? ` → <code>${escapeHtml(f.returns)}</code>` : ""} <span class="usage-note">${escapeHtml(f.language)}${f.security_definer ? ", security definer" : ""}</span></div>`).join("")
        : `<p class="usage-note">No functions of your own.</p>`}
      <p class="usage-note">Extensions: ${escapeHtml(schema.extensions.map((e) => `${e.name} ${e.installed_version}`).join(", ") || "none")}</p>
    </details>`;
}

/** Put a statement in the editor and go there, without running it. */
function openInEditor(ref, statement) {
  const editor = (state.sql[ref] ||= { statement: "", role: "", claims: "" });
  editor.statement = statement;
  editor.role = "";
  const target = `#/projects/${encodeURIComponent(ref)}/sql`;
  if (window.location.hash === target) {
    refreshView(ref);
  } else {
    window.location.hash = target;
  }
}

function projectViewAction(button) {
  const ref = button.dataset.ref || button.dataset.sqlStarter || button.dataset.tablesReload || button.dataset.tablesManaged;
  if (button.dataset.sqlStarter) {
    const typed = state.sql[ref]?.statement?.trim();
    if (typed && typed !== STARTER_SQL && !window.confirm("Replace what is in the editor with the example?")) return true;
    openInEditor(ref, STARTER_SQL);
    return true;
  }
  if (button.dataset.tablesReload) {
    loadTables(ref);
    return true;
  }
  const table = (key) => state.tables[ref]?.schema?.tables.find((t) => tableKey(t) === key);
  if (button.dataset.selectTable) {
    state.tables[ref].selected = button.dataset.selectTable;
    refreshView(ref);
    return true;
  }
  if (button.dataset.queryTable) {
    const t = table(button.dataset.queryTable);
    if (t) openInEditor(ref, `select * from ${quoteIdent(t.schema_name)}.${quoteIdent(t.name)} limit 100;`);
    return true;
  }
  if (button.dataset.enableRls) {
    const t = table(button.dataset.enableRls);
    if (t) {
      const name = `${quoteIdent(t.schema_name)}.${quoteIdent(t.name)}`;
      openInEditor(ref, `alter table ${name} enable row level security;

-- With no policy, the publishable key and signed-in users now see no rows.
-- Add a policy for what they should reach, for example:
-- create policy "anyone can read" on ${name} for select to anon, authenticated using (true);`);
    }
    return true;
  }
  return false;
}

/* ------------------------------------------------------------------ *
 * Account pages: access tokens and organization members
 *
 * #/tokens, #/organization and #/invite/<token>, over routes that already exist.
 *
 * - **A personal access token acts as you** on the platform API. Its value is
 *   shown once, straight after it is created, and held only in
 *   `state.issuedToken` while on screen -- dropped when dismissed, when the page
 *   changes, and on sign-out.
 * - **An invitation is a link shown once to the person who sent it.** Email
 *   delivery is not wired up, so the API returns the invitation's token to the
 *   inviter, and the page says so instead of implying an email went out. The link
 *   carries the token in the URL fragment, which a browser does not send to the
 *   server, and works only for the invited address, signed in, within seven days.
 * - **Controls follow the API's rules** rather than inviting a refusal: owners and
 *   admins manage members; only an owner grants or touches the owner role, or
 *   transfers ownership; nobody changes their own role. The API still enforces
 *   all of it -- this only avoids offering what it will refuse.
 * ------------------------------------------------------------------ */

// docs/ACCOUNTS.md, "Roles".
const ORG_ROLES = [
  ["owner", "Owner — everything, including billing, deleting the org, and transferring ownership"],
  ["admin", "Admin — manage projects, members, and API keys; not billing or org deletion"],
  ["developer", "Developer — create and operate projects; cannot manage members or billing"],
  ["billing", "Billing — manage payment and subscriptions only; no project data access"],
  ["viewer", "Viewer — read-only visibility of projects and usage"],
];
const roleName = (role) => (ORG_ROLES.find(([r]) => r === role)?.[1] || role).split(" — ")[0];
const TOKEN_EXPIRY = [["", "Never"], ["7", "7 days"], ["30", "30 days"], ["90", "90 days"], ["365", "1 year"]];

state.accountTokens = null; // the listing, or {error}
state.issuedToken = null; // {name, token} while shown -- the only place a token's value is held
state.orgId = null; // which organization the members page shows
state.members = {}; // org_id -> the listing, or {error}
state.issuedInvite = null; // {orgId, email, role, link} while shown
state.accountRoute = null;

function parseAccountRoute() {
  const hash = window.location.hash;
  if (hash === "#/tokens") return { kind: "tokens" };
  if (hash === "#/organization") return { kind: "organization" };
  const invite = hash.match(/^#\/invite\/([A-Za-z0-9_\-.~%]+)$/);
  if (invite) return { kind: "invite", token: decodeURIComponent(invite[1]) };
  return null;
}

function renderAccountRoute(route) {
  const changed = state.accountRoute?.kind !== route.kind;
  if (changed) {
    // A value shown once does not survive leaving the page it was shown on.
    state.issuedToken = null;
    state.issuedInvite = null;
  }
  state.accountRoute = route;
  document.title = `${{ tokens: "Access tokens", organization: "Organization", invite: "Invitation" }[route.kind]} · MaluDB`;
  if (route.kind === "tokens") {
    if (changed || !state.accountTokens) loadTokens();
  } else if (route.kind === "organization") {
    if (!state.orgId || !state.orgs.some((o) => o.org_id === state.orgId)) state.orgId = state.orgs[0]?.org_id || null;
    if (state.orgId && (changed || !state.members[state.orgId])) loadMembers(state.orgId);
  }
  if (changed) window.scrollTo(0, 0);
  refreshAccountView();
}

function refreshAccountView() {
  const route = state.accountRoute;
  if (!route) return;
  const view = $("#account-view");
  if (route.kind === "tokens") view.innerHTML = tokensPage();
  else if (route.kind === "organization") view.innerHTML = organizationPage();
  else view.innerHTML = invitePage(route.token);
}

/* -- Access tokens ------------------------------------------------------ */

async function loadTokens() {
  try {
    state.accountTokens = await listTokens();
  } catch (error) {
    state.accountTokens = { error: error instanceof ApiError ? error.message : "Could not load your tokens." };
  }
  refreshAccountView();
}

function tokensPage() {
  const tokens = state.accountTokens;
  const issued = state.issuedToken;
  const now = Date.now();
  const rows = !tokens
    ? `<p class="usage-note">Loading…</p>`
    : tokens.error
      ? `<p class="form-error">${escapeHtml(tokens.error)}</p>`
      : tokens.length
        ? tokens.map((t) => {
            const expired = t.expires_at && new Date(t.expires_at).getTime() <= now;
            return `
              <div class="api-key account-item" data-expired="${expired ? "true" : "false"}">
                <header><strong>${escapeHtml(t.name)}</strong>
                  <span class="badge">${expired ? "expired" : t.expires_at ? `expires ${escapeHtml(formatDate(t.expires_at))}` : "no expiry"}</span></header>
                <p class="usage-note"><code>${escapeHtml(t.token_prefix)}…</code> · created ${escapeHtml(formatDate(t.created_at))} ·
                  ${t.last_used_at ? `last used ${escapeHtml(formatDate(t.last_used_at))}` : "never used"}</p>
                <button class="button secondary small danger" type="button" data-token-revoke="${escapeHtml(t.id)}"
                  data-token-name="${escapeHtml(t.name)}">Revoke</button>
              </div>`;
          }).join("")
        : `<p class="usage-note">No tokens yet.</p>`;
  return `
    <div class="grid-main-side">
      <div class="card">
        <div class="card-head"><h2>Your tokens</h2></div>
        ${issued ? `
          <div class="key-issued" data-state="secret" role="status">
            <p><strong>Copy this token now.</strong> It is shown once and cannot be retrieved again. If it is lost, create another
              and revoke this one.</p>
            <code class="key-value" id="issued-token">${escapeHtml(issued.token)}</code>
            <div class="usage-actions">
              <button class="button primary small" type="button" data-key-copy="issued-token">Copy</button>
              <button class="button secondary small" type="button" data-token-dismiss>I have saved it</button>
            </div>
          </div>` : ""}
        <div class="account-list keys-panel">${rows}</div>
      </div>
      <div class="card">
        <div class="card-head"><h2>Create a token</h2></div>
        <p class="usage-note">A personal access token lets a script call the platform API — this site's <code>/api</code> — as you,
          with everything your account can do: for example, running SQL with <code>POST /v1/projects/&lt;ref&gt;/sql</code>.
          Send it as <code>Authorization: Bearer &lt;token&gt;</code>. It cannot create other tokens, and resetting your password
          revokes all of them. <a href="./docs.html#tables" target="_blank" rel="noopener">An example in the docs</a>.</p>
        <form class="stack" data-token-form novalidate>
          <p class="form-error" role="alert" hidden></p>
          <label>Name <input name="name" type="text" maxlength="200" placeholder="deploy script" required>
            <small class="field-error" data-error-for="name" hidden></small></label>
          <label>Expires <select name="expires">${TOKEN_EXPIRY.map(([v, l]) => `<option value="${escapeHtml(v)}"${v === "90" ? " selected" : ""}>${escapeHtml(l)}</option>`).join("")}</select></label>
          <div><button class="button primary" type="submit" data-busy="Creating…">Create token</button></div>
        </form>
      </div>
    </div>`;
}

/* -- Organization ------------------------------------------------------- */

async function loadMembers(orgId) {
  try {
    state.members[orgId] = await listMembers(orgId);
  } catch (error) {
    state.members[orgId] = { error: error instanceof ApiError ? error.message : "Could not load the members." };
  }
  refreshAccountView();
}

function organizationPage() {
  if (!state.orgs.length) return `<div class="card"><p class="usage-note">You are not in an organization.</p></div>`;
  const org = state.orgs.find((o) => o.org_id === state.orgId) || state.orgs[0];
  const myRole = org.role;
  const manager = myRole === "owner" || myRole === "admin";
  const owner = myRole === "owner";
  const members = state.members[org.org_id];
  const me = state.me.id;
  const grantable = ORG_ROLES.filter(([r]) => owner || r !== "owner");
  const picker = state.orgs.length > 1
    ? `<label class="org-picker">Organization <select data-org-picker>${state.orgs.map((o) =>
        `<option value="${escapeHtml(o.org_id)}"${o.org_id === org.org_id ? " selected" : ""}>${escapeHtml(o.name)}</option>`).join("")}</select></label>`
    : "";
  const list = !members
    ? `<p class="usage-note">Loading…</p>`
    : members.error
      ? `<p class="form-error">${escapeHtml(members.error)}</p>`
      : members.map((m) => {
          const self = m.user_id === me;
          // What the API would allow: not your own role, and the owner tier only for an owner.
          const canEdit = manager && !self && (owner || m.role !== "owner");
          const role = canEdit
            ? `<select data-member-role="${escapeHtml(m.user_id)}" data-member-email="${escapeHtml(m.email)}" aria-label="Role for ${escapeHtml(m.email)}">
                 ${grantable.map(([r]) => `<option value="${escapeHtml(r)}"${r === m.role ? " selected" : ""}>${escapeHtml(roleName(r))}</option>`).join("")}
               </select>`
            : `<span class="badge">${escapeHtml(roleName(m.role))}</span>`;
          return `
            <div class="member-row">
              <span class="member-email"><span class="avatar" aria-hidden="true">${escapeHtml(initialOf(m.email))}</span>
                <span>${escapeHtml(m.email)}${self ? ` <span class="usage-note">(you)</span>` : ""}</span></span>
              <span class="member-role">${role}</span>
              ${canEdit ? `<button class="button secondary small danger" type="button" data-member-remove="${escapeHtml(m.user_id)}"
                data-member-email="${escapeHtml(m.email)}">Remove</button>` : `<span></span>`}
            </div>`;
        }).join("");
  const issued = state.issuedInvite && state.issuedInvite.orgId === org.org_id ? state.issuedInvite : null;
  const others = Array.isArray(members) ? members.filter((m) => m.user_id !== me) : [];
  return `
    <div class="grid-main-side">
      <div class="card">
        <div class="card-head">
          <div>
            <h2>${escapeHtml(org.name)}</h2>
            <p class="usage-note">Your role: <strong>${escapeHtml(roleName(myRole))}</strong></p>
          </div>
          ${picker}
        </div>
        <div class="member-list">${list}</div>
        ${!manager ? `<p class="usage-note">An owner or admin can invite, change roles and remove members.</p>` : `
        <details class="help"><summary>What each role can do</summary>
          <ul>${ORG_ROLES.map(([, text]) => `<li>${escapeHtml(text)}</li>`).join("")}</ul>
        </details>`}
      </div>
      <div class="stack">
        ${!manager ? "" : `
        <div class="card">
          <div class="card-head"><h2>Invite someone</h2></div>
          ${issued ? `
            <div class="key-issued" data-state="secret" role="status">
              <p><strong>Send this link to ${escapeHtml(issued.email)}.</strong> No email is sent yet, so the link is shown here once.
                It joins them as <strong>${escapeHtml(roleName(issued.role))}</strong>, works only when they are signed in as
                ${escapeHtml(issued.email)}, and expires in 7 days.</p>
              <code class="key-value" id="issued-invite">${escapeHtml(issued.link)}</code>
              <div class="usage-actions">
                <button class="button primary small" type="button" data-key-copy="issued-invite">Copy link</button>
                <button class="button secondary small" type="button" data-invite-dismiss>Done</button>
              </div>
            </div>` : ""}
          <form class="stack" data-invite-form data-org="${escapeHtml(org.org_id)}" novalidate>
            <p class="form-error" role="alert" hidden></p>
            <label>Email <input name="email" type="email" required placeholder="teammate@example.com">
              <small class="field-error" data-error-for="email" hidden></small></label>
            <label>Role <select name="role">${grantable.map(([r]) => `<option value="${escapeHtml(r)}"${r === "developer" ? " selected" : ""}>${escapeHtml(roleName(r))}</option>`).join("")}</select></label>
            <div><button class="button primary" type="submit" data-busy="Inviting…">Create invitation</button></div>
          </form>
        </div>`}
        ${owner && others.length ? `
        <div class="card">
          <div class="card-head"><h2>Transfer ownership</h2></div>
          <form class="stack" data-transfer-form data-org="${escapeHtml(org.org_id)}" novalidate>
            <p class="form-error" role="alert" hidden></p>
            <p class="usage-note">Makes another member the owner. You stay in the organization as an admin.</p>
            <label>New owner <select name="to_user_id">${others.map((m) => `<option value="${escapeHtml(m.user_id)}">${escapeHtml(m.email)}</option>`).join("")}</select></label>
            <div><button class="button secondary danger" type="submit" data-busy="Transferring…">Transfer ownership…</button></div>
          </form>
        </div>` : ""}
      </div>
    </div>`;
}

/* -- Accepting an invitation -------------------------------------------- */

function invitePage(token) {
  return `
    <div class="card auth-card invite-card">
      <span class="auth-mark brand-mark" aria-hidden="true">M</span>
      <h2>Join an organization</h2>
      <p class="usage-note">You have been invited to join an organization. Accepting works only for the address the invitation was
        sent to — you are signed in as <strong>${escapeHtml(state.me.email)}</strong>.</p>
      <form class="usage-actions" data-accept-form novalidate>
        <p class="form-error" role="alert" hidden></p>
        <input type="hidden" name="token" value="${escapeHtml(token)}">
        <button class="button primary" type="submit" data-busy="Joining…">Join the organization</button>
        <a class="button secondary" href="#/">Not now</a>
      </form>
    </div>`;
}

async function accountForm(form) {
  if (form.matches("[data-token-form]")) {
    await runForm(form, async (data) => {
      const name = String(data.get("name") || "").trim();
      if (!name) throw new ApiError("Give the token a name.", { status: 0, fields: { name: "Required." } });
      const days = Number(data.get("expires") || 0);
      const expiresAt = days ? new Date(Date.now() + days * 86_400_000).toISOString() : null;
      const issued = await createToken({ name, expiresAt });
      state.issuedToken = { name: issued.name, token: issued.token };
      toast("Token created. Copy it now.", "success");
      await loadTokens();
    });
  } else if (form.matches("[data-invite-form]")) {
    await runForm(form, async (data) => {
      const orgId = form.dataset.org;
      const invite = await inviteMember(orgId, { email: String(data.get("email") || "").trim(), role: String(data.get("role")) });
      const link = `${window.location.origin}${window.location.pathname}#/invite/${encodeURIComponent(invite.token)}`;
      state.issuedInvite = { orgId, email: invite.email, role: invite.role, link };
      toast("Invitation created. Send the link yourself.", "success");
      refreshAccountView();
    });
  } else if (form.matches("[data-transfer-form]")) {
    await runForm(form, async (data) => {
      const orgId = form.dataset.org;
      const to = String(data.get("to_user_id"));
      const email = state.members[orgId]?.find((m) => m.user_id === to)?.email || "that member";
      if (!window.confirm(`Make ${email} the owner of this organization? You will become an admin, and only they can make you owner again.`)) return;
      await transferOwnership(orgId, to);
      toast(`${email} is now the owner.`, "success");
      // Members first, then your own role: drawn the other way round, the page briefly
      // offered to edit the new owner -- your new role against the old member list.
      await loadMembers(orgId);
      await loadDashboard();
    });
  } else if (form.matches("[data-accept-form]")) {
    await runForm(form, async (data) => {
      const org = await acceptInvitation(String(data.get("token")));
      toast(`You joined ${org.name}.`, "success");
      window.history.replaceState(null, "", "#/organization");
      state.orgId = org.org_id;
      await loadDashboard();
    });
  }
}

async function accountAction(control) {
  if (control.dataset.keyCopy) {
    await copyKey(control.dataset.keyCopy);
  } else if (control.matches("[data-token-dismiss]")) {
    state.issuedToken = null;
    refreshAccountView();
  } else if (control.matches("[data-invite-dismiss]")) {
    state.issuedInvite = null;
    refreshAccountView();
  } else if (control.dataset.tokenRevoke) {
    if (!window.confirm(`Revoke "${control.dataset.tokenName}"? Anything using it stops working immediately.`)) return;
    await revokeToken(control.dataset.tokenRevoke);
    toast("Token revoked.", "success");
    await loadTokens();
  } else if (control.dataset.memberRemove) {
    const orgId = state.orgId;
    if (!window.confirm(`Remove ${control.dataset.memberEmail} from this organization? They lose access to its projects.`)) return;
    await removeMember(orgId, control.dataset.memberRemove);
    toast(`${control.dataset.memberEmail} was removed.`, "success");
    await loadMembers(orgId);
  }
}

async function changeMemberRole(select) {
  const orgId = state.orgId;
  const previous = state.members[orgId]?.find((m) => m.user_id === select.dataset.memberRole)?.role;
  if (select.value === "owner" && !window.confirm(`Make ${select.dataset.memberEmail} an owner? Owners can remove other owners and delete the organization.`)) {
    select.value = previous;
    return;
  }
  try {
    await setMemberRole(orgId, select.dataset.memberRole, select.value);
    toast(`${select.dataset.memberEmail} is now ${roleName(select.value)}.`, "success");
  } catch (error) {
    toast(error instanceof ApiError ? error.message : "Could not change the role.", "error");
  }
  await loadMembers(orgId);
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
    state.creating = false;
    toast(`Creating ${project.display_name}. It will show as Ready in a minute or so.`, "success");
    await loadDashboard();
  });

  $("#signout").addEventListener("click", async () => {
    await signOut();
    state.me = null;
    state.orgs = [];
    state.projects = [];
    state.creating = undefined;
    state.usage = {};
    state.upgradeRequests = {};
    state.memory = {};
    state.apiKeys = {};
    state.issuedKey = {}; // a secret still on screen does not survive sign-out
    state.accountTokens = null;
    state.issuedToken = null; // a token or invitation link still on screen does not survive sign-out
    state.members = {};
    state.issuedInvite = null;
    state.accountRoute = null;
    $("#account-view").hidden = true;
    $("#account-view").innerHTML = "";
    state.sql = {}; // statements and results are the customer's; they leave with the session
    state.tables = {};
    state.route = null;
    $("#project-view").hidden = true;
    $("#project-view").innerHTML = "";
    $("#project-nav").innerHTML = "";
    $("#dashboard").hidden = false;
    clearTimeout(loadMemory.timer);
    clearTimeout(loadDashboard.timer);
    renderSession();
    renderProjects();
    toast("Signed out.");
  });

  // The console's frame: the menu on narrow screens, and the page header's actions.
  $("#menu-toggle").addEventListener("click", () => {
    const open = !document.body.classList.contains("nav-open");
    document.body.classList.toggle("nav-open", open);
    $("#sidebar-scrim").hidden = !open;
    $("#menu-toggle").setAttribute("aria-expanded", String(open));
  });
  $("#sidebar-scrim").addEventListener("click", closeNav);
  $("#sidebar").addEventListener("click", (event) => {
    if (event.target.closest("a")) closeNav();
  });
  $("#page-actions").addEventListener("click", (event) => {
    const button = event.target.closest("[data-new-project]");
    if (!button) return;
    state.creating = !state.creating;
    $("#create-project-card").hidden = !state.creating;
    button.setAttribute("aria-expanded", String(state.creating));
    if (state.creating) $('#create-project-form input[name="display_name"]').focus();
  });

  // The theme: the system's unless chosen here, and then remembered in this browser only.
  $("#theme-toggle").addEventListener("click", () => {
    const root = document.documentElement;
    const dark = root.dataset.theme
      ? root.dataset.theme === "dark"
      : window.matchMedia("(prefers-color-scheme: dark)").matches;
    root.dataset.theme = dark ? "light" : "dark";
    try {
      localStorage.setItem("maludb.theme", root.dataset.theme);
    } catch {
      /* the choice still holds for this page load */
    }
  });

  // The project pages: #/projects/<ref>[/sql|tables|keys|usage|memory].
  window.addEventListener("hashchange", () => renderRoute());
  const view = $("#project-view");
  view.addEventListener("submit", (event) => {
    const keys = event.target.closest("[data-keys-form]");
    if (keys) {
      event.preventDefault();
      keysForm(keys);
      return;
    }
    const memory = event.target.closest("[data-memory-form]");
    if (memory) {
      event.preventDefault();
      memoryForm(memory);
      return;
    }
    const form = event.target.closest("[data-sql-form]");
    if (!form) return;
    event.preventDefault();
    runSqlForm(form);
  });
  view.addEventListener("click", (event) => {
    const keyAction = event.target.closest("[data-key-copy], [data-key-dismiss], [data-key-revoke]");
    if (keyAction) {
      keysAction(keyAction).catch((e) => toast(e instanceof ApiError ? e.message : "Something went wrong.", "error"));
      return;
    }
    const action = event.target.closest("[data-memory-delete], [data-memory-remove-key]");
    if (action) {
      memoryAction(action).catch((e) => toast(e instanceof ApiError ? e.message : "Something went wrong.", "error"));
      return;
    }
    const move = event.target.closest("[data-upgrade-ref]");
    if (move) {
      upgrade(move.dataset.upgradeRef, move.dataset.upgradePlan, move);
      return;
    }
    const button = event.target.closest(
      "[data-sql-starter], [data-tables-reload], [data-select-table], [data-query-table], [data-enable-rls]",
    );
    if (button && projectViewAction(button)) event.preventDefault();
  });
  view.addEventListener("change", (event) => {
    // The model picker: a new provider lists its own models, with its default chosen;
    // "Other model…" reveals the name field.
    const models = event.target.closest('[data-memory-form="models"]');
    if (models) {
      const catalog = state.memory[models.dataset.ref]?.spaces?.models;
      const kind = event.target.dataset.modelProvider || event.target.dataset.modelKind;
      if (!kind) return;
      const select = models.querySelector(`[data-model-kind="${kind}"]`);
      if (event.target.dataset.modelProvider) {
        select.innerHTML = modelOptions(catalog, kind, event.target.value, null);
      }
      models.querySelector(`[data-other-for="${kind}"]`).hidden = select.value !== OTHER_MODEL;
      return;
    }
    const role = event.target.closest("[data-sql-role]");
    if (role) {
      const form = role.closest("[data-sql-form]");
      state.sql[form.dataset.ref].role = role.value;
      $("[data-sql-claims]", form).hidden = !role.value;
      return;
    }
    const managed = event.target.closest("[data-tables-managed]");
    if (managed) {
      state.tables[managed.dataset.tablesManaged].showManaged = managed.checked;
      refreshView(managed.dataset.tablesManaged);
    }
  });
  // What is typed survives switching pages; it is kept in memory only.
  view.addEventListener("input", (event) => {
    const form = event.target.closest("[data-sql-form]");
    if (!form || !["statement", "claims"].includes(event.target.name)) return;
    state.sql[form.dataset.ref][event.target.name] = event.target.value;
  });
  view.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && (event.ctrlKey || event.metaKey) && event.target.name === "statement") {
      event.preventDefault();
      event.target.closest("form").requestSubmit();
    }
  });

  // The account pages: #/tokens, #/organization, #/invite/<token>.
  const account = $("#account-view");
  account.addEventListener("submit", (event) => {
    const form = event.target.closest("[data-token-form], [data-invite-form], [data-transfer-form], [data-accept-form]");
    if (!form) return;
    event.preventDefault();
    accountForm(form);
  });
  account.addEventListener("click", (event) => {
    const control = event.target.closest("[data-key-copy], [data-token-dismiss], [data-invite-dismiss], [data-token-revoke], [data-member-remove]");
    if (!control) return;
    accountAction(control).catch((e) => toast(e instanceof ApiError ? e.message : "Something went wrong.", "error"));
  });
  account.addEventListener("change", (event) => {
    if (event.target.matches("[data-member-role]")) {
      changeMemberRole(event.target);
    } else if (event.target.matches("[data-org-picker]")) {
      state.orgId = event.target.value;
      state.issuedInvite = null;
      if (!state.members[state.orgId]) loadMembers(state.orgId);
      refreshAccountView();
    }
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
    if (window.location.hash.startsWith("#/invite/")) {
      // Signed out with an invitation link: sign in first; the address is kept, so the
      // invitation opens once you are in.
      $$("[data-tab]").find((b) => b.dataset.tab === "signin")?.click();
      $("#start").scrollIntoView();
      toast("Sign in with the address you were invited as to accept the invitation.");
    }
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
