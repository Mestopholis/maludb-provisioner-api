"""The published pricing page, checked against what the platform enforces.

`frontend/app.js` carries `PUBLIC_PLANS`, a curated copy of the plan limits.
It is curated rather than fetched because ADR-037 keeps `/v1/plans`
authenticated: that endpoint returns `plans.config_json.limits` verbatim,
including `work_mem_mb`, `temp_file_limit_mb`, `postgrest_pool_size` and the
statement, lock and idle-transaction timeouts. Publishing those tells anyone
designing a workload precisely where every threshold sits. The ADR says the
public view should be "a curated projection with prices in it".

A curated copy drifts. The first version of that copy advertised **1 GB of
database storage on Free**, which is the *object* storage limit -- the database
limit is 500 MB. That is a wrong number on the page a customer decides from, and
nothing would have caught it.

So the copy carries the entitlement key and value it publishes, and this test
proves the page matches `entitlements.DEFAULTS`.
"""

from __future__ import annotations

import pathlib
import re

import pytest

from services.control_plane import entitlements

APP_JS = pathlib.Path(__file__).resolve().parent.parent / "frontend" / "app.js"

# ADR-037 names these as the reason /v1/plans is authenticated. None of them
# may appear in the published copy.
WITHHELD = (
    "work_mem_mb",
    "temp_file_limit_mb",
    "postgrest_pool_size",
    "statement_timeout_ms",
    "lock_timeout_ms",
    "idle_in_transaction_timeout_ms",
)

_PLAN = re.compile(r'code:\s*"(?P<code>\w+)"')
_SPEC = re.compile(r'\{\s*key:\s*"(?P<key>\w+)",\s*value:\s*(?P<value>\d+)')


def _published() -> dict[str, dict[str, int]]:
    """`{plan_code: {entitlement_key: published_value}}` parsed from the page.

    A regex over a format this repository controls. If someone reformats
    `PUBLIC_PLANS` past what this matches, the emptiness assertions below fail
    loudly rather than letting the test pass vacuously -- which is the failure
    direction that matters.
    """
    source = APP_JS.read_text()
    block = source[source.index("const PUBLIC_PLANS = [") : source.index("];", source.index("const PUBLIC_PLANS = ["))]

    out: dict[str, dict[str, int]] = {}
    current: str | None = None
    for line in block.splitlines():
        plan = _PLAN.search(line)
        if plan:
            current = plan.group("code")
            out[current] = {}
            continue
        spec = _SPEC.search(line)
        if spec and current:
            out[current][spec.group("key")] = int(spec.group("value"))
    return out


def test_the_page_publishes_something_for_every_plan():
    """Guards every other test here from passing on an empty parse."""
    published = _published()
    assert set(published) == set(entitlements.DEFAULTS), (
        f"published plans {sorted(published)} do not match the catalogue "
        f"{sorted(entitlements.DEFAULTS)}"
    )
    for code, specs in published.items():
        assert len(specs) >= 6, f"{code} publishes only {len(specs)} figures; the parse looks broken"


@pytest.mark.parametrize("code", sorted(entitlements.DEFAULTS))
def test_every_published_figure_matches_what_is_enforced(code):
    """A wrong number here is a wrong number on the page a customer buys from."""
    published = _published()[code]
    actual = entitlements.DEFAULTS[code]

    wrong = {
        key: (value, actual.get(key))
        for key, value in published.items()
        if actual.get(key) != value
    }
    assert not wrong, (
        f"{code} advertises figures the platform does not enforce "
        f"(published, enforced): {wrong}"
    )


def test_the_page_withholds_the_limits_adr_037_names():
    """Publishing these describes where a workload would sit to stay under them."""
    source = APP_JS.read_text()
    block = source[source.index("const PUBLIC_PLANS = [") : source.index("];", source.index("const PUBLIC_PLANS = ["))]
    leaked = [k for k in WITHHELD if k in block]
    assert not leaked, (
        f"the public pricing copy publishes {leaked}, which ADR-037 keeps behind "
        "authentication"
    )


def test_free_does_not_advertise_what_it_cannot_do():
    """ADR-039 and the entitlements: no direct connection, no PITR, no Realtime.

    Stated on the card because a customer who discovers it after building is a
    refund, and because these three are the reasons to buy the next tier up.
    """
    source = APP_JS.read_text()
    free_block = source[source.index('code: "free"') : source.index('code: "starter"')]

    assert entitlements.DEFAULTS["free"]["direct_database_access"] is False
    assert entitlements.DEFAULTS["free"]["pitr_window_hours"] == 0
    assert entitlements.DEFAULTS["free"]["realtime_connections"] == 0

    for phrase in ("No direct database connection", "No point-in-time recovery", "No Realtime"):
        assert phrase in free_block, f"the Free card does not say {phrase!r}"
