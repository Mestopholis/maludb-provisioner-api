# Tenancy

## Tenancy unit

One project equals one PostgreSQL/MaluDB database.

A project is not:

- a VM;
- a container;
- a schema inside another customer's database.

## Shared-cluster model

Many project databases share one MaluDB/PostgreSQL cluster.

PostgreSQL roles are cluster-scoped, so all generated role names must be globally unique on that node.

## Ownership

The MaluDB platform owns the tenant database.

The customer receives constrained roles appropriate to the interface:

- API/service role(s);
- tenant-admin-like role for paid direct SQL access;
- Auth-related roles as required;
- no superuser;
- no database ownership;
- no arbitrary role creation;
- no arbitrary database creation.

## Isolation requirements

A tenant must not be able to:

- connect to another tenant database;
- inherit another tenant role;
- alter cluster-wide configuration;
- inspect secrets belonging to another tenant;
- execute untrusted server-side code;
- install arbitrary extensions;
- write arbitrary server files;
- terminate or inspect privileged sessions beyond allowed scope.

## Naming

Generated examples:

```text
database: mldb_<project_ref>
role:     mldb_<project_ref>_authenticator
role:     mldb_<project_ref>_auth
role:     mldb_<project_ref>_admin
```

Use a strict project-ref character set suitable for safe generated identifiers. Still quote identifiers correctly in SQL.

### Exception: the three Supabase-compatible role names

The globally-unique rule above applies to per-project roles. It cannot apply to `anon`, `authenticated`, and `service_role`, because migrated Supabase RLS policies name them literally and renaming them per tenant would break every migrated policy.

Those three are created once per node as privilege-free `NOLOGIN` names shared by all tenant databases. They are safe to share because privileges are granted on per-database objects, so a grant in one tenant does not exist in another. Only the per-project authenticator logs in.

Role **membership** is the exception to the exception: it is cluster-global, so no per-tenant role may ever be granted *to* a shared role.

See ADR-016 and `specs/tenant-role-model.md`.

## Direct SQL

Free tier: not exposed.

Paid tier: may expose a constrained connection string through a direct or pooled endpoint.

Direct SQL access must still obey role/database configuration including:

- connection limits;
- statement timeouts;
- memory/temp limits;
- restricted extensions;
- no owner/superuser capability.
