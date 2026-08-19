# Scripts

Reserved for development/operations scripts such as:

- local test-node bootstrap;
- compatibility test setup;
- safe project smoke tests;
- schema validation;
- development certificate/DNS helpers where appropriate.

Present:

- `realtime-test-cluster.sh` — builds and drops the throwaway cluster the
  Realtime assertions need (`wal_level = logical`, and a `pg_hba.conf` that is
  itself under test).
- `require-security-review.sh` — the merge gate for AGENTS.md's recorded
  security review. Run by CI on every pull request, and runnable locally:
  `scripts/require-security-review.sh main HEAD`. Asserted by
  `tests/test_security_review_gate.py`, because a control this repository does
  not test is the shape of bug it keeps finding.
- `export-openapi.py` — regenerates `specs/control-plane-api.yaml`; `--check`
  fails on drift, which CI runs.
- `bench-gateway*.py`, `spike-provision-tenant.sh` — measurement, not product.

Production provisioning must not devolve into undocumented one-off shell scripts. Operational scripts that remain part of the product should be documented and tested.
