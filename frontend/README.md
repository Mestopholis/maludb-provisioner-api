# MaluDB Frontend

This is a dependency-free static frontend for the control-plane API. It is
intentionally not React and not PHP: the backend already exposes JSON routes,
and this UI does not need a build step or a second server-side runtime.

## Run locally

From the repository root:

```bash
cd frontend
python dev-server.py --api http://127.0.0.1:8112
```

Open `http://127.0.0.1:5173`.

The UI defaults to `/api`, and `dev-server.py` proxies that path to the public
control plane app. This avoids requiring CORS middleware just to run the local
frontend.

Start the public control plane separately:

```bash
.venv/bin/uvicorn --factory services.control_plane.main:create_public_app --port 8112
```

For local development, the internal app on port `8111` also works:

```bash
python dev-server.py --api http://127.0.0.1:8111
```

## What it covers

- Landing/product positioning for the surfaces already present in this repo.
- Plan comparison cards, using live `/v1/plans` after sign-in and repository
  defaults before sign-in.
- Signup, signin, signout and session persistence.
- Organization project listing and project creation.
- Project usage, API key list/create/revoke, direct DB connection display, and
  billing checkout start.

## Product caveat

The repo defines three plan tiers in `specs/plans-and-limits.yaml`: `free`,
`starter`, and `production`. It does **not** establish public currency pricing,
so the UI displays limits and says pricing is configured externally.
