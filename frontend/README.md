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

Open `http://127.0.0.1:5173`. The UI calls `/api`, which `dev-server.py`
proxies to the control plane, so no CORS middleware is needed just to develop.

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
