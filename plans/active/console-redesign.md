# Execution Plan: Console redesign

Status: IN PROGRESS  
Human owner: Joseph Lehman  
Agent: Claude Code  
Branch: feat/console-redesign  
Related task: tasks/PHASE-07-DASHBOARD.md  
Dependencies: none — frontend only, over routes the public control plane already serves

## Objective

Make the customer frontend usable as an application and give it one clean visual
language, taking the layout conventions of an admin-dashboard template (Duralux,
TemplateMonster item 497496) as inspiration: a fixed sidebar, a top bar with the page
title and breadcrumb, white cards on a light grey page, stat cards with progress bars,
and data tables.

Inspiration only. No markup, CSS, script, font or image is taken from the template: it
is licensed, and it is jQuery/Bootstrap, which would end the frontend's no-dependency,
no-build-step property (`frontend/README.md`). The look is rebuilt in `styles.css`.

## Scope

- A design system in `styles.css`: colour, type, spacing and radius tokens; light theme by
  default, dark under `prefers-color-scheme` and a toggle kept per browser.
- The signed-in console as an application shell: sidebar navigation, top bar with the
  account, a project list as a table, and every project feature on a page of its own:
  - `#/projects/<ref>` — overview: status, API URL, usage stat cards, plan;
  - `#/projects/<ref>/sql`, `/tables` — unchanged behaviour, new frame;
  - `#/projects/<ref>/keys`, `/usage`, `/memory` — the panels that were toggles inside
    project cards;
  - `#/organization`, `#/tokens`, `#/invite/<token>` — unchanged behaviour, new frame.
- The public pages restyled to match: landing, plans, sign-in card.
- Narrow screens: the sidebar becomes an off-canvas menu; no horizontal page scroll.

## Non-goals

- **An operator/admin web console.** Operators use `cp-manage`. A web console needs the
  staff access model `docs/ACCOUNTS.md` describes (explicit, time-bounded, audited) and an
  ADR; recorded in `docs/OPEN-QUESTIONS.md`.
- New API routes or changed API behaviour.
- A framework, bundler, web font download or any third-party script.
- MFA, password reset UI, or anything else Phase 07 deferred.

## Preconditions

- The properties the frontend tests hold stay held: escaping of every interpolation,
  secrets held only in state while on screen and dropped on leaving, confirmations before
  irreversible actions, routes the public app serves, and the public pricing projection.

## Implementation steps

1. Rewrite `styles.css` around tokens; keep the `[hidden]` rule.
2. Restructure `index.html`: sidebar inside `#console`, header outside it carrying the
   account (the tests' layout rules still hold).
3. `app.js`: route table for project pages; panels render into the project page instead of
   card toggles; leaving the keys page drops a key on screen (was: closing the panel).
4. Project list as a table with status pills and stat cards; create-project as a card
   opened from the top bar.
5. Landing, plans and sign-in restyled; emoji icons replaced by inline SVG.
6. Update the frontend tests where a toggle became a page, keeping each property.
7. Verify in headless Chromium at desktop and phone widths, light and dark, against the
   dev control plane.

## Verification

- [x] `pytest tests/test_frontend_*.py tests/test_public_pricing.py`
- [x] `ruff check .`
- [x] Chromium walk-through: sign in, project list, overview, SQL, tables, keys (create and
      dismiss a secret), usage, memory, organization, tokens, sign out — no page errors
- [x] 390 px width: no horizontal scroll, menu opens and closes
- [x] `frontend/README.md` updated
- [x] Security review recorded as a commit trailer (none)

## Risks

- **Structure-reading tests** hold security properties by locating sections of `app.js`.
  Moving code can make a test pass vacuously; each changed assertion is rewritten against
  the new location, never deleted.
- **A secret key shown on a page** must still leave when the person navigates away, now
  that there is no panel to close.

## Decision log

- 2026-09-16 — Customer console before an admin console (owner's choice).
- 2026-09-16 — Stay dependency-free; rebuild the template's look in our own CSS (owner's choice).
- 2026-09-16 — System font stack led by Inter where installed; no font download, so a
  visitor's address is not sent to a font host.

## Progress log

- 2026-09-16 — Plan written; current console and template demo screenshotted for reference.
- 2026-09-16 — Built: `styles.css` rewritten on tokens (light/dark), sidebar shell, project list
  table with stat cards, project pages (overview, SQL, tables, keys, usage, memory), account pages
  in cards, landing and sign-in restyled with an inline SVG sprite. Tests: keys test now holds
  "leaving the page drops the secret"; SQL test drops the router it no longer contains; new
  `tests/test_frontend_project_pages.py`, whose escaping check walks nested templates (the older
  panel tests reach one level; audited by hand, nothing unescaped found there).
- 2026-09-16 — Verified in headless Chromium against the dev control plane: every page at 1440 px
  light and dark and at 390 px, no page errors, no horizontal overflow, menu opens and closes; a
  secret key created on the keys page is gone after visiting another page (key revoked after);
  unknown project address falls back to the list; sign-out clears the project view.
- 2026-09-16 — Operator web console recorded in `docs/OPEN-QUESTIONS.md`.
