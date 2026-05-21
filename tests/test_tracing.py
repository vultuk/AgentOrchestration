from concurrent.futures import ThreadPoolExecutor

import pytest

from src.orchestrator.tracing import (
    TraceAggregationClosed,
    TraceAggregationRuntime,
    TraceAggregationState,
    TraceMemoryLimitExceeded,
    TraceSpan,
)


def span(span_id: str, payload: str = "") -> TraceSpan:
    return TraceSpan(
        span_id=span_id,
        name="worker.step",
        duration_ms=12.5,
        attributes={"payload": payload},
    )


class TestTraceAggregationRuntime:
    def test_appends_spans_within_memory_limit(self):
        runtime = TraceAggregationRuntime(memory_limit_bytes=4096)

        runtime.append_span("run-1", span("s1"))
        runtime.append_span("run-1", span("s2"))

        snapshot = runtime.snapshot("run-1")
        assert snapshot["state"] == "open"
        assert snapshot["memory_used"] <= snapshot["memory_limit"]
        assert [item["span_id"] for item in snapshot["spans"]] == ["s1", "s2"]

    def test_rejects_before_storing_span_that_exceeds_memory_limit(self):
        runtime = TraceAggregationRuntime(
            memory_limit_bytes=220,
            clock=lambda: 50.0,
        )
        runtime.append_span("run-2", span("s1", payload="small"))

        existing = runtime.snapshot("run-2")
        with pytest.raises(TraceMemoryLimitExceeded):
            runtime.append_span("run-2", span("s2", payload="x" * 500))

        snapshot = runtime.snapshot("run-2")
        assert snapshot["state"] == "rejected"
        assert snapshot["spans"] == existing["spans"]
        assert snapshot["memory_used"] == existing["memory_used"]

        outcome = runtime.terminal_outcome("run-2")
        assert outcome["state"] == "rejected"
        assert outcome["span_count"] == 1
        assert outcome["finished_at"] == 50.0

    def test_terminal_aggregation_rejects_late_retries(self):
        runtime = TraceAggregationRuntime(memory_limit_bytes=4096)
        runtime.append_span("run-3", span("s1"))
        assert runtime.complete("run-3")

        with pytest.raises(TraceAggregationClosed):
            runtime.append_span("run-3", span("retry"))

        outcome = runtime.terminal_outcome("run-3")
        assert outcome["state"] == "completed"
        assert outcome["span_count"] == 1

    def test_cancel_records_terminal_outcome_and_leaves_no_open_state(self):
        runtime = TraceAggregationRuntime(memory_limit_bytes=4096)
        runtime.append_span("run-4", span("s1"))

        assert runtime.cancel("run-4")
        assert runtime.state("run-4") == TraceAggregationState.CANCELLED
        assert not runtime.cancel("run-4")
        assert runtime.terminal_outcome("run-4")["state"] == "cancelled"

        with pytest.raises(TraceAggregationClosed):
            runtime.append_span("run-4", span("late"))

    def test_concurrent_appends_do_not_exceed_budget(self):
        runtime = TraceAggregationRuntime(memory_limit_bytes=600)
        spans = [span(f"s{i}", payload="x" * 40) for i in range(20)]

        def append(candidate: TraceSpan) -> bool:
            try:
                runtime.append_span("run-5", candidate)
                return True
            except (TraceAggregationClosed, TraceMemoryLimitExceeded):
                return False

        with ThreadPoolExecutor(max_workers=8) as executor:
            results = list(executor.map(append, spans))

        snapshot = runtime.snapshot("run-5")
        assert any(results)
        assert not all(results)
        assert snapshot["memory_used"] <= snapshot["memory_limit"]
        assert runtime.terminal_outcome("run-5")["state"] == "rejected"
