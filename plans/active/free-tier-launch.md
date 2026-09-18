# Execution Plan: Free tier live on test.maludb.org

Status: IN PROGRESS — slices 1–6 done on the rehearsal VMs (2026-09-17); 7–10 remain  
Human owner: Joseph Lehman  
Agent: Claude Code  
Branch: one per slice, `free/<slice>`  
Related task: `tasks/DEPLOYMENT.md`; `plans/active/launch.md` (this plan launches its Developer tier alone)  
Dependencies: ADR-083 (accepted, PR #203); `plans/active/deployment-rehearsal.md` (the VMs this promotes)

**Slices are "free slice N"; human-owned steps are "free step H-N".**

## Objective

Open public signups for the **Developer (free) tier** on the rehearsal deployment
(control plane 10.120.0.173, node 10.120.0.172, `https://test.maludb.org`, projects at
`https://<ref>.test.maludb.org`) as a public beta, with every feature the free tier is sold on
working end to end through the official client on launch day:

- Data API, API keys, SQL editor and table browser (built; exercised on the rehearsal);
- **Auth** (GoTrue), including confirmation and password-reset email;
- **Storage** (files);
- **Memory spaces** (ADR-079), with the customer's own model provider keys;
- **Vector search and the schema graph** (maludb_core in tenant databases).

Decided 2026-09-17 by the owner: build ADR-083 first; promote the rehearsal VMs at
test.maludb.org rather than build new ones or move domains; all four feature groups above at
launch; the platform's MaluMail key exists and will be placed on the host by the owner.

## Scope

- Everything that must be true of the free tier for a stranger to sign up, build on it, and
  not be able to harm other tenants or the platform's costs.
- Paid plans remain in the catalogue but are **not sold**: no Stripe, and the public page says
  paid tiers are coming rather than offering them.

## Non-goals

- Billing, Stripe and paid-plan copy (`plans/active/launch.md` slices 1–2, H-4).
- Realtime (not in the free plan: `realtime_connections: 0`).
- Multi-node and moving off the rehearsal VMs; a production domain.
- Operator console write actions.

## Preconditions

- `main` green; ADR-083 merged.
- The rehearsal's open findings are either fixed below or explicitly accepted below.

## Implementation steps

### Free slice 1 — The maintenance pass runs (ADR-083)

Split pass as decided: `run_all` takes the passes to run; a node command runs only the sleep
pass as `maludb-gateway` with the gateway role, idle queries filtered by node; migration for
`node_maintenance_runs` with the own-node policy; `maintenance_runs` added to
`gateway_grants.UNREACHABLE_TABLES`; units and timers for both hosts; preflight
"node maintenance". Install on both VMs, re-run `gateway grant`. **Why first:** without it a free
project is never measured, never restricted at its storage ceiling, and its workers never
sleep (ADR-022).

### Free slice 2 — Email (MaluMail)

`MALUMAIL_API` on the control plane (owner places the key, H-1); platform sender settings;
verify on the rehearsal: platform password reset delivers, and a free project's Auth hook sends
signup confirmation and recovery mail through `platform_default`. Preflight gains an email check
if none exists (a free tier whose password reset silently sends nothing is not launchable).

### Free slice 3 — Auth on the node

GoTrue workers woken by the gateway for a free project (the unit and binary exist on the node;
never exercised on a real signup). Verify with `@supabase/supabase-js` against
`<ref>.test.maludb.org`: sign up, confirm by email, sign in, RLS by `auth.uid()`, recover.
Fix whatever the runbook lacks, as the rehearsal did for the Data API.

### Free slice 4 — Storage on the node

The object store (the pinned SeaweedFS), the shared storage worker (`storage-api` image,
pinned), node storage secret, the data address and `pg_hba` line the test cluster script
builds, and a `docs/DEPLOYMENT.md` section for all of it (there is none today). Verify with the
official client: create a bucket, upload, download, RLS on `storage.objects`, and a second
project cannot reach the first's objects. Object-storage measurement and egress ceilings then
run in the slice-1 pass.

### Free slice 5 — Memory spaces on the node

Memory worker, query embedder and egress proxy (DEPLOYMENT §1.6), the `cp_memory_worker` and
`cp_memory_embedder` roles (preflight warns today), ingest and search end to end with the owner's
Anthropic and Voyage keys set as a customer would set them.

### Free slice 6 — Vector search and the schema graph

Pins and `node extension-check` on the rehearsal node, `extension grants` (ADR-076); a free
project enables vectors and the data-model graph and both answer through the Data API.

### Free slice 7 — Backups (ADR-086)

Found surveying the VMs: `cp-manage node backup` runs pgBackRest locally and writes the control-plane
database, which no host of the two-machine deployment can do; every repository rule reads `repo1-*`
only; `archive_mode` is off on the node; nothing backs up the control plane or copies SeaweedFS.

- **7a — Recording role.** Migration: `record_node_backup`, `record_node_backup_check`,
  `nodes.backup_recorder_role`; `cp-manage node backup-recorder grant`; refusal of overlapping roles;
  import-graph and grant tests.
- **7b — Node runner.** `python -m services.control_plane.node_backup {backup,check}` as `postgres`,
  recording through 7a; `maludb-node-backup.{service,timer}`; readiness joins the node's repository
  report; finding 21 in preflight.
- **7c — Two repositories.** Readiness, retention and ADR-064 per `repoN`; SFTP (other site) and R2
  config in the runbook; `archive_mode = on` (one node restart, scheduled with the owner).
- **7d — Control plane and objects.** Nightly `control-plane backup` shipped encrypted to both
  destinations; nightly `rclone sync` of the platform bucket to R2; preflight on dump age.
- **7e — Verify.** Point-in-time restore of one free project from each repository (restore run on the
  node, attended); control-plane restore passing `control-plane verify --reach-nodes`; a customer file
  recovered from the R2 copy.

### Free slice 8 — Abuse controls see real clients

Finding 19: behind Nginx Proxy Manager every request reaches the public app from 127.0.0.1, so
signup and sign-in limits see one client. Decide and document the trusted-proxy topology
(`mod_remoteip` or a trusted hop count) and verify limits by address. Turnstile keys (H-2) so
`captcha_required` passes preflight. The abuse report has a named reviewer (H-5).

### Free slice 9 — The public site says what is true

Pricing shows Developer as available and paid tiers as coming (no price that cannot be bought);
Turnstile site key in `index.html`; links to terms, privacy and acceptable use (H-4) and a
support address (H-6); docs page claims checked against what slices 3–6 verified;
`MALUDB_SIGNUPS_OPEN = true` is the **last** change, after slice 10.

### Free slice 10 — Rehearsal leftovers and the launch check

Finding 11 (first data key race) fixed or accepted; the previous install's services
(`maludb-api`, `maludb-mc2dbd`, `maludb-modeld`) removed from both VMs and the leftover
`10.120.0.250` `pg_hba` lines removed or explained; `cp-manage deploy preflight` exits 0 (or 2 with
advisories accepted here); `docs/DEPLOYMENT.md` §5 completed; a stranger's signup through the real
site reaches an ACTIVE project and every feature above works from the official client.

## Human-owned steps

| Step | What | Needed by |
|---|---|---|
| H-1 ✅ | Place the MaluMail platform API key on 10.120.0.173 (a root-600 file; never in chat) and name the sending address/domain | slice 2 |
| H-2 ✅ | Cloudflare Turnstile site key and secret for test.maludb.org | slices 8–9 |
| H-3 | **Off-host targets deferred 2026-09-17 by the owner: local backups for now (ADR-087).** Originally decided: a VM on the owner's second Proxmox server (another site) and Cloudflare R2 free tier.** Still to do: the VM reachable from 10.120.0.172 over SSH; an R2 bucket for backups and one for objects, each with a token scoped to it; the KEK and staff key copied off both hosts to a store holding neither backup credential | slice 7 |
| H-4 ◐ | Terms of service, privacy policy, acceptable-use policy — **drafted 2026-09-17** as `frontend/{terms,privacy,acceptable-use}.html`, written from what the platform does. **The owner must fill every [PLACEHOLDER] and have a lawyer read them before signups open**: legal entity, address, jurisdiction, retention periods, liability cap, support/security/abuse addresses | slice 9 |
| H-5 ✅ | Who reviews the abuse report and how often — **the owner, weekly** (2026-09-17); on the console page and in DEPLOYMENT §5 | slice 8 |
| H-6 | Support address and where incidents are announced; the single-node position stated | slice 9 |

## Verification

- [ ] Preflight on the rehearsal exits 0, or 2 with each advisory accepted in the decision log
- [ ] Official-client walkthrough on a fresh free project: Data API, Auth (with real email), Storage,
      memory ingest and search, vector query
- [ ] A free project over its database ceiling is restricted by the pass; an idle project's workers sleep
- [ ] A second project cannot reach the first's rows or objects
- [ ] Backups: a restore of one free project to a point in time, off-site repository
- [ ] Rate limits count by client address through the real proxy
- [ ] Security review recorded on every code slice

## Risks

- **One node.** Free signups fill it; capacity is watched in the operator console, and signups close
  (`MALUDB_SIGNUPS_OPEN`) before placement refuses.
- **Storage and Auth were never exercised on these VMs**, so slices 3–4 may surface runbook gaps of
  the rehearsal's kind; each becomes a finding and a fix, not a workaround.
- **Cost of memory spaces** is the customer's provider bill, not ours; the egress proxy is what keeps the
  memory worker from reaching anything else.

## Decision log

- 2026-09-17 — Owner: ADR-083 first; rehearsal VMs at test.maludb.org; Auth, Storage, memory spaces,
  vector search and schema graph at launch; MaluMail key available.
- 2026-09-17 — Owner: Auth enabled for every project (ADR-084). The object store's S3 port listens on the
  node's private address behind its own firewall, so the control plane can measure and delete objects
  (ADR-085).
- 2026-09-17 — Owner: backups go to a VM on the second Proxmox server, at another site, and to
  Cloudflare R2's free tier, both; ADR-086 accepted.
- 2026-09-17 — Owner: the node's PostgreSQL may restart now. Archiving went on with an **interim local
  repository** on the node, because `archive_mode = on` needs a working destination at once and the
  off-host repositories wait on H-3. It is a stated, temporary deviation from ADR-086 decision 3;
  `backup-check` fails it as co-located, and it is replaced, not kept, when H-3 is done.
- 2026-09-17 — Owner: **use local backups for now; skip the second-site VM and R2.** H-3's off-host
  targets are deferred. ADR-087 (accepted) records a time-boxed, per-node acceptance of the local
  repository so preflight reports it rather than failing forever; 7d and 7e continue locally.

## Progress log

- 2026-09-17 — Plan written from the owner's decisions and a survey of both VMs: no maintenance timers, no
  email configured, no storage worker or object store, GoTrue installed but unexercised, pgBackRest
  installed but `archive_mode` off, captcha secret absent, previous install's units still present.
- 2026-09-17 — **Slice 1** (#205): maintenance split deployed; both timers run; wake on request verified.
- 2026-09-17 — **Slice 3a** (#207, ADR-084): Auth on for every project.
- 2026-09-17 — **Slice 4** (#208, ADR-085): SeaweedFS unit, data address, object-store firewall,
  `cp-manage node storage-prepare`; the storage unit had never started a container (`ProtectHome` hid
  `/run/user`). Official client: bucket, upload, download, list, signed URL, anon refused.
- 2026-09-17 — **Slice 2** (#206, #209): MaluMail hook. GoTrue refuses a plain-HTTP hook off loopback, so
  every Auth wake 503'd until a loopback relay (`maludb-email-hook-relay`) was added. Signup through the
  official client reached MaluMail (200).
- 2026-09-17 — **Slice 5** (#210, #211): memory worker, egress proxy and query embedder on the control
  plane as narrowed roles. Findings fixed: the embedder admitted every private range (now named nodes
  only); preflight read superusers as console roles. Owner feedback reorganised the Models form and the
  provider-keys card. Official E2E with the owner's Anthropic and Voyage keys: ingest 202, worker 6.3 s,
  search by text 200; publishable key 403.
- 2026-09-17 — **Slice 8a** (#212, H-2): Turnstile secret placed by the owner and verified against
  siteverify; site key in `index.html`. Preflight has no failures (exit 2, advisories: gateway role not
  checkable from the control plane, backup, console bind).
- 2026-09-17 — **Slice 6**: pins and `extension-check` already agreed; `extension grants` current. Through
  the customer API with the owner's token, both enable jobs succeeded within 3 s. Official client: graph
  relations, nodes and named edges (FK, view dependencies) after a refresh; vector compartment create,
  insert, filtered search, list, delete; over-plan dimensions `PT403`; publishable key `42501` on both.
  **Gap for slice 9:** the console has no control to enable either feature; a free customer needs a
  personal access token and the API.
- 2026-09-17 — **Slices 7a, 7b** (#215, #216) deployed: recorder role `backup_node01`, runner and timers.
  **7c on the rehearsal:** pgBackRest configured (interim local repo1, encrypted, 30 days by time),
  `archive_mode = on` with one restart at 16:31 UTC, stanza created, `check` passes, first full backup
  `20260917-163138F` (63 MB to 6.4 MB, 20 s) recorded through the recorder; full and diff timers on.
  `node backups` ok; `backup-check` fails only on ADR-064 co-location. Code: readiness per `repoN` and
  by repository type (an SFTP repository's path was about to be judged against the node's own disks).
- 2026-09-17 — **7d deployed and ADR-087 applied** (#218, #219): node-01's local repository accepted until
  2026-12-16; nightly control-plane dump on .173 (`cp-20260917T173944Z.sql` first); preflight exits 2 with
  no failures.
- 2026-09-17 — **7e restore drills.** *Node:* a marker written through the SQL console (`before` at
  17:40:30 UTC, then `after`); `restore run --ref 8zn07rbf --target-time 17:40:30` on the node with
  temporary access (a `pg_hba` line on each VM, a minimal root-only env file) and `--beyond-entitlement`,
  after confirming the free plan correctly refuses point-in-time recovery. Restored in 39.1 s beside the
  live database: restored `before`, live `after`, `auth`/`storage` owned by their per-tenant roles, the
  neighbour answering throughout. Access removed and verified refused afterwards; drill copy and marker
  table dropped. **Finding:** the scratch cluster's marker outlived `pg_dropcluster` and would have broken
  the next restore -- fixed in #220. *Control plane:* the nightly dump restored into a scratch database in
  3 s with `ON_ERROR_STOP=1`; `control-plane verify --reach-nodes` unwrapped node and project credentials
  with the KEK and administered node-01 with the recovered credential; scratch database dropped.
  **Still the owner's:** copy `/etc/maludb/keys/kek` and `staff-key` off the control plane by hand.
- 2026-09-17 — **Slice 9, console controls.** The data-model graph and vector compartments could be
  turned on only through the API with a personal access token (found verifying slice 6). A **MaluDB**
  page on each project now shows each feature's state, plan limits and latest job, and turns them on
  and off; enabling is queued, so the page follows the job. It offers no reader: both are read through
  the project's Data API with its secret key, which a session is not. `docs.html` gains the matching
  section. **H-5 answered:** the owner reviews the abuse report weekly.
- 2026-09-17 — **Slice 9, legal drafts (H-4).** Terms, Privacy and Acceptable Use written from what the
  platform actually does, linked from both footers and from the signup form. They state the beta's real
  limits rather than a template's promises: one machine, backups beside the data (ADR-087), a single
  copy of uploaded files (ADR-085), restore to the last backup for a free project. The privacy page
  lists only the processors the code calls (MaluMail, Cloudflare Turnstile, the host) and says model
  provider keys are the customer's. Tests hold all of that, and hold every operator blank to
  [SQUARE BRACKETS] so none can pass unnoticed. **Not legally reviewed; placeholders unfilled.**
- 2026-09-17 — **Slice 10 begun.** Finding 11 fixed (#225). The previous install's three units
  (`maludb-api`, `maludb-mc2dbd`, `maludb-modeld`, all disabled) removed from both VMs, with copies
  left in `/root/removed-units-2026-09-17`; every service still running and nothing failed. DEPLOYMENT
  §5 rewritten around what the deployment now actually has: the node's backups and their acceptance,
  a restore that has been *run* rather than recorded, the trusted-proxy setting, the abuse cadence,
  the legal pages, no leftovers of a previous install, and signups opened last.
  **Answered by the owner:** `10.120.0.250` is the Proxmox host. The line -- any role, any database,
  TLS not required -- is removed from both VMs, with the previous file kept as
  `/root/pg_hba.conf.before-250-removal`. No connection was using it; both clusters and every service
  kept serving.
- 2026-09-17 — **Signup walkthrough on the live site.** Through the real form: the page, the legal links
  and the footer render, and **Turnstile refused to let the form submit without a solved challenge**
  (Cloudflare blocks the automated browser, which is the widget working; a human must do the final
  browser pass). The rest was then driven through the API with the challenge briefly off: signup 201,
  sign-in, a personal organization, a project (`3u2rzs6e`) **PROVISIONED in 208 s**, publishable and
  secret keys, a table created through the SQL editor route, and with the official client: the secret
  key reads and writes, project Auth accepts a signup, and Storage creates a bucket, uploads and
  downloads. The challenge and `MALUDB_SIGNUPS_OPEN = false` were restored, and a signup with no
  challenge is refused again (400).
  - *Expected, not a fault:* a new table has no row-level security, so the publishable key reads it.
    The docs say to turn RLS on and the table browser warns about exactly that table.
  - **Blocking gap found: nothing can delete a project.** There is no customer route, no
    `cp-manage` command, and `project cleanup` refuses a database that was ever provisioned. The
    drafted Terms and Privacy pages promise deletion, so today they are untrue. Slice 10b, below.
  - **Also:** provisioning took 208 s, longer than the "about a minute" the docs and the console's
    copy imply. Worth measuring before launch.

### Free slice 10b — Deleting a project (and closing an account)

Found by the walkthrough. A customer must be able to remove what they created, and the platform must
be able to honour a deletion request: it stores personal data and the beta's own pages promise it.
`jobs.cleanup` already drops roles, the database and the project's objects for a *failed* project;
this slice gives that a deliberate, guarded path for a live one -- a customer route that marks the
project and queues the work, an operator command, the storage worker's tenant deregistered, keys
revoked, and the record kept (`deleted_at`) rather than the row removed. Until it exists, the Terms
and Privacy pages must not promise deletion.
- 2026-09-17 — **Slice 8, first half** (#221): rehearsal finding 19 fixed and measured. Topology is
  visitor → Nginx Proxy Manager (TLS, 10.120.0.1) → Apache :80 → uvicorn 127.0.0.1, so Apache put the
  TLS proxy in the last `X-Forwarded-For` hop *and* uvicorn rewrote the client itself for loopback
  peers: every visitor counted as 10.120.0.1, in the limits and in session and audit rows.
  `MALUDB_TRUSTED_PROXIES=127.0.0.1,10.120.0.1` on the control plane, `--no-proxy-headers` on the
  three units. Before: dev box spent its 20 sign-in attempts, and the node was refused on its first.
  After: the node and the control plane each get their own bucket (401), the dev box stays refused
  (429), and a forged `X-Forwarded-For` does not move it to a fresh one.
  **Left in slice 8:** a named abuse reviewer and a review cadence (H-5).
- 2026-09-17 — **Slice 10b deployed** (#226) and used: the walkthrough project `3u2rzs6e` was deleted
  with `cp-manage project delete`. Database dropped, 6 roles dropped, its uploaded object removed
  from the bucket, the row kept as `DELETED`. **Finding:** deregistering the shared storage worker
  failed, because its admin API is on the node's loopback and the control plane cannot reach it
  (ADR-085's stated gap, which also stops the `storage_tenants` pass reconciling). A deleted
  tenant therefore keeps a registration holding its database URL and JWT secret in the worker's
  metadata database, pointing at a database that no longer exists. Removed by hand on the node this
  time (`DELETE /tenants/<ref>` on 127.0.0.1:5001). **Must be closed before signups open**, since
  every deletion will leave one; the options are a node-side job that reconciles the worker's tenant
  list against the control plane (the node maintenance timer already runs there, as the gateway), or
  admitting the control plane to the worker's admin port the way ADR-085 admits it to S3. The first
  keeps the admin port off the network, which is why ADR-085 put it on loopback; the second is a
  smaller change to an ADR that was just accepted. Recorded as **free slice 10c**.

### Free slice 10c — A deleted tenant leaves nothing behind on the node — **done**

`node_storage reconcile`, hourly on the node as `maludb-gateway`: the worker's tenant list against
the projects the node still serves, deregistering the rest.

**The plan's worry resolved the other way round.** It said that because `delete_project` clears
`node_id`, the gateway can no longer see the deleted row, so the instruction had to be *recorded* as
work for the node. In fact that is the mechanism: ADR-072's policies key on `node_id`, so clearing
it makes a deleted project invisible to exactly the role doing the reconciling, and "the worker holds
a ref this node cannot see" already means "deregister it". No queue, no new table, no migration --
and it covers a project moved to another node too, which a recorded instruction would not have.

Two things the build changed on the way:

- The admin API is now `storage_admin`, a leaf importing `httpx` and `models`. `storage_workers`
  derives the worker's secrets and so reaches `crypto` and `config`; node-side code must not, which
  is `node_maintenance`'s rule. Every name is re-exported, so no caller changed.
- The unit reads `/etc/maludb/storage/reconcile.env`, not the worker's `storage.env`. Sourcing the
  latter was the first draft and was wrong: it also carries `AUTH_ENCRYPTION_KEY` and the
  multitenant database's URL -- between them every tenant's Storage credentials on the node -- and
  the object store's secret key, and a process environment is readable by any process of the same
  user. That would have handed all of it to the gateway user once an hour to save writing a file.
  `storage-prepare --print reconcile` renders the two values it actually needs, from the same
  settings as `storage.env` in the same command, so the two cannot drift.

It refuses rather than guesses: more than `MAX_DEREGISTRATIONS` (50) stale refs in one pass
deregisters nothing and says so, because that is the shape of a gateway role that lost its node
mapping, not of a day's deletions. Same for a control plane it cannot reach.

Operator step in DEPLOYMENT §2.7: install `reconcile.env`, then the timer.

### Free slice 10d — Deletion leaves nothing, and the plan dropdown offers only what works

Both found by the owner's manual browser signup pass on 2026-09-18 — the one part of slice 10 an
automated run could not do, because Cloudflare blocks the automated browser and the Turnstile widget
is the thing under test. The pass itself succeeded: a real account through the real form, a project,
a table with RLS through the SQL editor, a memory space, and a delete. Provisioning took **5 s**, not
the 208 s the API walkthrough measured on 2026-09-17, so the docs' "about a minute" is now
conservative rather than wrong; the two measurements are three orders apart and neither should go in
customer copy until it is understood which was the anomaly.

**Deletion was incomplete, in two ways.** `jobs._drop_roles` carried a written-out list of six role
names. Four conditional roles have arrived since — `replicator`, `vectors`, `memwriter`, `memreader` —
and none had joined it, so the deleted project left `mldb_<ref>_memreader` and `mldb_<ref>_memwriter`
on the cluster, the second a LOGIN role, while the audit recorded `roles_dropped: 6` and the project
`DELETED`. The function's own docstring is an account of having fixed this exact leak once before, for
`executor`/`client`/`storage`, ending "a leak whose repair is a role name in a tuple should not outlive
the change that noticed it". It did. The list is now derived from `TenantNames.roles` rather than
written a second time, and `tests/test_provisioning_names.py` holds the derivation.

The existing deletion test could not have caught it: a freshly provisioned project has none of the
four, so `LIKE 'mldb_<ref>%'` was empty either way. The new test gives a project every role it can
have before deleting it.

Second, all seven `project_credentials` rows survived. With the roles dropped they authenticate
nothing, but they are recoverable plaintext for a project the customer asked to be rid of, they ride
in every control-plane dump, and `control-plane verify` unwraps each one. Deletion now removes them,
and says how many in the audit event. `api_keys` are left alone deliberately: hashed, so a revoked
row is a record rather than a secret.

**The plan dropdown offered three plans and accepted one.** `GET /v1/plans` returns the catalogue;
`POST .../projects` takes only the default plan and refuses anything else as `404 unknown plan` —
deliberately indistinguishable from an unknown code, so the endpoint cannot be used to map which
plans exist (the slice 5 security review, where naming a paid plan self-granted
`direct_database_access`). The console had no way to tell the two apart and rendered every plan as an
option, so two of the three were dead ends whose failure read as a broken platform. `PlanOut` gains
`self_serve`, true for the plan `create_project` actually accepts; the form offers those and names the
rest in a line of text. The rule stays in the control plane rather than becoming the string `"free"`
in a page.

**Still open after this slice:** H-4 placeholders and the lawyer's read, H-6 addresses, and no account
closure path — `walkthrough+1789675475363@maludb.org` and the owner's own test account both remain.
`MALUDB_SIGNUPS_OPEN = true` is currently set on the live page for the browser pass and must be put
back to `false` unless the pass is the launch.

### Free slice 10e — The customer's provider keys do not outlive the project

Found while demonstrating 10d on the deployment, which is the point of demonstrating it. A throwaway
project with an Anthropic key set on it was deleted through the customer route: the database, the nine
roles and the seven credentials went, and the `project_provider_keys` row stayed, `revoked_at` NULL and
the ciphertext intact. Nothing in the codebase had ever deleted from that table — `set_key` and
`remove_key` both mark `revoked_at` and keep the ciphertext, which is the right answer for rotation
and the wrong one for a project that no longer exists.

It is the same leak as 10d's and a worse one. A `project_credentials` row authenticates a role the
same job has just dropped, so what survived was useless as well as wrong. A provider key is the
customer's own credential at a third party: it keeps working, and keeps spending their money, after
they have deleted the project they trusted it to — and it rides in every control-plane dump until
someone notices. The Privacy page promises deletion; this was the part of it that was not true.

Deletion now removes them and records the count beside `credentials_removed`.

**Not fixed here, and worth a decision:** `remove_key` — the customer's own "remove this key" action —
also only sets `revoked_at`. A customer who removes a key from a live project still has it stored,
recoverable with the KEK. That is defensible as rotation history and indefensible as what the word
"remove" says on the page; whichever way it goes, the page and the table should agree.

Also cosmetic, unfixed: a deleted project's `memory_spaces` row still reads `state = active`.
