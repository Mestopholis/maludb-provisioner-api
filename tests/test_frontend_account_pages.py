"""The dashboard's Access tokens and Organization pages, against the contract and the rules they keep.

Driven in headless Chromium against the dev control plane with two accounts while being
built (create a token and use it, revoke it and see it refused; invite, accept signed out
and in, change a role, transfer ownership both ways, remove). What can be held without a
browser is held here:

- **every call is a public route** with that method;
- **the roles offered are the API's**, described as docs/ACCOUNTS.md describes them;
- **a token's value, and an invitation link, are shown once**: held only in state while on
  screen, dropped on dismiss, on leaving the page and on sign-out, never in storage;
- **the invitation page says no email is sent** -- which is true only while the API returns
  the invitation's token to the inviter, so this test reads that response shape and fails
  when email delivery lands, as a reminder to change the page;
- **controls mirror the API's rules**: not your own role; the owner tier only for an owner;
  transfer only for an owner; and the irreversible actions ask first;
- **every value interpolated into the pages is escaped**.
"""

from __future__ import annotations

import pathlib
import re

import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent
API_JS = (ROOT / "frontend" / "api.js").read_text()
APP_JS = (ROOT / "frontend" / "app.js").read_text()
ACCOUNTS = (ROOT / "docs" / "ACCOUNTS.md").read_text()
SPEC = yaml.safe_load((ROOT / "specs" / "control-plane-api.yaml").read_text())

CALLS = [
    ("listTokens", "get", "/v1/auth/tokens", "/v1/auth/tokens"),
    ("createToken", "post", "/v1/auth/tokens", "/v1/auth/tokens"),
    ("revokeToken", "delete", "/v1/auth/tokens/{token_id}", "/v1/auth/tokens/"),
    ("listMembers", "get", "/v1/organizations/{org_id}/members", "/members"),
    ("inviteMember", "post", "/v1/organizations/{org_id}/invitations", "/invitations"),
    ("setMemberRole", "put", "/v1/organizations/{org_id}/members/{user_id}", "/members/"),
    ("removeMember", "delete", "/v1/organizations/{org_id}/members/{user_id}", "/members/"),
    ("transferOwnership", "post", "/v1/organizations/{org_id}/transfer-ownership", "/transfer-ownership"),
    ("acceptInvitation", "post", "/v1/organizations/invitations/accept", "/invitations/accept?token="),
]


def _api_function(name: str) -> str:
    match = re.search(rf"export const {name} = .*?(?=\n\n|\nexport |\n/\*|\Z)", API_JS, re.S)
    assert match, f"api.js has no {name}"
    return match.group(0)


def _section() -> str:
    start = APP_JS.index(" * Account pages: access tokens and organization members")
    return APP_JS[start:APP_JS.index(" * Wiring", start)]


def _function(name: str) -> str:
    section = _section()
    start = section.index(f"function {name}(")
    following = re.search(r"\n(?:async )?function |\n/\* --|\Z", section[start + 1:])
    return section[start:start + 1 + following.start()]


def _schema(name: str) -> dict:
    return SPEC["components"]["schemas"][name]


def test_every_call_is_a_public_route_with_that_method():
    for function, method, path, fragment in CALLS:
        assert path in SPEC["paths"] and method in SPEC["paths"][path], f"{function}: {method} {path} is not served"
        body = _api_function(function)
        assert fragment in body, f"{function} does not call {fragment}"
        if method != "get":
            assert f'"{method.upper()}"' in body, f"{function} does not send {method.upper()}"


def test_the_roles_offered_are_the_apis_described_as_accounts_md_describes_them():
    pattern = _schema("InviteIn")["properties"]["role"]["pattern"]
    api_roles = set(re.match(r"\^\((.*)\)\$", pattern).group(1).split("|"))
    assert _schema("RoleIn")["properties"]["role"]["pattern"] == pattern
    start = APP_JS.index("const ORG_ROLES = [")
    offered = dict(re.findall(r'\["([a-z]+)", "([^"]+)"\]', APP_JS[start:APP_JS.index("];", start)]))
    assert set(offered) == api_roles
    for role in api_roles:
        row = re.search(rf"^\| `{role}` \| (.+) \|$", ACCOUNTS, re.M)
        assert row, f"docs/ACCOUNTS.md has no row for {role}"
        described = offered[role].split(" — ", 1)[1]
        assert described == row.group(1), f"{role}: the page must say what ACCOUNTS.md says"


def test_a_token_and_an_invitation_link_are_shown_once_and_never_stored():
    section = _section()
    assert "localStorage" not in section and "sessionStorage" not in section
    assert "state.issuedToken = { name: issued.name, token: issued.token }" in section
    route = _function("renderAccountRoute")
    assert "state.issuedToken = null" in route and "state.issuedInvite = null" in route, "leaving the page drops both"
    action = _function("accountAction")
    assert "state.issuedToken = null" in action and "state.issuedInvite = null" in action, "dismissing drops them"
    signout = APP_JS[APP_JS.index('$("#signout").addEventListener'):]
    before_toast = signout[:signout.index("toast(")]
    assert "state.issuedToken = null" in before_toast and "state.issuedInvite = null" in before_toast
    assert "shown once and cannot be retrieved again" in _function("tokensPage")


def test_the_invitation_link_carries_its_token_in_the_fragment():
    assert "#/invite/${encodeURIComponent(invite.token)}" in _function("accountForm")
    assert "?token=" not in _function("accountForm"), "a query string is sent to the server; a fragment is not"


def test_the_page_says_no_email_is_sent_only_while_the_api_hands_the_token_to_the_inviter():
    assert "token" in _schema("InviteOut")["properties"], (
        "the invitation response no longer carries its token -- email delivery has landed; "
        "change the Organization page, which tells the inviter to send the link themselves"
    )
    assert "No email is sent yet" in _function("organizationPage")


def test_controls_mirror_the_apis_rules():
    page = _function("organizationPage")
    assert 'const manager = myRole === "owner" || myRole === "admin";' in page
    assert 'const canEdit = manager && !self && (owner || m.role !== "owner");' in page, \
        "not your own role, and the owner tier only for an owner"
    assert 'ORG_ROLES.filter(([r]) => owner || r !== "owner")' in page, "only an owner may grant owner"
    assert "owner && others.length ?" in page, "transfer is offered only to an owner"


def test_the_irreversible_actions_ask_first():
    action = _function("accountAction")
    confirmed = (("dataset.tokenRevoke", "await revokeToken("), ("dataset.memberRemove", "await removeMember("))
    for marker, call in confirmed:
        branch = action[action.index(marker):]
        assert branch.index("window.confirm(") < branch.index(call)
    form = _function("accountForm")
    transfer = form[form.index("[data-transfer-form]"):]
    assert transfer.index("window.confirm(") < transfer.index("await transferOwnership(")
    role = _function("changeMemberRole")
    assert role.index('select.value === "owner" && !window.confirm(') < role.index("await setMemberRole(")


def test_after_a_transfer_members_reload_before_your_own_role():
    form = _function("accountForm")
    transfer = form[form.index("[data-transfer-form]"):form.index("[data-accept-form]")]
    assert transfer.index("await loadMembers(orgId)") < transfer.index("await loadDashboard()"), \
        "drawn the other way round, the page briefly offered to edit the new owner"


# Interpolations not escaped at the point of interpolation, each with why that is safe.
ALLOWED = (
    "rows", "list", "picker", "role",               # built from escaped parts in the same function
    "tokens.map(", "members.map(", "grantable.map(", "ORG_ROLES.map(", "others.map(", "TOKEN_EXPIRY.map(",
    "state.orgs.map(",
    "issued ?", "expired ?", "t.last_used_at ?", "t.expires_at ?", "self ?", "canEdit ?", "!manager ?",
    "owner && others.length ?", "v === \"90\" ?", "r === m.role ?", "r === \"developer\" ?",
    "o.org_id === org.org_id ?", "expired ? \"true\" : \"false\"",
    "{ tokens:",                                     # document.title
    # The invitation link, escaped when it is shown.
    "window.location.origin", "window.location.pathname", "encodeURIComponent(",
    "email",                                         # confirm() and toast() text
    "control.dataset.", "select.dataset.", "roleName(select.value)", "org.name",  # toast and confirm text
    "Number(data.get(",
)


def test_every_interpolation_in_the_pages_is_escaped():
    unescaped = []
    for expr in re.findall(r"\$\{([^{}]*(?:\{[^{}]*\}[^{}]*)*)\}", _section()):
        expr = expr.strip()
        if not expr or expr.startswith(("escapeHtml(", "CSS.escape(")):
            continue
        if any(expr.startswith(allowed) for allowed in ALLOWED):
            continue
        unescaped.append(expr)
    assert unescaped == [], f"unescaped values in the account pages: {unescaped}"
