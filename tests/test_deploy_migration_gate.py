from src.deploy import DeploymentMigrationGate, Migration, ReleasePlan


def test_successful_migrations_release_new_traffic():
    executed = []
    plan = ReleasePlan(
        current_version="2026.05.20",
        target_version="2026.05.21",
        migrations=[
            Migration(
                "add_task_state_index",
                run=lambda: executed.append("index") or True,
            ),
            Migration(
                "expand_task_payload",
                run=lambda: executed.append("payload") or True,
            ),
        ],
    )

    decision = DeploymentMigrationGate().evaluate(plan)

    assert executed == ["index", "payload"]
    assert decision.serving_version == "2026.05.21"
    assert decision.migrations_completed is True
    assert decision.new_traffic_enabled is True
    assert decision.rollout_blocked is False
    assert decision.audit_events[-1] == "rollout_released"


def test_migration_failure_keeps_prior_version_serving():
    executed = []
    plan = ReleasePlan(
        current_version="stable",
        target_version="candidate",
        migrations=[
            Migration(
                "add_column",
                run=lambda: executed.append("add") or True,
            ),
            Migration(
                "backfill_state",
                run=lambda: executed.append("backfill") and False,
            ),
            Migration(
                "drop_legacy_column",
                run=lambda: executed.append("drop") or True,
            ),
        ],
    )

    decision = DeploymentMigrationGate().evaluate(plan)

    assert executed == ["add", "backfill"]
    assert decision.serving_version == "stable"
    assert decision.target_version == "candidate"
    assert decision.new_traffic_enabled is False
    assert decision.rollout_blocked is True
    assert decision.reasons == ["migration failed: backfill_state"]
    assert (
        "migration_started:drop_legacy_column"
        not in decision.audit_events
    )


def test_incompatible_reversible_migration_blocks_before_running():
    executed = []
    plan = ReleasePlan(
        current_version="stable",
        target_version="candidate",
        migrations=[
            Migration(
                "rename_task_state",
                backward_compatible=False,
                run=lambda: executed.append("rename") or True,
            ),
        ],
    )

    decision = DeploymentMigrationGate().evaluate(plan)

    assert executed == []
    assert decision.compatibility_checked is True
    assert decision.compatibility_passed is False
    assert decision.migrations_completed is False
    assert decision.serving_version == "stable"
    assert decision.new_traffic_enabled is False
    assert decision.rollout_blocked is True


def test_irreversible_release_can_run_explicitly_noncompatible_migration():
    plan = ReleasePlan(
        current_version="stable",
        target_version="candidate",
        reversible=False,
        migrations=[
            Migration("drop_legacy_column", backward_compatible=False),
        ],
    )

    decision = DeploymentMigrationGate().evaluate(plan)

    assert decision.compatibility_checked is True
    assert decision.compatibility_passed is True
    assert decision.migrations_completed is True
    assert decision.new_traffic_enabled is True


def test_no_migration_release_still_checks_compatibility():
    plan = ReleasePlan(current_version="stable", target_version="candidate")

    decision = DeploymentMigrationGate().evaluate(plan)

    assert decision.compatibility_checked is True
    assert decision.compatibility_passed is True
    assert decision.migrations_completed is True
    assert decision.serving_version == "candidate"
