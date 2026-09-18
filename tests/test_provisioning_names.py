"""The identifiers derived from one project ref, and the list of them nothing may forget.

`TenantNames` names every database and role a tenant can have. Two of those names are
conditional on a feature the project turned on, which is exactly how they went missing from
the list of what deletion destroys: `jobs._drop_roles` wrote its own tuple, the four roles
added after it never joined, and a deleted project left `memreader` and `memwriter` on the
cluster -- `memwriter` a LOGIN role -- with the audit reporting a complete deletion.

So the list is derived, and this holds the derivation: a role name added to the dataclass is
in `roles` by existing, and a test that has to be updated to add one is the point.
"""

from __future__ import annotations

import dataclasses

from services.control_plane.provisioning import TenantNames

REF = "abcd1234"


def test_every_name_that_is_not_the_ref_or_the_database_is_a_role():
    names = TenantNames.for_ref(REF)
    fields = {f.name for f in dataclasses.fields(names)} - {"project_ref", "database"}
    assert set(names.roles) == {getattr(names, f) for f in fields}
    assert len(names.roles) == len(set(names.roles)) == len(fields), "no name appears twice"


def test_the_roles_are_the_ones_this_platform_has():
    """Written out once, here, so that adding a role is a deliberate change to a test rather
    than something that happens silently to `_drop_roles`, `tenant_movement` and the gateway."""
    assert set(TenantNames.for_ref(REF).roles) == {
        f"mldb_{REF}_authenticator",
        f"mldb_{REF}_auth",
        f"mldb_{REF}_admin",
        f"mldb_{REF}_executor",
        f"mldb_{REF}_client",
        f"mldb_{REF}_replicator",
        f"mldb_{REF}_storage",
        f"mldb_{REF}_vectors",
        f"mldb_{REF}_memwriter",
        f"mldb_{REF}_memreader",
    }


def test_every_role_is_prefixed_with_the_tenants_database():
    """The prefix is what makes `LIKE 'mldb_<ref>%'` a complete inventory of a tenant's roles,
    which is how deletion is checked -- and it is what keeps a tenant's names away from the
    cluster-wide `anon`, `authenticated` and `service_role`, which belong to every tenant."""
    names = TenantNames.for_ref(REF)
    assert all(r.startswith(f"{names.database}_") for r in names.roles)
