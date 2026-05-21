"""Pre-rollout migration gate for deployment safety."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, List, Optional


MigrationRunner = Callable[[], bool]


@dataclass(frozen=True)
class Migration:
    name: str
    backward_compatible: bool = True
    run: Optional[MigrationRunner] = None


@dataclass(frozen=True)
class ReleasePlan:
    current_version: str
    target_version: str
    migrations: List[Migration] = field(default_factory=list)
    reversible: bool = True


@dataclass(frozen=True)
class RolloutDecision:
    serving_version: str
    target_version: str
    migrations_completed: bool
    compatibility_checked: bool
    compatibility_passed: bool
    new_traffic_enabled: bool
    rollout_blocked: bool
    reasons: List[str]
    audit_events: List[str]


class DeploymentMigrationGate:
    def evaluate(self, plan: ReleasePlan) -> RolloutDecision:
        events = ["rollout_gate_started", "compatibility_check_started"]
        incompatible = [
            migration.name
            for migration in plan.migrations
            if not migration.backward_compatible
        ]
        if plan.reversible and incompatible:
            events.append("compatibility_check_failed")
            return self._blocked(
                plan,
                migrations_completed=False,
                compatibility_checked=True,
                compatibility_passed=False,
                reasons=[
                    "reversible deploy contains non-backward-compatible "
                    f"migrations: {', '.join(incompatible)}"
                ],
                audit_events=events,
            )

        events.append("compatibility_check_passed")
        for migration in plan.migrations:
            events.append(f"migration_started:{migration.name}")
            try:
                if migration.run is None:
                    passed = True
                else:
                    passed = bool(migration.run())
            except Exception:
                passed = False
            if not passed:
                events.append(f"migration_failed:{migration.name}")
                return self._blocked(
                    plan,
                    migrations_completed=False,
                    compatibility_checked=True,
                    compatibility_passed=True,
                    reasons=[f"migration failed: {migration.name}"],
                    audit_events=events,
                )
            events.append(f"migration_completed:{migration.name}")

        events.append("rollout_released")
        return RolloutDecision(
            serving_version=plan.target_version,
            target_version=plan.target_version,
            migrations_completed=True,
            compatibility_checked=True,
            compatibility_passed=True,
            new_traffic_enabled=True,
            rollout_blocked=False,
            reasons=[],
            audit_events=events,
        )

    def _blocked(
        self,
        plan: ReleasePlan,
        migrations_completed: bool,
        compatibility_checked: bool,
        compatibility_passed: bool,
        reasons: List[str],
        audit_events: List[str],
    ) -> RolloutDecision:
        return RolloutDecision(
            serving_version=plan.current_version,
            target_version=plan.target_version,
            migrations_completed=migrations_completed,
            compatibility_checked=compatibility_checked,
            compatibility_passed=compatibility_passed,
            new_traffic_enabled=False,
            rollout_blocked=True,
            reasons=reasons,
            audit_events=[*audit_events, "rollout_blocked"],
        )
