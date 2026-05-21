import pytest

from src.orchestrator.data_lake import (
    DataClassificationRegistry,
    DataLakeIngestionPipeline,
    DataLakePolicyError,
    DataLakeWriteManifest,
)


class TestDataLakeIngestionPipeline:
    def setup_method(self):
        self.registry = DataClassificationRegistry()
        self.registry.register_destination(
            "analytics.events",
            allowed_data_classes={"operational_event"},
            allowed_purposes={"usage_analytics", "reliability_review"},
        )
        self.pipeline = DataLakeIngestionPipeline(
            self.registry,
            clock=lambda: 123.0,
        )

    def test_write_requires_governance_manifest_fields(self):
        manifest = DataLakeWriteManifest(
            dataset="",
            destination="analytics.events",
            purpose="usage_analytics",
            data_class="operational_event",
            owner="ops",
        )

        with pytest.raises(DataLakePolicyError) as exc:
            self.pipeline.write(manifest, [{"task_id": "t1"}])

        assert "dataset" in str(exc.value)

    def test_write_accepts_approved_destination_policy(self):
        manifest = DataLakeWriteManifest(
            dataset="task_events",
            destination="analytics.events",
            purpose="usage_analytics",
            data_class="operational_event",
            owner="platform-ops",
        )

        write_id = self.pipeline.write(
            manifest,
            [{"task_id": "t1"}, {"task_id": "t2"}],
        )

        report = self.pipeline.audit_report()
        assert write_id == report["writes"][0]["write_id"]
        assert report["writes"][0]["record_count"] == 2
        assert report["writes"][0]["written_at"] == 123.0

    def test_write_blocks_unapproved_data_class(self):
        manifest = DataLakeWriteManifest(
            dataset="task_payloads",
            destination="analytics.events",
            purpose="usage_analytics",
            data_class="secret_payload",
            owner="platform-ops",
        )

        with pytest.raises(DataLakePolicyError) as exc:
            self.pipeline.write(manifest, [{"secret": "blocked"}])

        assert "does not allow secret_payload" in str(exc.value)

    def test_write_blocks_unapproved_purpose(self):
        manifest = DataLakeWriteManifest(
            dataset="task_events",
            destination="analytics.events",
            purpose="marketing_export",
            data_class="operational_event",
            owner="growth",
        )

        with pytest.raises(DataLakePolicyError) as exc:
            self.pipeline.write(manifest, [{"task_id": "t1"}])

        assert "marketing_export" in str(exc.value)

    def test_audit_report_lists_writes_by_purpose_and_owner(self):
        first = DataLakeWriteManifest(
            dataset="task_events",
            destination="analytics.events",
            purpose="usage_analytics",
            data_class="operational_event",
            owner="platform-ops",
        )
        second = DataLakeWriteManifest(
            dataset="scheduler_events",
            destination="analytics.events",
            purpose="reliability_review",
            data_class="operational_event",
            owner="sre",
        )

        self.pipeline.write(first, [{"task_id": "t1"}])
        self.pipeline.write(second, [{"task_id": "t2"}])

        report = self.pipeline.audit_report()
        assert report["total_writes"] == 2
        usage_entries = report["by_purpose"]["usage_analytics"]
        reliability_entries = report["by_purpose"]["reliability_review"]
        ops_entries = report["by_owner"]["platform-ops"]
        sre_entries = report["by_owner"]["sre"]

        assert usage_entries[0]["owner"] == "platform-ops"
        assert reliability_entries[0]["owner"] == "sre"
        assert ops_entries[0]["purpose"] == "usage_analytics"
        assert sre_entries[0]["purpose"] == "reliability_review"
        assert len(report["by_destination"]["analytics.events"]) == 2
        assert len(report["by_data_class"]["operational_event"]) == 2
