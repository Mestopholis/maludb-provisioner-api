# MaluDB Platform Agent Instructions

This repository may be developed by multiple human developers using different coding agents, including OpenAI Codex and Claude Code.

The repository is the source of truth. Do not rely on previous chat sessions as authoritative project state.

## Read before significant work

For any substantial implementation task, read:

1. `docs/REQUIREMENTS.md`
2. `docs/ARCHITECTURE.md`
3. `docs/DECISIONS.md`
4. `docs/MALUDB.md` — what MaluDB actually is, and its verified platform constraints
5. the applicable file under `tasks/`
6. any referenced file under `specs/`

For multi-step work, also read `PLANS.md` and create or update an execution plan under `plans/active/`.

## Architectural invariants

Do not violate these without an explicit architecture decision:

- A customer project is a dedicated PostgreSQL/MaluDB database plus constrained roles inside an already-running shared MaluDB database cluster.
- Creating a customer project must not provision a VM or container.
- MaluDB/platform infrastructure owns tenant databases. Customers do not receive PostgreSQL superuser or database-owner privileges.
- Free projects are API-only. Direct PostgreSQL access is a paid capability.
- Free-to-paid upgrades normally retain the same physical tenant database.
- Supabase compatibility is the first public compatibility target.
- MaluDB-specific functionality must extend the Supabase-compatible surface, not silently alter or break it.
- Project API traffic is routed by project identity and validated against project-scoped API keys.
- PostgREST and Auth may begin as per-active-project processes/configurations. Do not replace them with a custom multi-tenant implementation unless a later decision explicitly authorizes it.
- Resource governance is defense in depth: gateway limits plus PostgreSQL/MaluDB limits plus node-level capacity management.
- Customer-controlled extensions and privileged SQL capabilities must be allowlisted.
- Never make an undocumented architectural change merely to simplify an implementation.

## Agent-neutral workflow

- Do not create separate Codex-only and Claude-only architecture.
- The `docs/`, `specs/`, `tasks/`, and `plans/` directories are canonical for every agent.
- `CLAUDE.md` is an adapter, not a second source of truth.
- Record durable decisions in `docs/DECISIONS.md`.
- Record unresolved product/architecture choices in `docs/OPEN-QUESTIONS.md`.
- If implementation discovers that a decision is infeasible, stop that line of implementation, document the conflict, and propose an ADR-style change rather than silently deviating.

## Local development

Control plane: Python 3.12, FastAPI, psycopg3, no ORM (ADR-024). Requires
PostgreSQL 17 and `uv`.

```bash
# 1. Dependencies
uv venv --python 3.12
uv pip install -e ".[dev]"

# 2. Development key material (ADR-023). .dev/ is gitignored -- never commit it.
mkdir -p .dev
openssl rand -hex 32 > .dev/kek
openssl rand -hex 32 > .dev/pepper
chmod 600 .dev/kek .dev/pepper

# 3. Control-plane database. This one is plain PostgreSQL: maludb_core belongs
#    in tenant databases (ADR-015), not here.
sudo -u postgres psql -c "CREATE ROLE cp_dev LOGIN PASSWORD 'devonly'"
sudo -u postgres psql -c "CREATE DATABASE maludb_control_plane_dev OWNER cp_dev"

# 4. Environment
export MALUDB_ENV=development
export MALUDB_CONTROL_PLANE_DATABASE_URL="postgresql://cp_dev:devonly@127.0.0.1:5432/maludb_control_plane_dev"
export MALUDB_KEK_REF=.dev/kek
export MALUDB_TOKEN_PEPPER_REF=.dev/pepper

# 5. Migrate and run
.venv/bin/python -m services.control_plane.migrate
.venv/bin/uvicorn --factory services.control_plane.main:create_app --reload --port 8111
```

Swagger UI is then at `http://127.0.0.1:8111/docs`. It is disabled in
production by default (ADR-024).

Checks, all of which CI also runs:

```bash
.venv/bin/ruff check .                                  # lint
.venv/bin/python -m pytest -q                           # tests
.venv/bin/python scripts/export-openapi.py --check      # OpenAPI drift
.venv/bin/python -m services.control_plane.migrate      # idempotent; re-run is a no-op
```

After changing any route, regenerate the contract — CI fails otherwise:

```bash
.venv/bin/python scripts/export-openapi.py
```

Migrations are immutable once applied. The runner rejects a file whose checksum
changed; add a new migration instead of editing an old one.

## Development rules

- Prefer small, reviewable phases over broad rewrites.
- Keep infrastructure behavior configuration-driven.
- Never hard-code production plan limits in application logic.
- Secrets must never be committed to the repository.
- API keys and database passwords must be generated cryptographically.
- Store secret API key material hashed where verification semantics permit.
- Tenant IDs/project refs must be treated as untrusted input.
- SQL identifiers generated from project metadata must be validated and/or safely quoted.
- Provisioning operations must be idempotent or safely retryable.
- Destructive provisioning/cleanup operations must require explicit state checks.

## Compatibility rules

When implementing a Supabase-compatible feature:

- Prefer the official upstream protocol/behavior over creating a MaluDB-specific alternative.
- Add a black-box compatibility test using the official Supabase client when practical.
- Test the same behavior against Supabase and MaluDB where practical.
- Document intentional incompatibilities in `specs/compatibility-matrix.yaml`.
- Do not claim full Supabase compatibility until the matrix and automated tests support the claim.

## Definition of done

A task is not complete until:

- acceptance criteria in the task file are met;
- tests pass;
- security/isolation implications are considered;
- affected docs/specs are updated;
- any new architecture decision is recorded;
- the active execution plan is updated;
- no secrets or environment-specific credentials are committed.

## Code review rules

Reviewers and agents should pay special attention to:

- cross-tenant data access;
- SQL injection through generated database/role/schema identifiers;
- bypasses of API-only free-tier restrictions;
- API key/project mismatch;
- privilege escalation;
- missing rate/concurrency controls;
- unsafe retry behavior in provisioning;
- secret leakage in logs;
- code that assumes one database per VM;
- code that grants database ownership or superuser privileges to customers.
