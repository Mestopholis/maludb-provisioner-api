# Cutover runbook: moving a Supabase project to MaluDB

Phase 08 slice 8. This is the sequence to run, the window to book, and the
things that go wrong. It assumes `maludb-migrate` from this repository and a
MaluDB project already created.

Read `docs/MIGRATION-FROM-SUPABASE.md` first for *what* moves and what does
not. This document is *when*, and what to check afterwards.

## The window you are booking

**Writes to your Supabase project must stop before the data copy and stay
stopped until you have verified.** MaluDB cannot enforce that — it is somebody
else's platform — so the tooling checks arithmetically afterwards whether the
freeze actually held, and `verify` names any table that took a write.

Measured rate, for sizing (ADR-044):

| | |
|---|---|
| Copy rate | **≈1.9 MiB/s**, ≈12,000 rows/s |
| Measured on | 1,000,000 rows / 160.5 MiB, single table, source and destination on one host |
| Verify, counts only | ≈700 MiB/s |
| Verify, `--digest` | ≈120 MiB/s |

So the freeze is roughly **9 minutes per GiB** to copy, plus a few seconds per
GiB to verify with counts, or about ten times that with `--digest`.

| Source size | Copy | Verify (counts) | Verify (`--digest`) |
|---|---|---|---|
| 100 MiB | ~1 min | seconds | seconds |
| 1 GiB | ~9 min | ~2 s | ~9 s |
| 10 GiB | ~90 min | ~15 s | ~85 s |
| 50 GiB | ~7.5 h | ~75 s | ~7 min |

**Why it is not faster, and why that is deliberate.** Rows move as multi-row
`INSERT` statements through the same public SQL route a dashboard uses
(ADR-039, ADR-042). `COPY` over a direct connection would be far quicker and
would require the migration tool to hold a privilege the console does not —
which is the thing slice 1's containment work exists to prevent, and which the
free tier has no connection for at all. The cost of that choice is this rate.
If your database is large enough that the table above is unacceptable, say so
before you schedule anything: that is an argument for a different transport,
not for a longer outage.

`scan` prints an estimate for your own database. Pass the rate you measured
rather than trusting the figure above, which is one setup's:

```bash
maludb-migrate scan --throughput-mb-per-s 1.9
```

## Before the window

Nothing here needs an outage. Do it days earlier.

1. **Scan.** It reads your Supabase project and reports what would block a
   migration — extensions outside the allowlist, Storage objects, OAuth
   identities, anything Phase 08 does not carry (ADR-043).

   ```bash
   export MALUDB_SOURCE_DSN='postgresql://postgres:...@db.<ref>.supabase.co:5432/postgres'
   export MALUDB_TOKEN='<your MaluDB platform token>'
   maludb-migrate scan
   ```

   Use a role that can read every row. On Supabase that is `postgres`. A
   purpose-made read-only role is the responsible instinct and it is the wrong
   one here: row-level security applies to it, and a filtered read is not an
   error — it is a smaller number. The tooling turns that into a refusal rather
   than a silent undercount, so you will see `42501` rather than a clean report,
   but only if you let it fail rather than working around it.

2. **Fix every blocker, then scan again.** A blocker found during a freeze is a
   rolled-back cutover.

3. **Migrate the schema only, into the real destination project.** Safe to do
   while your application is still serving from Supabase — it creates no rows.

   ```bash
   maludb-migrate apply --project-ref <ref>
   ```

   Then look at the destination. This is the rehearsal, and it is where you
   find out that a function you forgot about does not compile.

## The window

4. **Stop writes at the source.** Your application, your cron jobs, your
   webhooks, anything with the service-role key. Pausing the Supabase project
   is the blunt version and it works.

5. **Copy the data, and keep the receipt.**

   ```bash
   maludb-migrate apply --project-ref <ref> --with-data --with-auth \
       --receipt cutover-receipt.json
   ```

   The receipt records what each table held at the moment it was copied. Keep
   it: without it the verification in step 6 **cannot tell a source that kept
   taking writes from a copy that fell short**, which are opposite problems with
   opposite remedies.

6. **Verify, while the source is still frozen.**

   ```bash
   maludb-migrate verify --project-ref <ref> --receipt cutover-receipt.json --digest
   ```

   Exit code 0 means the destination matches. Non-zero means do not cut over.
   Run it *before* letting anything write to either database: once writes resume
   at the source the two diverge legitimately and every difference it reports is
   noise.

   `--digest` compares content rather than row counts. Row counts alone cannot
   see a table whose rows all arrived and were changed on the way in — a
   `handle_updated_at` trigger does exactly that, and it is common enough that
   the copy disables triggers to prevent it. The digest is what confirms the
   prevention worked.

7. **Point your application at MaluDB** and let it write.

8. **Keep the Supabase project, frozen, for as long as your rollback plan
   needs.** Do not delete it the same day.

## What `verify` reports, and what each answer means

| Status | What happened | What to do |
|---|---|---|
| `source_grew` | The source took writes after that table was copied. **The freeze did not hold.** | Find what is still writing, stop it, re-copy. Rows accepted by a database you are about to stop using are lost rows. |
| `short` (table) | The destination holds fewer rows than the source. | The copy did not finish. Re-run it. |
| `short` (sequence) | Every row arrived; the sequence behind an `id` column did not move. | Your first insert would collide with a migrated row. Re-run the copy, which advances sequences, or `setval` it by hand. |
| `content_differs` | Same number of rows, different content. | Something rewrote rows on the way in — usually a trigger. Investigate before cutting over; the counts will not show you this again. |
| `columns_differ` | The two tables do not have the same columns. | The schema did not fully arrive. Fix the schema, then re-copy that table. |
| `unreadable` | One side refused the read. | Usually row-level security on the *source*: use a role that can read every row. Never work around it by letting RLS filter — that reports a clean migration of a subset. |

A clean run says which questions it actually answered — whether it compared
content or only counts, and whether it could check the freeze at all. Read that
line. A verification that could not check the freeze looks exactly like one that
checked it and found nothing.

## Things that have actually gone wrong

- **A migration that stops on its first statement.** A Supabase project keeps
  its tables in `public`, `pg_dump` emits `CREATE SCHEMA "public"`, and your
  MaluDB project already has one. Fixed in slice 8 — but if you are running an
  older build, this is what that `42P06` is.
- **`COMMENT ON SCHEMA "public"` refused with `42501`.** Schema comments need
  ownership and your project's `public` is the platform's. The comment is
  dropped; nothing else is.
- **A clean report from a filtered read.** Zero rows equals zero rows. Use a
  role that can read everything, and let a `42501` stop you rather than
  degrading to a role that cannot.
- **A freeze nobody actually enforced.** A background worker with the
  service-role key does not stop when the web application does.

## What is not covered

Storage objects, OAuth/magic-link/MFA/SSO identities, Edge Functions, and
anything else outside `specs/compatibility-matrix.yaml` (ADR-043). The scanner
reports these as blockers before the window rather than leaving them to be
discovered inside it. Password hashes migrate where GoTrue's format allows;
where it does not, those users reset their passwords and the scan says so in
advance.
