from datetime import datetime, timedelta, timezone

import pytest

from src.deploy.migration_preflight import (
    BackupStatus,
    MigrationMetadata,
    MigrationPreflightError,
    assert_migration_preflight,
    check_migration_preflight,
)


NOW = datetime(2026, 5, 21, 10, 0, tzinfo=timezone.utc)


def test_destructive_migration_fails_without_backup():
    migration = MigrationMetadata("drop-obsolete-table", destructive=True)

    report = check_migration_preflight(migration, now=NOW)

    assert not report.allowed
    assert report.destructive
    assert report.backup_timestamp is None
    assert report.restore_check_status == "missing"


def test_destructive_migration_fails_with_stale_backup():
    migration = MigrationMetadata("rewrite-users", destructive=True)
    backup = BackupStatus(
        backup_id="snapshot-1",
        created_at=NOW - timedelta(days=2),
        restore_verified_at=NOW - timedelta(days=2, minutes=-5),
    )

    report = check_migration_preflight(migration, backup=backup, now=NOW)

    assert not report.allowed
    assert report.backup_timestamp == backup.created_at
    assert report.restore_check_status == "stale"


def test_destructive_migration_fails_without_restore_verification():
    migration = MigrationMetadata("rebuild-events", destructive=True)
    backup = BackupStatus(
        backup_id="snapshot-2",
        created_at=NOW - timedelta(hours=2),
    )

    report = check_migration_preflight(migration, backup=backup, now=NOW)

    assert not report.allowed
    assert report.backup_timestamp == backup.created_at
    assert report.restore_check_status == "not_verified"


def test_destructive_migration_passes_with_recent_verified_backup():
    migration = MigrationMetadata("truncate-queue", destructive=True)
    backup = BackupStatus(
        backup_id="snapshot-3",
        created_at=NOW - timedelta(hours=3),
        restore_verified_at=NOW - timedelta(hours=2),
    )

    report = assert_migration_preflight(migration, backup=backup, now=NOW)

    assert report.allowed
    assert report.backup_timestamp == backup.created_at
    assert report.restore_check_status == "verified"
    assert (
        report.to_dict()["backup_timestamp"]
        == backup.created_at.isoformat()
    )


def test_non_destructive_migration_does_not_require_backup():
    migration = MigrationMetadata("add-index", destructive=False)

    report = assert_migration_preflight(migration, now=NOW)

    assert report.allowed
    assert report.restore_check_status == "not_required"


def test_migration_metadata_requires_destructive_declaration():
    with pytest.raises(ValueError, match="destructive"):
        MigrationMetadata.from_dict({"id": "missing-flag"})

    with pytest.raises(TypeError, match="boolean"):
        MigrationMetadata.from_dict({"id": "bad-flag", "destructive": "yes"})

    migration = MigrationMetadata.from_dict(
        {
            "id": "drop-column",
            "destructive": True,
            "description": "drops old column",
        }
    )
    assert migration.migration_id == "drop-column"
    assert migration.destructive is True


def test_assert_migration_preflight_raises_with_report():
    migration = MigrationMetadata("drop-table", destructive=True)

    with pytest.raises(MigrationPreflightError) as exc:
        assert_migration_preflight(migration, now=NOW)

    assert exc.value.report.restore_check_status == "missing"
