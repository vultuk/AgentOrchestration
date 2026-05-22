"""Authentication primitives for protected API routes."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, Iterable, Optional


READ_SCOPE = "agents:read"
WRITE_SCOPE = "agents:write"
ROLE_RANK = {"viewer": 1, "operator": 2, "admin": 3}


@dataclass(frozen=True)
class AuthPrincipal:
    subject: str
    scopes: frozenset[str]
    workspace_roles: Dict[str, str]
    expires_at: Optional[datetime] = None
    revoked: bool = False
    client_type: str = "token"
    metadata: Dict[str, str] = field(default_factory=dict)

    def is_expired(self, now: Optional[datetime] = None) -> bool:
        if self.expires_at is None:
            return False
        now = now or datetime.now(timezone.utc)
        expires_at = self.expires_at
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        return expires_at <= now

    def has_scope(self, required_scope: str) -> bool:
        return required_scope in self.scopes or "*" in self.scopes

    def has_workspace_role(
        self,
        workspace_id: str,
        required_role: str,
    ) -> bool:
        actual_role = self.workspace_roles.get(workspace_id)
        if actual_role is None:
            actual_role = self.workspace_roles.get("*")
        return ROLE_RANK.get(actual_role or "", 0) >= ROLE_RANK[required_role]


class AuthTokenStore:
    def __init__(self, tokens: Optional[Dict[str, AuthPrincipal]] = None):
        self._tokens = dict(tokens or {})

    @classmethod
    def from_environment(cls) -> "AuthTokenStore":
        tokens: Dict[str, AuthPrincipal] = {}
        bootstrap_token = os.getenv("AO_API_TOKEN")
        if bootstrap_token:
            tokens[bootstrap_token] = AuthPrincipal(
                subject="env-token",
                scopes=frozenset({READ_SCOPE, WRITE_SCOPE}),
                workspace_roles={"*": "admin"},
            )
        return cls(tokens)

    def add(self, token: str, principal: AuthPrincipal) -> None:
        self._tokens[token] = principal

    def get(self, token: str) -> Optional[AuthPrincipal]:
        return self._tokens.get(token)


def required_scope_for_method(method: str) -> str:
    if method.upper() in {"GET", "HEAD", "OPTIONS"}:
        return READ_SCOPE
    return WRITE_SCOPE


def required_role_for_method(method: str) -> str:
    if method.upper() in {"GET", "HEAD", "OPTIONS"}:
        return "viewer"
    return "operator"


def make_principal(
    subject: str,
    scopes: Iterable[str],
    workspace_roles: Dict[str, str],
    *,
    expires_at: Optional[datetime] = None,
    revoked: bool = False,
    client_type: str = "token",
) -> AuthPrincipal:
    return AuthPrincipal(
        subject=subject,
        scopes=frozenset(scopes),
        workspace_roles=dict(workspace_roles),
        expires_at=expires_at,
        revoked=revoked,
        client_type=client_type,
    )
