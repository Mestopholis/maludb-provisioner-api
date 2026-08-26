# Phase 10 — Storage

## Objective

Implement the defined Supabase-compatible Storage subset using selected object storage.

## Scope

- Object-store provider abstraction.
- Buckets/object metadata.
- Upload/download/delete.
- Signed URLs.
- Authorization/RLS integration.
- Migration path from Supabase Storage.

## Acceptance criteria

- [x] Object bytes are outside tenant Postgres DB. *(A claim with two halves, and the
      second is the one a test gets wrong by omission: "the bytes are in the object store"
      does not by itself say they are not **also** in PostgreSQL.
      `test_storage_workers.py::test_the_object_keys_are_prefixed_by_tenant_in_one_platform_bucket`
      asserts both after a real upload through a real `storage-api` -- the key is in
      SeaweedFS under the tenant's prefix, read out of the store itself rather than
      inferred from the API's answers, **and** schema `storage` has no `bytea` or
      large-object column to put bytes in and stays at metadata scale. Slice 0 counted
      `bytea` columns once by hand against one build of one image
      (`specs/storage-server-model.md`); an upstream release that started inlining small
      objects would not have failed anything, which is why the negative is now a test.
      The provider abstraction this criterion implies is configuration rather than code
      and `docs/STORAGE.md` argues why: `STORAGE_BACKEND` and `STORAGE_S3_ENDPOINT`
      already exist, and provisioning makes no object-store API call at all -- a project
      can be created while the store is unreachable, which is what actually keeps the
      tenant lifecycle decoupled from the vendor.)*
- [x] Metadata/authorization behavior passes compatibility tests. *(19 cases in
      `tests/compat/storage.mjs` driving `@supabase/supabase-js` over the gateway against
      a real `storage-api` and two real provisioned tenants, asserted one per behaviour by
      `tests/test_storage_compat.py`. Seven matrix entries moved to `supported` with
      `verified_by`, and `AGENTS.md` does not permit the claim ahead of the test.
      Authorization is the half worth naming: an RLS policy on `storage.objects` admits a
      signed-in user and refuses an anonymous one **for the same object through the same
      client**, refuses a user signed in elsewhere, and hides the object from an anonymous
      `list` rather than merely from `download`. That works because ADR-062 makes the
      gateway mint a role token from an opaque key -- until slice 5 drove the real client
      at it, every anonymous Storage call on the platform was refused 403 before any
      policy was consulted, which is the whole free tier, and a full Python suite had
      passed with the defect in it. Three matrix entries are recorded as intentional
      incompatibilities (ADR-060) because a client meets them at runtime, and five as
      deferrals -- including `storage_policy_authoring`, which is the one gap in this box
      and is named rather than ticked past: policies are enforced and a customer still
      cannot write one.)*
- [x] Cross-project object access is denied. *(Tested as a denial rather than assumed,
      because ADR-057 puts every tenant's objects in one platform bucket -- so isolation is
      a property of the metadata layer and the worker's credential scoping, **not** of the
      object store. Two projects, `stcp0001` and `stcp0002`, each with its own hosts entry,
      because the hostname is the routing key (ADR-008) and the second project cannot be a
      variation of the first: each creates a bucket of the same name holding a key of the
      same name and reads back its own bytes; a client built from one project's key and
      the other's URL is refused 401; a signed URL issued by one is not served by the
      other. Below the client, `test_storage_workers.py` makes the same claim against the
      worker directly -- same bucket and key names on one shared instance, a token signed
      for one tenant reaching nothing of another's, and the storage role unable to reach
      another tenant's database at all (`test_object_storage.py`).
      **And the harness itself is tested**: five of the suite's negative cases, three of
      them isolation claims, once passed against a dead port, because
      `expect(error !== null)` is satisfied by a connection refused. Every negative case now
      goes through `assertRefused`, which requires a server status and fails a transport
      error explicitly; the count passing against a dead endpoint is 0. An isolation test
      that proves nothing looks exactly like one that proves everything.)*

## What this phase did not do

Recorded here rather than left to a reader of the matrix, because these are the
things a customer arriving from Supabase will look for:

- **Storage policies are enforced but cannot be authored by a customer**
  (ADR-061). `CREATE POLICY` requires ownership of `storage.objects` and the
  owner is a platform-internal role. Granting the tenant admin membership in it
  -- Supabase's own arrangement -- is owner-level bypass of every storage
  policy including the customer's own. The safe shape is a mediated surface
  that validates what it creates, which is a slice rather than a grant.
- **Signed upload URLs, resumable/TUS uploads, image transformation, and the S3
  protocol endpoint** are deferred, each with its reasoning in
  `specs/compatibility-matrix.yaml`.
