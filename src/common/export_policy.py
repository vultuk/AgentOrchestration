"""Shared redaction policy for structured task exports."""

import csv
import json
from copy import deepcopy
from enum import Enum
from io import StringIO
from typing import Any, Dict, Iterable, List, Mapping, Optional, Union


MASKED_VALUE = "[REDACTED]"


class ExportPolicyError(ValueError):
    """Raised when export data is not covered by the redaction policy."""


class ExportFormat(str, Enum):
    JSON = "json"
    CSV = "csv"
    UI = "ui"


class FieldClassification(str, Enum):
    PUBLIC = "public"
    NESTED = "nested"
    MASKED = "masked"
    OMITTED = "omitted"


TASK_EXPORT_FIELD_POLICY: Mapping[str, FieldClassification] = {
    "id": FieldClassification.PUBLIC,
    "type": FieldClassification.PUBLIC,
    "target_agent": FieldClassification.PUBLIC,
    "queue": FieldClassification.PUBLIC,
    "priority": FieldClassification.PUBLIC,
    "status": FieldClassification.PUBLIC,
    "retries": FieldClassification.PUBLIC,
    "enqueued_at": FieldClassification.PUBLIC,
    "scheduled_for": FieldClassification.PUBLIC,
    "updated_at": FieldClassification.PUBLIC,
    "completed_at": FieldClassification.PUBLIC,
    "payload": FieldClassification.NESTED,
    "metadata": FieldClassification.NESTED,
    "config": FieldClassification.NESTED,
    "result": FieldClassification.NESTED,
    "error": FieldClassification.MASKED,
    "debug_context": FieldClassification.OMITTED,
    "handler": FieldClassification.OMITTED,
    "internal_metadata": FieldClassification.OMITTED,
    "internal_trace": FieldClassification.OMITTED,
    "sandbox_path": FieldClassification.OMITTED,
    "stack": FieldClassification.OMITTED,
    "worker_pid": FieldClassification.OMITTED,
}

RESTRICTED_FIELD_POLICY: Mapping[str, FieldClassification] = {
    "api_key": FieldClassification.MASKED,
    "authorization": FieldClassification.MASKED,
    "auth": FieldClassification.OMITTED,
    "auth_token": FieldClassification.MASKED,
    "cookie": FieldClassification.MASKED,
    "credential": FieldClassification.OMITTED,
    "password": FieldClassification.OMITTED,
    "private_key": FieldClassification.OMITTED,
    "refresh_token": FieldClassification.MASKED,
    "secret": FieldClassification.OMITTED,
    "session_id": FieldClassification.MASKED,
    "token": FieldClassification.MASKED,
}


def _coerce_format(export_format: Union[ExportFormat, str]) -> ExportFormat:
    try:
        return ExportFormat(export_format)
    except ValueError as exc:
        raise ExportPolicyError(
            f"Unsupported export format: {export_format}"
        ) from exc


def _normalize_key(field_name: Any) -> str:
    normalized = str(field_name).strip().lower()
    for char in ("-", " ", "."):
        normalized = normalized.replace(char, "_")
    return normalized


def _restricted_classification(
    field_name: Any,
) -> Optional[FieldClassification]:
    normalized = _normalize_key(field_name)
    if normalized in RESTRICTED_FIELD_POLICY:
        return RESTRICTED_FIELD_POLICY[normalized]

    for restricted, classification in RESTRICTED_FIELD_POLICY.items():
        if normalized.endswith(f"_{restricted}") or restricted in normalized:
            return classification

    return None


def sanitize_for_export(value: Any) -> Any:
    """Recursively mask or omit restricted values from nested export data."""
    if isinstance(value, Mapping):
        sanitized: Dict[str, Any] = {}
        for key, nested_value in value.items():
            classification = _restricted_classification(key)
            if classification == FieldClassification.OMITTED:
                continue
            if classification == FieldClassification.MASKED:
                sanitized[key] = MASKED_VALUE
                continue
            sanitized[key] = sanitize_for_export(nested_value)
        return sanitized

    if isinstance(value, list):
        return [sanitize_for_export(item) for item in value]

    if isinstance(value, tuple):
        return [sanitize_for_export(item) for item in value]

    return deepcopy(value)


def _validate_top_level_policy(record: Mapping[str, Any]) -> None:
    unknown_fields = sorted(set(record) - set(TASK_EXPORT_FIELD_POLICY))
    if unknown_fields:
        fields = ", ".join(unknown_fields)
        raise ExportPolicyError(f"Unclassified task export field(s): {fields}")


def serialize_task_record(
    record: Mapping[str, Any],
    export_format: Union[ExportFormat, str],
) -> Dict[str, Any]:
    """Apply the common export policy to a single structured task record."""
    _coerce_format(export_format)
    _validate_top_level_policy(record)

    sanitized: Dict[str, Any] = {}
    for field, classification in TASK_EXPORT_FIELD_POLICY.items():
        if field not in record:
            continue

        value = record[field]
        if classification == FieldClassification.OMITTED:
            continue
        if classification == FieldClassification.MASKED:
            sanitized[field] = None if value is None else MASKED_VALUE
            continue
        if classification == FieldClassification.NESTED:
            sanitized[field] = sanitize_for_export(value)
            continue
        sanitized[field] = deepcopy(value)

    return sanitized


def _csv_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        return json.dumps(value, sort_keys=True, separators=(",", ":"))
    return str(value)


def _export_csv(records: List[Dict[str, Any]]) -> str:
    fieldnames = [
        field
        for field in TASK_EXPORT_FIELD_POLICY
        if any(field in record for record in records)
    ]

    output = StringIO()
    writer = csv.DictWriter(output, fieldnames=fieldnames)
    writer.writeheader()
    for record in records:
        writer.writerow({
            field: _csv_value(record.get(field))
            for field in fieldnames
        })
    return output.getvalue()


def export_task_records(
    records: Iterable[Mapping[str, Any]],
    export_format: Union[ExportFormat, str],
) -> Union[List[Dict[str, Any]], str]:
    """Export task records after applying the shared redaction policy."""
    coerced_format = _coerce_format(export_format)
    sanitized = [
        serialize_task_record(record, coerced_format)
        for record in records
    ]

    if coerced_format == ExportFormat.CSV:
        return _export_csv(sanitized)

    return sanitized
