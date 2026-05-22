"""Retention exception governance metadata."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, List, Optional, Union
from uuid import uuid4

from src.common.metrics import MetricsCollector, metrics


class RetentionExceptionValidationError(ValueError):
    """Raised when retention exception metadata violates governance policy."""


DateValue = Union[datetime, str]


@dataclass(frozen=True)
class RetentionException:
    """A temporary retention exception with accountable ownership metadata."""

    exception_id: str
    artifact_category: str
    owner: str
    reason: str
    expires_at: datetime
    review_date: datetime
    created_at: datetime

    def is_expired(self, now: datetime) -> bool:
        return self.expires_at <= _to_utc(now)

    def is_review_due(self, now: datetime) -> bool:
        return self.review_date <= _to_utc(now)

    def to_record(
        self,
        *,
        now: Optional[datetime] = None,
    ) -> Dict[str, object]:
        checked_at = _to_utc(now) if now is not None else None
        return {
            "exception_id": self.exception_id,
            "artifact_category": self.artifact_category,
            "owner": self.owner,
            "reason": self.reason,
            "expires_at": _iso_utc(self.expires_at),
            "review_date": _iso_utc(self.review_date),
            "created_at": _iso_utc(self.created_at),
            "review_due": (
                self.is_review_due(checked_at) if checked_at else False
            ),
        }


class RetentionExceptionRegistry:
    """Stores retention exceptions before governance reporting."""

    def __init__(self, metrics_collector: Optional[MetricsCollector] = None):
        self._exceptions: Dict[str, RetentionException] = {}
        self._audit_log: List[Dict[str, str]] = []
        self._metrics = metrics_collector or metrics

    def register_exception(
        self,
        *,
        artifact_category: str,
        owner: str,
        reason: str,
        expires_at: DateValue,
        review_date: DateValue,
        exception_id: Optional[str] = None,
        now: Optional[datetime] = None,
    ) -> Dict[str, object]:
        """Validate and store a retention exception transactionally."""

        checked_at = _to_utc(now)
        candidate_id = _clean_text(
            exception_id or str(uuid4()),
            "exception_id",
        )

        try:
            exception = RetentionException(
                exception_id=candidate_id,
                artifact_category=_clean_text(
                    artifact_category,
                    "artifact_category",
                ),
                owner=_clean_text(owner, "owner"),
                reason=_clean_text(reason, "reason"),
                expires_at=_parse_datetime(expires_at, "expires_at"),
                review_date=_parse_datetime(review_date, "review_date"),
                created_at=checked_at,
            )
            self._validate_exception(exception, now=checked_at)
        except RetentionExceptionValidationError as exc:
            self._record_decision(
                "rejected",
                candidate_id,
                owner=owner,
                artifact_category=artifact_category,
                reason_code=str(exc),
                now=checked_at,
            )
            self._metrics.increment("retention.exceptions.rejected")
            raise

        self._exceptions[exception.exception_id] = exception
        self._record_decision(
            "accepted",
            exception.exception_id,
            owner=exception.owner,
            artifact_category=exception.artifact_category,
            reason_code="valid",
            now=checked_at,
        )
        self._metrics.increment("retention.exceptions.accepted")
        return exception.to_record(now=checked_at)

    def validate_governance(
        self,
        *,
        now: Optional[datetime] = None,
    ) -> List[Dict[str, object]]:
        """Fail closed when any stored exception has expired."""

        checked_at = _to_utc(now)
        expired = [
            exception
            for exception in self._exceptions.values()
            if exception.is_expired(checked_at)
        ]
        if expired:
            for exception in expired:
                self._record_decision(
                    "expired",
                    exception.exception_id,
                    owner=exception.owner,
                    artifact_category=exception.artifact_category,
                    reason_code="expired",
                    now=checked_at,
                )
            self._metrics.increment(
                "retention.exceptions.expired",
                len(expired),
            )
            refs = ", ".join(sorted(item.exception_id for item in expired))
            raise RetentionExceptionValidationError(
                f"retention exception expired: {refs}"
            )

        return [
            exception.to_record(now=checked_at)
            for exception in sorted(
                self._exceptions.values(),
                key=lambda item: item.exception_id,
            )
        ]

    def active_exceptions_by_owner(
        self,
        *,
        now: Optional[datetime] = None,
    ) -> Dict[str, List[Dict[str, object]]]:
        """Return active exceptions grouped by accountable owner."""

        checked_at = _to_utc(now)
        grouped: Dict[str, List[Dict[str, object]]] = {}
        active = [
            exception
            for exception in self._exceptions.values()
            if not exception.is_expired(checked_at)
        ]
        for exception in sorted(
            active,
            key=lambda item: (
                item.owner,
                item.artifact_category,
                item.expires_at,
                item.exception_id,
            ),
        ):
            grouped.setdefault(exception.owner, []).append(
                exception.to_record(now=checked_at)
            )
        return grouped

    def list_exceptions(
        self,
        *,
        now: Optional[datetime] = None,
    ) -> List[Dict[str, object]]:
        checked_at = _to_utc(now)
        return [
            exception.to_record(now=checked_at)
            for exception in sorted(
                self._exceptions.values(),
                key=lambda item: item.exception_id,
            )
        ]

    def audit_log(self) -> List[Dict[str, str]]:
        return [dict(event) for event in self._audit_log]

    def _validate_exception(
        self,
        exception: RetentionException,
        *,
        now: datetime,
    ) -> None:
        if exception.expires_at <= now:
            raise RetentionExceptionValidationError(
                "expires_at must be in the future"
            )
        if exception.review_date > exception.expires_at:
            raise RetentionExceptionValidationError(
                "review_date must not be after expires_at"
            )

    def _record_decision(
        self,
        decision: str,
        exception_id: str,
        *,
        owner: object,
        artifact_category: object,
        reason_code: str,
        now: datetime,
    ) -> None:
        self._audit_log.append(
            {
                "decision": decision,
                "exception_ref": _stable_ref(exception_id),
                "owner_ref": _stable_ref(str(owner or "")),
                "artifact_category": str(artifact_category or ""),
                "reason_code": reason_code,
                "checked_at": _iso_utc(now),
            }
        )


def _clean_text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RetentionExceptionValidationError(f"{field} is required")
    return value.strip()


def _parse_datetime(value: DateValue, field: str) -> datetime:
    if value is None:
        raise RetentionExceptionValidationError(f"{field} is required")
    if isinstance(value, datetime):
        return _to_utc(value)
    if isinstance(value, str) and value.strip():
        try:
            return _to_utc(
                datetime.fromisoformat(value.replace("Z", "+00:00"))
            )
        except ValueError as exc:
            raise RetentionExceptionValidationError(
                f"{field} must be an ISO datetime"
            ) from exc
    raise RetentionExceptionValidationError(f"{field} must be an ISO datetime")


def _to_utc(value: Optional[datetime]) -> datetime:
    parsed = value or datetime.now(timezone.utc)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _iso_utc(value: datetime) -> str:
    return _to_utc(value).isoformat().replace("+00:00", "Z")


def _stable_ref(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]
