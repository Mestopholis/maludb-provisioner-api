# MaluDB features

What a MaluDB project can do that a Supabase project cannot, and how to use it.
Everything here is **opt-in** and **extends** the Supabase-compatible surface:
turning a feature on adds to what your project serves and changes none of it.
That is measured, not promised — the published description of your `public`
schema is identical before and after (`specs/compatibility-matrix.yaml`,
`maludb_extensions`).

Decisions behind this page: ADR-074 and ADR-077. Customer-visible limits: your plan.

## The data-model graph

A map of your database's own structure: every table, view and function in
`public`, how they relate — foreign keys, views built on tables, functions that
read tables — and a full description of each relation's columns and keys. For
tooling that needs to understand a schema: documentation generators, ER
diagrams, code assistants, migration reviewers.

**It is a copy, as of the last refresh.** The platform builds it on request and
stores the result; it does not follow your schema live. Every row says when it
was taken (`refreshed_at`). **Refresh after a migration** if what reads it needs
to see the change.

### Turn it on

An organization **owner or admin**:

```bash
curl -X POST https://api.maludb.com/v1/projects/<ref>/maludb/datamodel/enable \
  -H "Authorization: Bearer <personal access token>"
```

`202 Accepted` means it is queued; `200` means it was already on. Enabling
builds the graph and takes its first copy, which for a schema of a few hundred
tables takes a few seconds. Check progress with the status route below.

It may be refused, with a sentence saying why:

| Answer | Why | What to do |
|---|---|---|
| `403` | your plan does not include it | every plan does by default; ask for it |
| `409` | the project is not active | wait for it to finish provisioning |
| `429` | your plan's hourly budget is spent (below) | wait for `Retry-After` |
| a failed job naming `maludb` or `maludb_memory` | your database already has a schema by that name | rename or drop it, then enable again — both names are reserved |
| a failed job naming an extension version | your project's node needs an upgrade first | contact support |

### Turn it off

An organization **owner or admin**:

```bash
curl -X POST https://api.maludb.com/v1/projects/<ref>/maludb/datamodel/disable \
  -H "Authorization: Bearer <personal access token>"
```

`202 Accepted`, or `200` if it was already off. **Turning it off withdraws the
graph; it deletes nothing.** The `maludb` schema stops being served — reads are
refused by name again within a few seconds — and refreshes are refused, but the
copy and everything behind it stay in your database. Enabling again rebuilds on
what is already there; that is a normal enablement, so it takes its few seconds
and counts against your budget.

- **It does not count against your budget**, and it works even if your plan no
  longer includes the feature, or the project is paused or suspended: switching
  something off should never depend on being allowed to switch it on.
- Nothing you read before is changed in `public`, before, during or after.

### Refresh it

Any **member** of the organization:

```bash
curl -X POST https://api.maludb.com/v1/projects/<ref>/maludb/datamodel/refresh \
  -H "Authorization: Bearer <personal access token>"
```

`202 Accepted`, queued. If a refresh is already waiting, your request **joins
it** (`"coalesced": true`) and costs nothing extra. If one is already *running*,
yours queues behind it — the running one may have started before your latest
migration.

### What your plan allows

Refreshes an hour, counted over the trailing hour:

| Plan | Refreshes an hour |
|---|---|
| Free | 6 |
| Starter | 30 |
| Production | 120 |

- **Enabling counts** against the same budget: it does everything a refresh does.
  So does enabling vector compartments (below) — this is your project's budget
  for MaluDB work the platform does on request.
- **A request the platform refused counts** — a reserved schema name, say —
  because it was your request that could not proceed.
- **A request that failed on the platform's side does not count.**
- Over the budget, the answer is `429` with a `Retry-After` header saying when the
  next one is allowed. Nothing is queued to run later.

### Check on it

```bash
curl https://api.maludb.com/v1/projects/<ref>/maludb/datamodel \
  -H "Authorization: Bearer <personal access token>"
```

Whether it is on, the schema version it was built with, your budget and how much
of it the last hour used, and the latest enable and refresh jobs — including
`detail` when one failed.

### Read it

With the official Supabase client, using your project's **secret key**, from
your own server:

```js
import { createClient } from '@supabase/supabase-js'

const supabase = createClient('https://<ref>.maludb.com', process.env.MALUDB_SECRET_KEY)

// Every relation, with its full description.
const { data: relations } = await supabase
  .schema('maludb')
  .from('datamodel_relations')
  .select('relation_name, kind, description, refreshed_at')

// How things connect, with both ends named.
const { data: edges } = await supabase
  .schema('maludb')
  .from('datamodel_edges')
  .select('relationship, source:datamodel_nodes!source_node_id(name), target:datamodel_nodes!target_node_id(name)')
```

| Table | One row per | Columns |
|---|---|---|
| `datamodel_relations` | table, view, materialized view, foreign table | `schema_name`, `relation_name`, `kind`, `description` (columns, primary key, foreign keys in and out), `refreshed_at` |
| `datamodel_nodes` | table, view, function, and the schema itself | `node_id`, `node_type` (`db_table`, `db_view`, `db_routine`, `db_schema_ns`), `name`, `refreshed_at` |
| `datamodel_edges` | relationship | `source_node_id`, `relationship` (`fk_references`, `depends_on`, `reads`, `belongs_to`, …), `target_node_id`, `provenance`, `refreshed_at` |

**Only the secret key can read it.** The publishable key, and a signed-in user's
token, are refused with `42501 permission denied for schema maludb`. This is
deliberate: the description of a table is given regardless of whether a caller
could read that table, so the graph describes tables your end users are not
allowed to see. Keep it on the server.

### Things to know

- **Just after enabling**, a read can be refused as *not enabled* for up to about
  five seconds while the platform's edge catches up.
- **What is left out:** anything installed by a PostgreSQL extension. MaluDB's
  own functions live in `public`, and without this filter a three-table project's
  graph would be mostly them. Your own functions are kept — including one that
  shares its name with an extension's.
- **Only `public` is mapped.** Other schemas are not in the graph.
- **Reading it does not wake anything expensive.** It is ordinary table reads
  through your Data API, counted against your API limits like any other.
- **Nothing here is live.** There is no "describe this table now" call; refresh,
  then read.

## Vector compartments

MaluDB's own vector store: named compartments of embeddings, each with a fixed
dimension and distance metric, searched exactly and filtered by metadata. You
bring the embeddings — from whichever model you use — as ordinary number arrays;
the platform stores and searches them.

**Server-side only.** Only your **secret key** (`service_role`) can call these
functions. A compartment has no row-level security, so anything that can search
it can read every chunk in it. For search that a signed-in user runs in the
browser, with RLS deciding what each user sees, use **pgvector on your own
tables** — it works on every plan, exactly as it does on Supabase
(`match_documents` and all).

### Turn it on

An organization **owner or admin**:

```bash
curl -X POST https://api.maludb.com/v1/projects/<ref>/maludb/vectors/enable \
  -H "Authorization: Bearer <personal access token>"
```

`202 Accepted` means it is queued; `200` means it was already on. It draws on the
same hourly budget as the data-model graph. It may be refused with `403` (your
plan does not include it), `409` (the project is not active), `429` (budget
spent), or a failed job naming `maludb` or `maludb_private` — schema names the
platform reserves; rename or drop yours and enable again.

**Turn it off** the same way with `/maludb/vectors/disable`. The functions stop
being served, and **nothing is deleted**: your compartments and vectors stay, and
enabling again finds them.

**Check on it** with `GET /v1/projects/<ref>/maludb/vectors`: whether it is on,
your plan's limits, and the latest enable and disable jobs. How many vectors you
have stored is `vector_compartments()`, below.

### Use it

With the official client and your **secret key**, on your server:

```js
const supabase = createClient('https://<ref>.maludb.com', '<secret key>')
const where = { namespace: 'docs', subject: 'page', verb: 'about' }

await supabase.schema('maludb').rpc('vector_compartment_create', { ...where, dimensions: 1536 })

await supabase.schema('maludb').rpc('vector_insert', {
  ...where, content: 'How to reset a password', embedding, metadata: { lang: 'en' },
})

const { data } = await supabase.schema('maludb').rpc('vector_search', {
  ...where, query: queryEmbedding, match_count: 5, filter: { lang: 'en' },
})
// [{ id, content, metadata, similarity, distance }, ...] nearest first
```

A compartment is named by three strings — `namespace`, `subject`, `verb` — which
you choose.

| Function | What it does |
|---|---|
| `vector_compartment_create(namespace, subject, verb, dimensions, metric)` | creates a compartment; `metric` is `cosine` (default), `l2` or `inner_product`. The same definition again returns the existing one. |
| `vector_insert(namespace, subject, verb, content, embedding, metadata)` | stores one vector; returns its `id` |
| `vector_insert_many(namespace, subject, verb, items)` | stores up to 1,000 `{content, embedding, metadata}` at once |
| `vector_search(namespace, subject, verb, query, match_count, filter)` | nearest first; `match_count` 1–1,000, default 10; `filter` keeps rows whose metadata contains it |
| `vector_delete(namespace, subject, verb, ids)` | deletes by `id`; returns how many |
| `vector_compartment_delete(namespace, subject, verb)` | deletes a compartment and everything in it |
| `vector_compartments()` | every compartment, with its dimensions, metric and vector count |
| `vector_explain(namespace, subject, verb)` | how a compartment is searched |

### What your plan allows

| Plan | Vectors | Dimensions | Compartments |
|---|---|---|---|
| Free | 10,000 | 1,536 | 10 |
| Starter | 50,000 | 1,536 | 50 |
| Production | 100,000 | 3,072 | 200 |

Vectors count across all your compartments. Stored vectors also count against
your database storage — about 8.5 KB each at 1,536 dimensions.

### Errors

| `code` | Meaning |
|---|---|
| `PT403` | a plan limit; `hint` names it (`vector_max_count`, `vector_max_dimension`, `vector_max_compartments`) |
| `PT400` | a missing or invalid argument |
| `PT404` | no such compartment |
| `PT409` | a compartment by that name already exists with other dimensions or metric |
| `42501` | the key is not your secret key |

### Things to know

- **Search is exact**, not approximate: it compares against every vector in the
  compartment, so its time grows with the compartment's size. Your plan's limits
  keep that bounded. For approximate search over very large sets, use pgvector's
  HNSW indexes on your own tables.
- **Deleting is immediate** and frees the space against your limit.
- **Moves and restores keep your vectors.** The platform carries them explicitly;
  a point-in-time restore brings back the vectors as of that time.

## What else MaluDB will offer

The memory pipeline and the knowledge graph are later Phase 12 surfaces, each
decided and documented before it ships. Nothing on this page depends on them.
