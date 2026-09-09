# Deployment

Not a phase. The phases build the product; this makes it installable.

Plan: `plans/active/deployment-topology.md`

## Why this exists as its own task

Phases 01–10 are complete and Phase 11 is nearly so, and none of it can be run
by somebody who did not write it. `deploy/` carries units for the five
per-project and per-node processes and none for the three that face a network:
the control-plane public app, the control-plane internal app, and the gateway.

ADR-037 splits the control plane in two and says in its consequences that
`deploy/` must describe the second listener. It does not. The property that
matters — the internal application is not reachable from the internet — is
therefore held by prose in `docs/CONTROL-PLANE.md` and by whoever is typing.
`AGENTS.md` already records what happens to controls held that way: twice a
security review was carried by a checklist, and twice it was skipped.

## Acceptance criteria

- [ ] An ADR records what a MaluDB node is trusted with. The gateway loads the
      whole key ring on the node (`services/gateway/main.py`), which contradicts
      that file's own docstring and means a node can decrypt every project's
      credentials on every node. Accepted with the blast radius written down, or
      narrowed — but decided before units are written.
- [ ] `deploy/` carries a unit for the control-plane public app, the
      control-plane internal app, and the gateway, each with an `.env.example`,
      following the `EnvironmentFile=/etc/maludb/*.env` convention the existing
      units use.
- [ ] A test parses the unit files and fails if the internal app's bind address
      is public. The ADR-037 control is asserted, not documented.
- [ ] `cp-manage deploy preflight` refuses a deployment with the internal app on
      a public address, unsynced plans, the default `maludb.local` gateway
      domain, no healthy registered node, key material readable by group or
      world, or a Stripe-configured deployment with an unmapped plan price.
- [ ] `docs/DEPLOYMENT.md` takes two fresh machines to a customer signing up
      through the real frontend and a project reaching ACTIVE, and has been
      followed end to end by someone reading only that document.
- [ ] The runbook states plainly that this is a single-node topology, that a
      second node needs hostname routing which does not exist, and what an
      outage of the one node looks like.
- [ ] Security review recorded as a commit trailer.

## Explicitly not in scope

- **Multi-node.** The gateway proxies only to `127.0.0.1` and never reads
  `nodes.internal_host`, so a second node needs a router in front of both
  gateways. That is a build, not a configuration, and it is the thing to do
  before a second node is bought.
- Splitting the selling page from the console — one artifact serves both.
- TLS issuance, and prices. `specs/plans-and-limits.yaml` holds limits and no
  currency, deliberately.

## What is already true and does not need building

Worth stating so it is not rebuilt: billing works and is inert until configured
(`cp-manage billing status` reports whether the deployment can take money);
`plans sync` seeds the catalogue; `node register`, `realtime-check` and
`backup-check` exist; the frontend is static and needs only an API base and a
Turnstile site key. The gap is packaging and a runbook, not features.
