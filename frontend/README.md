# MaluDB Frontend

A dependency-free static frontend for the control plane. No build step, no
second server-side runtime: the backend already exposes JSON, and the page is
three files plus a development proxy.

The priority is the **signup funnel** — a visitor becomes an account with a
personal organisation and can create a project — because that is what has to
work before anything can be sold.

## Run locally

Start the public control plane:

```bash
.venv/bin/uvicorn --factory services.control_plane.main:create_public_app --port 8112
```

Then the frontend:

```bash
cd frontend
python dev-server.py --api http://127.0.0.1:8112
```

Open `http://127.0.0.1:5173`. The UI calls `/api` and that is not configurable in the page -- `dev-server.py`
proxies it to whatever `--api` names, and in production a reverse proxy does the
same. The control plane ships no CORS middleware, so the page and the API must
share an origin either way; a settings field offering to point the page
somewhere else was removed because it could only ever produce a broken page.

The internal app on `8111` also works locally. It must never be the target of
a deployed frontend — see ADR-037.

## Files

| File | What it is |
|---|---|
| `index.html` | Markup. Also carries the Turnstile site key, as one editable line |
| `api.js` | Everything that talks to the control plane, and all error shaping |
| `app.js` | Rendering and form wiring |
| `styles.css` | Styles |
| `dev-server.py` | Static files + `/api` proxy, development only |

`api.js` is separate from `app.js` because the bugs were in that seam: the
previous single file mixed transport with rendering, and a failed request had
nowhere to go.

## Signups are closed until launch

`index.html` carries a second one-line switch beside the Turnstile key:

```html
<script>window.MALUDB_SIGNUPS_OPEN = false;</script>
```

While it is `false` the create-account form is replaced by a "Coming soon"
panel, the tab is relabelled, and no Turnstile script loads. **Sign-in stays
open**, so an operator can still reach their own account.

Closed rather than open-and-broken on purpose: signup itself works today, and
the customer's *next* action -- creating a project -- answers `503`, because
placement has no registered node to put it on. An honest "not yet" beats a
working form and a dead end.

Flip it to `true` when a node is registered and the deployment is real.

## Pricing on the public page

The plan cards a signed-out visitor sees come from `PUBLIC_PLANS` in `app.js`,
**not** from `/v1/plans`. ADR-037 keeps that endpoint authenticated because it
returns `plans.config_json.limits` verbatim -- `work_mem_mb`,
`temp_file_limit_mb`, `postgrest_pool_size`, statement and lock timeouts -- and
publishing those tells anyone designing a workload exactly where every threshold
sits. The ADR says the public view should be "a curated projection with prices
in it", which is what `PUBLIC_PLANS` is.

Every entry carries the entitlement `key` and `value` it publishes, and
`tests/test_public_pricing.py` proves the page matches `entitlements.DEFAULTS`.
So this is no longer the one place nothing checks -- change a limit in the
catalogue without changing the page and the suite fails, naming both numbers.

That test exists because the first version of this copy advertised **1 GB of
database storage on Free**. That is the *object* storage limit; the database
limit is 500 MB. A wrong number on the page a customer buys from.

What is published is what a customer needs in order to choose: storage, egress,
projects, connections, recovery window, Realtime and email volume. What is
withheld is what ADR-037 names -- `work_mem_mb`, `temp_file_limit_mb`,
`postgrest_pool_size` and the statement, lock and idle-transaction timeouts --
and a test asserts none of them appear.

Prices are `—` because the repository establishes none.

Once signed in, the live `/v1/plans` limits replace those cards, because by then
the reader is a customer deciding whether to upgrade rather than a stranger.

## The captcha

`POST /v1/auth/signup` requires a challenge token whenever the control plane
sets `MALUDB_CAPTCHA_REQUIRED`, **which defaults to on when
`MALUDB_ENV=production`**. Set the matching Cloudflare Turnstile site key in
`index.html`:

```html
<script>window.MALUDB_TURNSTILE_SITE_KEY = "0x4AAA...";</script>
```

Leave it empty in development, where the requirement is off and no third-party
script is loaded at all. Deploying with it empty against a production control
plane means every signup is refused — which is the failure this frontend
previously shipped with, because it hard-coded `captcha_token: null`.

The control plane fails **closed** when the challenge service is unreachable
(`captcha.py`), so an outage at Cloudflare stops signups rather than admitting
unverified ones. `MALUDB_CAPTCHA_FAIL_OPEN=1` inverts that, deliberately.

## What it covers

- Landing and plan comparison; limits come from live `/v1/plans` once signed in.
- **Signup, which signs you in.** `/v1/auth/signup` returns the user and no
  token, so the client chains `/v1/auth/signin` rather than asking a new
  customer to retype credentials they just chose.
- Sign in, sign out, session persisted in `localStorage`.
- Organisations and projects; project creation is offered only to an owner or
  admin, because the route refuses anyone else.
- Projects are created asynchronously (`202`), so the list polls until nothing
  is mid-flight.
- **Memory spaces** (ADR-079), per active project, beside plan and usage:
  - everyone in the organization sees the spaces, their models and the plan's memory limits,
    and which provider keys are set (by their last four characters only);
  - owners and admins can also create a space, name its models, delete it (typing the name back
    to confirm) and set or remove a provider key;
  - a key goes in through a password field that is cleared before the request is sent, and
    nothing on the page can show it again;
  - the panel follows a space being built or deleted until it settles.

  Storing and searching memories is not here. They run from the customer's server with the
  project's secret key, which a browser session should never hold.
  `tests/test_frontend_memory_panel.py` checks the panel's routes, key handling, delete
  confirmation and escaping against the files.

## What it does not cover yet

API keys, usage, billing checkout and the direct-connection panel. Those routes
exist on the public app; this rewrite deliberately stopped at the funnel rather
than carrying forward panels that were never exercised.

## Errors

Every request failure is shown, which is the substance of this rewrite rather
than a nicety:

| Case | What the page shows |
|---|---|
| `422` validation | The message next to the field that caused it. FastAPI's `detail` is a *list of objects*, not a string |
| `401` / `409` | The control plane's own sentence |
| `429` rate limit | The wait, from `Retry-After` |
| Control plane down | Which base URL failed, rather than "Failed to fetch" |
| Expired stored token | Cleared, with the signed-out view restored |

Password length is checked before the request, since the API's minimum is 12
and a round trip to learn that is a round trip wasted.

## Deploying it

The built artefact is the four static files; `dev-server.py` is not for
production. Serve them from any static host or nginx, and point the page at the
**public** control-plane listener. Set the Turnstile key first.

## Plan and usage

Each ACTIVE project has a **Plan & usage** panel: the plan in force, the billing
period and any failed-payment grace (ADR-051 wording: the earliest a restriction
can arrive), database, file and egress usage against the plan's ceilings with
their `ok` / `warning` / `restricted` / `exceeded` state in words, and the limits
that are enforced but not metered.

Owners and admins get a button per larger plan. It opens Stripe Checkout, and the
page only follows a `https://*.stripe.com` link. Stripe returns the customer to
`/?checkout=complete&project=<ref>` -- a query on the root, because this site is
static files with no routing -- and the page reopens that project's panel. On a
deployment without billing the same button files an upgrade request instead.

No amount is rendered from the API (ADR-052); the only prices are the published
list in `PUBLIC_PLANS`. `tests/test_frontend_contract.py` fails if `api.js`
requests a path the public control plane does not serve.
