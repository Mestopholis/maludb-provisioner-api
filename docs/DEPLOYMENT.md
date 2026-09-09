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

---

## 2. Node

### 2.1 PostgreSQL and extensions

```bash
sudo apt-get install -y postgresql-17 postgresql-17-wal2json pgbackrest podman
```

`wal2json` fails **silently** if missing: a client subscribes to Postgres
Changes successfully and no event is ever delivered, arriving as a ten-second
timeout that names neither the plugin nor the package.

Realtime additionally needs `wal_level = logical` (a restart) and ADR-031's
`pg_hba.conf` reject of physical replication — without which the first project
to enable Realtime holds a role that can take a byte-level copy of **every**
tenant database on the cluster.

### 2.2 The gateway's own database role (ADR-072)

Not optional, and the reason is worth reading. The gateway is internet-facing,
runs here, and holds the KEK — it must, because verifying a tenant's JWT needs
that project's signing key on every request. If it also connects as the control
plane's own role it can complete `nodes.admin_dsn()` and recover **every node's
superuser DSN**.

On the **control-plane** host:

```bash
sudo -u postgres psql -d maludb_control_plane \
  -c "CREATE ROLE gw LOGIN PASSWORD '<strong>'"
/opt/maludb/.venv/bin/python -m services.control_plane.manage gateway grant --role gw
```

It prints `nodes.admin_ciphertext/...: unreadable (ADR-072)` on success, and
exits non-zero naming the columns if not. Re-run it after any migration that
adds a table.

Then in the node's `/etc/maludb/gateway.env`:

```ini
MALUDB_GATEWAY_DATABASE_URL=postgresql://gw:<strong>@<control-plane>:5432/maludb_control_plane
```

A production gateway whose role can still read those columns **refuses to
start**. The check is the privilege, not the variable — a gateway pointed at the
control plane's DSN works perfectly and is fully exposed, so checking that the
variable is set would pass exactly the deployment that must fail.

### 2.3 Register it

From the control plane:

```bash
cp-manage node register --name node-01 \
  --hostname node-01.example.com --internal-host 10.0.0.20
cp-manage node backup-check --name node-01 --stanza maludb-node-01
cp-manage node realtime-check --name node-01   # only if Realtime is offered
```

Placement refuses a node without a **fresh health report**, so whatever records
health must be running before the first project is created.

### 2.4 The gateway

```bash
sudo useradd -r -s /usr/sbin/nologin maludb-gateway
sudo cp deploy/maludb-gateway.service /etc/systemd/system/
sudo cp deploy/gateway.env.example /etc/maludb/gateway.env
sudo chmod 600 /etc/maludb/gateway.env
sudoedit /etc/maludb/gateway.env                  # the narrowed DSN from 2.2
sudo systemctl daemon-reload && sudo systemctl enable --now maludb-gateway
```

It binds `0.0.0.0:8110` — it is the public front door for tenant traffic — so
put TLS in front and have `*.example.com` resolve there.

The gateway starts and stops the per-project worker units. Grant that with a
polkit rule or a sudoers entry scoped to the `maludb-postgrest@`,
`maludb-gotrue@` and `maludb-realtime@` templates, **not** blanket `systemctl`.

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
`cp-manage maintenance run` is what applies it (ADR-053), so schedule it.

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
- [ ] `cp-manage gateway grant --role gw` exits 0 and reports the admin columns
      unreadable.
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
