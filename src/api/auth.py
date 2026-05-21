"""Authentication helpers for API middleware."""

import base64
import hashlib
import hmac
import json
import os
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, Iterable, Optional, Set, Tuple

from src.common.errors import AuthenticationError


@dataclass(frozen=True)
class Principal:
    subject: str
    workspace_id: str
    scopes: Set[str]
    roles: Set[str]
    claims: Dict[str, Any]


class AuthorizationError(AuthenticationError):
    def __init__(self, message: str = "Forbidden"):
        super().__init__(message)


class AuthService:
    def __init__(
        self,
        secret: Optional[str] = None,
        expected_audience: Optional[str] = None,
        expected_issuer: Optional[str] = None,
        max_token_age_seconds: Optional[int] = None,
        revoked_token_ids: Optional[Iterable[str]] = None,
        now: Optional[Callable[[], float]] = None,
    ):
        self.secret = (
            secret if secret is not None else os.getenv("AO_JWT_SECRET", "")
        )
        self.expected_audience = expected_audience or os.getenv(
            "AO_WORKER_JWT_AUDIENCE",
            "agent-workers",
        )
        self.expected_issuer = (
            expected_issuer
            if expected_issuer is not None
            else os.getenv("AO_WORKER_JWT_ISSUER") or None
        )
        self.max_token_age_seconds = self._configured_max_token_age(
            max_token_age_seconds,
        )
        self.revoked_token_ids = set(revoked_token_ids or [])
        self._now = now or time.time

    def authenticate(
        self,
        token: str,
        *,
        required_scope: str,
        required_role: str,
        workspace_id: str,
    ) -> Principal:
        claims = self._decode_and_verify(token)
        self._require_audience(claims)
        self._require_issuer(claims)
        self._require_fresh_token(claims)
        self._require_not_revoked(claims)

        scopes = self._extract_scopes(claims)
        if required_scope not in scopes:
            raise AuthorizationError("Insufficient token scope")

        roles = self._extract_workspace_roles(claims, workspace_id)
        if required_role not in roles and "admin" not in roles:
            raise AuthorizationError("Insufficient workspace role")

        subject = claims.get("sub")
        if not isinstance(subject, str) or not subject.strip():
            raise AuthenticationError("Missing token subject")

        return Principal(
            subject=subject,
            workspace_id=workspace_id,
            scopes=scopes,
            roles=roles,
            claims=claims,
        )

    def revoke(self, token_id: str) -> None:
        self.revoked_token_ids.add(token_id)

    def _decode_and_verify(self, token: str) -> Dict[str, Any]:
        if not self.secret:
            raise AuthenticationError(
                "JWT verification secret is not configured"
            )

        parts = token.split(".")
        if len(parts) != 3:
            raise AuthenticationError("Malformed JWT")

        signing_input = f"{parts[0]}.{parts[1]}".encode("ascii")
        expected = hmac.new(
            self.secret.encode("utf-8"),
            signing_input,
            hashlib.sha256,
        ).digest()
        provided = self._urlsafe_decode(parts[2])
        if not hmac.compare_digest(expected, provided):
            raise AuthenticationError("Invalid JWT signature")

        header = self._decode_json_part(parts[0])
        if header.get("alg") != "HS256":
            raise AuthenticationError("Unsupported JWT algorithm")

        claims = self._decode_json_part(parts[1])
        if not isinstance(claims, dict):
            raise AuthenticationError("Malformed JWT claims")
        return claims

    def _require_audience(self, claims: Dict[str, Any]) -> None:
        audience = claims.get("aud")
        if isinstance(audience, str):
            audiences = {audience}
        elif isinstance(audience, list):
            audiences = {item for item in audience if isinstance(item, str)}
        else:
            audiences = set()

        if self.expected_audience not in audiences:
            raise AuthenticationError("Invalid JWT audience")

    def _require_issuer(self, claims: Dict[str, Any]) -> None:
        if self.expected_issuer is None:
            return
        if claims.get("iss") != self.expected_issuer:
            raise AuthenticationError("Invalid JWT issuer")

    def _require_fresh_token(self, claims: Dict[str, Any]) -> None:
        expires_at = claims.get("exp")
        if not isinstance(expires_at, (int, float)):
            raise AuthenticationError("Missing token expiry")
        if expires_at <= self._now():
            raise AuthenticationError("Token has expired")

        not_before = claims.get("nbf")
        if isinstance(not_before, (int, float)) and not_before > self._now():
            raise AuthenticationError("Token is not active yet")

        issued_at = claims.get("iat")
        if isinstance(issued_at, (int, float)) and issued_at > self._now():
            raise AuthenticationError("Token was issued in the future")
        if self.max_token_age_seconds is None:
            return
        if not isinstance(issued_at, (int, float)):
            raise AuthenticationError("Missing token issue time")
        if self._now() - issued_at > self.max_token_age_seconds:
            raise AuthenticationError("Token is too old")

    def _require_not_revoked(self, claims: Dict[str, Any]) -> None:
        token_id = claims.get("jti")
        if isinstance(token_id, str) and token_id in self.revoked_token_ids:
            raise AuthenticationError("Token has been revoked")

    @staticmethod
    def _configured_max_token_age(
        max_token_age_seconds: Optional[int],
    ) -> Optional[int]:
        if max_token_age_seconds is not None:
            return max_token_age_seconds
        raw_value = os.getenv("AO_WORKER_JWT_MAX_AGE_SECONDS")
        if raw_value in {None, ""}:
            return None
        try:
            parsed = int(raw_value)
        except ValueError as exc:
            raise ValueError(
                "AO_WORKER_JWT_MAX_AGE_SECONDS must be an integer"
            ) from exc
        return parsed if parsed > 0 else None

    @staticmethod
    def _extract_scopes(claims: Dict[str, Any]) -> Set[str]:
        raw_scopes = claims.get("scopes", claims.get("scope", ""))
        if isinstance(raw_scopes, str):
            return {scope for scope in raw_scopes.split() if scope}
        if isinstance(raw_scopes, list):
            return {
                scope for scope in raw_scopes
                if isinstance(scope, str) and scope
            }
        return set()

    @staticmethod
    def _extract_workspace_roles(
        claims: Dict[str, Any],
        workspace_id: str,
    ) -> Set[str]:
        raw_roles = claims.get("workspace_roles", claims.get("roles", {}))
        if isinstance(raw_roles, dict):
            workspace_roles = raw_roles.get(workspace_id, [])
        else:
            workspace_roles = raw_roles

        if isinstance(workspace_roles, str):
            return {workspace_roles}
        if isinstance(workspace_roles, list):
            return {
                role for role in workspace_roles
                if isinstance(role, str) and role
            }
        return set()

    @staticmethod
    def _decode_json_part(value: str) -> Any:
        try:
            payload = AuthService._urlsafe_decode(value).decode("utf-8")
            return json.loads(payload)
        except (ValueError, UnicodeDecodeError) as exc:
            raise AuthenticationError("Malformed JWT") from exc

    @staticmethod
    def _urlsafe_decode(value: str) -> bytes:
        padded = value + "=" * (-len(value) % 4)
        try:
            return base64.urlsafe_b64decode(padded.encode("ascii"))
        except (ValueError, UnicodeEncodeError) as exc:
            raise AuthenticationError("Malformed JWT") from exc


def agent_worker_requirements(
    method: str,
    path: str,
) -> Optional[Tuple[str, str]]:
    if not path.startswith("/api/v2/agents"):
        return None

    if method.upper() in {"GET", "HEAD"}:
        return "agents:read", "viewer"
    return "agents:write", "worker"
