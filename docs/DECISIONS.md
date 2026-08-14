# Architecture Decisions

This file records accepted project-level decisions. New durable decisions should be appended rather than hidden in implementation details.

## ADR-001 — Product wedge is Supabase compatibility

Status: Accepted

MaluDB Platform will initially compete as a Supabase-compatible production alternative. MaluDB-native functionality is added after/alongside compatibility and must not break supported Supabase behavior.

## ADR-002 — Tenant unit is a database, not infrastructure

Status: Accepted

Each project gets its own PostgreSQL/MaluDB database and constrained roles on an already-running shared MaluDB cluster.

Project creation does not provision a VM or container.

## ADR-003 — Proxmox VMs are pre-provisioned MaluDB nodes

Status: Accepted

Platform administrators provision/manage MaluDB VMs separately. The control plane schedules projects onto these existing nodes.

## ADR-004 — Platform retains database ownership

Status: Accepted

Customers do not technically own the PostgreSQL database and do not receive superuser privileges. They receive constrained roles that provide only the product-supported capabilities.

## ADR-005 — Free tier is API-only

Status: Accepted

Free projects do not receive public direct PostgreSQL connection credentials. This prevents bypass of API-layer rate/concurrency/quota controls.

## ADR-006 — Paid upgrade normally retains the database

Status: Accepted

Free-to-paid upgrade changes entitlements/limits without requiring a database migration. Operational movement to another node/pool may occur later but is decoupled from purchase.

## ADR-007 — Per-project PostgREST/Auth processes are acceptable for MVP

Status: Accepted

For the initial implementation, each active project may have its own PostgREST and Auth configuration/process.

Free project API workers may sleep while inactive. The platform can revisit process density after measuring real usage.

## ADR-008 — Project URL plus project-scoped API key

Status: Accepted

Public APIs use a stable project-specific hostname. The gateway must verify that the submitted API key belongs to the project referenced by the hostname.

## ADR-009 — Resource governance is layered

Status: Accepted

Use gateway throttling/concurrency, small DB pools, PostgreSQL/MaluDB settings, storage quotas, node scheduling, and eventually native MaluDB resource governance.

## ADR-010 — Extensions are allowlisted

Status: Accepted

Customers cannot install arbitrary PostgreSQL extensions on a shared node.

## ADR-011 — Repository is agent-neutral

Status: Accepted

Codex and Claude Code must work from the same canonical docs/specs/tasks/plans. `CLAUDE.md` imports the shared `AGENTS.md`; agent-specific files must not contain competing architecture.
