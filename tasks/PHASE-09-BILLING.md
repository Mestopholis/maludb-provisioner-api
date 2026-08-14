# Phase 09 — Billing and Paid Upgrade

## Objective

Turn a free project into a paid project without normally migrating its database.

## Scope

- Billing-provider integration after explicit selection.
- Subscriptions.
- Entitlements.
- Plan upgrade/downgrade.
- Paid direct DB access.
- Usage display.

## Acceptance criteria

- [ ] Upgrade changes entitlements while retaining database identity.
- [ ] Direct DB access appears only when entitled.
- [ ] Billing state and technical entitlement state reconcile safely.
- [ ] Failed payment does not automatically destroy data.
