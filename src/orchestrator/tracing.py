"""Trace aggregation runtime with server-side memory limits."""

from dataclasses import dataclass, field
from enum import Enum
import json
from threading import RLock
import time
from typing import Any, Callable, Dict, Iterable, List, Optional


class TraceAggregationState(str, Enum):
    OPEN = "open"
    COMPLETED = "completed"
    REJECTED = "rejected"
    CANCELLED = "cancelled"


TERMINAL_STATES = {
    TraceAggregationState.COMPLETED,
    TraceAggregationState.REJECTED,
    TraceAggregationState.CANCELLED,
}


class TraceAggregationError(RuntimeError):
    """Base class for trace aggregation failures."""


class TraceMemoryLimitExceeded(TraceAggregationError):
    """Raised when a trace write would exceed the aggregation budget."""


class TraceAggregationClosed(TraceAggregationError):
    """Raised when a terminal aggregation is modified."""


@dataclass(frozen=True)
class TraceSpan:
    span_id: str
    name: str
    duration_ms: float
    attributes: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "span_id": self.span_id,
            "name": self.name,
            "duration_ms": self.duration_ms,
            "attributes": dict(self.attributes),
        }


@dataclass(frozen=True)
class TraceTerminalOutcome:
    run_id: str
    state: TraceAggregationState
    reason: str
    memory_used: int
    span_count: int
    finished_at: float

    def to_dict(self) -> Dict[str, Any]:
        return {
            "run_id": self.run_id,
            "state": self.state.value,
            "reason": self.reason,
            "memory_used": self.memory_used,
            "span_count": self.span_count,
            "finished_at": self.finished_at,
        }


class TraceAggregationRuntime:
    def __init__(
        self,
        memory_limit_bytes: int,
        clock: Optional[Callable[[], float]] = None,
    ):
        if memory_limit_bytes <= 0:
            raise ValueError("memory_limit_bytes must be positive")
        self.memory_limit_bytes = memory_limit_bytes
        self._clock = clock or time.time
        self._lock = RLock()
        self._spans: Dict[str, List[TraceSpan]] = {}
        self._memory_used: Dict[str, int] = {}
        self._states: Dict[str, TraceAggregationState] = {}
        self._terminal_outcomes: Dict[str, TraceTerminalOutcome] = {}

    def start(self, run_id: str) -> None:
        with self._lock:
            self._ensure_not_terminal(run_id)
            self._states.setdefault(run_id, TraceAggregationState.OPEN)
            self._spans.setdefault(run_id, [])
            self._memory_used.setdefault(run_id, 0)

    def append_span(self, run_id: str, span: TraceSpan) -> None:
        span_size = self.estimate_span_size(span)
        with self._lock:
            self._ensure_open(run_id)
            next_memory = self._memory_used[run_id] + span_size
            if next_memory > self.memory_limit_bytes:
                reason = (
                    f"trace memory limit exceeded: {next_memory} > "
                    f"{self.memory_limit_bytes}"
                )
                self._record_terminal(
                    run_id,
                    TraceAggregationState.REJECTED,
                    reason,
                )
                raise TraceMemoryLimitExceeded(reason)

            self._spans[run_id].append(span)
            self._memory_used[run_id] = next_memory

    def extend(self, run_id: str, spans: Iterable[TraceSpan]) -> None:
        for span in spans:
            self.append_span(run_id, span)

    def complete(self, run_id: str) -> bool:
        with self._lock:
            if self._states.get(run_id) != TraceAggregationState.OPEN:
                return False
            self._record_terminal(
                run_id,
                TraceAggregationState.COMPLETED,
                "completed",
            )
            return True

    def cancel(self, run_id: str) -> bool:
        with self._lock:
            if self._states.get(run_id) in TERMINAL_STATES:
                return False
            self.start(run_id)
            self._record_terminal(
                run_id,
                TraceAggregationState.CANCELLED,
                "cancelled",
            )
            return True

    def state(self, run_id: str) -> Optional[TraceAggregationState]:
        with self._lock:
            return self._states.get(run_id)

    def terminal_outcome(self, run_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            outcome = self._terminal_outcomes.get(run_id)
            return outcome.to_dict() if outcome else None

    def snapshot(self, run_id: str) -> Dict[str, Any]:
        with self._lock:
            spans = [span.to_dict() for span in self._spans.get(run_id, [])]
            return {
                "run_id": run_id,
                "state": (self._states.get(run_id) or "").value,
                "memory_used": self._memory_used.get(run_id, 0),
                "memory_limit": self.memory_limit_bytes,
                "spans": spans,
            }

    def estimate_span_size(self, span: TraceSpan) -> int:
        payload = json.dumps(
            span.to_dict(),
            sort_keys=True,
            separators=(",", ":"),
        )
        return len(payload.encode("utf-8"))

    def _ensure_open(self, run_id: str) -> None:
        self.start(run_id)
        self._ensure_not_terminal(run_id)

    def _ensure_not_terminal(self, run_id: str) -> None:
        state = self._states.get(run_id)
        if state in TERMINAL_STATES:
            raise TraceAggregationClosed(
                f"trace aggregation {run_id} is already {state.value}"
            )

    def _record_terminal(
        self,
        run_id: str,
        state: TraceAggregationState,
        reason: str,
    ) -> None:
        self._states[run_id] = state
        self._terminal_outcomes[run_id] = TraceTerminalOutcome(
            run_id=run_id,
            state=state,
            reason=reason,
            memory_used=self._memory_used.get(run_id, 0),
            span_count=len(self._spans.get(run_id, [])),
            finished_at=self._clock(),
        )
