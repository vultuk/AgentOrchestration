from src.common.metrics import MetricsCollector
from src.orchestrator.workflow import (
    StepStatus,
    WorkflowManager,
    WorkflowStep,
)


def test_parallel_join_rejects_duplicate_branch_output_namespace():
    metrics = MetricsCollector()
    manager = WorkflowManager(metrics_collector=metrics)
    workflow = manager.create_workflow("parallel-review")
    executed = []

    workflow.add_step(
        WorkflowStep(
            "branch-a",
            lambda: executed.append("a"),
            branch_id="branch-a-private",
            join_id="join-private",
            output_namespace="shared-private",
        ),
    )
    workflow.add_step(
        WorkflowStep(
            "branch-b",
            lambda: executed.append("b"),
            branch_id="branch-b-private",
            join_id="join-private",
            output_namespace="shared-private",
        ),
    )

    assert manager.execute_workflow(workflow.id) is False

    assert executed == []
    assert workflow.status == StepStatus.FAILED
    assert [step.status for step in workflow.steps] == [
        StepStatus.PENDING,
        StepStatus.PENDING,
    ]
    assert workflow.validation_errors[-1]["reason"] == (
        "duplicate_output_namespace"
    )

    audit = manager.audit_records()
    assert audit[-1]["reason"] == "duplicate_output_namespace"
    assert audit[-1]["branch_ref"] != "branch-b-private"
    assert audit[-1]["namespace_ref"] != "shared-private"
    assert audit[-1]["join_ref"] != "join-private"

    snapshot = metrics.snapshot()
    assert (
        snapshot["counters"][
            "workflow.output_merger.rejected.duplicate_output_namespace"
        ]
        == 1
    )


def test_parallel_join_rejects_parent_child_output_namespace_collision():
    manager = WorkflowManager(metrics_collector=MetricsCollector())
    workflow = manager.create_workflow("parallel-prefix")
    executed = []

    workflow.add_step(
        WorkflowStep(
            "branch-a",
            lambda: executed.append("a"),
            branch_id="branch-a-private",
            join_id="join-private",
            output_namespace="results",
        ),
    )
    workflow.add_step(
        WorkflowStep(
            "branch-b",
            lambda: executed.append("b"),
            branch_id="branch-b-private",
            join_id="join-private",
            output_namespace="results.extra",
        ),
    )

    assert manager.execute_workflow(workflow.id) is False

    assert executed == []
    assert workflow.status == StepStatus.FAILED
    assert workflow.validation_errors[-1]["reason"] == (
        "duplicate_output_namespace"
    )
    assert workflow.validation_errors[-1]["output_namespace"] == (
        "results.extra"
    )
    assert manager.audit_records()[-1]["namespace_ref"] != "results.extra"


def test_parallel_join_allows_sibling_output_namespaces():
    manager = WorkflowManager(metrics_collector=MetricsCollector())
    workflow = manager.create_workflow("parallel-siblings")

    workflow.add_step(
        WorkflowStep(
            "branch-a",
            lambda: {"answer": 1},
            branch_id="branch-a-private",
            join_id="join-private",
            output_namespace="results.a",
        ),
    )
    workflow.add_step(
        WorkflowStep(
            "branch-b",
            lambda: {"answer": 2},
            branch_id="branch-b-private",
            join_id="join-private",
            output_namespace="results.b",
        ),
    )

    assert manager.execute_workflow(workflow.id) is True

    assert workflow.status == StepStatus.COMPLETED
    assert workflow.outputs == {
        "results.a": {"branch-a": {"answer": 1}},
        "results.b": {"branch-b": {"answer": 2}},
    }
    assert manager.audit_records() == []


def test_parallel_join_rejects_missing_branch_namespace_before_dispatch():
    metrics = MetricsCollector()
    manager = WorkflowManager(metrics_collector=metrics)
    workflow = manager.create_workflow("missing-namespace")
    executed = []

    workflow.add_step(
        WorkflowStep(
            "branch-a",
            lambda: executed.append("a"),
            branch_id="branch-a-private",
            join_id="join-private",
        ),
    )

    assert manager.execute_workflow(workflow.id) is False

    assert executed == []
    assert workflow.status == StepStatus.FAILED
    assert workflow.steps[0].status == StepStatus.PENDING
    assert workflow.validation_errors == [
        {
            "reason": "missing_output_namespace",
            "step_id": workflow.steps[0].id,
            "branch_id": "branch-a-private",
            "join_id": "join-private",
            "output_namespace": None,
        },
    ]
    assert manager.audit_records()[-1]["reason"] == "missing_output_namespace"


def test_unique_branch_output_namespaces_merge_without_collision():
    manager = WorkflowManager(metrics_collector=MetricsCollector())
    workflow = manager.create_workflow("parallel-success")

    workflow.add_step(
        WorkflowStep(
            "branch-a",
            lambda: {"answer": 1, "private": "a"},
            branch_id="branch-a-private",
            join_id="join-private",
            output_namespace="branch_a",
            output_keys=["answer"],
        ),
    )
    workflow.add_step(
        WorkflowStep(
            "branch-b",
            lambda: {"answer": 2, "private": "b"},
            branch_id="branch-b-private",
            join_id="join-private",
            output_namespace="branch_b",
            output_keys=["answer"],
        ),
    )

    assert manager.execute_workflow(workflow.id) is True

    assert workflow.status == StepStatus.COMPLETED
    assert [step.status for step in workflow.steps] == [
        StepStatus.COMPLETED,
        StepStatus.COMPLETED,
    ]
    assert workflow.outputs == {
        "branch_a": {"answer": 1},
        "branch_b": {"answer": 2},
    }
    assert workflow.validation_errors == []
    assert manager.audit_records() == []
