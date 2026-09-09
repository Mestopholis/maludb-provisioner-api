const DEFAULT_API_BASE = "/api";

const fallbackPlans = [
  {
    code: "free",
    name: "Free",
    limits: {
      direct_database_access: false,
      sql_console: true,
      api_worker_policy: "sleep_when_inactive",
      database_storage_bytes: 524_288_000,
      object_storage_bytes: 1_073_741_824,
      egress_bytes_per_month: 5_368_709_120,
      database_connections: 10,
      realtime_connections: 0,
      max_projects: 2,
      backup_retention_days: 7,
      pitr_window_hours: 0,
    },
  },
  {
    code: "starter",
    name: "Starter",
    limits: {
      direct_database_access: true,
      sql_console: true,
      api_worker_policy: "warm",
      database_storage_bytes: 8_589_934_592,
      object_storage_bytes: 26_843_545_600,
      egress_bytes_per_month: 107_374_182_400,
      database_connections: 30,
      realtime_connections: 200,
      max_projects: 20,
      backup_retention_days: 14,
      pitr_window_hours: 168,
    },
  },
  {
    code: "production",
    name: "Production",
    limits: {
      direct_database_access: true,
      sql_console: true,
      api_worker_policy: "warm",
      database_storage_bytes: 107_374_182_400,
      object_storage_bytes: 268_435_456_000,
      egress_bytes_per_month: 1_099_511_627_776,
      database_connections: 90,
      realtime_connections: 2_000,
      max_projects: 100,
      backup_retention_days: 30,
      pitr_window_hours: 720,
    },
  },
];

const products = [
  {
    icon: "DB",
    title: "Tenant Database",
    body: "A dedicated PostgreSQL/MaluDB database per project, with constrained roles and platform-owned infrastructure.",
  },
  {
    icon: "API",
    title: "Data API",
    body: "Supabase-shaped HTTP access routed by project hostname and validated against project-scoped keys.",
  },
  {
    icon: "AU",
    title: "Auth",
    body: "Per-project GoTrue workers with JWT/RLS integration and platform email hooks.",
  },
  {
    icon: "ST",
    title: "Storage",
    body: "S3-backed object storage available on every tier, bounded by per-plan held-byte and egress ceilings.",
  },
  {
    icon: "RT",
    title: "Realtime",
    body: "Per-project Realtime instances for paid tiers, using logical decoding with node-level safety controls.",
  },
  {
    icon: "SQL",
    title: "SQL Console",
    body: "Mediated SQL execution through the platform, available to every tier without handing free projects direct credentials.",
  },
  {
    icon: "BK",
    title: "Backups and PITR",
    body: "Node backups and per-tenant restore, with PITR windows governed by plan entitlement.",
  },
  {
    icon: "$",
    title: "Billing",
    body: "Checkout and subscription state are exposed by the control plane when billing is configured.",
  },
];

const state = {
  apiBase: localStorage.getItem("maludb.apiBase") || DEFAULT_API_BASE,
  token: localStorage.getItem("maludb.sessionToken") || "",
  me: null,
  orgs: [],
  plans: fallbackPlans,
  selectedProject: null,
};

const $ = (selector) => document.querySelector(selector);

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, (char) => {
    const entities = {
      "&": "&amp;",
      "<": "&lt;",
      ">": "&gt;",
      '"': "&quot;",
      "'": "&#39;",
    };
    return entities[char];
  });
}

function formatBytes(value) {
  if (value === null || value === undefined) return "not counted";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let size = Number(value);
  let unit = 0;
  while (size >= 1024 && unit < units.length - 1) {
    size /= 1024;
    unit += 1;
  }
  return `${size.toFixed(size >= 10 || unit === 0 ? 0 : 1)} ${units[unit]}`;
}

function formatBoolean(value, yes = "Yes", no = "No") {
  return value ? yes : no;
}

function limitOf(plan, key) {
  return plan.limits?.[key];
}

function toast(message, error = false) {
  const node = $("#toast");
  node.textContent = message;
  node.classList.toggle("error", error);
  node.classList.add("show");
  window.clearTimeout(toast.timer);
  toast.timer = window.setTimeout(() => node.classList.remove("show"), 4200);
}

async function api(path, options = {}) {
  const headers = new Headers(options.headers || {});
  if (!headers.has("Content-Type") && options.body) headers.set("Content-Type", "application/json");
  if (state.token) headers.set("Authorization", `Bearer ${state.token}`);
  const response = await fetch(`${state.apiBase}${path}`, { ...options, headers });
  if (response.status === 204) return null;
  const text = await response.text();
  const payload = text ? JSON.parse(text) : null;
  if (!response.ok) {
    const message = payload?.detail || payload?.message || `${response.status} ${response.statusText}`;
    throw new Error(typeof message === "string" ? message : JSON.stringify(message));
  }
  return payload;
}

function renderProducts() {
  $("#product-grid").innerHTML = products
    .map(
      (product) => `
        <article class="product-card">
          <div class="product-icon">${product.icon}</div>
          <h3>${escapeHtml(product.title)}</h3>
          <p>${escapeHtml(product.body)}</p>
        </article>
      `,
    )
    .join("");
}

function planFeatures(plan) {
  const pitr = Number(limitOf(plan, "pitr_window_hours") || 0);
  return [
    ["Database storage", formatBytes(limitOf(plan, "database_storage_bytes"))],
    ["Object storage", formatBytes(limitOf(plan, "object_storage_bytes"))],
    ["Monthly egress", formatBytes(limitOf(plan, "egress_bytes_per_month"))],
    ["Database connections", limitOf(plan, "database_connections") ?? "configured"],
    ["Realtime connections", limitOf(plan, "realtime_connections") ?? "configured"],
    ["Projects", limitOf(plan, "max_projects") ?? "configured"],
    ["Direct DB access", formatBoolean(limitOf(plan, "direct_database_access"))],
    ["SQL console", formatBoolean(limitOf(plan, "sql_console"))],
    ["Backups", `${limitOf(plan, "backup_retention_days") ?? "configured"} days`],
    ["PITR", pitr > 0 ? `${Math.round(pitr / 24)} days` : "backup restore only"],
  ];
}

function renderPlans(source = "defaults") {
  $("#plan-source").textContent =
    source === "live"
      ? "Showing live plan limits from the control-plane API."
      : "Showing repository defaults until you sign in and load live plans.";

  $("#plan-grid").innerHTML = state.plans
    .map((plan) => {
      const featured = plan.code === "starter" ? " featured" : "";
      return `
        <article class="plan-card${featured}">
          <div class="plan-name">
            <h3>${escapeHtml(plan.name)}</h3>
            <span class="plan-badge">${escapeHtml(plan.code)}</span>
          </div>
          <p class="price"><strong>Configured externally</strong>No currency pricing is established in this repo.</p>
          <ul class="feature-list">
            ${planFeatures(plan)
              .map(
                ([label, value]) =>
                  `<li><span>${escapeHtml(label)}</span><strong>${escapeHtml(value)}</strong></li>`,
              )
              .join("")}
          </ul>
        </article>
      `;
    })
    .join("");

  const options = state.plans
    .map((plan) => `<option value="${plan.code}">${plan.name}</option>`)
    .join("");
  $("#project-plan-select").innerHTML = `<option value="">Default plan</option>${options}`;
  $("#checkout-plan-select").innerHTML = options;
}

function renderSession() {
  const box = $("#session-box");
  if (!state.token) {
    box.innerHTML = "<p>No session token stored.</p>";
    return;
  }
  const email = state.me?.email || "Session token stored";
  box.innerHTML = `
    <p><strong>${escapeHtml(email)}</strong></p>
    <button class="button secondary small" id="signout-button" type="button">Sign out</button>
  `;
  $("#signout-button").addEventListener("click", signout);
}

function renderOrgs() {
  const select = $("#org-select");
  select.innerHTML = state.orgs
    .map(
      (org) =>
        `<option value="${escapeHtml(org.org_id)}">${escapeHtml(org.name)} · ${escapeHtml(org.role)}</option>`,
    )
    .join("");
  $("#org-label").textContent = state.orgs.length
    ? `${state.orgs.length} organization${state.orgs.length === 1 ? "" : "s"} loaded.`
    : "No organizations found.";
  $("#create-project-form").classList.toggle("hidden", !state.orgs.length);
}

function renderProjects(projects) {
  const grid = $("#project-grid");
  $("#project-empty").classList.toggle("hidden", projects.length > 0);
  grid.innerHTML = projects
    .map(
      (project) => `
        <article class="project-card">
          <header>
            <div>
              <h3>${escapeHtml(project.display_name)}</h3>
              <span class="status-pill">${escapeHtml(project.status)}</span>
            </div>
            <button class="button secondary small" data-project="${escapeHtml(project.project_ref)}" type="button">Open</button>
          </header>
          <p><code>${escapeHtml(project.project_ref)}</code></p>
          <p>${escapeHtml(project.api_url)}</p>
        </article>
      `,
    )
    .join("");
  grid.querySelectorAll("[data-project]").forEach((button) => {
    button.addEventListener("click", () => openProject(button.dataset.project));
  });
}

async function loadPlans() {
  try {
    state.plans = await api("/v1/plans");
    renderPlans("live");
  } catch (error) {
    renderPlans("defaults");
    toast(`Using default plans: ${error.message}`, true);
  }
}

async function loadMeAndDashboard() {
  if (!state.token) return;
  state.me = await api("/v1/auth/me");
  state.orgs = await api("/v1/organizations");
  renderSession();
  renderOrgs();
  await loadPlans();
  await loadProjects();
}

async function loadProjects() {
  if (!state.orgs.length) {
    renderProjects([]);
    return;
  }
  const projectsByOrg = await Promise.all(
    state.orgs.map((org) => api(`/v1/organizations/${org.org_id}/projects`)),
  );
  renderProjects(projectsByOrg.flat());
}

async function signin(event) {
  event.preventDefault();
  const data = Object.fromEntries(new FormData(event.currentTarget));
  const session = await api("/v1/auth/signin", {
    method: "POST",
    body: JSON.stringify(data),
  });
  state.token = session.token;
  localStorage.setItem("maludb.sessionToken", state.token);
  toast("Signed in.");
  await loadMeAndDashboard();
}

async function signup(event) {
  event.preventDefault();
  const data = Object.fromEntries(new FormData(event.currentTarget));
  if (!data.display_name) delete data.display_name;
  await api("/v1/auth/signup", {
    method: "POST",
    body: JSON.stringify({ ...data, captcha_token: null }),
  });
  toast("Account created. Sign in with the same credentials.");
}

async function signout() {
  try {
    await api("/v1/auth/signout", { method: "POST" });
  } catch {
    // A stale token should still be removed locally.
  }
  state.token = "";
  state.me = null;
  state.orgs = [];
  state.selectedProject = null;
  localStorage.removeItem("maludb.sessionToken");
  renderSession();
  renderOrgs();
  renderProjects([]);
  $("#project-detail").classList.add("hidden");
  toast("Signed out.");
}

async function createProject(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const data = Object.fromEntries(new FormData(form));
  const body = { display_name: data.display_name };
  if (data.plan_code) body.plan_code = data.plan_code;
  const project = await api(`/v1/organizations/${data.org_id}/projects`, {
    method: "POST",
    headers: { "Idempotency-Key": crypto.randomUUID() },
    body: JSON.stringify(body),
  });
  toast(`Project ${project.project_ref} requested.`);
  form.reset();
  await loadProjects();
}

async function openProject(projectRef) {
  const project = await api(`/v1/projects/${projectRef}`);
  state.selectedProject = project;
  $("#project-detail").classList.remove("hidden");
  $("#detail-title").textContent = project.display_name;
  $("#detail-subtitle").innerHTML =
    `<code>${escapeHtml(project.project_ref)}</code> · ${escapeHtml(project.status)} · ${escapeHtml(project.api_url)}`;
  $("#usage-panel").innerHTML = "";
  $("#keys-panel").innerHTML = "";
  $("#connection-panel").textContent = "";
  $("#billing-panel").innerHTML = "";
  location.hash = "project-detail";
}

function metric(label, value) {
  return `<div class="metric"><span>${escapeHtml(label)}</span><strong>${escapeHtml(value)}</strong></div>`;
}

async function loadUsage() {
  if (!state.selectedProject) return;
  const usage = await api(`/v1/projects/${state.selectedProject.project_ref}/usage`);
  $("#usage-panel").innerHTML = [
    metric("Plan", usage.plan_code),
    metric("Database", `${usage.storage?.used_bytes ?? "unknown"} / ${formatBytes(usage.storage?.limit_bytes)}`),
    metric("Objects", `${formatBytes(usage.object_storage?.used_bytes)} / ${formatBytes(usage.object_storage?.limit_bytes)}`),
    metric("Egress", `${formatBytes(usage.egress?.used_bytes)} / ${formatBytes(usage.egress?.limit_bytes)}`),
    metric("Email", `${usage.email?.used ?? 0} / ${usage.email?.limit ?? "configured"}`),
    metric("Realtime", `${usage.realtime?.enabled ? "enabled" : "disabled"} · ${usage.realtime?.connection_limit ?? 0} max`),
    metric("API requests", `${usage.api_requests?.limit ?? "configured"} / window`),
    metric("DB connections", usage.database_connections?.limit ?? "configured"),
  ].join("");
}

async function loadKeys() {
  if (!state.selectedProject) return;
  const keys = await api(`/v1/projects/${state.selectedProject.project_ref}/api-keys`);
  $("#keys-panel").innerHTML = keys.length
    ? keys
        .map(
          (key) => `
            <div class="key-row">
              <div>
                <strong>${escapeHtml(key.name || key.key_type)}</strong>
                <p class="muted">${escapeHtml(key.key_type)} · ${escapeHtml(key.key_identifier)} · ${key.revoked_at ? "revoked" : "active"}</p>
              </div>
              <button class="button secondary small" data-revoke-key="${escapeHtml(key.id)}" type="button">Revoke</button>
            </div>
          `,
        )
        .join("")
    : "<p class=\"muted\">No keys found.</p>";
  $("#keys-panel").querySelectorAll("[data-revoke-key]").forEach((button) => {
    button.addEventListener("click", () => revokeKey(button.dataset.revokeKey));
  });
}

async function createKey(event) {
  event.preventDefault();
  if (!state.selectedProject) return;
  const data = Object.fromEntries(new FormData(event.currentTarget));
  if (!data.name) data.name = null;
  const key = await api(`/v1/projects/${state.selectedProject.project_ref}/api-keys`, {
    method: "POST",
    body: JSON.stringify(data),
  });
  $("#keys-panel").innerHTML = `
    <div class="notice">
      Key material is shown once. Copy it now:
      <pre class="code-box">${escapeHtml(key.key || "(not returned)")}</pre>
    </div>
  `;
}

async function revokeKey(keyId) {
  if (!state.selectedProject) return;
  await api(`/v1/projects/${state.selectedProject.project_ref}/api-keys/${keyId}`, {
    method: "DELETE",
  });
  toast("Key revoked.");
  await loadKeys();
}

async function loadConnection() {
  if (!state.selectedProject) return;
  try {
    const connection = await api(`/v1/projects/${state.selectedProject.project_ref}/database/connection`);
    $("#connection-panel").textContent = connection.connection_string;
  } catch (error) {
    $("#connection-panel").textContent = error.message;
  }
}

async function startCheckout(event) {
  event.preventDefault();
  if (!state.selectedProject) return;
  const data = Object.fromEntries(new FormData(event.currentTarget));
  const checkout = await api(`/v1/projects/${state.selectedProject.project_ref}/billing/checkout`, {
    method: "POST",
    body: JSON.stringify(data),
  });
  $("#billing-panel").innerHTML = `
    <p class="muted">Checkout expires at ${escapeHtml(checkout.expires_at)}</p>
    <a class="button primary" href="${escapeHtml(checkout.checkout_url)}" target="_blank" rel="noreferrer">Open checkout</a>
  `;
}

function bindEvents() {
  $("#api-base").value = state.apiBase;
  $("#settings-form").addEventListener("submit", (event) => {
    event.preventDefault();
    state.apiBase = $("#api-base").value.replace(/\/$/, "");
    localStorage.setItem("maludb.apiBase", state.apiBase);
    toast("API base saved.");
  });
  $("#signin-form").addEventListener("submit", (event) => signin(event).catch((error) => toast(error.message, true)));
  $("#signup-form").addEventListener("submit", (event) => signup(event).catch((error) => toast(error.message, true)));
  $("#reload-plans").addEventListener("click", () => loadPlans());
  $("#refresh-dashboard").addEventListener("click", () => loadMeAndDashboard().catch((error) => toast(error.message, true)));
  $("#create-project-form").addEventListener("submit", (event) => createProject(event).catch((error) => toast(error.message, true)));
  $("#close-detail").addEventListener("click", () => $("#project-detail").classList.add("hidden"));
  $("#load-usage").addEventListener("click", () => loadUsage().catch((error) => toast(error.message, true)));
  $("#load-keys").addEventListener("click", () => loadKeys().catch((error) => toast(error.message, true)));
  $("#create-key-form").addEventListener("submit", (event) => createKey(event).catch((error) => toast(error.message, true)));
  $("#load-connection").addEventListener("click", () => loadConnection().catch((error) => toast(error.message, true)));
  $("#checkout-form").addEventListener("submit", (event) => startCheckout(event).catch((error) => toast(error.message, true)));
}

function init() {
  renderProducts();
  renderPlans();
  renderSession();
  renderOrgs();
  bindEvents();
  if (state.token) {
    loadMeAndDashboard().catch((error) => toast(error.message, true));
  }
}

init();
