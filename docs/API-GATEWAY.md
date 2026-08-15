# API Gateway and Project Routing

## Public routing

Target:

```text
https://<project-ref>.maludb.com/rest/v1/...
https://<project-ref>.maludb.com/auth/v1/...
https://<project-ref>.maludb.com/realtime/v1/...
https://<project-ref>.maludb.com/storage/v1/...
```

## Request flow

1. Parse and validate project ref from hostname.
2. Lookup project routing/key metadata from cache.
3. Reject non-active projects as appropriate.
4. Validate API key.
5. Ensure key belongs to the hostname project.
6. Apply plan-level API controls.
7. Resolve downstream project worker.
8. Start sleeping free worker if policy permits.
9. Proxy the request.
10. Record metrics/usage.

## API-key cache

Do not query the control-plane SQL database on every API request.

Expected model:

```text
gateway local cache / distributed cache
           |
       cache miss
           v
     control-plane DB
```

Revocation/update paths must invalidate cached material quickly.

## Requests that carry no API key

One exception to "validate the API key on every request": a link followed from
an email. A browser navigating to a confirmation or password-reset URL sends no
`apikey` header, so requiring one answers 401 for every confirmation on the
platform.

`/auth/v1/verify` is therefore reachable without a key. The credential on those
requests is the single-use token in the query string, which GoTrue verifies.

The exemption is an **exact path, not a prefix**, and everything else still
applies: the project still comes from the hostname, a project that is not
serving still refuses, and no `Authorization` is forwarded — minting a
`service_role` token for an anonymous link-follower would hand admin rights to
anyone holding a confirmation URL.

## Security

- Never route solely because an API key exists; verify project/key match.
- Do not trust `Host` without allowed-domain validation.
- Protect internal worker endpoints from direct internet access.
- Sanitize proxy headers.
- Apply request-body limits.
- Avoid exposing internal node/database names to public clients.
