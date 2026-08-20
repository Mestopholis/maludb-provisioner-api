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

- [ ] Upgrade changes entitlements while retaining database identity.
- [ ] Direct DB access appears only when entitled.
- [ ] Billing state and technical entitlement state reconcile safely.
- [ ] Failed payment does not automatically destroy data.
