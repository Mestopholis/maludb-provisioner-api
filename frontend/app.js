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
  createProject,
  listOrganizations,
  listPlans,
  listProjects,
  me,
  session,
  signIn,
  signOut,
  signUp,
} from "./api.js";

const PASSWORD_MIN = 12; // services/control_plane/api/auth.py: SignupIn

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

const state = {
  me: null,
  orgs: [],
  plans: [],
  projects: [],
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
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
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
  });
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

function renderSession() {
  const signedIn = Boolean(state.me);
  $("#auth-panel").hidden = signedIn;
  $("#account-panel").hidden = !signedIn;
  $("#dashboard").hidden = !signedIn;

  if (!signedIn) return;
  $("#account-email").textContent = state.me.email;
  $("#account-name").textContent = state.me.display_name || "—";
}

function renderPlans() {
  const grid = $("#plan-grid");
  if (!state.plans.length) {
    grid.innerHTML = `<p class="empty-state">Sign in to see the plans this deployment offers.</p>`;
    return;
  }
  grid.innerHTML = state.plans
    .map((plan) => {
      const limits = Object.entries(plan.limits || {})
        .slice(0, 6)
        .map(
          ([k, v]) =>
            `<li><span>${escapeHtml(k.replace(/_/g, " "))}</span><strong>${escapeHtml(v)}</strong></li>`,
        )
        .join("");
      return `
        <article class="plan-card">
          <h3>${escapeHtml(plan.name)}</h3>
          <p class="plan-code">${escapeHtml(plan.code)}</p>
          <ul class="plan-limits">${limits}</ul>
        </article>`;
    })
    .join("");
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

function renderProjects() {
  const grid = $("#project-grid");
  if (!state.projects.length) {
    grid.innerHTML = `<p class="empty-state">No projects yet. Create one above.</p>`;
    return;
  }
  grid.innerHTML = state.projects
    .map(
      (p) => `
      <article class="project-card" data-status="${escapeHtml(p.status)}">
        <header>
          <h4>${escapeHtml(p.display_name)}</h4>
          <span class="badge">${escapeHtml(p.status)}</span>
        </header>
        <p class="project-ref">${escapeHtml(p.project_ref)}</p>
        <p class="project-url"><code>${escapeHtml(p.api_url)}</code></p>
      </article>`,
    )
    .join("");
}

const PENDING = new Set(["ACTIVE", "FAILED", "DELETED"]);

async function loadDashboard() {
  state.me = await me();
  const [orgs, plans] = await Promise.all([listOrganizations(), listPlans()]);
  state.orgs = orgs;
  state.plans = plans;

  const perOrg = await Promise.all(orgs.map((o) => listProjects(o.org_id)));
  state.projects = perOrg.flat();

  renderSession();
  renderPlans();
  renderOrgs();
  renderProjects();

  const planSelect = $("#project-plan");
  planSelect.innerHTML = state.plans
    .map((p) => `<option value="${escapeHtml(p.code)}">${escapeHtml(p.name)}</option>`)
    .join("");

  // A project is created asynchronously (202) and reaches ACTIVE later, so the
  // dashboard polls while anything is still in flight rather than showing a
  // stale PROVISIONED forever.
  if (state.projects.some((p) => !PENDING.has(p.status))) {
    clearTimeout(loadDashboard.timer);
    loadDashboard.timer = setTimeout(() => loadDashboard().catch(() => {}), 4000);
  }
}

/* ------------------------------------------------------------------ *
 * Wiring
 * ------------------------------------------------------------------ */

function wire() {
  $("#api-base").value = session.base;

  submit($("#settings-form"), (data) => {
    session.base = String(data.get("apiBase") || "").trim() || "/api";
    toast(`API base set to ${session.base}.`);
  });

  submit($("#signup-form"), async (data, form) => {
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
    toast(`Creating ${project.display_name}. It will show as ACTIVE when ready.`, "success");
    await loadDashboard();
  });

  $("#signout").addEventListener("click", async () => {
    await signOut();
    state.me = null;
    state.orgs = [];
    state.projects = [];
    clearTimeout(loadDashboard.timer);
    renderSession();
    renderProjects();
    toast("Signed out.");
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
  turnstile.init();
  renderPlans();

  if (!session.token) {
    renderSession();
    return;
  }
  try {
    await loadDashboard();
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
