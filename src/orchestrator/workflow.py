"""Workflow Manager — Defines and executes multi-step agent workflows."""

from enum import Enum
from typing import Any, Callable, Dict, List, Optional
from uuid import uuid4

RESERVED_METADATA_KEYS = {
    "attempt",
    "handler",
    "id",
    "lifecycle",
    "queue",
    "result",
    "retries",
    "routing",
    "run_id",
    "state",
    "status",
    "step_id",
    "task_id",
    "timeout",
    "transition",
    "workflow_id",
}


class StepStatus(Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"


class WorkflowMetadataError(ValueError):
    def __init__(self, violations: List[Dict[str, str]]):
        self.violations = violations
        paths = ", ".join(item["path"] for item in violations)
        super().__init__(
            f"Reserved workflow metadata keys are not allowed: {paths}",
        )


def _normalize_metadata_key(key: Any) -> str:
    return str(key).strip().lower().replace("-", "_")


def find_reserved_metadata_keys(
    metadata: Optional[Dict[str, Any]],
    path: str = "metadata",
) -> List[Dict[str, str]]:
    if not metadata:
        return []
    if not isinstance(metadata, dict):
        return [
            {
                "path": path,
                "key": path.rsplit(".", 1)[-1],
                "reason": "metadata_must_be_mapping",
            },
        ]

    violations: List[Dict[str, str]] = []
    for key, value in metadata.items():
        key_name = str(key)
        key_path = f"{path}.{key_name}"
        normalized = _normalize_metadata_key(key_name)
        if normalized in RESERVED_METADATA_KEYS or normalized.startswith("__"):
            violations.append(
                {
                    "path": key_path,
                    "key": key_name,
                    "reason": "reserved_metadata_key",
                },
            )
            continue
        if isinstance(value, dict):
            violations.extend(find_reserved_metadata_keys(value, key_path))
    return violations


def validate_user_metadata(
    metadata: Optional[Dict[str, Any]],
    path: str,
) -> None:
    violations = find_reserved_metadata_keys(metadata, path)
    if violations:
        raise WorkflowMetadataError(violations)


class WorkflowStep:
    def __init__(
        self,
        name: str,
        handler: Callable,
        retries: int = 0,
        timeout: int = 300,
        metadata: Optional[Dict[str, Any]] = None,
    ):
        self.id = str(uuid4())
        self.name = name
        self.handler = handler
        self.retries = retries
        self.timeout = timeout
        self.status = StepStatus.PENDING
        self.result: Any = None
        self.error: Optional[str] = None
        self.metadata = dict(metadata or {})


class Workflow:
    def __init__(
        self,
        name: str,
        description: str = "",
        metadata: Optional[Dict[str, Any]] = None,
    ):
        self.id = str(uuid4())
        self.name = name
        self.description = description
        self.metadata = dict(metadata or {})
        self.steps: List[WorkflowStep] = []
        self._step_map: Dict[str, WorkflowStep] = {}
        self.status = StepStatus.PENDING
        self.audit_records: List[Dict[str, str]] = []

    def add_step(self, step: WorkflowStep) -> "Workflow":
        try:
            validate_user_metadata(
                step.metadata,
                f"workflow.steps.{step.name}.metadata",
            )
        except WorkflowMetadataError as exc:
            self.record_metadata_rejection(exc)
            raise
        self.steps.append(step)
        self._step_map[step.id] = step
        return self

    def get_step(self, step_id: str) -> Optional[WorkflowStep]:
        return self._step_map.get(step_id)

    def validate_definition_metadata(self) -> None:
        violations = find_reserved_metadata_keys(
            self.metadata,
            "workflow.metadata",
        )
        for step in self.steps:
            violations.extend(
                find_reserved_metadata_keys(
                    step.metadata,
                    f"workflow.steps.{step.name}.metadata",
                ),
            )
        if violations:
            raise WorkflowMetadataError(violations)

    def record_metadata_rejection(self, error: WorkflowMetadataError) -> None:
        for violation in error.violations:
            self.audit_records.append(
                {
                    "event": "workflow_metadata_rejected",
                    "workflow_id": self.id,
                    "path": violation["path"],
                    "key": violation["key"],
                    "reason": violation["reason"],
                    "status": self.status.value,
                },
            )


class WorkflowManager:
    def __init__(self):
        self._workflows: Dict[str, Workflow] = {}
        self.audit_records: List[Dict[str, str]] = []

    def create_workflow(
        self,
        name: str,
        description: str = "",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Workflow:
        workflow = Workflow(name, description, metadata)
        try:
            workflow.validate_definition_metadata()
        except WorkflowMetadataError as exc:
            workflow.record_metadata_rejection(exc)
            self.audit_records.extend(workflow.audit_records)
            raise
        self._workflows[workflow.id] = workflow
        return workflow

    def get_workflow(self, workflow_id: str) -> Optional[Workflow]:
        return self._workflows.get(workflow_id)

    def list_workflows(self) -> List[Workflow]:
        return list(self._workflows.values())

    def delete_workflow(self, workflow_id: str) -> bool:
        return self._workflows.pop(workflow_id, None) is not None

    def execute_workflow(self, workflow_id: str) -> bool:
        workflow = self._workflows.get(workflow_id)
        if not workflow:
            return False

        try:
            workflow.validate_definition_metadata()
        except WorkflowMetadataError as exc:
            workflow.record_metadata_rejection(exc)
            self.audit_records.extend(
                workflow.audit_records[-len(exc.violations):],
            )
            return False

        workflow.status = StepStatus.RUNNING
        for step in workflow.steps:
            step.status = StepStatus.RUNNING
            try:
                result = step.handler()
                step.result = result
                step.status = StepStatus.COMPLETED
            except Exception as e:
                step.error = str(e)
                step.status = StepStatus.FAILED
                workflow.status = StepStatus.FAILED
                return False

        workflow.status = StepStatus.COMPLETED
        return True

# 2019-03-27T19:58:07 update

# 2019-05-09T09:42:56 update

# 2019-12-03T10:07:42 update

# 2020-01-16T18:43:28 update

# 2020-03-20T10:40:15 update

# 2020-04-17T15:36:50 update

# 2020-05-04T14:44:01 update

# 2020-06-16T13:17:31 update

# 2020-08-05T17:00:24 update

# 2020-09-04T08:29:23 update

# 2020-09-09T17:52:02 update

# 2020-10-23T10:57:44 update

# 2020-12-05T20:55:47 update

# 2021-01-15T19:23:40 update

# 2021-02-03T20:43:12 update

# 2021-03-16T12:26:47 update

# 2021-04-20T14:33:28 update

# 2021-10-14T15:03:32 update

# 2021-10-21T17:24:55 update

# 2021-11-16T17:01:08 update

# 2021-11-22T09:51:21 update

# 2021-12-21T16:15:47 update

# 2022-03-23T16:52:27 update

# 2022-12-21T09:25:50 update

# 2023-01-09T09:55:25 update

# 2023-01-13T11:06:15 update

# 2023-01-26T11:00:59 update

# 2023-02-23T08:56:54 update

# 2023-05-17T08:07:16 update

# 2023-06-06T17:09:34 update

# 2023-06-13T10:35:28 update

# 2023-08-24T20:36:06 update

# 2023-10-30T19:10:13 update

# 2024-01-02T08:27:25 update

# 2024-01-24T12:13:15 update

# 2024-02-08T13:35:49 update

# 2024-05-07T16:09:24 update

# 2024-05-11T09:48:46 update

# 2024-05-21T19:25:41 update

# 2024-06-05T12:00:30 update

# 2024-06-25T09:40:26 update

# 2024-09-17T13:49:39 update

# 2024-10-14T17:39:35 update

# 2024-11-27T20:14:35 update

# 2024-12-25T19:31:41 update

# 2025-01-16T13:15:09 update

# 2025-02-05T14:06:59 update

# 2025-02-17T20:55:11 update

# 2025-04-30T19:36:53 update

# 2025-07-17T10:14:40 update

# 2025-08-29T12:13:15 update

# 2025-09-03T13:51:11 update

# 2025-09-19T16:08:24 update

# 2025-11-27T08:38:12 update

# 2026-01-27T13:23:38 update

# 2026-01-28T11:22:50 update
