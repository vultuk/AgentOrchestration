from src.orchestrator.workflow import StepStatus, WorkflowManager


class TestWorkflowSubworkflowGuards:
    def setup_method(self):
        self.manager = WorkflowManager()

    def test_start_subworkflow_records_parent_attempt_and_revision(self):
        parent = self.manager.create_workflow("parent")
        parent.attempt = 1
        parent.revision = 7
        parent.status = StepStatus.RUNNING

        child = self.manager.start_subworkflow(
            parent.id,
            "child",
            expected_parent_attempt=1,
            expected_parent_revision=7,
        )

        assert child is not None
        assert child.parent_id == parent.id
        assert child.parent_attempt == 1
        assert child.parent_revision == 7
        assert parent.revision == 8
        assert parent.audit_log[-1]["event"] == "subworkflow_started"
        assert parent.audit_log[-1]["child_workflow_id"] == child.id

    def test_rejects_subworkflow_after_parent_failure_race(self):
        parent = self.manager.create_workflow("parent")
        parent.attempt = 2
        parent.revision = 4
        parent.status = StepStatus.RUNNING
        captured_attempt = parent.attempt
        captured_revision = parent.revision

        parent.transition(
            StepStatus.FAILED,
            "parent_failed_before_child_start",
        )

        child = self.manager.start_subworkflow(
            parent.id,
            "orphan-child",
            expected_parent_attempt=captured_attempt,
            expected_parent_revision=captured_revision,
        )

        assert child is None
        assert parent.status == StepStatus.FAILED
        assert len(self.manager.list_workflows()) == 1
        rejection = parent.audit_log[-1]
        assert rejection["event"] == "subworkflow_start_rejected"
        assert rejection["parent_status"] == "failed"
        assert "parent_revision_changed" in rejection["reason"]
        assert "parent_not_running" in rejection["reason"]

    def test_rejects_duplicate_subworkflow_start_with_stale_revision(self):
        parent = self.manager.create_workflow("parent")
        parent.attempt = 1
        parent.revision = 3
        parent.status = StepStatus.RUNNING

        first = self.manager.start_subworkflow(
            parent.id,
            "first-child",
            expected_parent_attempt=1,
            expected_parent_revision=3,
        )
        second = self.manager.start_subworkflow(
            parent.id,
            "second-child",
            expected_parent_attempt=1,
            expected_parent_revision=3,
        )

        assert first is not None
        assert second is None
        assert len(self.manager.list_workflows()) == 2
        assert parent.audit_log[-1]["event"] == "subworkflow_start_rejected"
        assert parent.audit_log[-1]["reason"] == "parent_revision_changed"
