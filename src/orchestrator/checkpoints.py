"""Idempotent checkpoint persistence for resumable worker tasks."""

import copy
import hashlib
import json
import logging
import threading
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from src.common.metrics import metrics

logger = logging.getLogger(__name__)


class CheckpointDigestMismatchError(ValueError):
    """Raised when a retry writes new data for an existing checkpoint key."""


@dataclass(frozen=True)
class CheckpointRecord:
    key: str
    task_id: str
    step_id: str
    attempt: int
    payload: Any
    digest: str
    created_at: float
    updated_at: float
    write_count: int = 1


class CheckpointStore:
    """Persist worker checkpoints under deterministic keys."""

    def __init__(self):
        self._records: Dict[str, CheckpointRecord] = {}
        self._audit_events: List[Dict[str, Any]] = []
        self._lock = threading.RLock()

    @staticmethod
    def make_key(task_id: str, step_id: str, attempt: int) -> str:
        return f"task:{task_id}:step:{step_id}:attempt:{attempt}"

    @staticmethod
    def content_digest(payload: Any) -> str:
        if isinstance(payload, (bytes, bytearray, memoryview)):
            encoded = bytes(payload)
        else:
            encoded = json.dumps(
                payload,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    @staticmethod
    def _clone_record(record: CheckpointRecord) -> CheckpointRecord:
        return CheckpointRecord(
            key=record.key,
            task_id=record.task_id,
            step_id=record.step_id,
            attempt=record.attempt,
            payload=copy.deepcopy(record.payload),
            digest=record.digest,
            created_at=record.created_at,
            updated_at=record.updated_at,
            write_count=record.write_count,
        )

    def write(
        self,
        task_id: str,
        step_id: str,
        attempt: int,
        payload: Any,
    ) -> CheckpointRecord:
        """Persist a checkpoint or upsert an identical retry."""
        key = self.make_key(task_id, step_id, attempt)
        digest = self.content_digest(payload)
        now = time.time()

        with self._lock:
            current = self._records.get(key)
            if current:
                if current.digest != digest:
                    self._audit("rejected", key, "digest_mismatch")
                    metrics.increment("checkpoint.write.digest_mismatch")
                    logger.error(
                        "checkpoint write rejected: digest_mismatch key=%s",
                        self._fingerprint(key),
                    )
                    raise CheckpointDigestMismatchError(
                        f"Checkpoint {key} already exists with digest "
                        f"{current.digest}; "
                        f"new digest {digest} does not match",
                    )
                record = CheckpointRecord(
                    key=current.key,
                    task_id=current.task_id,
                    step_id=current.step_id,
                    attempt=current.attempt,
                    payload=copy.deepcopy(current.payload),
                    digest=current.digest,
                    created_at=current.created_at,
                    updated_at=now,
                    write_count=current.write_count + 1,
                )
                self._records[key] = record
                self._audit("idempotent", key, "same_digest")
                metrics.increment("checkpoint.write.idempotent")
                return self._clone_record(record)

            record = CheckpointRecord(
                key=key,
                task_id=str(task_id),
                step_id=str(step_id),
                attempt=int(attempt),
                payload=copy.deepcopy(payload),
                digest=digest,
                created_at=now,
                updated_at=now,
            )
            self._records[key] = record
            self._audit("created", key, "new_key")
            metrics.increment("checkpoint.write.created")
            return self._clone_record(record)

    def get(
        self,
        task_id: str,
        step_id: str,
        attempt: int,
    ) -> Optional[CheckpointRecord]:
        key = self.make_key(task_id, step_id, attempt)
        with self._lock:
            record = self._records.get(key)
            if not record:
                return None
            return self._clone_record(record)

    def latest_for_step(
        self,
        task_id: str,
        step_id: str,
    ) -> Optional[CheckpointRecord]:
        with self._lock:
            matches: List[CheckpointRecord] = [
                record
                for record in self._records.values()
                if (
                    record.task_id == str(task_id)
                    and record.step_id == str(step_id)
                )
            ]
        if not matches:
            return None
        latest = max(
            matches,
            key=lambda record: (record.attempt, record.updated_at),
        )
        return self._clone_record(latest)

    def list_records(self) -> List[CheckpointRecord]:
        with self._lock:
            return [
                self._clone_record(record)
                for record in self._records.values()
            ]

    def list_audit_events(self) -> List[Dict[str, Any]]:
        with self._lock:
            return [dict(event) for event in self._audit_events]

    def _audit(self, decision: str, key: str, reason: str) -> None:
        self._audit_events.append(
            {
                "decision": decision,
                "reason": reason,
                "checkpoint": self._fingerprint(key),
                "timestamp": time.time(),
            },
        )

    @staticmethod
    def _fingerprint(key: str) -> str:
        return hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]
