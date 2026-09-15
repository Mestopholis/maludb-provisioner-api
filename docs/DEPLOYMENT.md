# Deployment

Two fresh machines to a customer signing up and a project serving traffic.

This document is the runbook. `plans/active/deployment-topology.md` is the plan
that produced it and `tasks/DEPLOYMENT.md` tracks what is still missing.

## What runs where, and what must not share a host

| Process | Role | Faces |
|---|---|---|
| control-plane **public** app | control plane | the internet, behind TLS |
| control-plane **internal** app | control plane | a private interface **only** |
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
sudo mkdir -p /opt/maludb /etc/maludb
sudo git clone https://github.com/Mestopholis/maludb-provisioner-api.git /opt/maludb
cd /opt/maludb
uv venv --python 3.12 && uv pip install -e .
# Installs the cp-manage, cp-migrate and maludb-gateway entry points used below.
```

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

`/etc/maludb/control-plane.env`:

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
sudo cp deploy/control-plane.env.example /etc/maludb/control-plane.env
sudo chmod 600 /etc/maludb/control-plane.env      # it carries a database password
sudoedit /etc/maludb/control-plane.env            # fill in every CHANGEME
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
sudo cp /etc/maludb/control-plane.env /etc/maludb/provisioner.env
sudo systemctl enable --now maludb-provisioner
```

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
refuses to bind a public address. Keep that port closed to everything except the nodes.

---

## 2. Node

### 2.1 PostgreSQL and extensions

```bash
sudo apt-get install -y postgresql-17 postgresql-17-wal2json pgbackrest podman
# vector at a version specs/extension-versions.yaml lists, then held (ADR-075):
sudo apt-get install -y postgresql-17-pgvector=0.8.6-1.pgdg24.04+1
sudo apt-mark hold postgresql-17-pgvector
```

**Hold `postgresql-17-pgvector`.** Every tenant on a node loads the same
`vector.so`, so a routine `apt upgrade` changes the code under all of them at
once. The platform does not install packages, but it notices: a node whose
packages disagree with its pin takes no new projects (2.2). Moving it later is a
procedure with an order — pin, package, workers, check, tenants — in
`docs/MALUDB.md`, "Changing a pin, in order".

`wal2json` fails **silently** if missing: a client subscribes to Postgres
Changes successfully and no event is ever delivered, arriving as a ten-second
timeout that names neither the plugin nor the package.

Realtime additionally needs `wal_level = logical` (a restart) and ADR-031's
`pg_hba.conf` reject of physical replication — without which the first project
to enable Realtime holds a role that can take a byte-level copy of **every**
tenant database on the cluster.

### 2.2 Register it

The control plane provisions by connecting to the node's PostgreSQL as a
superuser, so first let it in — **from the control-plane host's address only**,
over TLS. On the node:

```bash
sudo -u postgres psql -c "ALTER SYSTEM SET listen_addresses = 'localhost,10.0.0.20'"
sudo -u postgres psql -c "CREATE ROLE maludb_provisioner LOGIN SUPERUSER PASSWORD '<strong>'"
echo "hostssl all maludb_provisioner 10.0.0.10/32 scram-sha-256" \
  | sudo tee -a /etc/postgresql/17/main/pg_hba.conf
sudo systemctl restart postgresql@17-main        # listen_addresses needs a restart
```

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
MALUDB_GATEWAY_DATABASE_URL=postgresql://gw:<strong>@<control-plane>:5432/maludb_control_plane
```

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

## 3. The website

Four static files. `dev-server.py` is a development proxy and is **not**
deployed.

```
frontend/index.html  frontend/app.js  frontend/api.js  frontend/styles.css
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

    AddType text/javascript .js      # ES modules are refused otherwise

    # Required once signups open. See below.
    ProxyPass        /api/ http://127.0.0.1:8112/
    ProxyPassReverse /api/ http://127.0.0.1:8112/
</VirtualHost>
```

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
