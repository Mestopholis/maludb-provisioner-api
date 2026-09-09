# Execution Plan: deployable topology

Status: NOT STARTED
Human owner: Joseph Lehman
Agent: Claude Code
Branch: feat/deployment-topology
Related task: tasks/DEPLOYMENT.md
Dependencies: none — Phases 01–11 are merged and `main` is green

## Objective

Make this repository deployable by somebody who did not write it, onto two
machines, without reading `docs/` to work out which process binds to which
interface.

The product is built: phases 1 to 10 are complete and Phase 11 is nearly so.
What is missing is packaging. `deploy/` ships five systemd units — PostgREST,
GoTrue, Realtime, storage, provisioner — and **none for the gateway or for
either control-plane listener**. ADR-037's own consequences say "deployment
grows a second listener, which `docs/CONTROL-PLANE.md` and `deploy/` must
describe". `docs/CONTROL-PLANE.md` describes it. `deploy/` does not. So the one
control that must not be misconfigured — which application faces the internet —
is currently carried by prose, and the failure mode is silent: an internal app
bound to a public interface serves routes whose only other protection is their
own signature.

## Where things run, and why

Two machines for launch.

| Process | Machine | Interface |
|---|---|---|
| control-plane public app | control plane | public, behind TLS |
| control-plane internal app | control plane | **private only** |
| provisioner worker | control plane | none (outbound) |
| control-plane PostgreSQL | control plane | private |
| PostgreSQL + `maludb_core` | node | private + the data address |
| gateway | node | public, behind TLS |
| PostgREST / GoTrue / Realtime / storage | node | loopback / data address |

**The gateway is node-local and cannot be moved.** It proxies to
`http://127.0.0.1:{port}` and holds a `SystemdSupervisor` for each worker
template; nothing in `services/gateway/` reads `nodes.internal_host`. A gateway
serves the tenants on its own machine and no others.

**The control plane must not be on a node.** Its database holds every node's
superuser DSN, encrypted with the KEK.

Two paths run node → control plane, and both are load-bearing: the gateway opens
the **control-plane database** directly (`db.init_pool` in
`services/gateway/main.py`), and a project's GoTrue posts to the **internal
app**'s `/internal/hooks/email/{ref}`.

## The KEK question, which this plan must answer before it writes a unit

`services/gateway/main.py` calls `crypto.KeyRing(settings.kek)` and
`key_ring.load(conn)` — the **whole** ring, on the node. It needs it: waking a
sleeping worker means decrypting that project's `jwt_signing` key and database
password (`app.py:690, 748, 760, 771, 1106`).

That contradicts the file's own docstring, which says the two processes "sit on
different sides of a trust boundary: the control plane ... holds the KEK, while
this listens to the public internet", and it contradicts the spirit of ADR-038,
which moved provisioning into a worker so the internet-facing application could
not reach a node credential.

The consequence is concrete and belongs in a decision rather than in a comment:
**a node is as trusted as the control plane**, because a node holds the key that
decrypts every project's credentials on every node — not just its own tenants'.
Separating the two machines protects the control-plane *database* and does not
protect the KEK.

This plan does not silently ship that. Step 1 is an ADR that either accepts it
with the blast radius written down, or narrows it. Writing systemd units that
place KEK material on a public-facing machine, without a decision saying that is
intended, is exactly the "undocumented architectural change to simplify an
implementation" `AGENTS.md` forbids.

## Scope

- An ADR resolving the KEK-on-the-node question.
- systemd units for the three unpackaged processes: control-plane public app,
  control-plane internal app, gateway.
- `docs/DEPLOYMENT.md`: a runbook that takes two fresh machines to a project
  serving traffic.
- A preflight command that refuses a deployment which has the interfaces wrong.
- `deploy/` env-file templates matching the `EnvironmentFile=` convention the
  existing five units already use (`/etc/maludb/*.env`).

## Non-goals

- **Multi-node.** A second node needs hostname-level routing to the right node's
  gateway, and nothing implements that. Out of scope, and named in the runbook
  as the thing to build before a second node is bought.
- Splitting the selling page from the console. One artifact serves both today.
- TLS certificate automation. The runbook says what is needed; issuing it is the
  operator's.
- Prices. `specs/plans-and-limits.yaml` holds limits and deliberately no
  currency. A business decision, not a deployment one.
- Any change to how the gateway resolves upstreams.

## Preconditions

- `main` green. (It is, as of `b7c2bfb`.)
- A wildcard DNS record and certificate for the gateway domain — ADR-008 makes
  the hostname the routing key, so `*.example.com` must reach the gateway.

## Implementation steps

1. **ADR: what a node is trusted with.** ✅ Written as **ADR-072
   (Proposed)** — needs the owner's acceptance before step 2 begins.

   The finding is worse than this plan assumed. It is not only that a node can
   decrypt project credentials: the gateway holds the *control plane's own*
   database credentials (`settings.database_url` is a single field) plus the
   KEK, and `nodes.admin_dsn()` needs nothing else — the `node_id` is readable
   and the AAD is derived from it. **A compromised gateway on any node yields
   the PostgreSQL superuser DSN of every node in the fleet.**

   ADR-038 exists to prevent exactly this and is enforced by an import-graph
   test — but that test walks the *control plane's* public routers, and the
   gateway is a second internet-facing application built later.

   ADR-072 proposes keeping the KEK on the node and removing the fleet from its
   reach instead: a dedicated database role for the gateway with no access to
   `nodes.admin_ciphertext` and visibility limited to its own node's projects.
   Steps 2 and 3 below depend on that role existing.

2. **`deploy/maludb-control-plane-public.service` and
   `-internal.service`.** Two units, `EnvironmentFile=/etc/maludb/control-plane.env`,
   differing in factory and bind address. The internal one binds a private
   address explicitly — never `0.0.0.0` — and the unit carries a comment saying
   why, because that line is the whole control. Both `After=postgresql.service`.

3. **`deploy/maludb-gateway.service`.** `uvicorn --factory
   services.gateway.main:build`. Runs as a user that may `systemctl start` the
   worker templates and no more. Document that this machine holds KEK material,
   pointing at the ADR from step 1.

4. **`deploy/*.env.example` for each.** Every variable the process reads, with
   the ones that are easy to get wrong called out: `MALUDB_GATEWAY_DOMAIN`
   (defaults to `maludb.local`, which routes nothing in production),
   `MALUDB_CAPTCHA_REQUIRED` (defaults on in production — a frontend without a
   Turnstile site key then refuses every signup), and both key-material refs,
   which the loader rejects if group- or world-readable.

5. **`cp-manage deploy preflight`.** Refuses a misconfiguration rather than
   documenting it. Checks, each of which is a real failure mode already
   observed in this repo:
   - the internal app is not bound to a public address;
   - `plans sync` has run, or creating a project answers 503;
   - the gateway domain is not the `maludb.local` default;
   - a node is registered, healthy, and `backup-check` has passed;
   - key material is mode 0600;
   - if Stripe is configured at all, every offered plan has a price mapping —
     otherwise checkout answers 409 naming it.

6. **`docs/DEPLOYMENT.md`.** Two machines to a serving project, in order:
   control-plane database and migrations, `plans sync`, key material, the three
   units, node build (`maludb_core`, `wal2json`, `pg_hba`), `node register`,
   `realtime-check`, `backup-check`, health, DNS and TLS, the frontend's API
   base and Turnstile key, `deploy preflight`, then a real signup and project
   creation as the acceptance test.

7. **Frontend deployment note.** Where the static files go and that they must
   point at the **public** app. `dev-server.py` is development-only and says so.

## Verification

- [ ] `cp-manage deploy preflight` fails a deliberately misconfigured
      deployment — internal app on `0.0.0.0`, unsynced plans, default gateway
      domain, unmapped Stripe price — and passes a correct one. Tested, not
      asserted.
- [ ] A test parses each new unit file and asserts the internal one binds a
      non-public address, so a future edit that "simplifies" it fails the suite.
      This is the ADR-037 control, and prose has already failed to hold it once.
- [ ] The runbook is executed end to end on two fresh VMs by someone following
      only the document, ending in a signup through the real frontend and a
      project reaching ACTIVE.
- [ ] `ruff`, full suite, OpenAPI drift, migrations idempotent.
- [ ] Security review recorded as a commit trailer.

## Risks

- **A unit file is not a security control by itself.** A bind address in a unit
  is defeated by a firewall rule, a reverse proxy, or a container publishing a
  port. The runbook states the property — the internal app is unreachable from
  the internet — and gives the check that proves it from outside the machine,
  rather than implying the unit is sufficient.

- **The KEK decision reopened work, as expected, and step 1 is now the
  critical path.** ADR-072 proposes a dedicated gateway database role; the
  gateway unit cannot be written until it exists, because the unit's
  `EnvironmentFile` is where its DSN is set and pointing it at the control
  plane's DSN silently restores the hazard. Units wait on the ADR being
  accepted.

- **A correct preflight can still be defeated by a wrong DSN.** ADR-072's
  narrowing lives in database grants, not in the gateway's code, so a gateway
  configured with the control-plane DSN works perfectly and is fully exposed.
  The preflight must check *which role* the gateway connects as, not that the
  gateway functions.

- **Single node is a single point of failure.** Phase 11 gives per-tenant
  restore and a tested backup path; it does not give failover. The runbook
  should say plainly what an outage looks like, so it is a known position rather
  than a surprise on the first bad afternoon.

- **The preflight can only check what it can see.** It runs on the control
  plane; it cannot prove the gateway's certificate or that DNS resolves from the
  internet. Say so in its output rather than letting a green preflight read as
  "deployment is correct".

## Decision log

- 2026-09-09 — Two machines, not one: the control-plane database holds every
  node's superuser DSN.
- 2026-09-09 — Gateway on the node, because it proxies to loopback and drives
  systemd. Not a deployment preference; a property of the code.
- 2026-09-09 — Multi-node is out of scope. The gateway never reads another
  node's address, so a second node needs a router in front that does not exist.
- 2026-09-09 — The KEK-on-the-node contradiction is resolved by ADR **before**
  any unit is written, not documented afterwards.

## Progress log

- 2026-09-09 — Plan written.
- 2026-09-09 — Step 1 done: ADR-072 written and **Proposed**. It found that the
  exposure is fleet-wide superuser rather than per-project credentials, and that
  ADR-038's enforcement test does not cover the gateway. Steps 2 onward are
  blocked on the owner accepting ADR-072, because the gateway's database role is
  an input to its unit file.
