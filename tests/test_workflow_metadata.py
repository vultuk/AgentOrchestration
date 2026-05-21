"""Regression coverage for workflow definition metadata validation."""

import pytest

from src.orchestrator.workflow import (
    StepStatus,
    WorkflowManager,
    WorkflowMetadataError,
    WorkflowStep,
)


def test_workflow_registration_rejects_reserved_metadata_keys():
    manager = WorkflowManager()

    with pytest.raises(WorkflowMetadataError) as error:
        manager.create_workflow(
            "invoice-review",
            metadata={"owner": "finance", "status": "running"},
        )

    assert "workflow.metadata.status" in str(error.value)
    assert manager.list_workflows() == []
    assert manager.audit_records == [
        {
            "event": "workflow_metadata_rejected",
            "workflow_id": manager.audit_records[0]["workflow_id"],
            "path": "workflow.metadata.status",
            "key": "status",
            "reason": "reserved_metadata_key",
            "status": "pending",
        },
    ]


def test_workflow_step_rejects_reserved_metadata_without_state_change():
    manager = WorkflowManager()
    workflow = manager.create_workflow("invoice-review")
    step = WorkflowStep(
        "approve",
        lambda: "ok",
        metadata={"display": "approval", "routing": "priority"},
    )

    with pytest.raises(WorkflowMetadataError):
        workflow.add_step(step)

    assert workflow.status == StepStatus.PENDING
    assert workflow.steps == []
    assert workflow.audit_records[-1]["path"] == (
        "workflow.steps.approve.metadata.routing"
    )
    assert workflow.audit_records[-1]["reason"] == "reserved_metadata_key"
    assert "priority" not in workflow.audit_records[-1].values()


def test_execution_defers_mutated_invalid_metadata_before_handlers_run():
    manager = WorkflowManager()
    workflow = manager.create_workflow(
        "invoice-review",
        metadata={"team": "ops"},
    )
    called = []
    workflow.add_step(WorkflowStep("approve", lambda: called.append("ran")))
    workflow.metadata["lifecycle"] = "running"

    assert manager.execute_workflow(workflow.id) is False
    assert called == []
    assert workflow.status == StepStatus.PENDING
    assert workflow.steps[0].status == StepStatus.PENDING
    assert workflow.audit_records[-1] == {
        "event": "workflow_metadata_rejected",
        "workflow_id": workflow.id,
        "path": "workflow.metadata.lifecycle",
        "key": "lifecycle",
        "reason": "reserved_metadata_key",
        "status": "pending",
    }


def test_valid_user_metadata_executes_successfully():
    manager = WorkflowManager()
    workflow = manager.create_workflow(
        "invoice-review",
        metadata={"owner": "finance", "labels": {"risk": "low"}},
    )
    workflow.add_step(
        WorkflowStep(
            "approve",
            lambda: "approved",
            metadata={"display_name": "Approval"},
        ),
    )

    assert manager.execute_workflow(workflow.id) is True
    assert workflow.status == StepStatus.COMPLETED
    assert workflow.steps[0].status == StepStatus.COMPLETED
    assert workflow.steps[0].result == "approved"
    assert workflow.audit_records == []
