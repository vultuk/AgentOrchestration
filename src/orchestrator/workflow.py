"""Workflow Manager — Defines and executes multi-step agent workflows."""

import logging
import re
from enum import Enum
from typing import Any, Callable, Dict, List, Mapping, Optional
from uuid import uuid4

from src.common.metrics import metrics

logger = logging.getLogger(__name__)

_DURATION_PATTERN = re.compile(
    r"^\s*(?P<value>\d+(?:\.\d+)?)\s*"
    r"(?P<unit>ms|s|sec|secs|m|min|mins|h|hr|hrs)\s*$",
)
_TIMEOUT_UNITS = {
    "timeout": 1.0,
    "timeout_seconds": 1.0,
    "timeout_sec": 1.0,
    "timeout_minutes": 60.0,
    "timeout_min": 60.0,
    "timeout_ms": 0.001,
    "timeout_milliseconds": 0.001,
}
_NESTED_TIMEOUT_UNITS = {
    "milliseconds": 0.001,
    "ms": 0.001,
    "seconds": 1.0,
    "second": 1.0,
    "sec": 1.0,
    "minutes": 60.0,
    "minute": 60.0,
    "min": 60.0,
    "hours": 3600.0,
    "hour": 3600.0,
    "hr": 3600.0,
}


class StepStatus(Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"


class WorkflowDefinitionError(ValueError):
    """Raised when workflow timing data is unsafe to register or dispatch."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _provided_keys(
    definition: Mapping[str, Any],
    allowed: Mapping[str, float],
) -> List[str]:
    return [
        key for key in allowed
        if key in definition and definition[key] is not None
    ]


def parse_timeout_duration(
    definition: Mapping[str, Any],
    default: float = 300.0,
) -> float:
    """Parse one timeout representation into seconds.

    Definitions may use one timeout unit only. Accepting multiple unit fields
    can silently change execution policy, so conflicting units are rejected
    before a workflow can be registered or dispatched.
    """

    keys = _provided_keys(definition, _TIMEOUT_UNITS)
    if not keys:
        return float(default)

    if len(keys) > 1:
        raise WorkflowDefinitionError(
            "conflicting_timeout_units",
            "workflow step defines multiple timeout units",
        )

    key = keys[0]
    raw_value = definition[key]
    if key == "timeout" and isinstance(raw_value, Mapping):
        nested_keys = _provided_keys(raw_value, _NESTED_TIMEOUT_UNITS)
        if len(nested_keys) != 1:
            raise WorkflowDefinitionError(
                "conflicting_timeout_units",
                "workflow step timeout mapping must define exactly one unit",
            )
        nested_key = nested_keys[0]
        return _positive_seconds(
            raw_value[nested_key],
            _NESTED_TIMEOUT_UNITS[nested_key],
        )

    if key == "timeout" and isinstance(raw_value, str):
        return _parse_duration_string(raw_value)

    return _positive_seconds(raw_value, _TIMEOUT_UNITS[key])


def _parse_duration_string(value: str) -> float:
    match = _DURATION_PATTERN.match(value)
    if not match:
        raise WorkflowDefinitionError(
            "invalid_timeout",
            "workflow step timeout string must include a supported unit",
        )

    amount = float(match.group("value"))
    unit = match.group("unit")
    multiplier = {
        "ms": 0.001,
        "s": 1.0,
        "sec": 1.0,
        "secs": 1.0,
        "m": 60.0,
        "min": 60.0,
        "mins": 60.0,
        "h": 3600.0,
        "hr": 3600.0,
        "hrs": 3600.0,
    }[unit]
    return _positive_seconds(amount, multiplier)


def _positive_seconds(value: Any, multiplier: float) -> float:
    if isinstance(value, bool):
        raise WorkflowDefinitionError(
            "invalid_timeout",
            "workflow step timeout must be numeric",
        )
    try:
        seconds = float(value) * multiplier
    except (TypeError, ValueError):
        raise WorkflowDefinitionError(
            "invalid_timeout",
            "workflow step timeout must be numeric",
        )

    if seconds <= 0:
        raise WorkflowDefinitionError(
            "invalid_timeout",
            "workflow step timeout must be positive",
        )
    return seconds


class WorkflowStep:
    def __init__(
        self,
        name: str,
        handler: Callable,
        retries: int = 0,
        timeout: float = 300.0,
    ):
        self.id = str(uuid4())
        self.name = name
        self.handler = handler
        self.retries = retries
        self.timeout = timeout
        self.status = StepStatus.PENDING
        self.result: Any = None
        self.error: Optional[str] = None


class Workflow:
    def __init__(self, name: str, description: str = ""):
        self.id = str(uuid4())
        self.name = name
        self.description = description
        self.steps: List[WorkflowStep] = []
        self._step_map: Dict[str, WorkflowStep] = {}
        self.status = StepStatus.PENDING

    def add_step(self, step: WorkflowStep) -> "Workflow":
        self.steps.append(step)
        self._step_map[step.id] = step
        return self

    def get_step(self, step_id: str) -> Optional[WorkflowStep]:
        return self._step_map.get(step_id)


class WorkflowManager:
    def __init__(self):
        self._workflows: Dict[str, Workflow] = {}
        self._audit_log: List[Dict[str, Any]] = []

    @property
    def audit_decisions(self) -> List[Dict[str, Any]]:
        return list(self._audit_log)

    def create_workflow(self, name: str, description: str = "") -> Workflow:
        workflow = Workflow(name, description)
        self._workflows[workflow.id] = workflow
        return workflow

    def register_workflow_definition(
        self,
        definition: Mapping[str, Any],
    ) -> Workflow:
        name = str(definition.get("name") or "").strip()
        if not name:
            self._record_definition_rejection("", "", "invalid_name")
            raise WorkflowDefinitionError(
                "invalid_name",
                "workflow definition requires a name",
            )

        workflow = Workflow(name, str(definition.get("description") or ""))
        for index, raw_step in enumerate(definition.get("steps") or []):
            if not isinstance(raw_step, Mapping):
                self._record_definition_rejection(
                    workflow.name,
                    "",
                    "invalid_step",
                )
                raise WorkflowDefinitionError(
                    "invalid_step",
                    "workflow step definition must be an object",
                )

            step_name = str(raw_step.get("name") or f"step-{index + 1}")
            try:
                timeout = parse_timeout_duration(raw_step)
            except WorkflowDefinitionError as exc:
                self._record_definition_rejection(
                    workflow.name,
                    step_name,
                    exc.code,
                )
                raise

            handler = raw_step.get("handler")
            if not callable(handler):
                self._record_definition_rejection(
                    workflow.name,
                    step_name,
                    "invalid_handler",
                )
                raise WorkflowDefinitionError(
                    "invalid_handler",
                    "workflow step requires a callable handler",
                )

            workflow.add_step(
                WorkflowStep(
                    step_name,
                    handler,
                    retries=int(raw_step.get("retries") or 0),
                    timeout=timeout,
                ),
            )

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
        if not self._validate_workflow_for_dispatch(workflow):
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

    def _validate_workflow_for_dispatch(self, workflow: Workflow) -> bool:
        for step in workflow.steps:
            if isinstance(step.timeout, (Mapping, str)):
                try:
                    step.timeout = parse_timeout_duration(
                        {"timeout": step.timeout},
                    )
                except WorkflowDefinitionError as exc:
                    self._record_definition_rejection(
                        workflow.name,
                        step.name,
                        exc.code,
                        workflow.id,
                        step.id,
                    )
                    return False

            try:
                _positive_seconds(step.timeout, 1.0)
            except WorkflowDefinitionError as exc:
                self._record_definition_rejection(
                    workflow.name,
                    step.name,
                    exc.code,
                    workflow.id,
                    step.id,
                )
                return False
        return True

    def _record_definition_rejection(
        self,
        workflow_name: str,
        step_name: str,
        reason: str,
        workflow_id: str = "",
        step_id: str = "",
    ) -> None:
        decision = {
            "event": "workflow_definition_rejected",
            "reason": reason,
            "workflow_id": workflow_id,
            "workflow_name": workflow_name,
            "step_id": step_id,
            "step_name": step_name,
        }
        self._audit_log.append(decision)
        metrics.increment("workflow.duration.invalid")
        logger.warning("Rejected workflow definition: %s", decision)

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
