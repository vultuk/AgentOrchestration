"""Webhook endpoint registration and public event delivery."""

from __future__ import annotations

import hashlib
import time
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple
from uuid import uuid4
from urllib.parse import urlparse


INTERNAL_FIELD_NAMES = {
    "agent_token",
    "attempt_id",
    "debug_context",
    "handler",
    "internal_metadata",
    "internal_trace",
    "private_state",
    "run_id",
    "sandbox_path",
    "secret",
    "stack",
    "token",
    "worker_pid",
}
INTERNAL_FIELD_CANONICALS = {
    "".join(ch for ch in name if ch.isalnum())
    for name in INTERNAL_FIELD_NAMES
}
INTERNAL_FIELD_PREFIXES = {
    "agenttoken",
    "attemptid",
    "debugcontext",
    "internal",
    "privatestate",
    "runid",
    "sandboxpath",
    "secret",
    "token",
    "workerpid",
}

DeliverFunc = Callable[[str, Dict[str, Any]], bool]


@dataclass
class WebhookEndpoint:
    id: str
    workspace_id: str
    url: str
    event_types: Tuple[str, ...]
    enabled: bool = True
    version: int = 1
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)


@dataclass
class DeliveryRecord:
    delivery_id: str
    endpoint_id: str
    endpoint_version: int
    workspace_id: str
    event_id: str
    event_type: str
    url: str
    status: str
    attempts: int
    payload: Dict[str, Any]
    error: str = ""


class WebhookRegistry:
    """Stores scoped webhook endpoints and sanitized delivery records."""

    def __init__(self):
        self._endpoints: Dict[str, WebhookEndpoint] = {}
        self._deliveries: Dict[Tuple[str, int, str], DeliveryRecord] = {}
        self._callbacks: Dict[Tuple[str, str, str], Dict[str, Any]] = {}

    def register_endpoint(
        self,
        workspace_id: str,
        url: str,
        event_types: Optional[Iterable[str]] = None,
    ) -> str:
        workspace_id = self._require_identifier(workspace_id, "workspace_id")
        validated_url = self._validate_url(url)
        endpoint_id = str(uuid4())
        self._endpoints[endpoint_id] = WebhookEndpoint(
            id=endpoint_id,
            workspace_id=workspace_id,
            url=validated_url,
            event_types=tuple(event_types or ("*",)),
        )
        return endpoint_id

    def disable_endpoint(self, workspace_id: str, endpoint_id: str) -> bool:
        endpoint = self._endpoint_for_workspace(workspace_id, endpoint_id)
        if not endpoint:
            return False
        endpoint.enabled = False
        endpoint.updated_at = time.time()
        return True

    def rotate_endpoint_url(
        self,
        workspace_id: str,
        endpoint_id: str,
        url: str,
    ) -> bool:
        endpoint = self._endpoint_for_workspace(workspace_id, endpoint_id)
        if not endpoint:
            return False
        endpoint.url = self._validate_url(url)
        endpoint.version += 1
        endpoint.updated_at = time.time()
        return True

    def deliver_event(
        self,
        workspace_id: str,
        event_type: str,
        event_id: str,
        payload: Dict[str, Any],
        deliver: Optional[DeliverFunc] = None,
    ) -> List[DeliveryRecord]:
        workspace_id = self._require_identifier(workspace_id, "workspace_id")
        event_id = self._require_identifier(event_id, "event_id")
        event_type = self._require_identifier(event_type, "event_type")
        safe_payload = self.shape_public_event(event_type, event_id, payload)
        records = []

        for endpoint in self._matching_endpoints(workspace_id, event_type):
            key = (endpoint.id, endpoint.version, event_id)
            existing = self._deliveries.get(key)
            if existing and existing.status == "delivered":
                records.append(existing)
                continue

            record = existing or self._new_delivery_record(
                endpoint,
                event_type,
                event_id,
                safe_payload,
            )
            delivery_payload = record.payload

            if not endpoint.enabled:
                record.status = "rejected"
                record.error = "endpoint_disabled"
                self._deliveries[key] = record
                records.append(record)
                continue

            record.attempts += 1
            try:
                if deliver:
                    sent = deliver(endpoint.url, deepcopy(delivery_payload))
                else:
                    sent = True
            except Exception as exc:
                sent = False
                record.error = str(exc)

            record.status = "delivered" if sent else "retry_pending"
            if sent:
                record.error = ""
            self._deliveries[key] = record
            records.append(record)

        return records

    def record_callback(
        self,
        workspace_id: str,
        endpoint_id: str,
        callback_id: str,
        payload: Dict[str, Any],
    ) -> Dict[str, Any]:
        endpoint = self._endpoint_for_workspace(workspace_id, endpoint_id)
        if not endpoint:
            return {"status": "rejected", "reason": "endpoint_not_found"}
        if not endpoint.enabled:
            return {"status": "rejected", "reason": "endpoint_disabled"}

        key = (endpoint.workspace_id, endpoint.id, callback_id)
        if key not in self._callbacks:
            self._callbacks[key] = {
                "status": "accepted",
                "endpoint_id": endpoint.id,
                "workspace_id": endpoint.workspace_id,
                "callback_id": callback_id,
                "payload": self._sanitize(payload),
            }
        return deepcopy(self._callbacks[key])

    def shape_public_event(
        self,
        event_type: str,
        event_id: str,
        payload: Dict[str, Any],
    ) -> Dict[str, Any]:
        return {
            "id": event_id,
            "type": event_type,
            "payload": self._sanitize(payload),
        }

    def delivery_records(self) -> List[DeliveryRecord]:
        return [
            DeliveryRecord(
                delivery_id=record.delivery_id,
                endpoint_id=record.endpoint_id,
                endpoint_version=record.endpoint_version,
                workspace_id=record.workspace_id,
                event_id=record.event_id,
                event_type=record.event_type,
                url=record.url,
                status=record.status,
                attempts=record.attempts,
                payload=deepcopy(record.payload),
                error=record.error,
            )
            for record in self._deliveries.values()
        ]

    def _new_delivery_record(
        self,
        endpoint: WebhookEndpoint,
        event_type: str,
        event_id: str,
        payload: Dict[str, Any],
    ) -> DeliveryRecord:
        return DeliveryRecord(
            delivery_id=self._delivery_id(
                endpoint.id,
                endpoint.version,
                event_id,
            ),
            endpoint_id=endpoint.id,
            endpoint_version=endpoint.version,
            workspace_id=endpoint.workspace_id,
            event_id=event_id,
            event_type=event_type,
            url=endpoint.url,
            status="pending",
            attempts=0,
            payload=payload,
        )

    def _matching_endpoints(
        self,
        workspace_id: str,
        event_type: str,
    ) -> Iterable[WebhookEndpoint]:
        for endpoint in self._endpoints.values():
            if endpoint.workspace_id != workspace_id:
                continue
            if (
                "*" not in endpoint.event_types
                and event_type not in endpoint.event_types
            ):
                continue
            yield endpoint

    def _endpoint_for_workspace(
        self,
        workspace_id: str,
        endpoint_id: str,
    ) -> Optional[WebhookEndpoint]:
        endpoint = self._endpoints.get(endpoint_id)
        if not endpoint or endpoint.workspace_id != workspace_id:
            return None
        return endpoint

    def _sanitize(self, value: Any) -> Any:
        if isinstance(value, dict):
            clean = {}
            for key, nested in value.items():
                key_name = str(key)
                if self._is_internal_field(key_name):
                    continue
                clean[key_name] = self._sanitize(nested)
            return clean
        if isinstance(value, list):
            return [self._sanitize(item) for item in value]
        if isinstance(value, tuple):
            return [self._sanitize(item) for item in value]
        return value

    def _is_internal_field(self, key: str) -> bool:
        normalized = key.strip().lower()
        compact = "".join(ch for ch in normalized if ch.isalnum())
        return (
            normalized in INTERNAL_FIELD_NAMES
            or compact in INTERNAL_FIELD_CANONICALS
            or normalized.startswith("_")
            or normalized.startswith("internal_")
            or any(
                compact.startswith(prefix)
                for prefix in INTERNAL_FIELD_PREFIXES
            )
        )

    def _delivery_id(
        self,
        endpoint_id: str,
        endpoint_version: int,
        event_id: str,
    ) -> str:
        raw = f"{endpoint_id}:{endpoint_version}:{event_id}".encode()
        return hashlib.sha256(raw).hexdigest()

    def _validate_url(self, url: str) -> str:
        parsed = urlparse((url or "").strip())
        if parsed.scheme != "https" or not parsed.netloc:
            raise ValueError("webhook endpoints must use an https URL")
        return parsed.geturl()

    def _require_identifier(self, value: str, name: str) -> str:
        value = (value or "").strip()
        if not value:
            raise ValueError(f"{name} is required")
        return value
