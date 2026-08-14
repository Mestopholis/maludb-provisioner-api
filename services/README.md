# Services

Implementation services will live here after Phase 01 chooses the control-plane/gateway stack.

Expected logical components:

- control-plane API;
- provisioning worker;
- gateway/project router;
- project service/worker manager;
- usage/metrics collector;
- billing adapter later.

Do not create a separate microservice merely because it appears in this list. Start with the simplest deployable architecture that preserves the boundaries in `docs/ARCHITECTURE.md`.
