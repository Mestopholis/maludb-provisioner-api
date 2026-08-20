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

## Two facts, not one (ADR-048)

A project's plan is two separate facts and the platform keeps them in two
separate places, on purpose.

| | Where | What it means | Who writes it |
|---|---|---|---|
| **Entitlement** | `projects.plan_id` | what the platform *enforces* — resolved by `entitlements.for_project`, counted against by the gateway, written to a node by `plan_apply` | `plan_change.change_plan`, and nothing else |
| **Subscription** | `subscriptions` | what is *paid for* — plan, state, and the moment that state became true | `subscriptions`, which never touches the other column |

The flow between them runs one way. `subscriptions.reconcile` computes the plan
a subscription entitles and hands it to `plan_change`; billing state proposes,
`plan_apply` disposes. Nothing that knows a billing provider exists can reach a
node, which is what makes swapping providers a change to one module.

The two can disagree, and that is a state with a name rather than a bug:

- **`unbilled`** — the project is on a plan no live subscription pays for.
  `cp-manage project set-plan` moves a project and takes no money, so this is
  every paid project until somebody records a subscription for it.
- **`diverged`** — a subscription entitles a plan the project is not on. An
  upgrade that was recorded and never applied, or a cancellation that was
  recorded and never enforced.

`cp-manage subscription drift` lists both. It reports and does not correct:
moving a project between plans is a change that should have somebody's name on
it, and `cp-manage subscription reconcile --ref <ref>` is that name.

### Subscription states

MaluDB's own, not a provider's. A provider's states are mapped onto these, so
that a provider's vocabulary never reaches the code that grants entitlements.

| State | Entitles |
|---|---|
| `incomplete` | the free tier — begun and never paid for |
| `trialing` | the subscription's plan |
| `active` | the subscription's plan |
| `past_due` | the subscription's plan |
| `canceled` | the free tier; terminal, and a returning customer gets a new row |

`past_due` keeping its plan is now a decision as well as a default: a failed
payment is not by itself a downgrade, and **ADR-051** settles how long that
lasts. Fourteen days — configurable, never a constant in application logic — of
entirely unchanged service. Then the subscription becomes `canceled`,
`reconcile` hands the default plan to `plan_change`, and the project meets
ADR-040's storage restriction: `INSERT` and `UPDATE` revoked, `SELECT`,
`DELETE` and `TRUNCATE` kept. Writes stop; reads do not; the database,
`project_ref`, API keys and rows all survive. **Nothing deletes customer data,
at any stage** — that is the acceptance criterion whose failure cannot be
undone, and it is met by there being no code that deletes.

Provider states are mapped onto the table above and never the reverse. Stripe
(ADR-049) happens to use the names `trialing` and `past_due` too; the mapping
must not rely on that, because the coincidence would not survive a provider
change.

Billing attaches to the organization (ADR-020) and the plan attaches to the
project, so a subscription carries both: the org is who pays, the project is
what the plan applies to. The pair is a composite foreign key, so a
subscription cannot name one organization and another organization's project.

## Upgrade

Normal free-to-paid upgrade changes entitlements/limits and keeps the tenant database in place.

A later background move to another node/pool may be performed for operational reasons, but it is not required to complete payment/upgrade.
