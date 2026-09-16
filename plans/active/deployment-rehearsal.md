# Deployment rehearsal: docs/DEPLOYMENT.md on real VMs

Status: in progress (started 2026-09-15). Pre-launch: no customers, no production node yet.

## Goal

Follow `docs/DEPLOYMENT.md` from zero on two VMs, as an operator would, and record every place the
runbook is wrong, incomplete or ambiguous. Fix the runbook (and code, where the gap is code) in
reviewable PRs. Nothing on the development box is used as a shortcut.

## Environment

| Host | Address | Role |
|---|---|---|
| node | 10.120.0.172 (`maludb-acct101`), Ubuntu 24.04.5, 6 vCPU, 7.9 GB, 160 GB free | PostgreSQL 17 + `maludb_core`, gateway, per-project workers |
| control plane | 10.120.0.173 (`public-dash`), Ubuntu 24.04.4, 6 vCPU, 3.9 GB | public app, provisioner, control-plane PostgreSQL, website |
| edge | Nginx Proxy Manager on Proxmox, public IP 67.211.209.250 | TLS; `test.maludb.org` -> control plane, `*.test.maludb.org` -> node :8110 |

Gateway domain: `test.maludb.org`. DNS: `test.maludb.org` resolves; `*.test.maludb.org` not yet.
Access: user `maludb`, passwordless sudo, key `maludb-deploy-rehearsal` from the dev box.

## Starting state of the node (not clean)

The VM had a previous MaluDB install. Recorded before changing anything:

- PostgreSQL 17.11 (PGDG), cluster `main` on `0.0.0.0:5432`, `ssl=on`, `wal_level=replica`,
  `shared_preload_libraries=pgaudit,pg_stat_statements`.
- `maludb_core` **0.104.0** available (pin is 0.105.3); `vector` 0.8.6 package installed, **not held**.
- `maludb-api.service`: `/home/maludb/maludb-python-api-server` (FastAPI/uvicorn) on `0.0.0.0:8000`.
- A `maludb` database (35 MB, maludb_core 0.104.0, vector 0.8.3); Apache on :80 (default page).
- Cluster roles as on the dev node: `maludb_*` groups from the extension, and logins `app`,
  `maludb_mc2dbd` (BYPASSRLS), `maludb_modeld`.

With the owner's agreement ("consider it a clean ubuntu with maludb installed on top; I don't need
the DB"): `maludb-api` stopped and disabled (files kept), `maludb` database backed up to
`/home/maludb/pre-rehearsal-backup/` and dropped. **The backup holds no table data**: maludb_core
0.104.0 registers no tables for pg_dump (ADR-078's finding), so every row of that extension-owned
database was skipped. Discovered after the drop; the owner did not need the data.

## Findings

Numbered as found. Each gets a fix (runbook, code or decision) or an explicit "accepted".

1. **§2.1 never installs `maludb_core`.** It installs apt packages and pgvector; building the
   extension at the pinned commit (build dependencies, clone, `make install`) appears only in
   `docs/MALUDB.md` ("For `maludb_core` the same order holds with `make install`..."). Fixed in the
   runbook PR (build at CI's `MALUDB_CORE_REF`, as done on the node).
2. **§2.1 states Realtime's cluster requirements without commands.** `wal_level = logical` and the
   ADR-031 `pg_hba.conf` replication reject are described; the commands live only in
   `scripts/realtime-test-cluster.sh`. Runbook now names every requirement and the script, and says
   Realtime is not rehearsed; the commands themselves are still to be run on the node. Open.
3. **A login role with BYPASSRLS on the node** (`maludb_mc2dbd`, from the MaluDB install). Tenant
   databases revoke CONNECT from PUBLIC, which should keep it out; to be verified on the node, not
   assumed.
4. **No command stores a node's provisioning credential.** `node register` takes no DSN, and
   `nodes.set_admin_dsn` is called only from tests. A deployment following the runbook registers a
   node that can never be provisioned onto: `admin_dsn()` answers "has no provisioning credential
   configured". The runbook also never says the node's PostgreSQL must accept that superuser
   connection from the control-plane host (listen address, `pg_hba.conf`, TLS). Code gap: needs a
   `cp-manage node credential set` that reads the DSN from stdin, never argv.
5. **§2.2 pins `maludb_core` 0.104.0**; the pinned release is 0.105.3. Fixed in #182.
6. **§2 never puts the repository on the node**, yet §2.4 copies `deploy/` units and runs the
   `maludb-gateway` entry point there. §1.1 installs it only on the control plane, and `uv` is not
   installed on a clean Ubuntu (neither VM had it). Fixed: §2.4 (#184) and §1.1 (runbook PR: uv,
   root-owned checkout).
7. **§1.4 overwrites the env file §1.3 just wrote** (`cp deploy/control-plane.env.example
   /etc/maludb/control-plane.env`). Fixed in the runbook PR.
8. **§3 lists four static files**; `docs.html` is a fifth since #179. Fixed in the runbook PR.
9. **Health reporting is required but unspecified.** §2.2 says "whatever records health must be
   running" and names nothing; there is no unit or timer for `node health`. It also cannot simply
   run on the node: `cp-manage` needs the control plane's database and KEK. The rehearsal uses a loop
   on the dev box (node `df` over ssh, `node health` on the control plane). Decided as ADR-080
   (option b, a node-side reporter with a one-function role), `feat/node-health-reporter`.
10. **No service could read the keys** (§1.2 + units). Keys `root:root 600`, every unit its own
    user, loader refuses group-readable: both listeners died with PermissionError. Fixed in #183
    (`LoadCredential=`).
11. **First start races on the first data key.** Public and internal listeners started together
    both minted `encryption_keys` version 1; one died on the primary key and `Restart=` recovered
    it. Harmless today, but a startup crash on a clean install reads as a broken deploy. Open.
12. **`MALUDB_PLATFORM_OWNER` is undocumented.** The provisioner refuses to start without it; it is
    not in `control-plane.env.example` or the runbook. `cp-manage` silently defaults to `postgres`.
    Fixed in the runbook PR (§1.3 and the example).
13. **The runbook never prepares backup.** `node backup-check` is run in §2.2 but nothing sets
    `archive_mode`, `archive_command` or a pgBackRest stanza; run from the control plane it also
    cannot inspect the repository (pgBackRest lives on the node). Not a placement requirement.
14. **The control plane's PostgreSQL must admit the gateway** from the node (`listen_addresses`,
    `pg_hba`), and the runbook does not say so; `gateway.env.example` has no `sslmode`. Fixed in the
    runbook PR.
15. **`ProtectHome=true` breaks libpq over TLS** (EACCES on `~/.postgresql/postgresql.crt`); the
    gateway's pool never initialised. Fixed in #183 (`ProtectHome=tmpfs`).
16. **A production gateway crashed at startup** (`KeyError: 0` in `node_identity`: tuple indexing
    on a `dict_row` pooled connection; tests used tuple mocks). Fixed in #183.
17. **The gateway could not start workers.** Read-only `/etc/maludb/postgrest`; sudo impossible
    under `NoNewPrivileges`; PostgREST (`maludb-api`) could not read a 0600 `maludb-gateway` config;
    PostgREST/GoTrue binaries and `maludb-api` never installed by the runbook. Fixed in #184
    (write paths, polkit rule, `LoadCredential=conf`, §2.4).
18. **Environment, not runbook:** the control plane's Apache docroot still serves the previous
    install (`index.php` "MaluAdmin", plus `graph.json`/`GRAPH_REPORT.md` from graphify) publicly at
    `https://test.maludb.org/`. Both VMs' `pg_hba.conf` also carry a leftover
    `host all all 10.120.0.250/32 scram-sha-256`.
19. **§3 assumes Apache terminates TLS** (`<VirtualHost *:443>`). Behind a TLS proxy on another
    host it is `*:80`, and then every request reaches the public app from 127.0.0.1 with the proxy's
    address as the last `X-Forwarded-For` hop, so `MALUDB_TRUST_FORWARDED_FOR` cannot recover the
    client address and signup/signin rate limits see one client. Needs a documented topology
    (trusted-proxy count, or `mod_remoteip`) before real traffic.
20. **`node health` erased the realtime and backup check results** (`record_health` replaced
    `metrics_json`). Fixed with ADR-080.
21. **Preflight reports "every placeable node has a stanza" for a node whose `backup-check` failed**
    (`archive_mode` off). A recorded stanza name is not a working backup. Open.
22. **§3's Apache block puts a comment after `AddType`**; Apache has no trailing comments, so the
    words become extensions. Fixed in the runbook PR.
23. **A key's public prefix was enough to use it.** Found while testing the wildcard certificate
    through NPM against `8zn07rbf`: the gateway cached each answer under the key's 8-character
    `key_identifier` and a hit never looked at the rest of the presented key, so `prefix +
    anything` was accepted for 30 seconds after any legitimate use -- as `service_role` for a
    secret key. The same keying let junk cache a failure that locked the real key out for 5
    seconds. Fixed in `fix/gateway-key-cache` (`plans/active/gateway-key-cache.md`).
24. **Nothing consumed the revocation announcements.** A revoked key kept working for up to 30
    seconds; only the test suite called `apply_revocation`, by hand. Same branch: a `LISTEN`
    consumer in the gateway.
25. **Keys reached the journal in clear.** `uvicorn.run` installs its own logging config over the
    JSON formatter, and its websocket access line carries `?apikey=<key>`. The redaction pattern
    would not have matched a real key anyway. Same branch.
26. **§2.2 admits only `maludb_provisioner` from the control plane**, so the dashboard's Tables
    panel and the SQL console fail on any deployment that follows the runbook: both connect as the
    tenant's own roles (ADR-039), and the node answers `no pg_hba.conf entry for host ..., user
    mldb_<ref>_authenticator`. What the customer sees is `could not reach the project's database`,
    naming neither the file nor the host. Found by clicking Tables as the rehearsal owner on a
    free project, which is the tier ADR-039 exists for. Fixed in the runbook (`hostssl all all
    <control plane>/32`), applied to the rehearsal node. Same shape as findings 4 and 14: the
    runbook says how to grant, not how to let the resulting connection happen.

    Two things that cost time and belong in the record. A `pg_hba.conf` line pasted through a
    wrapping terminal arrived split across two lines, twice; a *reload* keeps the previous
    configuration when the file is malformed, so nothing changed, nothing complained, and the
    broken file would have refused to start the server at the next restart. And the corrected
    line was first applied to the development box rather than the node -- the prompts differ by
    hostname alone. The runbook now says to check `pg_hba_file_rules` rather than assume.

## Log

- 2026-09-15: node inventoried and cleaned as above.
- 2026-09-15: control-plane VM had the same previous install (maludb-api on :8000, a `maludb`
  database with maludb_core 0.104.0, Apache on :80). With the owner's agreement: service stopped and
  disabled, database dumped to `/var/lib/postgresql/pre-rehearsal-backup/` (again no table data;
  about 10.6k rows in maludb_core tables not captured) and dropped.
- DNS for maludb.org is at GoDaddy (`domaincontrol.com`); `*.test.maludb.org` does not resolve yet.
- Control plane installed per §1.1–1.5 at `/opt/maludb` (root-owned; a local `rehearsal` branch
  merging #182–#184). Keys, `cp` database password and `gw` password under `/etc/maludb/`, root 600.
  Listeners, provisioner active; internal on 10.120.0.173:8111, public on loopback :8112.
- Node: `maludb_provisioner` superuser, `hostssl` from 10.120.0.173/32 only; `node-01` registered,
  credential stored and verified over TLS, pins 0.8.6/0.105.3 agree, active and eligible.
  Gateway active on :8110 as `gw` mapped to node-01; PostgREST 14.17 and GoTrue 2.195.0 installed.
- End to end: signup (201) and a free project `8zn07rbf` through the public API reached
  PROVISIONED; a secret key through the gateway woke `maludb-postgrest@8zn07rbf` and answered 200.
- Website: with the owner's agreement the previous docroot was moved to `/var/www/pre-rehearsal-html`
  (not served) and `000-default` disabled; `maludb.conf` serves `/opt/maludb/frontend` on :80 with
  `/api/` proxied to :8112. Through NPM: `https://test.maludb.org/` serves MaluDB, `graph.json` and
  `index.php` 404, `.md`/`.py` 403, `/.git/config` 404; headless Chromium signed in and listed
  `8zn07rbf`. Signups stay closed (`MALUDB_SIGNUPS_OPEN = false`).
- Health reporter (ADR-080) on the rehearsal: migration 0050 applied, `reporter_node01` granted
  (`table privileges: none`), `maludb-node-reporter` active on the node; the dev-box loop stopped.
  Stopping the node's PostgreSQL paused reports (`local PostgreSQL is not answering`) and they
  resumed on the next tick after it started. Preflight: `node health reporters` ok.
- All of #182–#184 merged; both VMs moved to `main` plus `feat/node-health-reporter`, installed units
  and polkit rule identical to the repository, REST through the gateway 200 again on that code.
