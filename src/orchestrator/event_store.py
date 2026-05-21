"""Separate operational event logs from append-only audit records."""

from __future__ import annotations

import copy
import hashlib
import json
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional
from uuid import uuid4


@dataclass(frozen=True)
class EventRetentionPolicy:
    """Retention windows for the two event stores."""

    operational_seconds: float = 7 * 24 * 60 * 60
    audit_seconds: float = 365 * 24 * 60 * 60

    def __post_init__(self) -> None:
        if self.operational_seconds <= 0:
            raise ValueError("operational retention must be positive")
        if self.audit_seconds <= 0:
            raise ValueError("audit retention must be positive")
        if self.audit_seconds < self.operational_seconds:
            raise ValueError(
                "audit retention cannot be shorter than operational retention"
            )


class EventRetentionStore:
    """Task event storage with separate operational and audit paths.

    Operational logs are compactable telemetry for dashboards. Audit records
    are append-only evidence with a digest chain and are never removed by
    operational cleanup.
    """

    def __init__(
        self,
        retention_policy: Optional[EventRetentionPolicy] = None,
        clock: Callable[[], float] = time.time,
    ):
        self.retention_policy = retention_policy or EventRetentionPolicy()
        self._clock = clock
        self._operational_logs: List[Dict[str, Any]] = []
        self._audit_records: List[Dict[str, Any]] = []
        self._audit_sequence = 0
        self._latest_audit_digest: Optional[str] = None

    def record_task_event(
        self,
        event_type: str,
        task_id: str,
        payload: Optional[Dict[str, Any]] = None,
        actor: Optional[str] = None,
        severity: str = "info",
        audit: bool = True,
    ) -> Dict[str, Optional[Dict[str, Any]]]:
        """Write a task lifecycle event to the appropriate stores."""

        operational_log = self.write_operational_log(
            event_type=event_type,
            task_id=task_id,
            payload=payload,
            severity=severity,
        )
        audit_record = None
        if audit:
            audit_record = self.append_audit_record(
                event_type=event_type,
                task_id=task_id,
                payload=payload,
                actor=actor,
            )
        return {
            "operational_log": operational_log,
            "audit_record": audit_record,
        }

    def write_operational_log(
        self,
        event_type: str,
        task_id: str,
        payload: Optional[Dict[str, Any]] = None,
        severity: str = "info",
    ) -> Dict[str, Any]:
        """Append compactable operational telemetry."""

        record = {
            "id": str(uuid4()),
            "event_type": event_type,
            "task_id": task_id,
            "severity": severity,
            "payload": copy.deepcopy(payload or {}),
            "created_at": self._clock(),
        }
        self._operational_logs.append(record)
        return copy.deepcopy(record)

    def append_audit_record(
        self,
        event_type: str,
        task_id: str,
        payload: Optional[Dict[str, Any]] = None,
        actor: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Append immutable audit evidence to the digest chain."""

        self._audit_sequence += 1
        record = {
            "sequence": self._audit_sequence,
            "id": str(uuid4()),
            "event_type": event_type,
            "task_id": task_id,
            "actor": actor,
            "payload": copy.deepcopy(payload or {}),
            "created_at": self._clock(),
            "previous_digest": self._latest_audit_digest,
        }
        record["digest"] = self._digest(record)
        self._latest_audit_digest = record["digest"]
        self._audit_records.append(record)
        return copy.deepcopy(record)

    def operational_logs(self) -> List[Dict[str, Any]]:
        """Return a defensive copy of current operational logs."""

        return copy.deepcopy(self._operational_logs)

    def audit_records(self) -> List[Dict[str, Any]]:
        """Return a defensive copy of append-only audit records."""

        return copy.deepcopy(self._audit_records)

    def cleanup_operational_logs(
        self,
        now: Optional[float] = None,
        retention_seconds: Optional[float] = None,
    ) -> int:
        """Compact operational logs without touching the audit store."""

        retention = (
            retention_seconds
            if retention_seconds is not None
            else self.retention_policy.operational_seconds
        )
        if retention <= 0:
            raise ValueError("retention must be positive")

        cutoff = (self._clock() if now is None else now) - retention
        before = len(self._operational_logs)
        self._operational_logs = [
            record
            for record in self._operational_logs
            if record["created_at"] >= cutoff
        ]
        return before - len(self._operational_logs)

    def cleanup_expired(self, now: Optional[float] = None) -> Dict[str, int]:
        """Run scheduled cleanup using only operational retention settings."""

        return {
            "operational_deleted": self.cleanup_operational_logs(now=now),
            "audit_deleted": 0,
        }

    def audit_records_expired_before(
        self,
        now: Optional[float] = None,
    ) -> List[Dict[str, Any]]:
        """Report audit records past audit retention without deleting them."""

        cutoff = (
            (self._clock() if now is None else now)
            - self.retention_policy.audit_seconds
        )
        expired = [
            record
            for record in self._audit_records
            if record["created_at"] < cutoff
        ]
        return copy.deepcopy(expired)

    def verify_audit_chain(self) -> bool:
        """Validate every audit digest and link."""

        previous_digest = None
        for expected_sequence, record in enumerate(
            self._audit_records,
            start=1,
        ):
            if record["sequence"] != expected_sequence:
                return False
            if record["previous_digest"] != previous_digest:
                return False

            record_without_digest = dict(record)
            digest = record_without_digest.pop("digest")
            if digest != self._digest(record_without_digest):
                return False
            previous_digest = digest
        return True

    @staticmethod
    def _digest(record: Dict[str, Any]) -> str:
        encoded = json.dumps(
            record,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()
