"""Data lake ingestion policy controls."""

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Optional, Set
from uuid import uuid4
import time


class DataLakePolicyError(ValueError):
    """Raised when a data lake write violates governance policy."""


@dataclass(frozen=True)
class DataLakeWriteManifest:
    dataset: str
    destination: str
    purpose: str
    data_class: str
    owner: str
    metadata: Dict[str, Any] = field(default_factory=dict)

    def missing_required_fields(self) -> List[str]:
        required = {
            "dataset": self.dataset,
            "destination": self.destination,
            "purpose": self.purpose,
            "data_class": self.data_class,
            "owner": self.owner,
        }
        return [
            field_name
            for field_name, value in required.items()
            if not str(value or "").strip()
        ]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "dataset": self.dataset,
            "destination": self.destination,
            "purpose": self.purpose,
            "data_class": self.data_class,
            "owner": self.owner,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True)
class DestinationPolicy:
    destination: str
    allowed_data_classes: Set[str]
    allowed_purposes: Optional[Set[str]] = None

    def allows(self, data_class: str, purpose: str) -> bool:
        if data_class not in self.allowed_data_classes:
            return False
        if self.allowed_purposes is None:
            return True
        return purpose in self.allowed_purposes


@dataclass(frozen=True)
class DataLakeWriteRecord:
    write_id: str
    manifest: DataLakeWriteManifest
    record_count: int
    written_at: float

    def to_audit_entry(self) -> Dict[str, Any]:
        return {
            "write_id": self.write_id,
            "dataset": self.manifest.dataset,
            "destination": self.manifest.destination,
            "purpose": self.manifest.purpose,
            "data_class": self.manifest.data_class,
            "owner": self.manifest.owner,
            "record_count": self.record_count,
            "written_at": self.written_at,
        }


class DataClassificationRegistry:
    def __init__(self):
        self._destinations: Dict[str, DestinationPolicy] = {}

    def register_destination(
        self,
        destination: str,
        allowed_data_classes: Iterable[str],
        allowed_purposes: Optional[Iterable[str]] = None,
    ) -> None:
        self._destinations[destination] = DestinationPolicy(
            destination=destination,
            allowed_data_classes=set(allowed_data_classes),
            allowed_purposes=(
                set(allowed_purposes)
                if allowed_purposes is not None
                else None
            ),
        )

    def require_allowed(self, manifest: DataLakeWriteManifest) -> None:
        missing = manifest.missing_required_fields()
        if missing:
            fields = ", ".join(sorted(missing))
            raise DataLakePolicyError(
                f"Missing data lake manifest fields: {fields}"
            )

        policy = self._destinations.get(manifest.destination)
        if not policy:
            raise DataLakePolicyError(
                f"Destination is not approved: {manifest.destination}"
            )

        if not policy.allows(manifest.data_class, manifest.purpose):
            raise DataLakePolicyError(
                "Destination policy does not allow "
                f"{manifest.data_class} for purpose {manifest.purpose}"
            )


class DataLakeIngestionPipeline:
    def __init__(
        self,
        registry: DataClassificationRegistry,
        clock: Optional[Callable[[], float]] = None,
    ):
        self.registry = registry
        self._clock = clock or time.time
        self._writes: List[DataLakeWriteRecord] = []

    def write(
        self,
        manifest: DataLakeWriteManifest,
        records: Iterable[Dict[str, Any]],
    ) -> str:
        self.registry.require_allowed(manifest)
        materialized_records = list(records)
        write_id = str(uuid4())
        self._writes.append(
            DataLakeWriteRecord(
                write_id=write_id,
                manifest=manifest,
                record_count=len(materialized_records),
                written_at=self._clock(),
            )
        )
        return write_id

    def audit_report(self) -> Dict[str, Any]:
        entries = [record.to_audit_entry() for record in self._writes]
        return {
            "total_writes": len(entries),
            "writes": entries,
            "by_purpose": self._group_entries(entries, "purpose"),
            "by_owner": self._group_entries(entries, "owner"),
            "by_destination": self._group_entries(entries, "destination"),
            "by_data_class": self._group_entries(entries, "data_class"),
        }

    def _group_entries(
        self,
        entries: List[Dict[str, Any]],
        key: str,
    ) -> Dict[str, List[Dict[str, Any]]]:
        grouped: Dict[str, List[Dict[str, Any]]] = {}
        for entry in entries:
            grouped.setdefault(entry[key], []).append(entry)
        return grouped
