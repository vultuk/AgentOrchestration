"""Artifact retention policy validation for cleanup scheduling."""

from dataclasses import dataclass
import hashlib
import time
from typing import Any, Dict, Mapping, Optional


CLEANUP_TASK_TYPES = {
    "artifact_cleanup",
    "artifact_retention_cleanup",
    "cleanup_artifact",
    "cleanup_artifacts",
    "retention_cleanup",
}

TRANSITIONAL_STATES = {
    "accepted",
    "binding",
    "dispatching",
    "enqueued",
    "in_progress",
    "pending",
    "queued",
    "running",
    "scheduling",
    "starting",
    "transitioning",
}

TERMINAL_STATES = {
    "cancelled",
    "canceled",
    "completed",
    "deleted",
    "expired",
    "failed",
    "finished",
    "skipped",
    "succeeded",
    "terminated",
}


@dataclass(frozen=True)
class ArtifactRetentionDecision:
    """Result of validating an artifact cleanup scheduling attempt."""

    allowed: bool
    action: str
    reason: str
    artifact_id: Optional[str] = None
    lifecycle_state: Optional[str] = None

    def audit_context(self) -> Dict[str, Optional[str]]:
        return {
            "action": self.action,
            "reason": self.reason,
            "artifact_ref": self.artifact_ref(),
            "lifecycle_state": self.lifecycle_state,
        }

    def artifact_ref(self) -> Optional[str]:
        if not self.artifact_id:
            return None
        digest = hashlib.sha256(self.artifact_id.encode("utf-8")).hexdigest()
        return f"artifact:{digest[:12]}"


class ArtifactRetentionPolicyValidator:
    """Rejects stale or policy-violating artifact cleanup transitions."""

    POLICY_KEYS = ("artifact_retention", "artifact_policy", "retention_policy")
    ARTIFACT_ID_KEYS = (
        "artifact_id",
        "artifact",
        "artifact_key",
        "resource_id",
    )
    LIFECYCLE_STATE_KEYS = (
        "lifecycle_state",
        "run_state",
        "task_state",
        "handler_state",
        "state",
    )
    RETAIN_UNTIL_KEYS = (
        "retain_until",
        "retention_until",
        "delete_after",
        "cleanup_after",
    )
    CLEANUP_GENERATION_KEYS = (
        "cleanup_generation",
        "cleanup_version",
        "scheduled_generation",
    )
    POLICY_GENERATION_KEYS = (
        "policy_generation",
        "current_generation",
        "expected_generation",
    )

    def validate_cleanup_schedule(
        self,
        task: Mapping[str, Any],
        now: Optional[float] = None,
    ) -> ArtifactRetentionDecision:
        if not self.is_cleanup_task(task):
            return ArtifactRetentionDecision(
                True,
                "allow",
                "not-artifact-cleanup",
            )

        policy = self._policy(task)
        artifact_id = self.artifact_id(task)
        lifecycle_state = self._string_value(
            policy,
            task,
            *self.LIFECYCLE_STATE_KEYS,
        )
        lifecycle_state = lifecycle_state.lower() if lifecycle_state else None

        if not artifact_id:
            return ArtifactRetentionDecision(
                False,
                "reject",
                "missing-artifact-id",
                lifecycle_state=lifecycle_state,
            )

        if lifecycle_state in TRANSITIONAL_STATES:
            return ArtifactRetentionDecision(
                False,
                "reject",
                "lifecycle-state-not-terminal",
                artifact_id=artifact_id,
                lifecycle_state=lifecycle_state,
            )

        if lifecycle_state and lifecycle_state not in TERMINAL_STATES:
            return ArtifactRetentionDecision(
                False,
                "reject",
                "unknown-lifecycle-state",
                artifact_id=artifact_id,
                lifecycle_state=lifecycle_state,
            )

        retain_until = self._float_value(policy, task, *self.RETAIN_UNTIL_KEYS)
        current_time = now if now is not None else time.time()
        if retain_until is not None and retain_until > current_time:
            return ArtifactRetentionDecision(
                False,
                "reject",
                "retention-window-active",
                artifact_id=artifact_id,
                lifecycle_state=lifecycle_state,
            )

        cleanup_generation = self._int_value(
            policy,
            task,
            *self.CLEANUP_GENERATION_KEYS,
        )
        policy_generation = self._int_value(
            policy,
            task,
            *self.POLICY_GENERATION_KEYS,
        )
        if (
            cleanup_generation is not None
            and policy_generation is not None
            and cleanup_generation != policy_generation
        ):
            return ArtifactRetentionDecision(
                False,
                "reject",
                "stale-cleanup-generation",
                artifact_id=artifact_id,
                lifecycle_state=lifecycle_state,
            )

        return ArtifactRetentionDecision(
            True,
            "allow",
            "artifact-cleanup-policy-valid",
            artifact_id=artifact_id,
            lifecycle_state=lifecycle_state,
        )

    def is_cleanup_task(self, task: Mapping[str, Any]) -> bool:
        task_type = str(task.get("type", "")).lower().replace("-", "_")
        return (
            bool(task.get("artifact_cleanup"))
            or task_type in CLEANUP_TASK_TYPES
            or ("artifact" in task_type and "cleanup" in task_type)
            or any(key in task for key in self.POLICY_KEYS)
        )

    def artifact_id(self, task: Mapping[str, Any]) -> Optional[str]:
        policy = self._policy(task)
        return self._string_value(policy, task, *self.ARTIFACT_ID_KEYS)

    def _policy(self, task: Mapping[str, Any]) -> Mapping[str, Any]:
        for key in self.POLICY_KEYS:
            value = task.get(key)
            if isinstance(value, Mapping):
                return value
        return task

    def _string_value(
        self,
        policy: Mapping[str, Any],
        task: Mapping[str, Any],
        *keys: str,
    ) -> Optional[str]:
        value = self._first_value(policy, task, *keys)
        if value is None:
            return None
        text = str(value).strip()
        return text or None

    def _float_value(
        self,
        policy: Mapping[str, Any],
        task: Mapping[str, Any],
        *keys: str,
    ) -> Optional[float]:
        value = self._first_value(policy, task, *keys)
        if value is None:
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    def _int_value(
        self,
        policy: Mapping[str, Any],
        task: Mapping[str, Any],
        *keys: str,
    ) -> Optional[int]:
        value = self._first_value(policy, task, *keys)
        if value is None:
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    def _first_value(
        self,
        policy: Mapping[str, Any],
        task: Mapping[str, Any],
        *keys: str,
    ) -> Any:
        for key in keys:
            if key in policy:
                return policy[key]
        for key in keys:
            if key in task:
                return task[key]
        return None
