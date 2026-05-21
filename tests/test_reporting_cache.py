import pytest

from src.common.errors import AuthenticationError
from src.common.reporting_cache import (
    ReportAuthorizationContext,
    ReportResultCache,
)


class AccessStore:
    def __init__(self):
        self._current = {}

    def grant(
        self,
        requester_id,
        workspace_id,
        roles,
        permissions=("reports:read",),
        version=1,
    ):
        context = ReportAuthorizationContext(
            requester_id=requester_id,
            workspace_id=workspace_id,
            roles=roles,
            permissions=permissions,
            auth_version=version,
        )
        self._current[(requester_id, workspace_id)] = context
        return context

    def revoke(self, requester_id, workspace_id):
        self._current.pop((requester_id, workspace_id), None)

    def authorize(self, _report_id, context):
        current = self._current.get(
            (context.requester_id, context.workspace_id)
        )
        return current == context and "reports:read" in context.permissions

    def authorize_admin(self, report_id, context):
        return self.authorize(report_id, context) and "admin" in context.roles


def test_cache_key_includes_workspace_role_permission_and_version():
    access = AccessStore()
    cache = ReportResultCache()
    admin = access.grant("user-1", "workspace-a", ("admin",), version=1)
    viewer = access.grant("user-1", "workspace-b", ("viewer",), version=1)

    admin_result = cache.get_or_compute(
        "revenue",
        {"range": "30d"},
        admin,
        lambda: {"rows": ["admin-only"]},
        access.authorize,
    )
    viewer_result = cache.get_or_compute(
        "revenue",
        {"range": "30d"},
        viewer,
        lambda: {"rows": ["viewer"]},
        access.authorize,
    )

    assert admin_result == {"rows": ["admin-only"]}
    assert viewer_result == {"rows": ["viewer"]}
    assert cache.size() == 2


def test_role_downgrade_does_not_serve_stale_admin_report():
    access = AccessStore()
    cache = ReportResultCache()
    admin = access.grant("user-1", "workspace-a", ("admin",), version=1)
    producer_calls = []

    assert cache.get_or_compute(
        "pipeline",
        {"team": "enterprise"},
        admin,
        lambda: producer_calls.append("admin") or {"private": True},
        access.authorize_admin,
    ) == {"private": True}

    viewer = access.grant("user-1", "workspace-a", ("viewer",), version=2)

    with pytest.raises(AuthenticationError, match="report access denied"):
        cache.get_or_compute(
            "pipeline",
            {"team": "enterprise"},
            admin,
            lambda: producer_calls.append("stale-admin"),
            access.authorize_admin,
        )
    with pytest.raises(AuthenticationError, match="report access denied"):
        cache.get_or_compute(
            "pipeline",
            {"team": "enterprise"},
            viewer,
            lambda: producer_calls.append("viewer"),
            access.authorize_admin,
        )

    assert producer_calls == ["admin"]


def test_workspace_removal_revalidates_before_cached_hit_or_producer():
    access = AccessStore()
    cache = ReportResultCache()
    context = access.grant(
        "user-2",
        "workspace-removed",
        ("admin",),
        version=1,
    )
    producer_calls = []

    assert cache.get_or_compute(
        "usage",
        {"range": "today"},
        context,
        lambda: producer_calls.append("initial") or {"rows": [1]},
        access.authorize,
    ) == {"rows": [1]}

    access.revoke("user-2", "workspace-removed")

    with pytest.raises(AuthenticationError, match="report access denied"):
        cache.get_or_compute(
            "usage",
            {"range": "today"},
            context,
            lambda: producer_calls.append("after-removal"),
            access.authorize,
        )

    assert producer_calls == ["initial"]
    assert cache.size() == 0


def test_revocation_during_producer_is_denied_before_store_or_return():
    access = AccessStore()
    cache = ReportResultCache()
    context = access.grant("user-2", "workspace-a", ("admin",), version=1)
    producer_calls = []

    def revoked_mid_compute():
        producer_calls.append("started")
        access.revoke("user-2", "workspace-a")
        return {"rows": ["stale"]}

    with pytest.raises(AuthenticationError, match="report access denied"):
        cache.get_or_compute(
            "usage",
            {"range": "today"},
            context,
            revoked_mid_compute,
            access.authorize,
        )

    assert producer_calls == ["started"]
    assert cache.size() == 0


def test_explicit_workspace_invalidation_removes_all_matching_entries():
    access = AccessStore()
    cache = ReportResultCache()
    context_a = access.grant("user-1", "workspace-a", ("analyst",), version=1)
    context_b = access.grant("user-1", "workspace-b", ("analyst",), version=1)
    cache.set(
        "activity",
        {"page": 1},
        context_a,
        {"workspace": "a"},
        access.authorize,
    )
    cache.set(
        "activity",
        {"page": 1},
        context_b,
        {"workspace": "b"},
        access.authorize,
    )

    assert cache.invalidate_workspace("workspace-a") == 1

    assert (
        cache.get("activity", {"page": 1}, context_a, access.authorize)
        is None
    )
    assert cache.get("activity", {"page": 1}, context_b, access.authorize) == {
        "workspace": "b"
    }


def test_unordered_query_values_use_stable_keys_and_results_are_copied():
    access = AccessStore()
    cache = ReportResultCache()
    context = access.grant("user-3", "workspace-a", ("analyst",), version=1)
    original = {"rows": [{"segment": "paid"}]}
    cache.set(
        "segments",
        {"segments": {"organic", "paid"}},
        context,
        original,
        access.authorize,
    )
    original["rows"].append({"segment": "mutated"})

    cached = cache.get(
        "segments",
        {"segments": {"paid", "organic"}},
        context,
        access.authorize,
    )
    cached["rows"].append({"segment": "local-change"})

    assert cached == {
        "rows": [{"segment": "paid"}, {"segment": "local-change"}]
    }
    assert cache.get(
        "segments",
        {"segments": {"organic", "paid"}},
        context,
        access.authorize,
    ) == {"rows": [{"segment": "paid"}]}


def test_audit_records_do_not_include_report_payloads():
    access = AccessStore()
    audit_events = []
    cache = ReportResultCache(audit_sink=audit_events.append)
    context = access.grant("user-4", "workspace-a", ("admin",), version=1)

    cache.get_or_compute(
        "sensitive",
        {"range": "7d"},
        context,
        lambda: {"secret_payload": "do-not-log"},
        access.authorize,
    )
    cache.get("sensitive", {"range": "7d"}, context, access.authorize)

    assert [event["event"] for event in audit_events] == ["store", "hit"]
    assert all("secret_payload" not in event for event in audit_events)
    assert audit_events[0]["workspace_id"] == "workspace-a"
