import asyncio
import time

import pytest

from src.orchestrator.scheduler import TaskScheduler


class TestArtifactRetentionScheduling:
    def setup_method(self):
        self.scheduler = TaskScheduler()

    def _cleanup_task(self, **policy):
        return {
            "type": "artifact_cleanup",
            "artifact_retention": {
                "artifact_id": "artifact-123",
                "lifecycle_state": "completed",
                "cleanup_generation": 4,
                "policy_generation": 4,
                **policy,
            },
        }

    def test_rejects_cleanup_while_lifecycle_is_transitioning(self):
        task = self._cleanup_task(lifecycle_state="running")
        original_policy = dict(task["artifact_retention"])

        with pytest.raises(ValueError, match="lifecycle-state-not-terminal"):
            self.scheduler.schedule(task, delay=0)

        assert task["artifact_retention"] == original_policy
        assert (
            self.scheduler.audit_records()[-1]["reason"]
            == "lifecycle-state-not-terminal"
        )

    def test_rejects_cleanup_before_retention_window_expires(self):
        task = self._cleanup_task(retain_until=time.time() + 3600)

        with pytest.raises(ValueError, match="retention-window-active"):
            self.scheduler.schedule(task, delay=0)

        audit = self.scheduler.audit_records()[-1]
        assert audit["event"] == "cleanup_rejected"
        assert audit["artifact_ref"].startswith("artifact:")
        assert "artifact-123" not in str(audit)

    def test_rejects_stale_cleanup_generation(self):
        task = self._cleanup_task(cleanup_generation=3, policy_generation=4)

        with pytest.raises(ValueError, match="stale-cleanup-generation"):
            self.scheduler.schedule(task, delay=0)

        assert (
            self.scheduler.audit_records()[-1]["reason"]
            == "stale-cleanup-generation"
        )

    def test_rejects_duplicate_cleanup_schedules_for_same_artifact(self):
        self.scheduler.schedule(self._cleanup_task(), delay=60)

        with pytest.raises(ValueError, match="duplicate-cleanup-scheduled"):
            self.scheduler.schedule(self._cleanup_task(), delay=60)

        assert (
            self.scheduler.audit_records()[-1]["reason"]
            == "duplicate-cleanup-scheduled"
        )

    def test_allows_due_cleanup_after_terminal_policy_validation(self):
        task_id = self.scheduler.schedule(self._cleanup_task(), delay=-1)

        task = asyncio.run(self.scheduler.dequeue())

        assert task is not None
        assert task["id"] == task_id
        assert task["artifact_retention"]["artifact_id"] == "artifact-123"
        assert self.scheduler.complete(task_id)
        assert self.scheduler.schedule(self._cleanup_task(), delay=60)

    def test_non_cleanup_tasks_are_not_blocked_by_retention_validator(self):
        task_id = self.scheduler.schedule(
            {"type": "send_email", "state": "running"},
            delay=-1,
        )

        task = asyncio.run(self.scheduler.dequeue())

        assert task["id"] == task_id
