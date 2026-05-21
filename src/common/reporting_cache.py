"""Authorization-aware cache for report results."""

from __future__ import annotations

import hashlib
import json
import time
from copy import deepcopy
from dataclasses import dataclass, field
from threading import RLock
from typing import Any, Callable, Dict, Iterable, Mapping, Optional, Tuple

from src.common.errors import AuthenticationError


AuditSink = Callable[[Mapping[str, Any]], None]
AuthorizeReport = Callable[[str, "ReportAuthorizationContext"], bool]
ProduceReport = Callable[[], Any]


def _normalize_sequence(
    values: Iterable[Any],
    *,
    ordered: bool,
) -> Tuple[Any, ...]:
    normalized = tuple(_normalize_value(value) for value in values)
    if ordered:
        return normalized
    return tuple(sorted(normalized, key=repr))


def _normalize_mapping(
    value: Mapping[str, Any],
) -> Tuple[Tuple[str, Any], ...]:
    return tuple(
        sorted(
            (str(key), _normalize_value(item))
            for key, item in value.items()
        )
    )


def _normalize_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return _normalize_mapping(value)
    if isinstance(value, (list, tuple)):
        return _normalize_sequence(value, ordered=True)
    if isinstance(value, (set, frozenset)):
        return _normalize_sequence(value, ordered=False)
    return value


def _stable_digest(value: Any) -> str:
    serialized = json.dumps(
        _normalize_value(value),
        default=str,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ReportAuthorizationContext:
    """Requester authorization state that can change report visibility."""

    requester_id: str
    workspace_id: str
    roles: Tuple[str, ...] = field(default_factory=tuple)
    permissions: Tuple[str, ...] = field(default_factory=tuple)
    auth_version: str = ""

    def __init__(
        self,
        requester_id: str,
        workspace_id: str,
        roles: Iterable[str] = (),
        permissions: Iterable[str] = (),
        auth_version: object = "",
    ):
        object.__setattr__(self, "requester_id", str(requester_id))
        object.__setattr__(self, "workspace_id", str(workspace_id))
        object.__setattr__(
            self,
            "roles",
            tuple(sorted({str(role) for role in roles})),
        )
        object.__setattr__(
            self,
            "permissions",
            tuple(sorted({str(permission) for permission in permissions})),
        )
        object.__setattr__(self, "auth_version", str(auth_version))

    def fingerprint(
        self,
    ) -> Tuple[str, str, Tuple[str, ...], Tuple[str, ...], str]:
        return (
            self.requester_id,
            self.workspace_id,
            self.roles,
            self.permissions,
            self.auth_version,
        )


@dataclass(frozen=True)
class _CacheEntry:
    result: Any
    context: ReportAuthorizationContext
    stored_at: float


class ReportResultCache:
    """Cache report results without crossing workspace or role boundaries."""

    def __init__(
        self,
        *,
        ttl_seconds: Optional[float] = None,
        audit_sink: Optional[AuditSink] = None,
    ):
        self.ttl_seconds = ttl_seconds
        self._audit_sink = audit_sink
        self._lock = RLock()
        self._entries: Dict[
            Tuple[
                str,
                str,
                Tuple[str, str, Tuple[str, ...], Tuple[str, ...], str],
            ],
            _CacheEntry,
        ] = {}

    def get_or_compute(
        self,
        report_id: str,
        query: Mapping[str, Any],
        context: ReportAuthorizationContext,
        producer: ProduceReport,
        authorize: AuthorizeReport,
    ) -> Any:
        """Return a cached report only after fresh authorization succeeds."""
        self._require_authorized(report_id, context, authorize)
        key = self._key(report_id, query, context)

        with self._lock:
            entry = self._entries.get(key)
            if entry and not self._is_expired(entry):
                self._require_authorized(report_id, entry.context, authorize)
                self._audit("hit", report_id, context)
                return deepcopy(entry.result)
            if entry:
                self._entries.pop(key, None)
                self._audit("expired", report_id, context)

        result = producer()
        self._require_authorized(report_id, context, authorize)
        with self._lock:
            self._entries[key] = _CacheEntry(
                result=deepcopy(result),
                context=context,
                stored_at=time.time(),
            )
            self._audit("store", report_id, context)
        return deepcopy(result)

    def get(
        self,
        report_id: str,
        query: Mapping[str, Any],
        context: ReportAuthorizationContext,
        authorize: AuthorizeReport,
    ) -> Optional[Any]:
        self._require_authorized(report_id, context, authorize)
        key = self._key(report_id, query, context)

        with self._lock:
            entry = self._entries.get(key)
            if not entry:
                self._audit("miss", report_id, context)
                return None
            if self._is_expired(entry):
                self._entries.pop(key, None)
                self._audit("expired", report_id, context)
                return None
            self._require_authorized(report_id, entry.context, authorize)
            self._audit("hit", report_id, context)
            return deepcopy(entry.result)

    def set(
        self,
        report_id: str,
        query: Mapping[str, Any],
        context: ReportAuthorizationContext,
        result: Any,
        authorize: AuthorizeReport,
    ) -> None:
        self._require_authorized(report_id, context, authorize)
        with self._lock:
            self._entries[self._key(report_id, query, context)] = _CacheEntry(
                result=deepcopy(result),
                context=context,
                stored_at=time.time(),
            )
            self._audit("store", report_id, context)

    def invalidate_workspace(self, workspace_id: str) -> int:
        return self._invalidate(
            lambda entry: entry.context.workspace_id == str(workspace_id)
        )

    def invalidate_requester(self, requester_id: str) -> int:
        return self._invalidate(
            lambda entry: entry.context.requester_id == str(requester_id)
        )

    def invalidate_subject(self, requester_id: str, workspace_id: str) -> int:
        requester_id = str(requester_id)
        workspace_id = str(workspace_id)
        return self._invalidate(
            lambda entry: entry.context.requester_id == requester_id
            and entry.context.workspace_id == workspace_id
        )

    def clear(self) -> int:
        with self._lock:
            removed = len(self._entries)
            self._entries.clear()
            return removed

    def size(self) -> int:
        with self._lock:
            return len(self._entries)

    def _invalidate(self, predicate: Callable[[_CacheEntry], bool]) -> int:
        with self._lock:
            before = len(self._entries)
            self._entries = {
                key: entry
                for key, entry in self._entries.items()
                if not predicate(entry)
            }
            return before - len(self._entries)

    def _key(
        self,
        report_id: str,
        query: Mapping[str, Any],
        context: ReportAuthorizationContext,
    ) -> Tuple[
        str,
        str,
        Tuple[str, str, Tuple[str, ...], Tuple[str, ...], str],
    ]:
        return (
            str(report_id),
            _stable_digest(query),
            context.fingerprint(),
        )

    def _is_expired(self, entry: _CacheEntry) -> bool:
        return self.ttl_seconds is not None and (
            time.time() - entry.stored_at
        ) > self.ttl_seconds

    def _require_authorized(
        self,
        report_id: str,
        context: ReportAuthorizationContext,
        authorize: AuthorizeReport,
    ) -> None:
        if not authorize(str(report_id), context):
            self._audit("denied", report_id, context)
            self.invalidate_subject(context.requester_id, context.workspace_id)
            raise AuthenticationError("report access denied")

    def _audit(
        self,
        event: str,
        report_id: str,
        context: ReportAuthorizationContext,
    ) -> None:
        if self._audit_sink is None:
            return
        self._audit_sink(
            {
                "event": event,
                "report_id": str(report_id),
                "requester_id": context.requester_id,
                "workspace_id": context.workspace_id,
                "roles": context.roles,
                "permissions": context.permissions,
                "auth_version": context.auth_version,
            }
        )
