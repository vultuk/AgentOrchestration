"""Preflight checks for database migrations."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional


DEFAULT_BACKUP_MAX_AGE = timedelta(hours=24)


class MigrationPreflightError(RuntimeError):
    """Raised when a migration is blocked by deployment preflight."""

    def __init__(self, report: "MigrationPreflightReport"):
        super().__init__(report.reason)
        self.report = report


def _ensure_aware(value: datetime, field: str) -> datetime:
    if not isinstance(value, datetime):
        raise TypeError(f"{field} must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def _parse_timestamp(value: Any, field: str) -> datetime:
    if isinstance(value, datetime):
        return _ensure_aware(value, field)
    if isinstance(value, str):
        normalized = value.replace("Z", "+00:00")
        return _ensure_aware(datetime.fromisoformat(normalized), field)
    raise TypeError(f"{field} must be an ISO timestamp or datetime")


@dataclass(frozen=True)
class MigrationMetadata:
    """Metadata required before a migration can be deployed."""

    migration_id: str
    destructive: bool
    description: str = ""

    def __post_init__(self) -> None:
        if not self.migration_id:
            raise ValueError("migration metadata must include a migration id")
        if not isinstance(self.destructive, bool):
            raise TypeError("migration metadata destructive must be a boolean")

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "MigrationMetadata":
        if "destructive" not in data:
            raise ValueError("migration metadata must declare destructive")
        destructive = data["destructive"]
        if not isinstance(destructive, bool):
            raise TypeError("migration metadata destructive must be a boolean")

        migration_id = (
            data.get("migration_id") or data.get("id") or data.get("name")
        )
        if not migration_id:
            raise ValueError("migration metadata must include a migration id")

        return cls(
            migration_id=str(migration_id),
            destructive=destructive,
            description=str(data.get("description", "")),
        )


@dataclass(frozen=True)
class BackupStatus:
    """Backup metadata from backup and restore verification systems."""

    backup_id: str
    created_at: datetime
    restore_verified_at: Optional[datetime] = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "created_at",
            _ensure_aware(self.created_at, "created_at"),
        )
        if self.restore_verified_at is not None:
            object.__setattr__(
                self,
                "restore_verified_at",
                _ensure_aware(self.restore_verified_at, "restore_verified_at"),
            )

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "BackupStatus":
        backup_id = data.get("backup_id") or data.get("id")
        if not backup_id:
            raise ValueError("backup status must include a backup id")
        if "created_at" not in data:
            raise ValueError("backup status must include created_at")

        restore_verified_at = data.get("restore_verified_at")
        return cls(
            backup_id=str(backup_id),
            created_at=_parse_timestamp(data["created_at"], "created_at"),
            restore_verified_at=(
                _parse_timestamp(restore_verified_at, "restore_verified_at")
                if restore_verified_at
                else None
            ),
        )


@dataclass(frozen=True)
class MigrationPreflightReport:
    """Structured result for release tooling and operator logs."""

    migration_id: str
    destructive: bool
    allowed: bool
    backup_timestamp: Optional[datetime]
    restore_check_status: str
    reason: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "migration_id": self.migration_id,
            "destructive": self.destructive,
            "allowed": self.allowed,
            "backup_timestamp": (
                self.backup_timestamp.isoformat()
                if self.backup_timestamp
                else None
            ),
            "restore_check_status": self.restore_check_status,
            "reason": self.reason,
        }


def check_migration_preflight(
    migration: MigrationMetadata,
    backup: Optional[BackupStatus] = None,
    now: Optional[datetime] = None,
    max_backup_age: timedelta = DEFAULT_BACKUP_MAX_AGE,
) -> MigrationPreflightReport:
    """Return whether a migration may proceed.

    Destructive migrations require a backup that is both recent and restore
    verified. Non-destructive migrations do not require backup verification.
    """

    checked_at = _ensure_aware(now or datetime.now(timezone.utc), "now")

    if not migration.destructive:
        return MigrationPreflightReport(
            migration_id=migration.migration_id,
            destructive=False,
            allowed=True,
            backup_timestamp=backup.created_at if backup else None,
            restore_check_status="not_required",
            reason="migration is not destructive",
        )

    if backup is None:
        return MigrationPreflightReport(
            migration_id=migration.migration_id,
            destructive=True,
            allowed=False,
            backup_timestamp=None,
            restore_check_status="missing",
            reason=(
                "destructive migration requires a recent "
                "restore-verified backup"
            ),
        )

    backup_age = checked_at - backup.created_at
    if backup.created_at > checked_at:
        return MigrationPreflightReport(
            migration_id=migration.migration_id,
            destructive=True,
            allowed=False,
            backup_timestamp=backup.created_at,
            restore_check_status="invalid_timestamp",
            reason="backup timestamp is in the future",
        )

    if backup_age > max_backup_age:
        return MigrationPreflightReport(
            migration_id=migration.migration_id,
            destructive=True,
            allowed=False,
            backup_timestamp=backup.created_at,
            restore_check_status="stale",
            reason="backup is older than the allowed freshness window",
        )

    if backup.restore_verified_at is None:
        return MigrationPreflightReport(
            migration_id=migration.migration_id,
            destructive=True,
            allowed=False,
            backup_timestamp=backup.created_at,
            restore_check_status="not_verified",
            reason="backup has not passed restore verification",
        )

    restore_check_invalid = (
        backup.restore_verified_at < backup.created_at
        or backup.restore_verified_at > checked_at
    )
    if restore_check_invalid:
        return MigrationPreflightReport(
            migration_id=migration.migration_id,
            destructive=True,
            allowed=False,
            backup_timestamp=backup.created_at,
            restore_check_status="invalid_restore_check",
            reason="restore verification timestamp is outside the valid range",
        )

    return MigrationPreflightReport(
        migration_id=migration.migration_id,
        destructive=True,
        allowed=True,
        backup_timestamp=backup.created_at,
        restore_check_status="verified",
        reason="destructive migration has a recent restore-verified backup",
    )


def assert_migration_preflight(
    migration: MigrationMetadata,
    backup: Optional[BackupStatus] = None,
    now: Optional[datetime] = None,
    max_backup_age: timedelta = DEFAULT_BACKUP_MAX_AGE,
) -> MigrationPreflightReport:
    """Return the preflight report or raise when the migration is blocked."""

    report = check_migration_preflight(
        migration=migration,
        backup=backup,
        now=now,
        max_backup_age=max_backup_age,
    )
    if not report.allowed:
        raise MigrationPreflightError(report)
    return report
