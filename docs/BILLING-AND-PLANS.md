# Billing and Plans

## Philosophy

Plans should be understandable and predictable.

The architecture should be resource/feature driven, while exact public prices and quotas remain configurable.

## Accepted distinctions

Free:

- API-only;
- constrained resources;
- sleeping API workers;
- no public direct PostgreSQL connection.

Paid may add:

- direct/pool PostgreSQL access;
- higher API/query limits;
- larger storage;
- always-warm workers;
- backups;
- PITR;
- Realtime capacity;
- support/SLA features;
- placement on production-oriented node pools.

## Important rule

Plan behavior must be data/configuration-driven.

Do not scatter checks like:

```text
if plan == "free"
```

through unrelated services when a centralized entitlement/limits model can be used.

## Upgrade

Normal free-to-paid upgrade changes entitlements/limits and keeps the tenant database in place.

A later background move to another node/pool may be performed for operational reasons, but it is not required to complete payment/upgrade.
