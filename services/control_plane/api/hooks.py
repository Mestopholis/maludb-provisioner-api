"""GoTrue's Send Email Hook (ADR-029).

Called by a project's Auth worker, not by a customer. The route is excluded
from the public OpenAPI document because it is not part of the customer API and
should be unreachable from the internet -- but network position is defence in
depth here, not the control. **The signature is the authentication**: nothing
else identifies the caller, and the secret is per project.

A note on what an attacker would gain by reaching this endpoint without a valid
signature: they could make the platform send a confirmation message to an
address of their choosing, spending a quota unit and putting a real link in a
real inbox. That is why the signature is verified before anything else is read
out of the payload, and why the timestamp window is enforced -- a captured call
would otherwise replay indefinitely, since the body never changes.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Header, Request, Response, status

from services.control_plane import mail

log = logging.getLogger(__name__)

router = APIRouter(tags=["internal"], include_in_schema=False)


@router.post("/internal/hooks/email/{project_ref}", status_code=status.HTTP_200_OK)
async def send_email_hook(
    project_ref: str,
    request: Request,
    response: Response,
    webhook_id: str = Header(default="", alias="webhook-id"),
    webhook_timestamp: str = Header(default="", alias="webhook-timestamp"),
    webhook_signature: str = Header(default="", alias="webhook-signature"),
) -> dict:
    """Send one auth email, or tell GoTrue why not.

    The status code is the contract with GoTrue, and the distinction matters
    more than it looks:

    - **200** means handled. GoTrue proceeds, and the signup or reset succeeds.
      A suppressed recipient returns 200 deliberately: the address is
      permanently undeliverable, so failing the request would turn a dead
      address into a user who cannot sign up *and* an error the application
      cannot act on.
    - **4xx/5xx** makes GoTrue fail the request and retry. Reserved for
      conditions a retry could actually fix, plus authentication failures.
    """
    body = await request.body()

    try:
        result = mail.handle_hook(
            project_ref=project_ref,
            body=body,
            webhook_id=webhook_id,
            timestamp=webhook_timestamp,
            signature_header=webhook_signature,
        )
    except mail.MailError as exc:
        # Deliberately coarse. The caller is a worker, not a person, and a
        # detailed reason would describe a project's configuration to whoever
        # managed to reach the endpoint.
        log.warning("email hook refused for project %s: %s", project_ref, type(exc).__name__)
        response.status_code = _status_for(exc)
        return {"error": {"http_code": response.status_code, "message": "email could not be sent"}}

    return result


def _status_for(exc: mail.MailError) -> int:
    if isinstance(exc, mail.QuotaExceeded):
        # 429 so GoTrue backs off rather than hammering an exhausted allowance,
        # and so the condition is legible as a quota condition rather than a
        # generic failure -- which is what the phase's acceptance criterion
        # asks for.
        return status.HTTP_429_TOO_MANY_REQUESTS
    if isinstance(exc, mail.HookNotAuthenticated):
        return status.HTTP_401_UNAUTHORIZED
    if isinstance(exc, mail.SendingDisabled):
        return status.HTTP_403_FORBIDDEN
    return status.HTTP_502_BAD_GATEWAY
