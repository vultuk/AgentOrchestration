"""Webhook endpoint registration and idempotent delivery helpers."""

import hashlib
import logging
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

from src.common.metrics import MetricsCollector

logger = logging.getLogger(__name__)


@dataclass
class WebhookEndpoint:
    workspace_id: str
    endpoint_id: str
    url: str
    version: int = 1
    enabled: bool = True


@dataclass
class WebhookDeliveryRecord:
    workspace_id: str
    endpoint_id: str
    endpoint_version: int
    event_id: str
    event_type: str
    event_revision: int
    idempotency_key: str
    status: str
    reason: str = ""
    attempts: int = 1
    created_at: float = 0.0
    updated_at: float = 0.0


class WebhookDeliveryService:
    """Scopes webhook delivery by workspace and event idempotency key."""

    def __init__(self, metrics_collector: Optional[MetricsCollector] = None):
        self.metrics = metrics_collector or MetricsCollector()
        self._endpoints: Dict[Tuple[str, str], WebhookEndpoint] = {}
        self._deliveries: Dict[str, WebhookDeliveryRecord] = {}
        self._latest_revision: Dict[Tuple[str, str, str, str], int] = {}
        self._audit: List[Dict[str, Any]] = []

    def register_endpoint(
        self,
        workspace_id: str,
        endpoint_id: str,
        url: str,
        enabled: bool = True,
    ) -> Dict[str, Any]:
        self._validate_ref(workspace_id, "workspace_id")
        self._validate_ref(endpoint_id, "endpoint_id")
        self._validate_url(url)
        endpoint = WebhookEndpoint(
            workspace_id=workspace_id,
            endpoint_id=endpoint_id,
            url=url,
            enabled=enabled,
        )
        self._endpoints[(workspace_id, endpoint_id)] = endpoint
        return self._safe_endpoint(endpoint)

    def disable_endpoint(
        self,
        workspace_id: str,
        endpoint_id: str,
    ) -> Dict[str, Any]:
        endpoint = self._require_endpoint(workspace_id, endpoint_id)
        endpoint.enabled = False
        return self._safe_endpoint(endpoint)

    def rotate_endpoint(
        self,
        workspace_id: str,
        endpoint_id: str,
        url: Optional[str] = None,
    ) -> Dict[str, Any]:
        endpoint = self._require_endpoint(workspace_id, endpoint_id)
        if url is not None:
            self._validate_url(url)
            endpoint.url = url
        endpoint.version += 1
        endpoint.enabled = True
        return self._safe_endpoint(endpoint)

    def deliver_event(
        self,
        workspace_id: str,
        endpoint_id: str,
        event_id: str,
        event_type: str,
        payload: Optional[Dict[str, Any]] = None,
        event_revision: int = 1,
        endpoint_version: Optional[int] = None,
        idempotency_key: Optional[str] = None,
    ) -> Dict[str, Any]:
        endpoint = self._endpoints.get((workspace_id, endpoint_id))
        requested_version = endpoint_version
        if endpoint and requested_version is None:
            requested_version = endpoint.version
        key = idempotency_key or self._make_idempotency_key(
            workspace_id,
            endpoint_id,
            event_id,
            event_type,
            requested_version or 0,
        )
        existing = self._deliveries.get(key)
        if existing:
            existing.attempts += 1
            existing.updated_at = time.time()
            self._metric("duplicate")
            return self._safe_delivery(existing, decision="duplicate")

        if not endpoint:
            return self._rejected(
                workspace_id,
                endpoint_id,
                requested_version or 0,
                event_id,
                event_type,
                event_revision,
                key,
                "endpoint_not_found",
            )
        if not endpoint.enabled:
            return self._rejected_from_endpoint(
                endpoint,
                event_id,
                event_type,
                event_revision,
                key,
                "endpoint_disabled",
            )
        if requested_version != endpoint.version:
            return self._rejected_from_endpoint(
                endpoint,
                event_id,
                event_type,
                event_revision,
                key,
                "endpoint_rotated",
            )
        parsed_revision = self._event_revision(event_revision)
        if parsed_revision is None:
            return self._rejected_from_endpoint(
                endpoint,
                event_id,
                event_type,
                0,
                key,
                "invalid_event_revision",
            )
        event_revision = parsed_revision

        latest_key = (workspace_id, endpoint_id, event_id, event_type)
        latest = self._latest_revision.get(latest_key, 0)
        if event_revision < latest:
            return self._rejected_from_endpoint(
                endpoint,
                event_id,
                event_type,
                event_revision,
                key,
                "stale_event_revision",
            )

        now = time.time()
        record = WebhookDeliveryRecord(
            workspace_id=workspace_id,
            endpoint_id=endpoint_id,
            endpoint_version=endpoint.version,
            event_id=event_id,
            event_type=event_type,
            event_revision=event_revision,
            idempotency_key=key,
            status="queued",
            created_at=now,
            updated_at=now,
        )
        self._deliveries[key] = record
        self._latest_revision[latest_key] = event_revision
        self._metric("accepted")
        self._audit_decision(record, "accepted")
        return self._safe_delivery(record, decision="accepted")

    def record_callback(
        self,
        workspace_id: str,
        endpoint_id: str,
        idempotency_key: str,
        callback_status: str,
        endpoint_version: Optional[int] = None,
    ) -> Dict[str, Any]:
        record = self._deliveries.get(idempotency_key)
        if not record:
            self._metric("callback_rejected")
            return self._safe_callback_rejection(
                workspace_id,
                endpoint_id,
                idempotency_key,
                "delivery_not_found",
            )
        if (
            record.workspace_id != workspace_id
            or record.endpoint_id != endpoint_id
        ):
            self._metric("callback_rejected")
            return self._safe_callback_rejection(
                workspace_id,
                endpoint_id,
                idempotency_key,
                "workspace_mismatch",
            )
        if (
            endpoint_version is not None
            and endpoint_version != record.endpoint_version
        ):
            self._metric("callback_rejected")
            return self._safe_callback_rejection(
                workspace_id,
                endpoint_id,
                idempotency_key,
                "endpoint_version_mismatch",
            )
        terminal_statuses = {"delivered", "failed", "rejected"}
        if record.status in terminal_statuses:
            self._metric("callback_duplicate")
            return self._safe_delivery(record, decision="duplicate")
        if callback_status not in {"delivered", "failed"}:
            self._metric("callback_rejected")
            return self._safe_callback_rejection(
                workspace_id,
                endpoint_id,
                idempotency_key,
                "invalid_callback_status",
            )

        record.status = callback_status
        record.updated_at = time.time()
        self._metric("callback_accepted")
        self._audit_decision(record, "callback_accepted")
        return self._safe_delivery(record, decision="callback_accepted")

    def get_delivery(self, idempotency_key: str) -> Optional[Dict[str, Any]]:
        record = self._deliveries.get(idempotency_key)
        if not record:
            return None
        return self._safe_delivery(record, decision="found")

    def delivery_count(self) -> int:
        return len(self._deliveries)

    def audit_records(self) -> List[Dict[str, Any]]:
        return [dict(record) for record in self._audit]

    def _rejected_from_endpoint(
        self,
        endpoint: WebhookEndpoint,
        event_id: str,
        event_type: str,
        event_revision: int,
        idempotency_key: str,
        reason: str,
    ) -> Dict[str, Any]:
        return self._rejected(
            endpoint.workspace_id,
            endpoint.endpoint_id,
            endpoint.version,
            event_id,
            event_type,
            event_revision,
            idempotency_key,
            reason,
        )

    def _rejected(
        self,
        workspace_id: str,
        endpoint_id: str,
        endpoint_version: int,
        event_id: str,
        event_type: str,
        event_revision: int,
        idempotency_key: str,
        reason: str,
    ) -> Dict[str, Any]:
        now = time.time()
        record = WebhookDeliveryRecord(
            workspace_id=workspace_id,
            endpoint_id=endpoint_id,
            endpoint_version=endpoint_version,
            event_id=event_id,
            event_type=event_type,
            event_revision=event_revision,
            idempotency_key=idempotency_key,
            status="rejected",
            reason=reason,
            created_at=now,
            updated_at=now,
        )
        self._deliveries[idempotency_key] = record
        self._metric("rejected")
        self.metrics.increment(f"webhooks.delivery.rejected.{reason}")
        self._audit_decision(record, "rejected")
        return self._safe_delivery(record, decision="rejected")

    def _safe_endpoint(self, endpoint: WebhookEndpoint) -> Dict[str, Any]:
        return {
            "workspace_ref": self._ref(endpoint.workspace_id),
            "endpoint_ref": self._ref(endpoint.endpoint_id),
            "version": endpoint.version,
            "enabled": endpoint.enabled,
        }

    def _safe_delivery(
        self,
        record: WebhookDeliveryRecord,
        decision: str,
    ) -> Dict[str, Any]:
        return {
            "decision": decision,
            "status": record.status,
            "reason": record.reason,
            "idempotency_key": record.idempotency_key,
            "workspace_ref": self._ref(record.workspace_id),
            "endpoint_ref": self._ref(record.endpoint_id),
            "event_ref": self._ref(record.event_id),
            "event_type": record.event_type,
            "event_revision": record.event_revision,
            "endpoint_version": record.endpoint_version,
            "attempts": record.attempts,
        }

    def _safe_callback_rejection(
        self,
        workspace_id: str,
        endpoint_id: str,
        idempotency_key: str,
        reason: str,
    ) -> Dict[str, Any]:
        return {
            "decision": "rejected",
            "status": "rejected",
            "reason": reason,
            "idempotency_key": idempotency_key,
            "workspace_ref": self._ref(workspace_id),
            "endpoint_ref": self._ref(endpoint_id),
        }

    def _audit_decision(
        self,
        record: WebhookDeliveryRecord,
        decision: str,
    ) -> None:
        audit = self._safe_delivery(record, decision)
        audit.pop("idempotency_key", None)
        self._audit.append(audit)
        logger.info(
            "webhook delivery decision=%s status=%s reason=%s "
            "workspace_ref=%s endpoint_ref=%s event_ref=%s",
            decision,
            record.status,
            record.reason or "none",
            audit["workspace_ref"],
            audit["endpoint_ref"],
            audit["event_ref"],
        )

    def _metric(self, decision: str) -> None:
        self.metrics.increment(f"webhooks.delivery.{decision}")

    def _make_idempotency_key(
        self,
        workspace_id: str,
        endpoint_id: str,
        event_id: str,
        event_type: str,
        endpoint_version: int,
    ) -> str:
        raw = "|".join(
            [
                workspace_id,
                endpoint_id,
                str(endpoint_version),
                event_id,
                event_type,
            ]
        )
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def _ref(self, value: str) -> str:
        if not value:
            return "unknown"
        return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]

    def _validate_ref(self, value: str, field: str) -> None:
        if not value or not value.strip():
            raise ValueError(f"{field} is required")

    def _validate_url(self, url: str) -> None:
        parsed = urlparse(url)
        if parsed.scheme != "https" or not parsed.netloc:
            raise ValueError("webhook endpoint must be an https URL")

    def _require_endpoint(
        self,
        workspace_id: str,
        endpoint_id: str,
    ) -> WebhookEndpoint:
        endpoint = self._endpoints.get((workspace_id, endpoint_id))
        if not endpoint:
            raise KeyError("webhook endpoint not found")
        return endpoint

    def _event_revision(self, value: int) -> Optional[int]:
        try:
            revision = int(value)
        except (TypeError, ValueError):
            return None
        return revision if revision >= 0 else None


webhook_delivery_service = WebhookDeliveryService()
