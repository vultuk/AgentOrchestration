from src.common.metrics import metrics
from src.orchestrator.workflow import StepStatus, WorkflowManager, WorkflowStep


def test_execute_binds_explicit_false_default_before_dispatch():
    manager = WorkflowManager()
    workflow = manager.create_workflow("false-defaults")
    workflow.define_parameter("allow_retry", default=False)
    workflow.add_step(
        WorkflowStep(
            "read-bound-parameter",
            lambda: workflow.bound_parameters["allow_retry"],
        )
    )

    assert manager.execute_workflow(workflow.id)

    assert workflow.bound_parameters["allow_retry"] is False
    assert workflow.steps[0].result is False
    assert workflow.status is StepStatus.COMPLETED
    assert workflow.audit_log[-1]["decision"] == "bound"
    assert workflow.audit_log[-1]["parameter_keys"] == ["allow_retry"]
    assert "False" not in str(workflow.audit_log[-1])


def test_execute_preserves_explicit_false_override_before_dispatch():
    manager = WorkflowManager()
    workflow = manager.create_workflow("false-override")
    workflow.define_parameter("send_notifications", default=True)
    workflow.add_step(
        WorkflowStep(
            "read-override",
            lambda: workflow.bound_parameters["send_notifications"],
        )
    )

    assert manager.execute_workflow(
        workflow.id,
        {"send_notifications": False},
    )

    assert workflow.bound_parameters["send_notifications"] is False
    assert workflow.steps[0].result is False
    assert workflow.status is StepStatus.COMPLETED


def test_rejects_forbidden_override_before_workflow_starts():
    manager = WorkflowManager()
    workflow = manager.create_workflow("policy-locked")
    workflow.define_parameter(
        "allow_external_routing",
        default=False,
        allow_override=False,
    )
    workflow.add_step(WorkflowStep("must-not-run", lambda: "ran"))

    assert not manager.execute_workflow(
        workflow.id,
        {"allow_external_routing": True},
    )

    assert workflow.status is StepStatus.PENDING
    assert workflow.steps[0].status is StepStatus.PENDING
    assert workflow.bound_parameters == {}
    assert workflow.audit_log[-1]["decision"] == "rejected"
    assert workflow.audit_log[-1]["reason"] == (
        "workflow_parameter_override_forbidden"
    )
    assert "True" not in str(workflow.audit_log[-1])


def test_missing_required_parameter_rejected_before_dispatch():
    manager = WorkflowManager()
    workflow = manager.create_workflow("required-parameter")
    workflow.define_parameter("routing_policy", required=True)
    workflow.add_step(WorkflowStep("must-not-run", lambda: "ran"))

    assert not manager.execute_workflow(workflow.id)

    assert workflow.status is StepStatus.PENDING
    assert workflow.steps[0].status is StepStatus.PENDING
    assert workflow.bound_parameters == {}
    assert workflow.audit_log[-1]["decision"] == "rejected"
    assert workflow.audit_log[-1]["reason"] == (
        "required_workflow_parameter_missing"
    )


def test_rejects_lifecycle_rebind_and_preserves_workflow_state():
    manager = WorkflowManager()
    workflow = manager.create_workflow("running-workflow")
    workflow.define_parameter("allow_retry", default=False)
    workflow.bound_parameters = {"allow_retry": False}
    workflow.status = StepStatus.RUNNING
    before_parameters = dict(workflow.bound_parameters)

    assert not manager.bind_parameters(
        workflow.id,
        {"allow_retry": True, "private_token": "secret-value"},
    )

    assert workflow.status is StepStatus.RUNNING
    assert workflow.bound_parameters == before_parameters
    assert workflow.audit_log[-1]["decision"] == "rejected"
    assert workflow.audit_log[-1]["reason"] == "workflow_not_pending"
    assert workflow.audit_log[-1]["parameter_keys"] == [
        "allow_retry",
        "private_token",
    ]
    audit_text = str(workflow.audit_log[-1])
    assert "secret-value" not in audit_text
    assert "True" not in audit_text
    assert metrics.snapshot()["counters"]["workflow.parameters.rejected"] >= 1


def test_rejects_unknown_parameter_before_workflow_starts():
    manager = WorkflowManager()
    workflow = manager.create_workflow("unknown-parameter")
    workflow.define_parameter("enabled", default=False)
    workflow.add_step(WorkflowStep("must-not-run", lambda: "ran"))

    assert not manager.execute_workflow(
        workflow.id,
        {"enabled": False, "policy_override": "private-policy"},
    )

    assert workflow.status is StepStatus.PENDING
    assert workflow.steps[0].status is StepStatus.PENDING
    assert workflow.bound_parameters == {}
    assert workflow.audit_log[-1]["decision"] == "rejected"
    assert workflow.audit_log[-1]["reason"] == "unknown_workflow_parameter"
    assert workflow.audit_log[-1]["parameter_keys"] == [
        "enabled",
        "policy_override",
    ]
    assert "private-policy" not in str(workflow.audit_log[-1])
