# Phase 09 — Billing and Paid Upgrade

## Objective

Turn a free project into a paid project without normally migrating its database.

## Scope

- Billing-provider integration after explicit selection. **Selected
  2026-08-20: Stripe** (ADR-049), with merchant-of-record status via Stripe
  Managed Payments as deployment configuration rather than a second
  integration. Hosted Checkout only — Elements would foreclose it.
- Subscriptions.
- Entitlements.
- Plan upgrade/downgrade.
- Paid direct DB access.
- Usage display.

## Acceptance criteria

- [x] Upgrade changes entitlements while retaining database identity. *(Both halves, and
      they were built as separate slices because they fail separately. Identity is
      asserted rather than argued: `plan_change.identity` reads the tenant database name,
      the `project_ref`, the node and every API key id, and
      `test_plan_change.py::test_a_plan_change_keeps_the_database_the_ref_the_node_and_the_keys`
      compares the tuple across a real change on a real node. Nothing moves because
      nothing was written that could -- ADR-006 already decoupled purchase from
      placement. Entitlements reaching the node is slice 0's `plan_apply`, which the
      change runs as its second half; `test_plan_apply.py::test_a_plan_change_is_invisible_until_it_is_applied_and_then_it_is_not`
      is the negative that gives that assertion its meaning, and
      `test_the_change_reaches_the_node_rather_than_only_the_row` is the positive. The
      project also never leaves a serving status while it happens, which is a property
      three gates depend on.)*
- [x] Direct DB access appears only when entitled. *(ADR-047 gives paid direct SQL a role
      of its own rather than lending out the tenant admin role, so "entitled" is a
      `rolcanlogin` on the node and not only a refusal in a route -- and both are tested.
      Control plane: `test_database_connection.py` refuses a free project its credential
      and refuses it rotation, refuses a member who is not a manager, and scans every
      response and audit row for the password. Node: `test_direct_sql.py` has a free
      project unable to log in at all, and the credential that does work unable to reach
      another tenant, own its schema, read platform bookkeeping, install extensions, or
      undo the ADR-018 hardening. Revocation on downgrade is
      `test_plan_change.py::test_a_downgrade_revokes_direct_access_on_the_node`, and
      `test_plan_apply.py::test_a_downgrade_closes_the_door_and_an_upgrade_reopens_it_with_the_same_key`
      pins that reopening does not mint a second secret.)*
- [x] Billing state and technical entitlement state reconcile safely. *(ADR-048 is the
      whole answer: a subscription records what is paid for and writes no entitlement,
      ever. `test_subscriptions.py::test_recording_a_subscription_changes_no_entitlement`
      and `::test_a_cancellation_changes_no_entitlement_either` are that property stated
      as tests. Reconciliation is a separate, idempotent pass --
      `::test_reconcile_moves_the_project_onto_the_plan_being_paid_for` and
      `::test_reconciling_twice_is_uneventful` -- which ADR-053 puts in the maintenance
      run rather than in the webhook, because the webhook cannot reach a node. "Safely"
      is carried by the database rather than by check-then-act: the partial unique index
      that refuses a second live subscription, and the `state_as_of` comparison moved
      into the UPDATE's WHERE clause so a concurrent writer cannot land the out-of-order
      downgrade the read-side guard was supposed to stop. Divergence is reported rather
      than hidden -- `::test_a_paid_project_nobody_is_paying_for_is_reported_as_unbilled`,
      `::test_a_subscription_whose_plan_never_reached_the_project_is_reported`, and
      slice 0's drift pass.)*
- [x] Failed payment does not automatically destroy data. *(ADR-051, and the one criterion
      whose failure is unrecoverable, so it is tested end to end on a real node:
      `test_billing_grace.py::test_the_end_of_grace_keeps_every_row_the_database_and_the_keys`
      creates a table and a row in the tenant database, mints two API keys explicitly so
      the assertion cannot be vacuous, runs a card failure through the full fourteen days,
      and finds the rows, the database, the `project_ref` and the keys where they were.
      What is taken away is write access, through ADR-040's existing revoke, not data.
      `::test_nothing_in_the_grace_path_can_delete_a_project` guards the path itself, and
      the deferral tests are the other half of it: a provider that cannot be reached, or
      an error that merely mentions 404, takes nothing away rather than guessing.)*
