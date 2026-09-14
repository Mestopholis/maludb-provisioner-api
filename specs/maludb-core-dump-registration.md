# maludb_core dump registration — what each table needs (ADR-078)

Registration slice 0 of `plans/active/maludb-core-dump-registration.md`. Measured
2026-09-14 on a fresh database with `maludb_core` 0.104.0: every table the
extension owns, its rows at install, the markers on those rows, which roles and
functions write it, and its triggers.

## Summary

| Class | Tables | Registration |
|---|---|---|
| Customer and runtime data | 140 | registered, **no filter** |
| Mixed, marked by `owner_schema` | 4 (+3 child tables) | registered, filter excludes `owner_schema = 'maludb_core'` |
| Mixed, marked by `system_defined` | 2 | registered, `WHERE NOT system_defined` |
| Mixed, **no marker**, writable by MaluDB admin roles | 3 | upstream adds `system_defined`, then as above |
| Catalogues only a superuser can write | 3 | **not registered** |
| Per-database secrets generated at install | 2 | **not registered** — decided by the owner (below) |

Every sequence owned by a registered table is registered with it.

## 1. Customer and runtime data — 140 tables, no filter

Empty at install, so every row in them was written after it: register without a
filter. This includes queues, caches and derived data (`malu$ann_index`, the
embedding queue); carrying derived rows is harmless and cheaper than rebuilding
them.

`malu$account`, `malu$account_role`, `malu$active_memory_pool`, `malu$active_memory_pool_access`, `malu$active_memory_pool_member`, `malu$active_memory_pool_tag`, `malu$ann_delta`, `malu$ann_index`, `malu$attribute_template`, `malu$auth_token`, `malu$auth_token_use`, `malu$backup_manifest`, `malu$backup_verification`, `malu$bound_prompt`, `malu$budget_policy`, `malu$chat_index_append_audit`, `malu$chat_index_tree`, `malu$chat_message`, `malu$chat_session`, `malu$claim`, `malu$community`, `malu$community_membership`, `malu$derivation_ledger`, `malu$document`, `malu$document_svpor_hint`, `malu$document_tag`, `malu$document_type`, `malu$embedding_adapter`, `malu$embedding_dirty`, `malu$embedding_job`, `malu$embedding_output`, `malu$embedding_space`, `malu$enabled_schema`, `malu$enabled_schema_object`, `malu$episode_object`, `malu$episode_replay`, `malu$episode_type`, `malu$event`, `malu$event_delivery`, `malu$event_subscription`, `malu$fact`, `malu$fact_claim`, `malu$index_migration`, `malu$ingest_extraction`, `malu$ingestion_checkpoint`, `malu$ingestion_connector`, `malu$jwt_signing_key`, `malu$legal_hold`, `malu$lifecycle_policy`, `malu$listener_config`, `malu$local_memory_node`, `malu$local_model_capability`, `malu$log_drain`, `malu$log_drain_run`, `malu$maut_score`, `malu$maut_weight`, `malu$mc2db_invocation`, `malu$mc2db_prompt`, `malu$mc2db_resource`, `malu$mc2db_tool_http_endpoint`, `malu$memory`, `malu$memory_detail_object`, `malu$memory_extraction`, `malu$memory_extraction_config`, `malu$model_alias`, `malu$model_provider`, `malu$model_registry`, `malu$model_request`, `malu$model_response`, `malu$node_conflict_record`, `malu$node_sync_record`, `malu$object_embedding`, `malu$object_grant`, `malu$page_index_tree`, `malu$partition`, `malu$payload_schema`, `malu$pending_claim`, `malu$pool_presence`, `malu$pool_presence_event`, `malu$preview_env`, `malu$preview_env_seed`, `malu$prompt_render`, `malu$prompt_template`, `malu$prompt_variable`, `malu$query_hint`, `malu$queue`, `malu$queue_job`, `malu$queue_lease`, `malu$raw_ingest`, `malu$reinforcement_event`, `malu$relationship_edge`, `malu$rest_invocation`, `malu$retrieval_decision_audit`, `malu$retrieval_envelope`, `malu$role`, `malu$schedule`, `malu$schedule_run`, `malu$secret`, `malu$secret_use`, `malu$secret_version`, `malu$semantic_edge`, `malu$session`, `malu$session_context`, `malu$skill_access`, `malu$skill_embedding`, `malu$skill_execution_record`, `malu$skill_execution_step`, `malu$skill_file`, `malu$skill_keyword`, `malu$skill_package`, `malu$skill_state`, `malu$skill_subject`, `malu$skill_transition`, `malu$skill_verb`, `malu$source_object`, `malu$source_object_reference`, `malu$source_package`, `malu$source_verification`, `malu$storage_adapter`, `malu$structure_pass_audit`, `malu$supersession_edge`, `malu$svpor_attribute`, `malu$svpor_predicate`, `malu$svpor_statement`, `malu$svpor_subject`, `malu$svpor_subject_relationship_edge`, `malu$svpor_verb`, `malu$vector_chunk`, `malu$vector_compartment`, `malu$vector_demo`, `malu$vector_index_status`, `malu$vector_subject`, `malu$vector_tombstone`, `malu$vector_verb`, `malu$verbatim_archive`, `malu$workflow_candidate`, `malu$workflow_cluster`, `malu$workflow_cluster_member`, `malu$workflow_step`, `malu$workflow_trace`.

## 2. Mixed, marked by `owner_schema`

Installed rows carry `owner_schema = 'maludb_core'` (the schema current when the
extension script ran); a customer's rows carry the schema they were written from.

| Table | Installed | Filter |
|---|---|---|
| `malu$audit_event` | 35 (`rest_endpoint_register` at install) | `owner_schema <> 'maludb_core'` |
| `malu$rest_endpoint` | 35 | `owner_schema <> 'maludb_core'` |
| `malu$mc2db_server` | 2 | `owner_schema <> 'maludb_core'` |
| `malu$mc2db_tool` | 44 | `owner_schema <> 'maludb_core'` |
| `malu$mc2db_tool_sql_function`, `_external_exec`, `_mcp_proxy` | 42, 1, 1 | `tool_id IN (SELECT tool_id FROM maludb_core."malu$mc2db_tool" WHERE owner_schema <> 'maludb_core')` |

Two caveats the upstream PR must state:

- **A customer's change to an installed row is not carried** — disabling a
  built-in endpoint (`rest_disable_endpoint`), say. The restored database gets the
  new install's row. Acceptable for built-ins, and to be said in upstream docs.
- **`owner_schema = 'maludb_core'` is not always "installed".** Anything that
  writes with `maludb_core` first on its path gets it too — the platform's vector
  wrappers do (compartments slice 0, finding 3). None of those writes reach these
  four tables today, and the acceptance test must fail if one ever does.

## 3. Mixed, marked by `system_defined`

| Table | Installed | Filter |
|---|---|---|
| `malu$svpor_subject_type` | 25, all `system_defined` | `NOT system_defined` |
| `malu$svpor_verb_type` | 30, all `system_defined` | `NOT system_defined` |

## 4. Mixed, no marker — upstream adds one

Writable by MaluDB's admin roles (`maludb_memory_admin`, `maludb_llm_admin`,
`maludb_llm_model_admin`), so a customer row can exist, and nothing on an
installed row says it was installed.

| Table | Installed | Writable by |
|---|---|---|
| `malu$metric_definition` | 17 | `maludb_memory_admin` |
| `malu$safety_policy` | 4 | `maludb_llm_admin` |
| `malu$retry_policy` | 1 | `maludb_llm_admin`, `maludb_llm_model_admin` |

The upgrade script adds `system_defined boolean NOT NULL DEFAULT false`, sets it
true on the installed rows (by their known keys), and registers with
`NOT system_defined`. Chosen over a key-range filter, which a later upgrade
script adding a built-in row after customer rows would silently break.

## 5. Catalogues only a superuser can write — not registered

`malu$object_type` (31), `malu$relationship_type` (16), `malu$source_type` (12):
`INSERT` and `UPDATE` are held by the installing superuser alone and no extension
function writes them. Every row is the extension's, and the new install brings
its own.

## 6. Per-database secrets — not registered

| Table | Installed | What depends on it |
|---|---|---|
| `malu$secret_master_key` | 1 (`key_material`) | every secret in MaluDB's in-database secret store |
| `malu$auth_pepper` | 1 (`pepper`) | verification of MaluDB's in-database auth tokens (`auth_token_*`) |

Registering either puts that database's key in every dump. Not registering either
makes a moved or restored tenant unable to read its MaluDB secrets or verify its
MaluDB auth tokens. The platform uses neither store today (ADR-023; its API keys
are the control plane's), and no customer role can reach them.

**Decided 2026-09-14 by the repository owner: neither is registered.** A dump never
holds a key, and each restored database keeps the key its own install generated.
What that means for the rows that depend on them, which *are* registered as
customer data (class 1):

- `malu$secret_version.value_encrypted` arrives, and cannot be decrypted with the
  target's master key.
- `malu$auth_token.token_hash` arrives, and does not verify against the target's
  pepper.
- `malu$jwt_signing_key` holds public keys only (`public_jwk`) and is unaffected.

The upstream pull request documents that MaluDB's in-database secret store and
auth tokens do not survive `pg_dump`. Revisit if the platform adopts either.

## Triggers that fire during `pg_restore`

An extension's triggers are created by `CREATE EXTENSION`, before `pg_restore`
loads data, so unlike a customer's own triggers they **fire on every restored
row**. Twelve tables have them:

| Effect on restore | Triggers |
|---|---|
| Validation, idempotent | `_payload_validate_{claim,fact,memory,mdo,episode,source_package}`, `_svpor_{subject,verb}_normalize_type_tg` |
| Writes other rows — **duplicates what the dump already carries** | `_episode_subject_mint`, `_svpor_auto_resolve`, `_embedding_dirty_{episode,attribute,statement,subject,verb}_tg`, the two label-refresh triggers |
| **May refuse a restored row** | `_source_package_seal_lock`, `_skill_package_content_guard` |

Upstream cannot easily make twelve triggers restore-aware. The platform can avoid
them: `pg_restore` run with `PGOPTIONS='-c session_replication_role=replica'`
(the platform restores as a superuser) fires no ordinary trigger, which is what a
data load of already-consistent rows wants — the rows arrive as they left. **To be
verified in registration slice 2's acceptance test**, including that a customer's
own triggers in `public` are unaffected (they are created after the data load in
any case).

## Other properties checked

- No foreign key leaves the extension; no row-level security is forced (128
  tables enable it; a superuser dump and restore bypass it).
- 14 identity or generated columns: `pg_dump` omits generated columns from
  `COPY` and restores identity columns as written.
- No partitioned tables.
