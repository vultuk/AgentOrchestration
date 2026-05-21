import pytest

from src.common.metrics import metrics
from src.orchestrator.workflow import (
    StepStatus,
    WorkflowDefinitionError,
    WorkflowManager,
    WorkflowStep,
)


class TestWorkflowDurationParsing:
    def setup_method(self):
        self.manager = WorkflowManager()

    def test_register_rejects_conflicting_units_before_state_changes(self):
        before = metrics.snapshot()["counters"].get(
            "workflow.duration.invalid",
            0,
        )
        definition = {
            "name": "billing-export",
            "steps": [
                {
                    "name": "charge-card",
                    "handler": lambda: "ok",
                    "timeout_seconds": 30,
                    "timeout_minutes": 1,
                    "private_runtime_data": "do-not-copy",
                },
            ],
        }

        with pytest.raises(WorkflowDefinitionError) as exc:
            self.manager.register_workflow_definition(definition)

        assert exc.value.code == "conflicting_timeout_units"
        assert self.manager.list_workflows() == []
        audit = self.manager.audit_decisions[-1]
        assert audit == {
            "event": "workflow_definition_rejected",
            "reason": "conflicting_timeout_units",
            "workflow_id": "",
            "workflow_name": "billing-export",
            "step_id": "",
            "step_name": "charge-card",
        }
        audit_text = str(audit)
        assert "do-not-copy" not in audit_text
        assert (
            metrics.snapshot()["counters"]["workflow.duration.invalid"]
            == before + 1
        )

    def test_register_parses_single_duration_unit_and_executes(self):
        workflow = self.manager.register_workflow_definition(
            {
                "name": "billing-export",
                "steps": [
                    {
                        "name": "charge-card",
                        "handler": lambda: "ok",
                        "timeout_minutes": 2,
                    },
                ],
            },
        )

        assert workflow.steps[0].timeout == 120.0
        assert self.manager.execute_workflow(workflow.id) is True
        assert workflow.status == StepStatus.COMPLETED
        assert workflow.steps[0].result == "ok"

    def test_dispatch_rejects_stale_conflict_without_running_workflow(self):
        called = []
        workflow = self.manager.create_workflow("manual-import")
        step = WorkflowStep(
            "load-records",
            lambda: called.append("ran"),
            timeout=30,
        )
        step.timeout = {"seconds": 30, "minutes": 1}
        workflow.add_step(step)

        assert self.manager.execute_workflow(workflow.id) is False
        assert workflow.status == StepStatus.PENDING
        assert step.status == StepStatus.PENDING
        assert called == []
        assert (
            self.manager.audit_decisions[-1]["reason"]
            == "conflicting_timeout_units"
        )
