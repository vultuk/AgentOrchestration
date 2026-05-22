"""Shared event-bus intake guards for orchestrator lifecycle transitions."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from hashlib import sha256
from typing import Any, Dict, List, Optional

from src.common.metrics import MetricsCollector, metrics


TERMINAL_STATES = {"completed", "failed", "cancelled"}
ALLOWED_TRANSITIONS = {
    "pending": {"running", "cancelled"},
    "running": {"completed", "failed", "cancelled"},
}


@dataclass(frozen=True)
class EventDecision:
    accepted: bool
    reason: str
    run_id: str
    lifecycle: str
    revision: int
    attempt: int


class EventIntake:
    def __init__(self, metrics_collector: MetricsCollector = metrics):
        self._states: Dict[str, Dict[str, Any]] = {}
        self._audit: List[Dict[str, Any]] = []
        self._metrics = metrics_collector

    @property
    def audit_records(self) -> List[Dict[str, Any]]:
        return deepcopy(self._audit)

    def get_state(self, run_id: str) -> Optional[Dict[str, Any]]:
        state = self._states.get(run_id)
        return deepcopy(state) if state is not None else None

    def ingest(self, event: Dict[str, Any]) -> EventDecision:
        normalized = self._normalize_event(event)
        current = self._states.get(normalized["run_id"])

        decision = self._validate(normalized, current)
        if decision.accepted:
            self._states[normalized["run_id"]] = {
                "tenant_id": normalized["tenant_id"],
                "attempt": normalized["attempt"],
                "revision": normalized["revision"],
                "lifecycle": normalized["lifecycle"],
            }
            self._metrics.increment("event_intake.accepted")
        else:
            self._metrics.increment("event_intake.rejected")

        self._audit_decision(normalized, decision)
        return decision

    def _validate(
        self,
        event: Dict[str, Any],
        current: Optional[Dict[str, Any]],
    ) -> EventDecision:
        if current and event["tenant_id"] != current["tenant_id"]:
            return self._decision(event, False, "tenant_mismatch")

        if current and event["attempt"] < current["attempt"]:
            return self._decision(event, False, "stale_attempt")

        if current and event["attempt"] == current["attempt"]:
            if event["revision"] < current["revision"]:
                return self._decision(event, False, "stale_revision")
            if event["revision"] == current["revision"]:
                return self._decision(event, False, "duplicate_revision")

        if current and not self._transition_allowed(
            current["lifecycle"],
            event["lifecycle"],
        ):
            return self._decision(event, False, "invalid_lifecycle")

        return self._decision(event, True, "accepted")

    def _transition_allowed(self, current: str, next_state: str) -> bool:
        if current == next_state:
            return False
        if current in TERMINAL_STATES:
            return False
        return next_state in ALLOWED_TRANSITIONS.get(current, set())

    def _normalize_event(self, event: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "event_id": self._required_text(event, "event_id"),
            "run_id": self._required_text(event, "run_id"),
            "tenant_id": self._required_text(event, "tenant_id"),
            "attempt": int(event.get("attempt", 0)),
            "revision": int(event.get("revision", 0)),
            "lifecycle": self._required_text(event, "lifecycle"),
        }

    def _required_text(self, event: Dict[str, Any], field: str) -> str:
        value = str(event.get(field, "")).strip()
        if not value:
            raise ValueError(f"{field} is required")
        return value

    def _decision(
        self,
        event: Dict[str, Any],
        accepted: bool,
        reason: str,
    ) -> EventDecision:
        return EventDecision(
            accepted=accepted,
            reason=reason,
            run_id=event["run_id"],
            lifecycle=event["lifecycle"],
            revision=event["revision"],
            attempt=event["attempt"],
        )

    def _audit_decision(
        self,
        event: Dict[str, Any],
        decision: EventDecision,
    ) -> None:
        self._audit.append(
            {
                "event_id": event["event_id"],
                "run_id": event["run_id"],
                "tenant_hash": sha256(
                    event["tenant_id"].encode("utf-8"),
                ).hexdigest()[:12],
                "accepted": decision.accepted,
                "reason": decision.reason,
                "attempt": decision.attempt,
                "revision": decision.revision,
                "lifecycle": decision.lifecycle,
            },
        )
