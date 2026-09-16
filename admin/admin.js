/**
 * The operator console (ADR-082 slice 4).
 *
 * Same conventions as the customer console (frontend/app.js), and the same rules:
 *
 * - **Every value interpolated into HTML is escaped** where it is interpolated, or is a
 *   number formatted here, or a literal. `tests/test_admin_frontend.py` walks every interpolation.
 * - **Nothing the API returns is written to browser storage.** The theme is the one thing
 *   kept, and it is not data.
 * - **The session is the HttpOnly cookie** the API sets; this script never sees it. Every
 *   request that changes state sends `X-MaluDB-Staff: 1`, which a cross-site form cannot.
 * - **The page's CSP allows no inline script or style**, so bar widths are set through
 *   CSSOM after rendering rather than as `style` attributes.
 */

const API = "/admin/v1";

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

const escapeHtml = (value) =>
  String(value ?? "").replace(
    /[&<>"']/g,
    (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c],
  );

class ApiError extends Error {
  constructor(message, status) {
    super(message);
    this.status = status;
  }
}

async function api(path, { method = "GET", body } = {}) {
  const headers = { Accept: "application/json" };
  if (method !== "GET") headers["X-MaluDB-Staff"] = "1";
  if (body !== undefined) headers["Content-Type"] = "application/json";
  let response;
  try {
    response = await fetch(`${API}${path}`, {
      method,
      headers,
      credentials: "same-origin",
      body: body === undefined ? undefined : JSON.stringify(body),
    });
  } catch {
    throw new ApiError("Could not reach the operator console API.", 0);
  }
  if (response.status === 204) return null;
  const payload = await response.json().catch(() => null);
  if (!response.ok) {
    const detail = payload && typeof payload.detail === "string" ? payload.detail : `${response.status} ${response.statusText}`;
    throw new ApiError(detail, response.status);
  }
  return payload;
}

/* -- formatting -------------------------------------------------------------------- */

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
const formatTime = (iso) =>
  iso ? new Date(iso).toLocaleString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" }) : "—";
const count = (n) => Number(n || 0).toLocaleString();

// Tones for the shared .badge styles.
const TONE = {
  active: "ready", trialing: "ready", applied: "ready", ok: "ready", ACTIVE: "ready", PROVISIONED: "ready",
  past_due: "failed", unpaid: "failed", failed: "failed", exceeded: "failed", restricted: "failed", FAILED: "failed",
  warning: "working", received: "working", incomplete: "working", RETRY_WAIT: "working",
};
const badge = (value) =>
  value === null || value === undefined
    ? `<span class="muted">—</span>`
    : `<span class="badge" data-tone="${escapeHtml(TONE[value] || "")}">${escapeHtml(value)}</span>`;

/** A meter from the API's {used, limit, percent, state, over_zero_ceiling}. Width applied by `applyBars`. */
function miniMeter(meter, bytes = true) {
  const used = meter.used === null ? "never measured" : bytes ? formatBytes(meter.used) : count(meter.used);
  const limit = bytes ? formatBytes(meter.limit) : count(meter.limit);
  const pct = meter.over_zero_ceiling ? 100 : Math.min(100, Number(meter.percent || 0));
  const label = meter.over_zero_ceiling ? "over a zero ceiling" : meter.percent === null ? "" : `${Number(meter.percent)}%`;
  return `
    <div class="mini-meter" data-state="${escapeHtml(meter.state || "ok")}">
      <small>${escapeHtml(used)} / ${escapeHtml(limit)} ${escapeHtml(label)}</small>
      <div class="usage-bar"><span data-pct="${Number(pct)}"></span></div>
    </div>`;
}

function applyBars(root = document) {
  for (const bar of $$("[data-pct]", root)) bar.style.width = `${Number(bar.dataset.pct)}%`;
}

function statCard(icon, value, label, note = "") {
  return `
    <div class="stat-card">
      <div class="stat-top">
        <span class="stat-icon"><svg class="icon"><use href="#${escapeHtml(icon)}"></use></svg></span>
        <div><span class="stat-value">${escapeHtml(value)}</span><span class="stat-label">${escapeHtml(label)}</span></div>
      </div>
      <div class="stat-foot"><span>${escapeHtml(note)}</span></div>
    </div>`;
}

/** A card holding a table. `head` is literal column names; `rows` is HTML built by the caller from escaped parts. */
function tableCard(title, head, rows, empty) {
  return `
    <div class="card">
      <div class="card-head"><h2>${escapeHtml(title)}</h2></div>
      ${rows.length
        ? `<div class="table-scroll"><table class="data-table">
             <thead><tr>${head.map((h) => `<th>${escapeHtml(h)}</th>`).join("")}</tr></thead>
             <tbody>${rows.join("")}</tbody>
           </table></div>`
        : `<p class="usage-note">${escapeHtml(empty)}</p>`}
    </div>`;
}

const customerLink = (id, name) => `<a href="#/customers/${encodeURIComponent(id)}">${escapeHtml(name)}</a>`;

function toast(message, kind = "info") {
  const node = $("#toast");
  node.textContent = message;
  node.dataset.kind = kind;
  node.classList.add("show");
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => node.classList.remove("show"), 5000);
}

/* -- pages ------------------------------------------------------------------------- */

const PAGES = {
  overview: { title: "Overview", render: overviewPage },
  sales: { title: "Sales", render: salesPage },
  "billing-events": { title: "Billing events", render: eventsPage },
  customers: { title: "Customers", render: customersPage },
  customer: { title: "Customer", render: customerPage },
  usage: { title: "Usage", render: usagePage },
  abuse: { title: "Abuse review", render: abusePage },
  nodes: { title: "Nodes", render: nodesPage },
  provisioning: { title: "Provisioning", render: provisioningPage },
};

async function overviewPage() {
  const o = await api("/overview");
  const plans = o.projects_by_plan.map((p) => `<tr><td>${escapeHtml(p.plan_code)}</td><td>${count(p.projects)}</td></tr>`);
  const states = o.subscriptions_by_state.map((s) => `<tr><td>${badge(s.state)}</td><td>${count(s.subscriptions)}</td></tr>`);
  return `
    <div class="stat-grid">
      ${statCard("i-users", count(o.organizations), "Organizations", `${count(o.users)} users, ${count(o.users_last_7_days)} new this week`)}
      ${statCard("i-db", count(o.projects), "Projects", `${count(o.projects_serving)} serving, ${count(o.projects_failed)} failed`)}
      ${statCard("i-clock", count(o.in_grace), "In grace", "Failed payments not yet restricted")}
      ${statCard("i-file", count(o.events_7d), "Billing events this week", `${count(o.events_7d_unhandled)} failed or unfinished`)}
    </div>
    <div class="grid-2">
      ${tableCard("Projects by plan", ["Plan", "Projects"], plans, "No projects yet.")}
      ${tableCard("Subscriptions by state", ["State", "Subscriptions"], states, "No subscriptions yet.")}
    </div>
    ${o.pending_reconciliation ? `<p class="usage-state">${count(o.pending_reconciliation)} paid-for change(s) are waiting for the maintenance pass.</p>` : ""}`;
}

async function salesPage(params) {
  const state = params.get("state") || "";
  const query = state ? `?state=${encodeURIComponent(state)}` : "";
  const s = await api(`/sales${query}`);
  const subs = s.subscriptions.map((r) => `
    <tr><td>${escapeHtml(r.project_ref)}<span class="cell-note">${escapeHtml(r.project_name)}</span></td>
      <td>${customerLink(r.org_id, r.org_name)}</td><td>${escapeHtml(r.plan_code)}</td><td>${badge(r.state)}</td>
      <td class="nowrap">${escapeHtml(formatDate(r.state_since))}</td><td class="nowrap">${escapeHtml(formatDate(r.period_end))}</td>
      <td><code>${escapeHtml(r.provider_subscription_id || "—")}</code></td></tr>`);
  const grace = s.in_grace.map((r) => `
    <tr><td>${escapeHtml(r.project_ref)}</td><td>${customerLink(r.org_id, r.org_name)}</td><td>${escapeHtml(r.plan_code)}</td>
      <td class="nowrap">${escapeHtml(formatDate(r.state_since))}</td><td class="nowrap">${escapeHtml(formatDate(r.expires_at))}</td></tr>`);
  const pending = s.pending_reconciliation.map((r) => `
    <tr><td>${escapeHtml(r.project_ref)}</td><td>${escapeHtml(r.plan_code)}</td><td>${badge(r.state)}</td>
      <td class="nowrap">${escapeHtml(formatTime(r.state_as_of))}</td></tr>`);
  const states = ["", "active", "trialing", "past_due", "incomplete", "unpaid", "paused", "canceled"];
  return `
    <form class="filters" data-filter="sales">
      <label>State <select name="state">${states.map((v) => `<option value="${escapeHtml(v)}"${v === state ? " selected" : ""}>${escapeHtml(v || "Any")}</option>`).join("")}</select></label>
    </form>
    ${tableCard(`Failed payments in grace (${Number(s.grace_days)} days)`, ["Project", "Customer", "Plan", "Past due since", "Restriction from"], grace, "No failed payments.")}
    ${tableCard("Subscriptions", ["Project", "Customer", "Plan", "State", "Since", "Period ends", "Stripe subscription"], subs, "No subscriptions.")}
    ${tableCard("Waiting for the maintenance pass", ["Project", "Plan", "State", "As of"], pending, "Nothing waiting.")}`;
}

async function eventsPage(params) {
  const outcome = params.get("outcome") || "";
  const events = await api(`/billing-events${outcome ? `?outcome=${encodeURIComponent(outcome)}` : ""}`);
  const rows = events.map((e) => `
    <tr><td class="nowrap">${escapeHtml(formatTime(e.received_at))}</td><td><code>${escapeHtml(e.event_type)}</code></td>
      <td>${badge(e.outcome)}</td><td>${escapeHtml(e.project_ref || "—")}</td><td>${e.livemode ? "live" : "test"}</td>
      <td>${escapeHtml(e.note || "")}</td></tr>`);
  const outcomes = ["", "failed", "received", "refused", "applied", "ignored"];
  return `
    <form class="filters" data-filter="billing-events">
      <label>Outcome <select name="outcome">${outcomes.map((v) => `<option value="${escapeHtml(v)}"${v === outcome ? " selected" : ""}>${escapeHtml(v || "Any")}</option>`).join("")}</select></label>
    </form>
    ${tableCard("What Stripe delivered", ["Received", "Event", "Outcome", "Project", "Mode", "Note"], rows, "No events.")}`;
}

async function customersPage(params) {
  const q = params.get("q") || "";
  const rows = (await api(`/customers${q ? `?q=${encodeURIComponent(q)}` : ""}`)).map((c) => `
    <tr><td>${customerLink(c.id, c.display_name)}${c.is_personal ? `<span class="cell-note">personal</span>` : ""}</td>
      <td>${escapeHtml(c.owners.join(", "))}</td><td>${count(c.members)}</td><td>${count(c.projects)}</td>
      <td>${count(c.paying_subscriptions)}</td><td class="nowrap">${escapeHtml(formatDate(c.created_at))}</td></tr>`);
  return `
    <form class="filters" data-filter="customers">
      <label>Search <input name="q" type="search" value="${escapeHtml(q)}" placeholder="name, slug or member email"></label>
      <button class="button secondary" type="submit">Search</button>
    </form>
    ${tableCard("Organizations", ["Name", "Owners", "Members", "Projects", "Paying", "Created"], rows, "No organizations match.")}`;
}

async function customerPage(params, id) {
  const c = await api(`/customers/${encodeURIComponent(id)}`);
  setHeader(c.display_name, [["#/customers", "Customers"], [null, c.display_name]]);
  const members = c.members.map((m) => `
    <tr><td>${escapeHtml(m.email)}${m.display_name ? `<span class="cell-note">${escapeHtml(m.display_name)}</span>` : ""}</td>
      <td>${escapeHtml(m.role)}</td><td>${badge(m.status)}</td><td class="nowrap">${escapeHtml(formatDate(m.last_login_at))}</td>
      <td class="nowrap">${escapeHtml(formatDate(m.joined_at))}</td></tr>`);
  const projects = c.projects.map((p) => `
    <tr><td>${escapeHtml(p.project_ref)}<span class="cell-note">${escapeHtml(p.display_name)}</span></td>
      <td>${badge(p.status)}</td><td>${escapeHtml(p.plan_code)}</td>
      <td>${escapeHtml(formatBytes(p.database_bytes))} ${badge(p.storage_state)}</td>
      <td>${escapeHtml(formatBytes(p.object_bytes))}</td><td class="nowrap">${escapeHtml(formatDate(p.created_at))}</td></tr>`);
  const subs = c.subscriptions.map((s) => `
    <tr><td>${escapeHtml(s.project_ref)}</td><td>${escapeHtml(s.plan_code)}</td><td>${badge(s.state)}</td>
      <td class="nowrap">${escapeHtml(formatDate(s.period_end))}</td><td><code>${escapeHtml(s.provider_customer_id || "—")}</code></td></tr>`);
  const events = c.billing_events.map((e) => `
    <tr><td class="nowrap">${escapeHtml(formatTime(e.received_at))}</td><td><code>${escapeHtml(e.event_type)}</code></td>
      <td>${badge(e.outcome)}</td><td>${escapeHtml(e.project_ref)}</td><td>${escapeHtml(e.note || "")}</td></tr>`);
  return `
    <p class="usage-note">Opening this page is recorded in the audit trail as a staff view of this organization.
      ${c.is_personal ? "A personal organization." : ""} Created ${escapeHtml(formatDate(c.created_at))}.</p>
    ${tableCard("Members", ["Email", "Role", "Status", "Last sign-in", "Joined"], members, "No members.")}
    ${tableCard("Projects", ["Project", "Status", "Plan", "Database", "Files", "Created"], projects, "No projects.")}
    <div class="grid-2">
      ${tableCard("Subscriptions", ["Project", "Plan", "State", "Period ends", "Stripe customer"], subs, "No subscriptions.")}
      ${tableCard("Billing events", ["Received", "Event", "Outcome", "Project", "Note"], events, "No events.")}
    </div>`;
}

function usageRows(rows) {
  return rows.map((r) => `
    <tr><td>${escapeHtml(r.project_ref)}<span class="cell-note">${escapeHtml(r.display_name)}</span></td>
      <td>${customerLink(r.org_id, r.org_name)}<span class="cell-note">${count(r.account_age_days)} days old</span></td>
      <td>${escapeHtml(r.plan_code)}</td><td>${miniMeter(r.database)}</td><td>${miniMeter(r.objects)}</td>
      <td>${miniMeter(r.egress)}</td><td>${miniMeter(r.email_day, false)}</td></tr>`);
}

const USAGE_HEAD = ["Project", "Customer", "Plan", "Database", "Files", "Egress (month)", "Email (day)"];

async function usagePage(params) {
  const plan = params.get("plan") || "";
  const rows = await api(`/usage${plan ? `?plan=${encodeURIComponent(plan)}` : ""}`);
  return `
    <form class="filters" data-filter="usage">
      <label>Plan <input name="plan" value="${escapeHtml(plan)}" placeholder="any"></label>
      <button class="button secondary" type="submit">Filter</button>
    </form>
    ${tableCard("Highest pressure first", USAGE_HEAD, usageRows(rows), "No projects.")}`;
}

async function abusePage(params) {
  const plan = params.get("plan") || "free";
  const min = params.get("min_percent") || "50";
  const rows = await api(`/abuse?plan=${encodeURIComponent(plan)}&min_percent=${encodeURIComponent(min)}`);
  return `
    <form class="filters" data-filter="abuse">
      <label>Plan <input name="plan" value="${escapeHtml(plan)}"></label>
      <label>At least (%) <input name="min_percent" type="number" min="0" max="1000" value="${escapeHtml(min)}"></label>
      <button class="button secondary" type="submit">Filter</button>
    </form>
    <p class="usage-note">Projects pressing on their ceilings, youngest accounts first among equals. This page reports; suspending
      a project is an explicit action taken with <code>cp-manage</code>. CPU and live connections are node-side and not here.</p>
    ${tableCard("Pressure", USAGE_HEAD, usageRows(rows), "Nothing at or above that pressure.")}`;
}

async function nodesPage() {
  const nodes = await api("/nodes");
  if (!nodes.length) return `<div class="card"><p class="usage-note">No nodes registered.</p></div>`;
  const meter = (used, limit) => {
    const pct = limit > 0 ? Math.min(100, Math.round((Number(used) / Number(limit)) * 100)) : 100;
    return `<div class="mini-meter"><small>${count(used)} / ${count(limit)}</small>
      <div class="usage-bar"><span data-pct="${Number(pct)}"></span></div></div>`;
  };
  return `<div class="node-grid">${nodes.map((n) => `
    <div class="card node-card">
      <div class="card-head">
        <div><h2>${escapeHtml(n.name)}</h2><p class="usage-note">${escapeHtml(n.node_pool)} pool · ${escapeHtml(n.status)}</p></div>
        <span class="badge" data-tone="${n.accepting ? "ready" : "failed"}">${n.accepting ? "accepting" : "not accepting"}</span>
      </div>
      <dl>
        <div><dt>Projects</dt><dd>${meter(n.projects, n.max_projects)}</dd></div>
        <div><dt>Warm</dt><dd>${meter(n.warm_projects, n.max_warm_projects)}</dd></div>
        <div><dt>Connections (projected)</dt><dd>${meter(n.projected_connections, n.usable_connections)}</dd></div>
        <div><dt>Replication slots</dt><dd>${meter(n.committed_slots, n.usable_replication_slots)}</dd></div>
        <div><dt>Free disk</dt><dd>${escapeHtml(formatBytes(n.free_disk_bytes))}
          <span class="cell-note">floor ${escapeHtml(formatBytes(n.min_free_disk_bytes))}</span></dd></div>
        <div><dt>Last health report</dt><dd>${escapeHtml(formatTime(n.last_health_at))}
          ${n.health_stale ? `<span class="cell-note">stale</span>` : ""}</dd></div>
        <div><dt>Realtime</dt><dd>${n.realtime_ready ? "prepared" : "not prepared"}</dd></div>
        <div><dt>Backups</dt><dd>${n.backup_ready ? "ready" : "not ready"}</dd></div>
      </dl>
      ${n.refusal ? `<p class="usage-state refusal">${escapeHtml(n.refusal)}</p>` : ""}
    </div>`).join("")}</div>`;
}

async function provisioningPage() {
  const p = await api("/provisioning");
  const row = (r) => `
    <tr><td>${escapeHtml(r.project_ref)}<span class="cell-note">${escapeHtml(r.display_name)}</span></td>
      <td>${customerLink(r.org_id, r.org_name)}</td><td>${badge(r.status)}</td><td>${escapeHtml(r.node_name || "—")}</td>
      <td>${escapeHtml(r.attempt ?? "—")}</td><td><code>${escapeHtml(r.error_code || "—")}</code></td>
      <td class="nowrap">${escapeHtml(formatTime(r.failed_at || r.requested_at || r.created_at))}</td>
      <td class="nowrap">${escapeHtml(formatTime(r.retry_after))}</td></tr>`;
  const head = ["Project", "Customer", "Status", "Node", "Attempt", "Error", "Since", "Retry after"];
  return `
    <p class="usage-note">Retrying or cleaning up is <code>cp-manage project retry</code> and <code>cleanup</code>, run on the control plane.</p>
    ${tableCard("Failed or waiting to retry", head, p.failed.map(row), "Nothing failed.")}
    ${tableCard(`In setup for more than ${Number(p.stuck_after_minutes)} minutes`, head, p.stuck.map(row), "Nothing stuck.")}`;
}

/* -- frame ------------------------------------------------------------------------- */

function setHeader(title, crumbs = []) {
  $("#page-title").textContent = title;
  $("#breadcrumb").innerHTML = crumbs
    .map(([href, text]) => (href ? `<a href="${escapeHtml(href)}">${escapeHtml(text)}</a>` : `<span>${escapeHtml(text)}</span>`))
    .join('<span class="sep" aria-hidden="true">/</span>');
  document.title = `${title} · MaluDB operator console`;
}

function parseRoute() {
  const [path, query = ""] = window.location.hash.replace(/^#\/?/, "").split("?");
  const params = new URLSearchParams(query);
  const customer = path.match(/^customers\/([0-9a-f-]{36})$/);
  if (customer) return { page: "customer", id: customer[1], params };
  return { page: PAGES[path] ? path : "overview", params };
}

async function renderRoute() {
  if (!state.staff) return;
  closeNav();
  const route = parseRoute();
  const page = PAGES[route.page];
  for (const link of $$("[data-nav]")) {
    const on = link.dataset.nav === (route.page === "customer" ? "customers" : route.page);
    link.classList.toggle("active", on);
    if (on) link.setAttribute("aria-current", "page");
    else link.removeAttribute("aria-current");
  }
  setHeader(page.title, [[null, "Operator"], [null, page.title]]);
  const view = $("#view");
  view.innerHTML = `<div class="card"><p class="usage-note">Loading…</p></div>`;
  const rendering = (renderRoute.seq = (renderRoute.seq || 0) + 1);
  try {
    const html = await page.render(route.params, route.id);
    if (rendering !== renderRoute.seq) return; // a newer navigation finished first
    view.innerHTML = html;
    applyBars(view);
  } catch (error) {
    if (error instanceof ApiError && error.status === 401) {
      signedOut("Your session ended. Sign in again.");
      return;
    }
    view.innerHTML = `<div class="card"><p class="form-error">${escapeHtml(error.message)}</p></div>`;
  }
}

const state = { staff: null };

function showSignedIn(staff) {
  state.staff = staff;
  document.body.classList.add("is-console");
  $("#signin").hidden = true;
  $("#console").hidden = false;
  $("#nav-account").hidden = false;
  $("#staff-name").textContent = staff.display_name || "";
  $("#staff-email").textContent = staff.email;
  $("#staff-avatar").textContent = (staff.display_name || staff.email || "?").trim()[0].toUpperCase();
  renderRoute();
}

function signedOut(message) {
  state.staff = null;
  document.body.classList.remove("is-console");
  $("#console").hidden = true;
  $("#nav-account").hidden = true;
  $("#view").innerHTML = ""; // what staff saw leaves with the session
  $("#signin").hidden = false;
  setHeader("Staff sign-in");
  if (message) toast(message);
  $('#signin-form input[name="email"]').focus();
}

function closeNav() {
  document.body.classList.remove("nav-open");
  $("#sidebar-scrim").hidden = true;
  $("#menu-toggle").setAttribute("aria-expanded", "false");
}

function wire() {
  $("#signin-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const form = event.currentTarget;
    const error = $(".form-error", form);
    const button = $('button[type="submit"]', form);
    error.hidden = true;
    button.disabled = true;
    const label = button.textContent;
    button.textContent = button.dataset.busy;
    const data = new FormData(form);
    try {
      const staff = await api("/session", {
        method: "POST",
        body: { email: String(data.get("email") || "").trim(), password: String(data.get("password") || ""),
                code: String(data.get("code") || "").replace(/\s/g, "") },
      });
      form.reset(); // the password and code do not stay in the page
      showSignedIn(staff);
    } catch (e) {
      form.elements.code.value = "";
      error.textContent = e.status === 429 ? "Too many attempts; wait a few minutes." : e.message;
      error.hidden = false;
    } finally {
      button.disabled = false;
      button.textContent = label;
    }
  });

  $("#signout").addEventListener("click", async () => {
    try {
      await api("/session", { method: "DELETE" });
    } catch {
      /* the cookie is cleared server-side when reachable; the page leaves either way */
    }
    signedOut("Signed out.");
  });

  // Filters become the address, so a filtered page can be reloaded and shared with other staff.
  $("#view").addEventListener("submit", (event) => {
    const form = event.target.closest("[data-filter]");
    if (!form) return;
    event.preventDefault();
    const params = new URLSearchParams();
    for (const [key, value] of new FormData(form)) if (String(value).trim()) params.set(key, String(value).trim());
    const query = params.toString();
    window.location.hash = `#/${form.dataset.filter}${query ? `?${query}` : ""}`;
  });
  $("#view").addEventListener("change", (event) => {
    const form = event.target.closest("[data-filter]");
    if (form && event.target.tagName === "SELECT") form.requestSubmit();
  });

  window.addEventListener("hashchange", renderRoute);

  $("#menu-toggle").addEventListener("click", () => {
    const open = !document.body.classList.contains("nav-open");
    document.body.classList.toggle("nav-open", open);
    $("#sidebar-scrim").hidden = !open;
    $("#menu-toggle").setAttribute("aria-expanded", String(open));
  });
  $("#sidebar-scrim").addEventListener("click", closeNav);

  $("#theme-toggle").addEventListener("click", () => {
    const root = document.documentElement;
    const dark = root.dataset.theme ? root.dataset.theme === "dark" : window.matchMedia("(prefers-color-scheme: dark)").matches;
    root.dataset.theme = dark ? "light" : "dark";
    try {
      localStorage.setItem("maludb.theme", root.dataset.theme);
    } catch {
      /* the choice still holds for this page load */
    }
  });
}

async function start() {
  wire();
  try {
    showSignedIn(await api("/session"));
  } catch {
    signedOut();
  }
}

start();
