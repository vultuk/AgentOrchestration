"""Webhook endpoint registration and delivery safeguards."""

import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, Mapping, Optional, Set
from urllib.parse import urlparse


class WebhookValidationError(ValueError):
    """Raised when a webhook endpoint or delivery is not trusted."""


CallbackSender = Callable[[str, Mapping[str, Any]], Mapping[str, Any]]


@dataclass(frozen=True)
class WebhookEndpoint:
    id: str
    workspace_id: str
    url: str
    events: Set[str]
    active: bool = True
    secret_version: str = "v1"
    rotation: int = 0
    metadata: Dict[str, Any] = field(default_factory=dict)

    def public_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "url": self.url,
            "events": sorted(self.events),
            "active": self.active,
        }


@dataclass(frozen=True)
class WebhookDeliveryRecord:
    id: str
    endpoint_id: str
    workspace_id: str
    event: str
    idempotency_key: str
    callback_payload: Dict[str, Any]
    status_code: int
    delivered: bool

    def public_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "endpoint_id": self.endpoint_id,
            "event": self.event,
            "status_code": self.status_code,
            "delivered": self.delivered,
            "payload": self.callback_payload,
        }


class WebhookManager:
    """Register and deliver workspace-scoped webhooks safely."""

    def __init__(
        self,
        environment: str = "development",
        callback_sender: Optional[CallbackSender] = None,
    ):
        self.environment = environment.lower()
        self._callback_sender = callback_sender or self._default_sender
        self._endpoints: Dict[str, WebhookEndpoint] = {}
        self._deliveries: Dict[str, WebhookDeliveryRecord] = {}

    @property
    def endpoints(self) -> Dict[str, WebhookEndpoint]:
        return dict(self._endpoints)

    def register_endpoint(
        self,
        workspace_id: str,
        url: str,
        events: Iterable[str],
        endpoint_id: Optional[str] = None,
        secret_version: str = "v1",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> WebhookEndpoint:
        clean_workspace = self._require_workspace(workspace_id)
        clean_url = self._validate_endpoint_url(url)
        clean_events = self._normalize_events(events)
        endpoint = WebhookEndpoint(
            id=endpoint_id or str(uuid.uuid4()),
            workspace_id=clean_workspace,
            url=clean_url,
            events=clean_events,
            secret_version=self._require_value(
                secret_version,
                "secret_version",
            ),
            metadata=dict(metadata or {}),
        )
        self._endpoints[endpoint.id] = endpoint
        return endpoint

    def disable_endpoint(self, endpoint_id: str) -> WebhookEndpoint:
        endpoint = self._require_endpoint(endpoint_id)
        disabled = WebhookEndpoint(
            id=endpoint.id,
            workspace_id=endpoint.workspace_id,
            url=endpoint.url,
            events=set(endpoint.events),
            active=False,
            secret_version=endpoint.secret_version,
            rotation=endpoint.rotation,
            metadata=dict(endpoint.metadata),
        )
        self._endpoints[endpoint_id] = disabled
        return disabled

    def rotate_secret(
        self,
        endpoint_id: str,
        secret_version: str,
    ) -> WebhookEndpoint:
        endpoint = self._require_endpoint(endpoint_id)
        rotated = WebhookEndpoint(
            id=endpoint.id,
            workspace_id=endpoint.workspace_id,
            url=endpoint.url,
            events=set(endpoint.events),
            active=endpoint.active,
            secret_version=self._require_value(
                secret_version,
                "secret_version",
            ),
            rotation=endpoint.rotation + 1,
            metadata=dict(endpoint.metadata),
        )
        self._endpoints[endpoint_id] = rotated
        return rotated

    def deliver_event(
        self,
        workspace_id: str,
        endpoint_id: str,
        event: str,
        payload: Mapping[str, Any],
        delivery_id: Optional[str] = None,
        secret_version: Optional[str] = None,
        idempotency_key: Optional[str] = None,
    ) -> WebhookDeliveryRecord:
        endpoint = self._authorize_delivery(
            workspace_id,
            endpoint_id,
            event,
            secret_version,
        )
        clean_delivery_id = delivery_id or str(uuid.uuid4())
        clean_idempotency_key = idempotency_key or ":".join(
            [endpoint.id, event, clean_delivery_id]
        )
        if clean_idempotency_key in self._deliveries:
            return self._deliveries[clean_idempotency_key]

        callback_payload = {
            "delivery_id": clean_delivery_id,
            "event": event,
            "data": self._sanitize_payload(payload),
        }
        response = self._callback_sender(endpoint.url, callback_payload)
        status_code = int(response.get("status_code", 202))
        record = WebhookDeliveryRecord(
            id=clean_delivery_id,
            endpoint_id=endpoint.id,
            workspace_id=endpoint.workspace_id,
            event=event,
            idempotency_key=clean_idempotency_key,
            callback_payload=callback_payload,
            status_code=status_code,
            delivered=200 <= status_code < 300,
        )
        self._deliveries[clean_idempotency_key] = record
        return record

    def delivery_records(self, workspace_id: str) -> Dict[str, Dict[str, Any]]:
        clean_workspace = self._require_workspace(workspace_id)
        return {
            record.id: record.public_dict()
            for record in self._deliveries.values()
            if record.workspace_id == clean_workspace
        }

    def _authorize_delivery(
        self,
        workspace_id: str,
        endpoint_id: str,
        event: str,
        secret_version: Optional[str],
    ) -> WebhookEndpoint:
        clean_workspace = self._require_workspace(workspace_id)
        endpoint = self._require_endpoint(endpoint_id)
        if endpoint.workspace_id != clean_workspace:
            raise WebhookValidationError(
                "webhook endpoint is not in this workspace"
            )
        if not endpoint.active:
            raise WebhookValidationError("webhook endpoint is disabled")
        if event not in endpoint.events:
            raise WebhookValidationError(
                "webhook endpoint is not subscribed to event"
            )
        if (
            secret_version is not None
            and secret_version != endpoint.secret_version
        ):
            raise WebhookValidationError("webhook endpoint secret has rotated")
        self._validate_endpoint_url(endpoint.url)
        return endpoint

    def _validate_endpoint_url(self, url: str) -> str:
        clean_url = self._require_value(url, "url")
        parsed = urlparse(clean_url)
        if not parsed.scheme or not parsed.netloc:
            raise WebhookValidationError("webhook URL must be absolute")
        if (
            self.environment in {"prod", "production"}
            and parsed.scheme != "https"
        ):
            raise WebhookValidationError(
                "production webhook URLs must use HTTPS"
            )
        if parsed.scheme not in {"http", "https"}:
            raise WebhookValidationError("webhook URL scheme is not supported")
        return clean_url

    def _require_endpoint(self, endpoint_id: str) -> WebhookEndpoint:
        endpoint = self._endpoints.get(endpoint_id)
        if not endpoint:
            raise WebhookValidationError("webhook endpoint was not found")
        return endpoint

    @classmethod
    def _normalize_events(cls, events: Iterable[str]) -> Set[str]:
        clean_events = {
            event.strip()
            for event in events
            if isinstance(event, str) and event.strip()
        }
        if not clean_events:
            raise WebhookValidationError(
                "webhook endpoint needs at least one event"
            )
        return clean_events

    @staticmethod
    def _require_workspace(workspace_id: str) -> str:
        return WebhookManager._require_value(workspace_id, "workspace_id")

    @staticmethod
    def _require_value(value: str, field_name: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise WebhookValidationError(f"{field_name} is required")
        return value.strip()

    @classmethod
    def _sanitize_payload(cls, value: Any) -> Any:
        if isinstance(value, Mapping):
            return {
                key: cls._sanitize_payload(nested)
                for key, nested in value.items()
                if cls._is_public_key(key)
            }
        if isinstance(value, list):
            return [cls._sanitize_payload(item) for item in value]
        return value

    @staticmethod
    def _is_public_key(key: Any) -> bool:
        if not isinstance(key, str):
            return False
        return not (key.startswith("_") or key.startswith("internal_"))

    @staticmethod
    def _default_sender(
        url: str,
        payload: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        return {"status_code": 202}
