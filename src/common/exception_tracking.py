"""Sanitized exception tracking helpers."""

from __future__ import annotations

import time
from copy import deepcopy
from hashlib import sha256
from typing import Any, Dict, Iterable, List, Optional, Set


DANGEROUS_KEY_PARTS = (
    "authorization",
    "body",
    "cookie",
    "credential",
    "header",
    "input",
    "local",
    "password",
    "payload",
    "secret",
    "token",
)

TASK_ID_KEYS = {
    "task_id",
    "taskid",
    "task_uuid",
    "taskuuid",
}

ERROR_CLASS_KEYS = {
    "error_class",
    "errorclass",
    "exception_class",
    "exceptionclass",
    "error_type",
    "errortype",
}

SAFE_CONTEXT_KEYS = {
    "agent_id",
    "attempt",
    "component",
    "operation",
    "queue",
    "request_id",
    "revision",
    "run_id",
    "status",
    "step_id",
    "workflow_id",
    "workspace_id",
}

MAX_VALUE_LENGTH = 160


def sanitize_exception_context(
    context: Optional[Dict[str, Any]] = None,
    error: Optional[BaseException] = None,
    task: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Build a sanitized exception event from task/error context."""

    sources = [
        source for source in (context, task)
        if isinstance(source, dict)
    ]
    task_id = _first_task_id(sources)
    error_class = _first_error_class(sources)

    if error is not None:
        error_class = type(error).__name__

    event: Dict[str, Any] = {
        "task_id": task_id or "unknown",
        "error_class": error_class or "Exception",
        "captured_at": time.time(),
        "context": {},
    }
    for source in sources:
        _collect_safe_context(source, event["context"])

    event["fingerprint"] = _fingerprint(
        [
            event["task_id"],
            event["error_class"],
            event["context"].get("component", ""),
            event["context"].get("operation", ""),
        ]
    )
    return event


class ExceptionTracker:
    """Stores sanitized exception events for export to error tooling."""

    def __init__(self):
        self._events: List[Dict[str, Any]] = []

    def capture(
        self,
        error: BaseException,
        context: Optional[Dict[str, Any]] = None,
        task: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        event = sanitize_exception_context(
            context=context,
            error=error,
            task=task,
        )
        self._events.append(event)
        return deepcopy(event)

    def list_events(self) -> List[Dict[str, Any]]:
        return deepcopy(self._events)

    def lookup(
        self,
        task_id: Optional[str] = None,
        error_class: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        events = self._events
        if task_id is not None:
            events = [event for event in events if event["task_id"] == task_id]
        if error_class is not None:
            events = [
                event for event in events
                if event["error_class"] == error_class
            ]
        return deepcopy(events)


def _normalized_key(key: Any) -> str:
    return str(key).replace("-", "_").lower()


def _is_dangerous_key(key: Any) -> bool:
    normalized = _normalized_key(key)
    return any(part in normalized for part in DANGEROUS_KEY_PARTS)


def _is_safe_scalar(value: Any) -> bool:
    return isinstance(value, (str, int, float, bool)) or value is None


def _safe_value(value: Any) -> Any:
    if not _is_safe_scalar(value):
        return None
    if isinstance(value, str) and len(value) > MAX_VALUE_LENGTH:
        return value[:MAX_VALUE_LENGTH]
    return value


def _first_task_id(sources: Iterable[Dict[str, Any]]) -> Optional[str]:
    for source in sources:
        value = _find_lookup_value(source, TASK_ID_KEYS, set(), False)
        if value:
            return str(value)
    return None


def _first_error_class(sources: Iterable[Dict[str, Any]]) -> Optional[str]:
    for source in sources:
        value = _find_lookup_value(source, ERROR_CLASS_KEYS, set(), False)
        if value:
            return str(value)
    return None


def _find_lookup_value(
    value: Any,
    lookup_keys: Set[str],
    seen: Set[int],
    inside_task: bool,
) -> Optional[Any]:
    if id(value) in seen:
        return None
    if isinstance(value, dict):
        seen.add(id(value))
        for key, nested in value.items():
            normalized = _normalized_key(key)
            if normalized in lookup_keys and _is_safe_scalar(nested):
                return nested
            if inside_task and normalized == "id" and _is_safe_scalar(nested):
                return nested
            found = _find_lookup_value(
                nested,
                lookup_keys,
                seen,
                inside_task or normalized == "task",
            )
            if found:
                return found
    elif isinstance(value, list):
        seen.add(id(value))
        for item in value:
            found = _find_lookup_value(item, lookup_keys, seen, inside_task)
            if found:
                return found
    return None


def _collect_safe_context(
    value: Any,
    safe_context: Dict[str, Any],
    seen: Optional[Set[int]] = None,
) -> None:
    if seen is None:
        seen = set()
    if id(value) in seen:
        return
    if isinstance(value, dict):
        seen.add(id(value))
        for key, nested in value.items():
            normalized = _normalized_key(key)
            if _is_dangerous_key(key):
                continue
            if normalized in SAFE_CONTEXT_KEYS and _is_safe_scalar(nested):
                safe_context.setdefault(normalized, _safe_value(nested))
                continue
            _collect_safe_context(nested, safe_context, seen)
    elif isinstance(value, list):
        seen.add(id(value))
        for item in value:
            _collect_safe_context(item, safe_context, seen)


def _fingerprint(parts: Iterable[Any]) -> str:
    joined = "|".join(str(part) for part in parts)
    return sha256(joined.encode("utf-8")).hexdigest()
