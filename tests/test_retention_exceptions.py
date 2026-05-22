from datetime import datetime, timedelta, timezone

import pytest

from src.common.metrics import MetricsCollector
from src.orchestrator.retention import (
    RetentionExceptionRegistry,
    RetentionExceptionValidationError,
)


NOW = datetime(2026, 5, 22, 6, 45, tzinfo=timezone.utc)


def build_registry():
    return RetentionExceptionRegistry(metrics_collector=MetricsCollector())


@pytest.mark.parametrize(
    ("field", "override", "message"),
    [
        ("artifact_category", "", "artifact_category is required"),
        ("owner", "   ", "owner is required"),
        ("reason", "", "reason is required"),
        ("expires_at", None, "expires_at is required"),
        ("review_date", "", "review_date must be an ISO datetime"),
    ],
)
def test_retention_exception_requires_governance_metadata(
    field,
    override,
    message,
):
    registry = build_registry()
    payload = {
        "exception_id": "hold-1",
        "artifact_category": "agent-transcripts",
        "owner": "privacy-team",
        "reason": "customer deletion dispute",
        "expires_at": NOW + timedelta(days=7),
        "review_date": NOW + timedelta(days=2),
        "now": NOW,
    }
    payload[field] = override

    with pytest.raises(RetentionExceptionValidationError, match=message):
        registry.register_exception(**payload)

    assert registry.list_exceptions(now=NOW) == []
    assert registry.audit_log()[-1]["decision"] == "rejected"


def test_register_exception_requires_future_expiration_transactionally():
    registry = build_registry()

    with pytest.raises(RetentionExceptionValidationError, match="future"):
        registry.register_exception(
            exception_id="expired-at-write",
            artifact_category="agent-transcripts",
            owner="privacy-team",
            reason="stale hold",
            expires_at=NOW,
            review_date=NOW,
            now=NOW,
        )

    assert registry.list_exceptions(now=NOW) == []


def test_review_date_must_not_follow_expiration():
    registry = build_registry()

    with pytest.raises(RetentionExceptionValidationError, match="review_date"):
        registry.register_exception(
            exception_id="bad-review-date",
            artifact_category="dataset-export",
            owner="governance",
            reason="audit hold",
            expires_at=NOW + timedelta(days=2),
            review_date=NOW + timedelta(days=3),
            now=NOW,
        )

    assert registry.list_exceptions(now=NOW) == []


def test_expired_exception_fails_governance_validation_later():
    registry = build_registry()
    registry.register_exception(
        exception_id="legal-hold",
        artifact_category="training-dataset",
        owner="legal",
        reason="litigation hold",
        expires_at=NOW + timedelta(days=1),
        review_date=NOW + timedelta(hours=12),
        now=NOW,
    )

    with pytest.raises(RetentionExceptionValidationError, match="legal-hold"):
        registry.validate_governance(now=NOW + timedelta(days=2))

    assert registry.audit_log()[-1]["decision"] == "expired"


def test_validate_governance_returns_stable_records_for_active_exceptions():
    registry = build_registry()
    registry.register_exception(
        exception_id="active-hold",
        artifact_category="support-artifacts",
        owner="support-ops",
        reason="open customer investigation",
        expires_at="2026-05-29T06:45:00Z",
        review_date="2026-05-24T06:45:00Z",
        now=NOW,
    )

    records = registry.validate_governance(now=NOW + timedelta(days=1))

    assert records == [
        {
            "exception_id": "active-hold",
            "artifact_category": "support-artifacts",
            "owner": "support-ops",
            "reason": "open customer investigation",
            "expires_at": "2026-05-29T06:45:00Z",
            "review_date": "2026-05-24T06:45:00Z",
            "created_at": "2026-05-22T06:45:00Z",
            "review_due": False,
        }
    ]


def test_active_exception_report_groups_by_owner_and_sorts_entries():
    registry = build_registry()
    registry.register_exception(
        exception_id="bob-2",
        artifact_category="trace-cache",
        owner="bob",
        reason="debugging",
        expires_at=NOW + timedelta(days=4),
        review_date=NOW + timedelta(days=1),
        now=NOW,
    )
    registry.register_exception(
        exception_id="alice-2",
        artifact_category="model-snapshot",
        owner="alice",
        reason="quality audit",
        expires_at=NOW + timedelta(days=3),
        review_date=NOW + timedelta(days=1),
        now=NOW,
    )
    registry.register_exception(
        exception_id="alice-1",
        artifact_category="dataset-export",
        owner="alice",
        reason="legal hold",
        expires_at=NOW + timedelta(days=2),
        review_date=NOW + timedelta(hours=12),
        now=NOW,
    )

    report = registry.active_exceptions_by_owner(now=NOW + timedelta(hours=1))

    assert list(report) == ["alice", "bob"]
    assert [item["exception_id"] for item in report["alice"]] == [
        "alice-1",
        "alice-2",
    ]
    assert report["bob"][0]["artifact_category"] == "trace-cache"


def test_active_owner_report_excludes_expired_entries_without_mutation():
    registry = build_registry()
    registry.register_exception(
        exception_id="short-hold",
        artifact_category="short-lived-artifact",
        owner="ops",
        reason="incident review",
        expires_at=NOW + timedelta(hours=1),
        review_date=NOW + timedelta(minutes=30),
        now=NOW,
    )
    registry.register_exception(
        exception_id="long-hold",
        artifact_category="long-lived-artifact",
        owner="ops",
        reason="legal review",
        expires_at=NOW + timedelta(days=4),
        review_date=NOW + timedelta(days=1),
        now=NOW,
    )

    report = registry.active_exceptions_by_owner(now=NOW + timedelta(hours=2))
    report["ops"][0]["owner"] = "mutated"

    assert [item["exception_id"] for item in report["ops"]] == ["long-hold"]
    stored = {
        item["exception_id"]: item["owner"]
        for item in registry.list_exceptions(now=NOW)
    }
    assert stored == {"long-hold": "ops", "short-hold": "ops"}


def test_audit_and_metrics_record_sanitized_decisions():
    metrics = MetricsCollector()
    registry = RetentionExceptionRegistry(metrics_collector=metrics)
    registry.register_exception(
        exception_id="customer-123-secret",
        artifact_category="agent-transcripts",
        owner="privacy-team@example.com",
        reason="contains sensitive customer case details",
        expires_at=NOW + timedelta(days=5),
        review_date=NOW + timedelta(days=1),
        now=NOW,
    )

    audit_entry = registry.audit_log()[-1]
    snapshot = metrics.snapshot()

    assert audit_entry["decision"] == "accepted"
    assert audit_entry["exception_ref"] != "customer-123-secret"
    assert audit_entry["owner_ref"] != "privacy-team@example.com"
    assert "sensitive customer" not in str(audit_entry)
    assert snapshot["counters"]["retention.exceptions.accepted"] == 1
