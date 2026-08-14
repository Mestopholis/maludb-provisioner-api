# MaluDB Platform

MaluDB Platform is a managed database platform designed as a lower-cost, production-oriented alternative for applications currently built on Supabase.

The initial product strategy is:

1. Provide a Supabase-compatible developer surface.
2. Make migration from Supabase low-friction.
3. Run each customer project as its own PostgreSQL/MaluDB database on an existing shared MaluDB node.
4. Add MaluDB-specific memory/database capabilities without breaking Supabase compatibility.

## Repository purpose

This repository is intentionally structured so that human developers, OpenAI Codex, and Claude Code can all work from the same project knowledge.

- `AGENTS.md` — canonical agent working agreement.
- `CLAUDE.md` — thin Claude Code adapter that imports `AGENTS.md`.
- `PLANS.md` — execution-plan rules.
- `docs/` — product and architecture decisions.
- `specs/` — machine-readable or implementation-oriented specifications.
- `tasks/` — phased implementation scopes and acceptance criteria.
- `plans/` — active/completed execution plans.
- `tests/compatibility/` — eventual black-box Supabase compatibility suite.

## Product north star

An existing Supabase application should eventually be able to switch its project URL and API key to MaluDB and continue operating with minimal or no application-code changes for supported features.

Example target experience:

```javascript
import { createClient } from '@supabase/supabase-js'

const client = createClient(
  'https://<project-ref>.maludb.com',
  '<maludb-publishable-key>'
)
```

The first implementation milestone is intentionally narrower: prove that the official Supabase JavaScript client can perform supported CRUD/RPC operations against a MaluDB tenant database through the MaluDB API layer.

## Important status

This is a planning/scaffolding repository. It deliberately does not choose a control-plane programming language, billing provider, Redis implementation, object-storage provider, or final production limits until those decisions are made explicitly.

See `docs/DECISIONS.md` and `docs/OPEN-QUESTIONS.md`.
