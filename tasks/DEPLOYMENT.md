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

- [x] An ADR records what a MaluDB node is trusted with — **ADR-072**, written
      and *Proposed*, awaiting the owner's acceptance.
- [x] **ADR-072 accepted, and its narrowing implemented.** `cp-manage gateway grant --role <name>` applies the model,
      `gateway.main.assert_narrowed()` refuses to start a production gateway
      whose role can still read `nodes.admin_ciphertext`, and
      `tests/test_gateway_grants.py` proves both against a real role.
- [ ] **Per-node row narrowing** — the second half of ADR-072's intent, not
      done. The gateway can still read *other* nodes' project rows, so with the
      KEK it can decrypt their project credentials; what it can no longer do is
      recover a node's superuser DSN, which was the fleet-wide part. Closing
      this needs the gateway to know which node it is (it has no node identity
      today) and row-level policies on `projects` that do not disturb the
      control plane's own access — a slice of its own.
- [x] `deploy/` carries a unit for the control-plane public app, the
      control-plane internal app, and the gateway, plus
      `control-plane.env.example` and `gateway.env.example`, following the
      `EnvironmentFile=/etc/maludb/*.env` convention the existing units use.
      All three pass `systemd-analyze verify`.
- [x] A test parses the unit files and fails if the internal app's bind address
      is public — `tests/test_deploy_units.py`, 16 assertions. It also catches
      the likelier mistake: the two control-plane units differ only in a factory
      name and a bind address, so a copy-paste that lost either produces a
      service that starts, serves, and is wrong.
- [ ] `cp-manage deploy preflight` refuses a deployment with the internal app on
      a public address, unsynced plans, the default `maludb.local` gateway
      domain, no healthy registered node, key material readable by group or
      world, or a Stripe-configured deployment with an unmapped plan price.
- [x] `docs/DEPLOYMENT.md` takes two fresh machines to a customer signing up
      through the real frontend and a project reaching ACTIVE.
- [ ] **The runbook has been followed end to end by someone reading only that
      document.** Nothing in CI can establish this, and it is the criterion that
      actually matters: everything above is verified, and none of it proves the
      document is followable.
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
