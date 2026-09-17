# Deployment

Two fresh machines to a customer signing up and a project serving traffic.

This document is the runbook. `plans/active/deployment-topology.md` is the plan
that produced it and `tasks/DEPLOYMENT.md` tracks what is still missing.

## What runs where, and what must not share a host

| Process | Role | Faces |
|---|---|---|
| control-plane **public** app | control plane | the internet, behind TLS |
| control-plane **internal** app | control plane | a private interface **only** |
| operator console (ADR-082) | control plane | a private interface on the operator VPN **only** |
| provisioner worker | control plane | nothing inbound |
| control-plane PostgreSQL | control plane | private |
| PostgreSQL + `maludb_core` | node | private, plus the data address |
| **gateway** | node | the internet, behind TLS |
| PostgREST / GoTrue / Realtime / storage | node | loopback and the data address |
| the static site | anywhere | the internet |

Three rules, each with a reason rather than a preference.

**The control plane must not run on a node.** Its database holds every node's
superuser DSN, encrypted with the KEK.

**The gateway cannot be moved off the node.** It proxies to
`http://127.0.0.1:{port}` and drives systemd to wake sleeping workers; nothing
in `services/gateway/` reads `nodes.internal_host`. A gateway serves the tenants
on its own machine and no others. This is also why **multi-node is not
supported yet**: a second node needs something in front routing each project's
hostname to the right gateway, and nothing implements that.

**The internal application must never bind a public interface.** It serves
`/internal/hooks/email/{ref}`, whose only other protection is its own signature.

### The two-machine deviation, stated rather than hidden

`deploy/maludb-provisioner.service` says, in its own header: *"Do not install
this on a host that serves the public application."* The reason is ADR-038 — the
provisioner holds node superuser credentials and the KEK, and the surest way to
keep the internet-facing application away from them is for them to run somewhere
else entirely.

Taken strictly that is **three** machines: public app, then provisioner plus
internal app plus the control-plane database, then the node.

Running the public app and the provisioner on one host is a deviation. It is
defensible for a first deployment — they are separate processes under separate
users, and ADR-038's import-graph test still holds inside the public
application — but it trades away the host boundary the unit asks for. If the
public app is ever compromised at the host level, the KEK is on that host.

Decide deliberately. This runbook writes two machines because that is the
smallest thing that can sell, and says here what it costs.

## Prerequisites

- Two hosts, Debian/Ubuntu with PostgreSQL 17 (PGDG).
- A wildcard DNS record and certificate for the gateway domain. ADR-008 makes
  the **hostname the routing key**, so `*.example.com` must reach the gateway.
- Passwordless `sudo` on the node for the operator running provisioning.

---

## 1. Control plane

### 1.1 Install

```bash
# uv is not packaged for Ubuntu 24.04; install a pinned release system-wide.
curl -LsSf https://astral.sh/uv/0.8.17/install.sh -o /tmp/uv-install.sh
sudo env UV_INSTALL_DIR=/usr/local/bin UV_NO_MODIFY_PATH=1 sh /tmp/uv-install.sh

sudo mkdir -p /opt/maludb /etc/maludb
sudo git clone https://github.com/Mestopholis/maludb-provisioner-api.git /opt/maludb
cd /opt/maludb
sudo uv venv --python /usr/bin/python3.12 && sudo uv pip install -e .
# Installs the cp-manage, cp-migrate, maludb-gateway and maludb-node-reporter
# entry points used below.
```

The checkout and the virtualenv are **root-owned on purpose**: every service runs
as its own unprivileged user, and none of them should be able to change the code
it runs. Run `git` there as root (`sudo git -C /opt/maludb pull`); as another user
git refuses with "dubious ownership", which is the right answer. The node gets the
same install (2.4).

### 1.2 Key material

Two files, and **both are unrecoverable if lost**: the KEK unwraps every data
encryption key in the control-plane database, and ADR-070 makes the control
plane refuse to start rather than mint a replacement — a dump restored without
its keys is not a backup.

```bash
sudo mkdir -p /etc/maludb/keys
openssl rand -hex 32 | sudo tee /etc/maludb/keys/kek > /dev/null
openssl rand -hex 32 | sudo tee /etc/maludb/keys/pepper > /dev/null
sudo chmod 600 /etc/maludb/keys/*        # the loader refuses group/world-readable material
```

Back these up somewhere that is not the control-plane database.

They stay `root:root 600`, and no service reads them there: each unit that needs
them runs as its own user and declares `LoadCredential=kek:/etc/maludb/keys/kek`,
so systemd hands that user a private in-memory copy for as long as it runs. The
`MALUDB_KEK_REF`/`MALUDB_TOKEN_PEPPER_REF` paths in the environment files are what
`cp-manage` and `migrate` use when you run them as root. **Do not loosen the files'
mode or `chown` them to a service user** to make a unit start — that gives the key
to one user and still leaves the others without it. The node needs the same two
files at the same paths for the gateway (2.4).

### 1.3 Database and migrations

Plain PostgreSQL. `maludb_core` belongs in tenant databases (ADR-015), not here.

```bash
sudo -u postgres psql -c "CREATE ROLE cp LOGIN PASSWORD '<strong>'"
sudo -u postgres psql -c "CREATE DATABASE maludb_control_plane OWNER cp"
```

`/etc/maludb/control-plane.env`, mode 600 (it carries a database password). Start
from the example and fill in every `CHANGEME`:

```bash
sudo install -m 600 deploy/control-plane.env.example /etc/maludb/control-plane.env
sudoedit /etc/maludb/control-plane.env
```

```ini
MALUDB_ENV=production
MALUDB_CONTROL_PLANE_DATABASE_URL=postgresql://cp:<strong>@127.0.0.1:5432/maludb_control_plane
MALUDB_KEK_REF=/etc/maludb/keys/kek
MALUDB_TOKEN_PEPPER_REF=/etc/maludb/keys/pepper

# Defaults to maludb.local, which routes nothing. Project API URLs are derived
# from it, so a wrong value ships customers unreachable hostnames.
MALUDB_GATEWAY_DOMAIN=example.com

# Where password-reset links point.
MALUDB_DASHBOARD_URL=https://example.com

# Required in production: captcha_required defaults to ON when MALUDB_ENV is
# production, and signup fails closed when the challenge cannot be verified.
MALUDB_CAPTCHA_SECRET=<cloudflare turnstile secret>

# The node role that owns tenant databases. The provisioner refuses to start
# without it; cp-manage falls back to `postgres`, which is what a MaluDB node uses.
MALUDB_PLATFORM_OWNER=postgres
```

```bash
set -a && . /etc/maludb/control-plane.env && set +a
/opt/maludb/.venv/bin/python -m services.control_plane.migrate
/opt/maludb/.venv/bin/python -m services.control_plane.manage plans sync
```

`plans sync` is not optional. **Nothing else seeds the `plans` table**, and
without a `free` plan, creating a project answers 503.

### 1.4 The two listeners

```bash
sudo useradd -r -s /usr/sbin/nologin maludb-cp
sudo cp deploy/maludb-control-plane-public.service \
        deploy/maludb-control-plane-internal.service /etc/systemd/system/
# /etc/maludb/control-plane.env is the file written in 1.3. Do not copy the
# example over it again.
sudo systemctl daemon-reload
sudo systemctl enable --now maludb-control-plane-public maludb-control-plane-internal
```

`create_app` builds the **internal** application — every router, including the
email hook. Only `create_public_app` is safe to expose. The two units differ in
that factory and in their bind address, and `tests/test_deploy_units.py` asserts
both: a copy-paste that lost one produces a service that starts, serves, and is
wrong.

`MALUDB_INTERNAL_BIND` must be a private address. The public unit binds loopback
because TLS terminates in front of it.

### 1.5 The provisioner

```bash
sudo cp deploy/maludb-provisioner.service /etc/systemd/system/
sudo useradd -r -s /usr/sbin/nologin maludb-provisioner
sudo install -m 600 /etc/maludb/control-plane.env /etc/maludb/provisioner.env
sudo systemctl daemon-reload && sudo systemctl enable --now maludb-provisioner
```

### 1.5a The maintenance pass (ADR-083)

The control plane's half of the periodic passes: retrying failed provisioning, applying what was
paid for (ADR-053), ending failed-payment grace (ADR-051), measuring and enforcing database and
file storage, and the capacity, slot, backup and drift checks. **Without it purchases are recorded
and never applied, and storage limits are never enforced.** It runs as the provisioner, whose
environment and keys it needs, every minute:

```bash
sudo cp deploy/maludb-maintenance.service deploy/maludb-maintenance.timer /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now maludb-maintenance.timer
systemctl list-timers maludb-maintenance.timer
journalctl -u maludb-maintenance -n 40      # each pass and what it did
```

It runs `maintenance run --skip sleep`: sleeping idle workers is the node's half (§2.6).
Preflight's "maintenance pass" fails when no run has finished in fifteen minutes.

### 1.5b Email (MaluMail)

Two things send mail, and both fail **silently** without this: a platform user's password reset
(the route answers the same whether or not anything was sent, on purpose), and a project's Auth
confirmation and recovery mail. The platform sends through its own MaluMail account (ADR-029).

On the control plane, in `control-plane.env` and `provisioner.env` (both root 600):

```ini
MALUMAIL_API=<the platform's MaluMail key>
MALUDB_PLATFORM_EMAIL_FROM=noreply@<a domain verified in MaluMail>
MALUDB_PLATFORM_EMAIL_FROM_NAME=MaluDB
```

On **every node**: GoTrue posts its hook to the control plane's *internal* listener, but it
accepts a plain-HTTP hook URI **only on loopback** (it exits with "only localhost, 127.0.0.1, and
::1 are supported with http", and every Auth request answers 503). So the node runs a loopback
relay -- stock `systemd-socket-proxyd` -- and GoTrue posts to that. The relay decides nothing; the
control plane still verifies each hook's signature.

```bash
echo "MALUDB_EMAIL_HOOK_UPSTREAM=<control plane internal address>:8111" \
  | sudo install -m 0644 /dev/stdin /etc/maludb/email-hook-relay.env
sudo cp deploy/maludb-email-hook-relay.socket deploy/maludb-email-hook-relay.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now maludb-email-hook-relay.socket
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8119/healthz     # 200, from the control plane
```

and in `gateway.env`, the relay and the sender a project's settings start with:

```ini
MALUDB_EMAIL_HOOK_BASE_URL=http://127.0.0.1:8119
MALUDB_PLATFORM_EMAIL_FROM=noreply@<same domain>
```

The gateway refuses to start with a plain-HTTP hook URL that is not loopback.
A project's email settings are created the first time its Auth worker starts, as
`platform_default`; `cp-manage project email` moves one to `custom_domain`. **In production the
gateway refuses to start Auth without the hook**, so a missing setting shows up as Auth failing to
start rather than as confirmations that never arrive. Restart the public, internal and gateway
services after setting these; preflight's "email" check covers the control-plane half.

### 1.6 The memory worker and its egress proxy (ADR-079)

Two units on the control-plane host. The worker writes queued memory ingests into
tenants' memory spaces and calls model providers with customers' own keys; the proxy
is its only way to the internet, and allows exactly `api.openai.com`,
`api.anthropic.com` and `api.voyageai.com`.

**The worker gets its own database role first.** It holds the KEK, so as the control
plane's role it could open every node's superuser DSN and every tenant's database
password. Its role reads what it needs — memory writer credentials, live provider
keys, the memory queue — and nothing else:

```bash
sudo -u postgres psql -d maludb_control_plane <<'SQL'
CREATE ROLE cp_memory_worker NOLOGIN;
CREATE ROLE memworker LOGIN PASSWORD '<strong>' IN ROLE cp_memory_worker;
SQL
/opt/maludb/.venv/bin/python -m services.control_plane.manage memory-worker grant
```

The group's name is fixed: the row policies recognise **membership of
`cp_memory_worker`**, not a setting. Keep the login role `INHERIT` (the default), or
its grants do not apply. Re-run the grant after any migration, and `deploy preflight`
names a column the grant has not caught up with. A gateway role must never be a member —
the command, the preflight and the worker all refuse it.

```bash
sudo useradd -r -s /usr/sbin/nologin maludb-egress
sudo useradd -r -s /usr/sbin/nologin maludb-memory
sudo cp deploy/maludb-egress-proxy.service deploy/maludb-memory-worker.service /etc/systemd/system/
sudo install -m 600 -o maludb-memory deploy/memory-worker.env.example /etc/maludb/memory-worker.env
sudoedit /etc/maludb/memory-worker.env   # memworker's DSN, the key paths
sudo systemctl enable --now maludb-egress-proxy maludb-memory-worker
```

A production worker **refuses to start** when its role can read more than that, when
it is not a member of `cp_memory_worker` (it would claim nothing, silently), or
without `MALUDB_MEMORY_EGRESS_PROXY`.

**Search by text** (slice 6a) is a third unit, the query embedder: the gateways ask it to embed
a search's text. It is optional, and a gateway without `MALUDB_MEMORY_EMBEDDER_URL` answers 503
for search by text and keeps serving vector search. It has its own role, which verifies
customers' keys and opens provider keys, and nothing else:

```bash
sudo -u postgres psql -d maludb_control_plane <<'SQL'
CREATE ROLE cp_memory_embedder NOLOGIN;
CREATE ROLE memembed LOGIN PASSWORD '<strong>' IN ROLE cp_memory_embedder;
SQL
/opt/maludb/.venv/bin/python -m services.control_plane.manage memory-worker grant   # applies both models
sudo useradd -r -s /usr/sbin/nologin maludb-embedder
sudo cp deploy/maludb-memory-embedder.service /etc/systemd/system/
sudo install -m 600 -o maludb-embedder deploy/memory-embedder.env.example /etc/maludb/memory-embedder.env
sudoedit /etc/maludb/memory-embedder.env   # memembed's DSN, the key paths, the private bind address
sudo systemctl enable --now maludb-memory-embedder
```

Then on each node, in `/etc/maludb/gateway.env`:
`MALUDB_MEMORY_EMBEDDER_URL=http://<the bind address>:<port>`.

The gateway sends the customer's secret key to that URL with every text search, so
configuration refuses anything but a private or loopback **address literal**, and the embedder
refuses to bind a public address. The unit admits loopback only; name the nodes that may reach it
in a drop-in, one `/32` each:

```bash
sudo install -d /etc/systemd/system/maludb-memory-embedder.service.d
sudo install -m 0644 deploy/maludb-memory-embedder-nodes.conf.example \
  /etc/systemd/system/maludb-memory-embedder.service.d/nodes.conf
sudoedit /etc/systemd/system/maludb-memory-embedder.service.d/nodes.conf   # each node's address
sudo systemctl daemon-reload && sudo systemctl restart maludb-memory-embedder
```

Without it every gateway's search by text answers 503, which is the failure to want: before this
the unit admitted every private range, and on the rehearsal the whole operator network reached it.

---

### 1.7 The operator console (ADR-082)

Optional: a deployment need not run it. It serves read-only reports on sales, customers, usage,
abuse, nodes and provisioning to signed-in staff, and takes no action on anything.

```bash
# The staff key: separate material from the KEK (docs/SECRETS.md). Preflight refuses identical keys.
sudo sh -c 'openssl rand -hex 32 > /etc/maludb/keys/staff-key' && sudo chmod 600 /etc/maludb/keys/staff-key

# Staff accounts, from this host (docs/ACCOUNTS.md), in a terminal: it prompts for the password
# twice, prints the authenticator secret once, and waits for a code from the app. It needs the
# control plane's own environment (database, and the KEK to refuse a staff key equal to it) plus
# the staff key -- so load the file rather than passing -E, and use `ssh -t` when remote.
sudo bash -c 'cd /opt/maludb && set -a && . /etc/maludb/control-plane.env && set +a && \
  MALUDB_STAFF_KEY_REF=/etc/maludb/keys/staff-key \
  /opt/maludb/.venv/bin/python -m services.control_plane.manage staff create --email ops@example.com --name "Ops"'

# Its own database role: a NOLOGIN group and a LOGIN member, created as a superuser,
# then narrowed by cp-manage as the control plane's role (ADR-082 decision 4).
sudo -u postgres psql -d maludb_control_plane -c "CREATE ROLE cp_admin_console NOLOGIN"
# Generate the password on the host and pass it to psql on stdin, never argv (ps shows argv).
sudo -u postgres psql -d maludb_control_plane -c "CREATE ROLE cp_admin LOGIN PASSWORD '<strong>' IN ROLE cp_admin_console"
sudo bash -c 'cd /opt/maludb && set -a && . /etc/maludb/control-plane.env && set +a && \
  /opt/maludb/.venv/bin/python -m services.control_plane.manage admin-console grant'
# The gateway's grant is ALL TABLES minus a list; re-run it so the staff tables are revoked explicitly.
sudo bash -c 'cd /opt/maludb && set -a && . /etc/maludb/control-plane.env && set +a && \
  /opt/maludb/.venv/bin/python -m services.control_plane.manage gateway grant --role <gateway role> --node <node>'

# The listener: its own user, its own environment file, the staff key and nothing else.
sudo useradd -r -s /usr/sbin/nologin maludb-admin
sudo install -m 600 deploy/admin-console.env.example /etc/maludb/admin-console.env   # then edit
sudo cp deploy/maludb-control-plane-admin.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now maludb-control-plane-admin
```

`MALUDB_ADMIN_DATABASE_URL` names the `cp_admin` login, never the control plane's own role:
in production the console refuses to start as a role that can read a sealed column or a
customer verifier, write a staff credential, or is not in `cp_admin_console`. Re-run
`cp-manage admin-console grant` after a migration, as for the other narrowed roles; preflight
says when it is due. The group must not also contain a gateway, health reporter or memory
worker role, and the grant command refuses one.

The console's pages are at `http://<MALUDB_ADMIN_BIND>:8113/admin/` (reached over the VPN). Preflight reads the console's settings from the environment, so check it with them set:
`MALUDB_ADMIN_BIND=<bind> MALUDB_STAFF_KEY_REF=/etc/maludb/keys/staff-key` alongside
`control-plane.env`. It listens on `MALUDB_ADMIN_BIND:8113`, which must be a private address reached over the operator
VPN; `cp-manage deploy preflight` fails a wildcard or public bind and a staff key equal to the
KEK. Sign-in is `POST /admin/v1/session` with email, password and authenticator code; the
session is an HttpOnly, SameSite=Strict cookie scoped to `/admin`, Secure unless
`MALUDB_ADMIN_COOKIE_SECURE=false` says the listener is plain HTTP inside the VPN.

## 2. Node

### 2.1 PostgreSQL and extensions

```bash
sudo apt-get install -y postgresql-17 postgresql-17-wal2json pgbackrest podman
# vector at a version specs/extension-versions.yaml lists, then held (ADR-075):
sudo apt-get install -y postgresql-17-pgvector=0.8.6-1.pgdg24.04+1
sudo apt-mark hold postgresql-17-pgvector

# maludb_core, built at the commit CI tests (MALUDB_CORE_REF in .github/workflows/ci.yml).
# libssl-dev and libcurl4-openssl-dev are link-time requirements, not extras.
sudo apt-get install -y postgresql-server-dev-17 build-essential libssl-dev libcurl4-openssl-dev
git clone https://github.com/maludb/maludb-core.git ~/maludb-core
git -C ~/maludb-core checkout <MALUDB_CORE_REF>
make -C ~/maludb-core PG_CONFIG=/usr/lib/postgresql/17/bin/pg_config
sudo make -C ~/maludb-core PG_CONFIG=/usr/lib/postgresql/17/bin/pg_config install
sudo systemctl restart postgresql@17-main   # backends load the new library
```

`make install` only: the Makefile's `install-services` writes systemd units and
`/etc/maludb`, neither of which the platform uses (ADR-012). The version it
installs must be the one you pin in 2.2, or `node extension-check` refuses the node.

**Hold `postgresql-17-pgvector`.** Every tenant on a node loads the same
`vector.so`, so a routine `apt upgrade` changes the code under all of them at
once. The platform does not install packages, but it notices: a node whose
packages disagree with its pin takes no new projects (2.2). Moving it later is a
procedure with an order — pin, package, workers, check, tenants — in
`docs/MALUDB.md`, "Changing a pin, in order".

`wal2json` fails **silently** if missing: a client subscribes to Postgres
Changes successfully and no event is ever delivered, arriving as a ten-second
timeout that names neither the plugin nor the package.

Realtime additionally needs `wal_level = logical` (a restart), a bounded
`max_slot_wal_keep_size` (ADR-032), `wal2json` in `output_plugin_libraries` on
17.11 and later, and ADR-031's `pg_hba.conf` reject of physical replication —
without which the first project to enable Realtime holds a role that can take a
byte-level copy of **every** tenant database on the cluster. The commands, with
the order that matters (the reject must precede the default replication lines),
are the ones `scripts/realtime-test-cluster.sh` runs; `cp-manage node
realtime-check` says what is still missing. **Not yet rehearsed on a production
node** (`plans/active/deployment-rehearsal.md`): do not offer Realtime until it is.

### 2.2 Register it

The control plane provisions by connecting to the node's PostgreSQL as a
superuser, so first let it in — **from the control-plane host's address only**,
over TLS. On the node:

```bash
sudo -u postgres psql -c "ALTER SYSTEM SET listen_addresses = 'localhost,10.0.0.20'"
sudo -u postgres psql -c "CREATE ROLE maludb_provisioner LOGIN SUPERUSER PASSWORD '<strong>'"
echo "hostssl all maludb_provisioner 10.0.0.10/32 scram-sha-256" \
  | sudo tee -a /etc/postgresql/17/main/pg_hba.conf
# And the tenants' own roles, from the same address. Provisioning is not the
# only reason the control plane connects here: the SQL console and the schema
# browser run statements and read the catalogue as `mldb_<ref>_executor` and
# the other per-tenant roles, because reading as anything else shows a customer
# a database that is not the one their own statements run against. That surface
# is every tier's (ADR-039) -- free has no other way to create a table.
echo "hostssl all all 10.0.0.10/32 scram-sha-256" \
  | sudo tee -a /etc/postgresql/17/main/pg_hba.conf
sudo systemctl restart postgresql@17-main        # listen_addresses needs a restart
```

Without that second line the dashboard's **Tables** panel and the SQL console
answer `could not reach the project's database`, which names neither this file
nor the host being refused; the cause is only visible in the node's log as
`no pg_hba.conf entry for host ..., user mldb_<ref>_authenticator`.

It admits any role *from the control-plane host*, which sounds wider than it
is: that host already holds `maludb_provisioner`, a superuser on this node, so
it can already reach every tenant database whatever this file says. What the
rule must not become is a `host` line instead of `hostssl`, or a wider CIDR —
either would put tenant roles, whose passwords the platform stores, within
reach of the rest of the network.

A rule added after the cluster is running needs `sudo systemctl reload
postgresql`; the restart above covers it at install time. Verify with

```bash
sudo -u postgres psql -c "SELECT line_number, error FROM pg_hba_file_rules WHERE error IS NOT NULL"
```

An empty result means the file parses. This is worth running rather than
assuming, because a *reload* keeps the previous configuration when the file is
malformed: the rule silently does not apply, and the broken line waits until
the next restart, which then refuses to start the server.

Then from the control plane:

```bash
cp-manage node register --name node-01 \
  --hostname node-01.example.com --internal-host 10.0.0.20
cp-manage node credential set --name node-01     # prompts for the DSN, without echo
cp-manage node backup-check --name node-01 --stanza maludb-node-01
cp-manage node realtime-check --name node-01   # only if Realtime is offered
cp-manage node pin set --node node-01 --extension vector --version 0.8.6
cp-manage node pin set --node node-01 --extension maludb_core --version 0.105.3
cp-manage node extension-check --name node-01
```

**`node credential set` is what makes the node usable**, and it reads the DSN —
`postgresql://maludb_provisioner:<strong>@10.0.0.20:5432/postgres?sslmode=require` —
from a prompt or a pipe, never an argument, which would land in shell history
and in `ps`. It connects first and stores nothing unless that works as a
superuser; it says so when the connection is not TLS. Without it every command
that reaches the node, and every project placed there, fails with *has no
provisioning credential configured*. Run it again to rotate the password.

Placement refuses a node without a **fresh health report** (five minutes), so the
node's reporter (2.5) must be running before the first project is created. It also refuses one
whose last reported free disk is below `--min-free-disk-bytes` (20 GiB unless set).

**Running `node register` again updates the node.** It sets the addresses and pool,
plus any of `--max-projects`, `--max-warm-projects` and `--min-free-disk-bytes`
given. The status, capacity settings not given, and what the checks recorded all
stay as they were, and the command says so. Before this, a re-run changed only the
addresses and silently dropped the capacity flags.

**It also refuses a node without extension pins**, and one not checked since it
was pinned (ADR-075). `node extension-check` exits non-zero and names the reason
when the node disagrees: a pin missing, a package providing another version, or
backends still running a replaced `vector.so` after an upgrade — restart the
node's workers, then check again. A pin must be a version
`specs/extension-versions.yaml` lists. **Upgrading a deployment to this release
stops placement on every existing node until it is pinned and checked**; run the
last three commands above for each node as part of the upgrade.

### 2.3 The gateway's own database role (ADR-072)

Not optional, and the reason is worth reading. The gateway is internet-facing,
runs here, and holds the KEK — it must, because verifying a tenant's JWT needs
that project's signing key on every request. If it also connects as the control
plane's own role it can complete `nodes.admin_dsn()` and recover **every node's
superuser DSN**.

This comes after registration (§2.2) rather than before it: the command names the node, so the node has to exist.

On the **control-plane** host:

```bash
sudo -u postgres psql -d maludb_control_plane \
  -c "CREATE ROLE gw LOGIN PASSWORD '<strong>'"
/opt/maludb/.venv/bin/python -m services.control_plane.manage \
  gateway grant --role gw --node <node-name>
```

It prints `nodes.admin_ciphertext/...: unreadable (ADR-072)` on success, and
exits non-zero naming the columns if not. Re-run it after any migration that
adds a table.

`--node` is the other half, and a gateway is broken without it. The row
policies decide what this role can see by resolving `current_user` through
`nodes.gateway_role`, so a role that is granted but not mapped matches **no
rows**: the process starts, connects, and answers 404 for every project on its
own machine. It fails in that direction on purpose — a gateway that sees
nothing is safe and one that sees the fleet is the finding this ADR exists for —
and the gateway refuses to start in production rather than leaving you to
diagnose it.

**One role per node.** The column is `UNIQUE`, so pointing one role at a second
node is refused rather than quietly widening what a compromise of either
machine reaches.

Then in the node's `/etc/maludb/gateway.env`:

```ini
MALUDB_GATEWAY_DATABASE_URL=postgresql://gw:<strong>@<control-plane>:5432/maludb_control_plane?sslmode=require
```

The control plane's PostgreSQL has to accept that connection, from the node's
address only. On the control-plane host:

```bash
echo "hostssl maludb_control_plane gw <node address>/32 scram-sha-256" \
  | sudo tee -a /etc/postgresql/17/main/pg_hba.conf
sudo systemctl reload postgresql
```

`listen_addresses` must include the control plane's private address, and changing
it needs a restart rather than a reload. The health reporter's role (2.5) needs
the same kind of line.

A production gateway whose role can still read those columns **refuses to
start**. The check is the privilege, not the variable — a gateway pointed at the
control plane's DSN works perfectly and is fully exposed, so checking that the
variable is set would pass exactly the deployment that must fail.

### 2.4 The gateway and its workers

The gateway runs from the repository, so the node gets the same checkout as the
control plane (1.1), and the same `kek` and `pepper` files at the same paths (1.2).
It wakes each project's PostgREST and GoTrue on demand, so those binaries, the
user they run as, and the units come first:

```bash
# Worker binaries, at the versions CI tests (.github/workflows/ci.yml).
curl -sL https://github.com/PostgREST/postgrest/releases/download/v14.17/postgrest-v14.17-linux-static-x86-64.tar.xz \
  | sudo tar -xJ --no-same-owner -C /usr/local/bin
curl -sL https://github.com/supabase/auth/releases/download/v2.195.0/auth-v2.195.0-amd64.tar.xz \
  | sudo tar -xJ --no-same-owner -C /usr/local/bin      # gotrue, and the migrations beside it

sudo useradd -r -s /usr/sbin/nologin maludb-api        # every worker runs as this
sudo useradd -r -s /usr/sbin/nologin maludb-gateway
for d in postgrest gotrue realtime; do
  sudo install -d -o maludb-gateway -g maludb-gateway -m 0700 /etc/maludb/$d
done

sudo cp deploy/maludb-gateway.service deploy/maludb-postgrest@.service \
        deploy/maludb-gotrue@.service /etc/systemd/system/
sudo install -m 0644 deploy/50-maludb-gateway.rules /etc/polkit-1/rules.d/
sudo systemctl restart polkit

sudo cp deploy/gateway.env.example /etc/maludb/gateway.env
sudo chmod 600 /etc/maludb/gateway.env
sudoedit /etc/maludb/gateway.env                  # the narrowed DSN from 2.3
sudo systemctl daemon-reload && sudo systemctl enable --now maludb-gateway
```

It binds `0.0.0.0:8110` — it is the public front door for tenant traffic — so
put TLS in front and have `*.example.com` resolve there.

**How the gateway is allowed to start workers.** It writes a project's config
into `/etc/maludb/<worker>/` (the only part of `/etc/maludb` its unit may write)
and runs `systemctl start maludb-postgrest@<ref>`. The polkit rule authorises
exactly that: the three worker templates, an instance shaped like a project ref,
and `start`, `stop` and `restart` — not `enable`, not any other unit. sudo is not
an option, because the unit's `NoNewPrivileges=true` makes it unusable.

**Workers never read another tenant's config.** The configs are `0600
maludb-gateway`, and every worker runs as the shared `maludb-api`; systemd reads
the file as root and hands the one worker its own copy (`LoadCredential=`).
Do not `chown` those directories to `maludb-api` to make a worker start.

### 2.5 The health reporter (ADR-080)

A small process on the node sends the health report placement needs: free disk
under the PostgreSQL data directory, every minute, **only while the local cluster
accepts connections**. Its control-plane role can call one function and read
nothing, and the database decides which node it reports for from the role.

On the **control-plane** host:

```bash
sudo -u postgres psql -d maludb_control_plane \
  -c "CREATE ROLE reporter_node01 LOGIN PASSWORD '<strong>'"
echo "hostssl maludb_control_plane reporter_node01 <node address>/32 scram-sha-256" \
  | sudo tee -a /etc/postgresql/17/main/pg_hba.conf && sudo systemctl reload postgresql
cp-manage node reporter grant --role reporter_node01 --node node-01
```

It prints `table privileges: none` on success. It refuses the gateway's role, the
control plane's, a memory worker's, and a role already reporting for another node:
one role per node.

On the **node**:

```bash
sudo useradd -r -s /usr/sbin/nologin maludb-reporter
sudo install -m 600 deploy/node-reporter.env.example /etc/maludb/node-reporter.env
sudoedit /etc/maludb/node-reporter.env        # reporter_node01's DSN, sslmode=require
sudo cp deploy/maludb-node-reporter.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now maludb-node-reporter
journalctl -u maludb-node-reporter -n 1       # reported health for node-01: free_disk_bytes=...
```

`cp-manage node list` then shows the node's health advancing each minute, and
`deploy preflight` names any active node without a reporter. A node whose
PostgreSQL stops answering stops reporting and leaves placement within five
minutes; nothing needs to mark it unhealthy. `cp-manage node health` still works
for a one-off report, and merges into what the realtime and backup checks recorded.

---

### 2.6 Sleeping idle workers (ADR-083)

The node's half of the maintenance pass. It stops each project's PostgREST, GoTrue and Realtime
worker after it has been idle -- fifteen minutes, an hour for Realtime -- which is what free-tier
density rests on (ADR-022). It runs **as `maludb-gateway` with the gateway's own database role**:
that role already sees and updates only this node's projects, and the polkit rule from §2.4 already
lets `maludb-gateway` stop exactly those units. It is given no KEK.

```bash
sudo cp deploy/maludb-node-maintenance.service deploy/maludb-node-maintenance.timer /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now maludb-node-maintenance.timer
journalctl -u maludb-node-maintenance -n 20
```

After upgrading to the release that adds it, **re-run the gateway grant** on the control plane
(`cp-manage gateway grant --role <role> --node <node>`): it takes `maintenance_runs` out of the
gateway's reach, so a node cannot make the control plane's pass look healthy. Preflight's
"node maintenance" fails for an active node with no run in ten minutes, and "gateway role" fails
while the old grant stands.

### 2.7 Storage: the object store and the shared worker (ADR-055, ADR-058, ADR-085)

Three pieces on the node, and one command on the control plane that fills in their credentials.
SeaweedFS keeps its unauthenticated services on the data address and puts only its S3 gateway on the
node's private address, because the control plane measures and deletes objects too (ADR-085). A
firewall table of its own admits S3 from the control plane and nothing else. `NODE` is the node's
private address and `CP` the control plane's.

**On the node** -- the data address, the store, and the rootless user the worker runs as:

```bash
# The data address, persistent (systemd-networkd drives Ubuntu Server's netplan already).
sudo cp deploy/10-maludb-data.netdev deploy/10-maludb-data.network /etc/systemd/network/
sudo networkctl reload && ip -br addr show maludb-data      # 10.91.0.1/32

# PostgreSQL admits the worker at the data address, as the metadata role and the per-project storage
# roles only. listen_addresses must include 10.91.0.1 (`*` does); changing it is a restart.
echo 'host all maludb_storage_meta,/^mldb_[a-z0-9]+_storage$ 10.91.0.1/32 scram-sha-256' \
  | sudo tee -a /etc/postgresql/17/main/pg_hba.conf && sudo systemctl reload postgresql

# SeaweedFS, pinned (the version and checksum scripts/storage-test-cluster.sh tests).
curl -sSL -o /tmp/weed.tar.gz \
  https://github.com/seaweedfs/seaweedfs/releases/download/4.41/linux_amd64.tar.gz
echo "730f1ede19972c12954ee407b2d97679a2e4486d24fd987d371761ec395571b8  /tmp/weed.tar.gz" | sha256sum -c -
sudo tar -xzf /tmp/weed.tar.gz -C /usr/local/bin weed && rm /tmp/weed.tar.gz

sudo useradd -r -s /usr/sbin/nologin maludb-objects
sudo install -d -o maludb-objects -g maludb-objects -m 0700 /etc/maludb/object-store
sudo install -m 0644 deploy/object-store.env.example /etc/maludb/object-store/object-store.env
sudoedit /etc/maludb/object-store/object-store.env           # MALUDB_OBJECT_STORE_S3_ADDRESS=NODE
sudo install -m 0600 deploy/object-store-firewall.nft /etc/maludb/object-store/firewall.nft
sudoedit /etc/maludb/object-store/firewall.nft               # define CONTROL_PLANE = CP

# The worker's user already exists (§2.4). Rootless Podman needs a home outside /home (the unit
# closes /home), a subordinate id range, and lingering, which gives it /run/user/<uid> and a user
# manager to delegate the container's memory limit to.
sudo install -d -o maludb-api -g maludb-api -m 0700 /var/lib/maludb-api
sudo usermod -d /var/lib/maludb-api maludb-api
sudo usermod --add-subuids 200000-265535 --add-subgids 200000-265535 maludb-api
sudo loginctl enable-linger maludb-api
sudo -u maludb-api -H sh -c 'cd / && XDG_RUNTIME_DIR=/run/user/$(id -u) \
  podman pull docker.io/supabase/storage-api:v1.70.6'
sudo install -d -o maludb-api -g maludb-api -m 0700 /etc/maludb/storage
```

**On the control plane** -- generate the S3 credential once, add the storage settings, and have the
control plane seal the node's storage root, create its metadata database, and print the node's two
credential files straight into place. They touch no disk but the node's and no terminal; the command
refuses to print to one.

```bash
# The MALUDB_STORAGE_* block from control-plane.env.example, in provisioner.env and NOT control-plane.env:
# the provisioner and the maintenance pass use it, and the public application must not hold a credential
# to every customer's files. The secret from `openssl rand -hex 24`, typed into the file.
sudoedit /etc/maludb/provisioner.env
sudo systemctl restart maludb-provisioner
# Then, from an operator machine that can ssh to both hosts:
prepare() {  # prints one file on stdout, from the control plane
  ssh CP "sudo bash -c 'cd /opt/maludb && set -a && . /etc/maludb/provisioner.env && set +a && \
    .venv/bin/python -m services.control_plane.manage node storage-prepare --name node-01 --print $1'"
}
prepare identities | ssh NODE \
  'sudo install -o maludb-objects -g maludb-objects -m 0600 /dev/stdin /etc/maludb/object-store/s3.json'
prepare env | ssh NODE \
  'sudo install -o maludb-api -g maludb-api -m 0600 /dev/stdin /etc/maludb/storage/storage.env'
```

**On the node** -- start both, then tell the gateway Storage exists:

```bash
sudo cp deploy/maludb-object-store.service deploy/maludb-storage.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now maludb-object-store
sudo nft list table inet maludb_object_store >/dev/null && echo firewall loaded
curl -s -o /dev/null -w '%{http_code}\n' http://NODE:8333/     # 403: S3 answers, unauthenticated
sudo -u maludb-objects weed shell -master=10.91.0.1:9333 <<< 's3.bucket.create -name maludb'
sudo systemctl enable --now maludb-storage
journalctl -u maludb-storage -f                 # until it logs that it is listening

sudoedit /etc/maludb/gateway.env    # MALUDB_STORAGE_DB_HOST=10.91.0.1, MALUDB_STORAGE_S3_ENDPOINT=http://NODE:8333
sudo systemctl restart maludb-gateway
```

Then `cp-manage deploy preflight` on the control plane, **with `provisioner.env` loaded**: "object store" passes when the bucket answers
from there and every active node has a sealed storage root. From anywhere else on the private network
`curl http://NODE:8333/` must time out.

**What this does not give you.** The store keeps **one copy** of every object on the node's disk
(SeaweedFS replication `000`). ADR-069 counts that a production failure; `cp-manage storage
durability` cannot see the master from the control plane and reports it as undeclared. Until the
backup slice covers the store, a lost disk loses customers' files outright. And the maintenance
pass's `storage_tenants` reconciliation needs the worker's admin port on node loopback, so from the
control plane it reports "not ready" and does nothing; a worker that loses its metadata database is
repaired by hand, not by the pass (ADR-085).

### 2.8 Backups: the node's recorder role (ADR-086)

pgBackRest runs on the node, as `postgres`, and records what it did through a role that can call
three functions for its own node and nothing else (`start_node_backup`, `finish_node_backup`,
`record_node_backup_check`) -- the health reporter's pattern (§2.5). The node holds no KEK and no
control-plane role that can write a table.

On the **control-plane** host:

```bash
sudo -u postgres psql -d maludb_control_plane \
  -c "CREATE ROLE backup_node01 LOGIN PASSWORD '<strong>'"
echo "hostssl maludb_control_plane backup_node01 <node address>/32 scram-sha-256" \
  | sudo tee -a /etc/postgresql/17/main/pg_hba.conf && sudo systemctl reload postgresql
cp-manage node backup-recorder grant --role backup_node01 --node node-01
```

It prints `table privileges: none` on success. It refuses a gateway, reporter, memory worker or
console role, and a role already recording for another node; the gateway and reporter grants refuse
a recorder in turn.

On the **node** -- the runner and its timers (the pgBackRest configuration and repositories are
slice 7c; until then a check records an unreachable or failing repository, which is the truth):

```bash
sudo install -m 600 deploy/node-backup.env.example /etc/maludb/node-backup.env
sudoedit /etc/maludb/node-backup.env        # backup_node01's DSN (sslmode=require) and the stanza
sudo cp deploy/maludb-node-backup@.service deploy/maludb-node-backup-{full,diff,check}.timer \
        /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl start maludb-node-backup@check      # once, now
journalctl -u maludb-node-backup@check -n 5        # recorded the repository check for node-01: ...
sudo systemctl enable --now maludb-node-backup-{full,diff,check}.timer
```

`check` runs pgBackRest's `check` and `info` and reads the repository options **on the node**, and
records the report; then, on the control plane, `cp-manage node backup-check --name node-01 --stanza
<stanza>` reads the cluster's settings over the node credential and joins that report. A node with a
recorder is never inspected from the control plane, whose filesystem would answer the co-location
question about the wrong host. Preflight's "node backups" fails a placeable node whose last check did
not pass, is over a week old, or whose repository report is over 26 hours old (rehearsal finding 21:
a recorded stanza *name* used to pass).

After upgrading to the release that adds it (migration 0058), **re-run the gateway grant**
(`cp-manage gateway grant --role <role> --node <node>`): it takes `node_backups` out of the gateway's
reach, so a gateway cannot mark its own node backed up. Preflight's "gateway role" fails while the old
grant stands.

## 3. The website

Five static files. `dev-server.py` is a development proxy and is **not**
deployed.

```
frontend/index.html  frontend/app.js  frontend/api.js  frontend/styles.css  frontend/docs.html
```

**Do not put the repository inside the document root.** Apache will serve
`/.git/config`, and the whole history is reconstructable from there. Clone
above the docroot and point at the subdirectory:

```apache
<VirtualHost *:443>
    ServerName example.com
    DocumentRoot /opt/maludb/frontend

    <Directory /opt/maludb/frontend>
        Require all granted
        AllowOverride None
        Options -Indexes +FollowSymLinks
    </Directory>

    <FilesMatch "\.(py|md)$">
        Require all denied
    </FilesMatch>

    # ES modules are refused otherwise. (Apache has no trailing comments: on the
    # directive's own line these words would be read as more extensions.)
    AddType text/javascript .js

    # Required once signups open. See below.
    ProxyPass        /api/ http://127.0.0.1:8112/
    ProxyPassReverse /api/ http://127.0.0.1:8112/
</VirtualHost>
```

`a2enmod proxy proxy_http` first. **Behind a TLS proxy on another host** (Nginx
Proxy Manager, a load balancer) the same block listens on `*:80` for that proxy
alone. Know what that costs before opening signups: the public app then sees
every request from 127.0.0.1, with the proxy's address as the last
`X-Forwarded-For` hop, so signup and sign-in rate limits cannot tell customers
apart. Not yet solved (`plans/active/deployment-rehearsal.md`, finding 19).

### The same-origin constraint

**There is no CORS middleware in the control plane.** The page and the API must
therefore share an origin:

| Works | Does not |
|---|---|
| `example.com` and `example.com/api/*` | `www.example.com` and `api.example.com` |

Split hostnames fail every browser call on CORS preflight, and it presents as a
broken frontend rather than a missing header. Decide before issuing certificates.

While signups are closed the page makes **no API calls at all** — pricing is
rendered from a bundled copy — so the site can be published before the control
plane exists.

### Opening signups

Two lines in `index.html`:

```html
window.MALUDB_TURNSTILE_SITE_KEY = "0x4AAA...";  // must match the control plane's secret
window.MALUDB_SIGNUPS_OPEN = true;
```

Do not open signups before a node is registered and healthy. Signup itself will
succeed and the customer's first action — creating a project — will answer 503.

---

## 4. Billing

Off unless configured, and every other route works without it.

```bash
MALUDB_STRIPE_SECRET_KEY=sk_live_...
MALUDB_STRIPE_WEBHOOK_SECRET=whsec_...
cp-manage billing price set --plan starter --price price_...
cp-manage billing status
```

**Nothing else maps a plan to a price**, and a plan without one cannot be
bought — checkout answers 409 naming it. The webhook records what was paid for;
`cp-manage maintenance run` is what applies it (ADR-053), so schedule it --
about every minute. Every run is recorded in `maintenance_runs`, and
`deploy preflight` fails a deployment whose pass has never finished or has not
finished in fifteen minutes. **Which host runs it is not yet settled**
(`docs/OPEN-QUESTIONS.md`): it needs the full control-plane database and the
KEK, and its idle-worker pass drives systemd units that live on the node.

---

## 5. Before announcing

Start with the command, then do the things it cannot see:

```bash
cp-manage deploy preflight
```

Exit 0 is clean, 1 has failures, 2 is ready with advisories worth reading. It
checks the plan catalogue, the gateway domain, node placeability and backup
stanzas, the ADR-072 gateway role, and billing. It **cannot** prove the internal
listener is unreachable, that DNS resolves, or that a certificate is valid --
those are properties of the network, and the list below is how they get checked.


- [ ] `curl https://<internal-host>:8111/healthz` from **outside** the private
      network fails to connect. This is the ADR-037 property and the only way to
      confirm it is from outside the machine.
- [ ] `cp-manage plans list` shows the plans you intend to sell.
- [ ] `cp-manage node list` shows the node active with fresh health.
- [ ] `cp-manage gateway grant --role gw --node <node>` exits 0, reports the
      admin columns unreadable, and names the node whose projects are the only
      rows that role can see.
- [ ] `cp-manage billing status` reports the deployment can take money, if it
      should.
- [ ] A real signup through the real site reaches a project that becomes ACTIVE.
- [ ] `cp-manage control-plane backup --path /var/backups/cp.dump` runs, and
      its output is stored somewhere that is not the control-plane host.
      Store the KEK separately: ADR-070 makes a dump without its keys not a
      backup, and the control plane refuses to start rather than mint new ones.

## What this deployment does not give you

Stated so it is a known position rather than a bad afternoon.

- **One node is a single point of failure.** Phase 11 gives per-tenant restore
  and a tested backup path. It does not give failover. A node outage is an
  outage for every tenant on it.
- **Multi-node is not supported.** See the topology section.
- **A gateway can still read another node's project rows.** ADR-072 closed the
  fleet-wide half — a compromised gateway can no longer recover any node's
  superuser DSN — but narrowing it to its own node's rows needs a node identity
  the gateway does not have yet. Irrelevant with one node; a prerequisite for
  the second.
