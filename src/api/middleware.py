"""API middleware components."""

import json
import os
import time
import logging
from dataclasses import dataclass
from typing import Callable, Optional, Set, Tuple, Union
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

logger = logging.getLogger(__name__)


class AuthError(Exception):
    def __init__(self, status_code: int, message: str):
        super().__init__(message)
        self.status_code = status_code
        self.message = message


@dataclass(frozen=True)
class AuthPrincipal:
    subject: str
    workspace_role: str
    scopes: frozenset[str]


@dataclass(frozen=True)
class TokenRecord:
    subject: str
    workspace_role: str
    scopes: frozenset[str]
    revoked: bool = False
    expires_at: Optional[float] = None


class TokenAuthorizer:
    def __init__(self, tokens: Optional[Union[dict, list]] = None):
        self._tokens = self._normalize_tokens(tokens or {})

    @classmethod
    def from_config(cls, config: Optional[dict] = None) -> "TokenAuthorizer":
        config = config or {}
        tokens = config.get("auth_tokens")
        if tokens is None:
            env_tokens = os.getenv("AO_AUTH_TOKENS")
            if env_tokens:
                tokens = json.loads(env_tokens)
        return cls(tokens)

    def authorize_request(
        self,
        authorization: str,
        session_token: Optional[str],
        required_scope: str,
        allowed_roles: Set[str],
    ) -> AuthPrincipal:
        token = self._extract_request_token(authorization, session_token)
        record = self._tokens.get(token)
        if record is None:
            raise AuthError(401, "Unauthorized")
        is_expired = (
            record.expires_at is not None
            and record.expires_at <= time.time()
        )
        if record.revoked or is_expired:
            raise AuthError(401, "Unauthorized")
        if required_scope not in record.scopes:
            raise AuthError(403, "Forbidden")
        if record.workspace_role not in allowed_roles:
            raise AuthError(403, "Forbidden")
        return AuthPrincipal(
            record.subject,
            record.workspace_role,
            record.scopes,
        )

    @classmethod
    def _extract_request_token(
        cls,
        authorization: str,
        session_token: Optional[str],
    ) -> str:
        if authorization:
            return cls._extract_bearer_token(authorization)
        if session_token:
            return cls._extract_session_token(session_token)
        raise AuthError(401, "Unauthorized")

    @classmethod
    def _extract_bearer_token(cls, authorization: str) -> str:
        prefix = "Bearer "
        if not authorization.startswith(prefix):
            raise AuthError(401, "Unauthorized")
        token = authorization[len(prefix):].strip()
        return cls._validate_token_value(token)

    @classmethod
    def _extract_session_token(cls, session_token: str) -> str:
        return cls._validate_token_value(session_token.strip())

    @classmethod
    def _validate_token_value(cls, token: str) -> str:
        if not token or any(char.isspace() for char in token):
            raise AuthError(401, "Unauthorized")
        return token

    @classmethod
    def _normalize_tokens(
        cls,
        tokens: Union[dict, list],
    ) -> dict[str, TokenRecord]:
        if isinstance(tokens, list):
            token_items = ((entry["token"], entry) for entry in tokens)
        else:
            token_items = tokens.items()

        normalized = {}
        for token, raw_record in token_items:
            if not token:
                continue
            if raw_record is None:
                raw_record = {}
            scopes = frozenset(
                raw_record.get("scopes", ["agents:read", "agents:write"])
            )
            normalized[str(token)] = TokenRecord(
                subject=raw_record.get("subject", "api-client"),
                workspace_role=raw_record.get("workspace_role", "admin"),
                scopes=scopes,
                revoked=bool(raw_record.get("revoked", False)),
                expires_at=raw_record.get("expires_at"),
            )
        return normalized


class AuthMiddleware(BaseHTTPMiddleware):
    PUBLIC_API_PATHS = {"/api/v2/auth/token"}
    READ_METHODS = {"GET", "HEAD"}

    def __init__(
        self,
        app,
        authorizer: Optional[TokenAuthorizer] = None,
        session_cookie_name: str = "ao_session",
    ):
        super().__init__(app)
        self.authorizer = authorizer or TokenAuthorizer()
        self.session_cookie_name = session_cookie_name

    async def dispatch(
        self,
        request: Request,
        call_next: Callable,
    ) -> Response:
        auth_requirement = self._auth_requirement(request)
        if auth_requirement is not None:
            required_scope, allowed_roles = auth_requirement
            try:
                request.state.auth = self.authorizer.authorize_request(
                    request.headers.get("Authorization", ""),
                    request.cookies.get(self.session_cookie_name),
                    required_scope,
                    allowed_roles,
                )
            except AuthError as exc:
                return Response(
                    status_code=exc.status_code,
                    content=exc.message,
                )
        return await call_next(request)

    def _auth_requirement(
        self,
        request: Request,
    ) -> Optional[Tuple[str, Set[str]]]:
        path = self._canonical_api_path(request.url.path)
        if not self._is_protected_api_path(path):
            return None
        if path in self.PUBLIC_API_PATHS:
            return None
        if request.method in self.READ_METHODS:
            return "agents:read", {"admin", "operator", "viewer"}
        return "agents:write", {"admin", "operator"}

    @staticmethod
    def _canonical_api_path(path: str) -> str:
        normalized = "/" + path.lstrip("/")
        while "//" in normalized:
            normalized = normalized.replace("//", "/")
        if len(normalized) > 1:
            normalized = normalized.rstrip("/")
        return normalized

    @staticmethod
    def _is_protected_api_path(path: str) -> bool:
        return path == "/api/v2" or path.startswith("/api/v2/")


class RateLimitMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, max_requests: int = 100, window: int = 60):
        super().__init__(app)
        self.max_requests = max_requests
        self.window = window
        self._requests = {}

    async def dispatch(
        self,
        request: Request,
        call_next: Callable,
    ) -> Response:
        client_ip = request.client.host if request.client else "unknown"
        now = time.time()

        if client_ip not in self._requests:
            self._requests[client_ip] = []

        self._requests[client_ip] = [
            t for t in self._requests[client_ip] if now - t < self.window
        ]

        if len(self._requests[client_ip]) >= self.max_requests:
            return Response(status_code=429, content="Too many requests")

        self._requests[client_ip].append(now)
        return await call_next(request)


class LoggingMiddleware(BaseHTTPMiddleware):
    async def dispatch(
        self,
        request: Request,
        call_next: Callable,
    ) -> Response:
        start = time.time()
        response = await call_next(request)
        duration = time.time() - start
        logger.info(
            f"{request.method} {request.url.path} "
            f"{response.status_code} {duration:.3f}s"
        )
        return response

# 2019-03-01T18:35:19 update

# 2019-04-03T13:22:05 update

# 2019-04-30T17:18:49 update

# 2019-08-20T09:29:03 update

# 2019-08-30T15:52:06 update

# 2019-11-23T16:58:42 update

# 2020-02-18T10:04:07 update

# 2020-04-21T17:35:30 update

# 2020-05-22T11:10:34 update

# 2020-07-02T12:31:26 update

# 2020-07-05T13:52:59 update

# 2020-08-21T20:36:45 update

# 2021-01-19T09:17:15 update

# 2021-01-29T11:34:24 update

# 2021-02-04T15:21:21 update

# 2021-04-19T19:23:15 update

# 2021-05-20T16:50:15 update

# 2021-06-22T19:23:44 update

# 2021-09-09T13:44:55 update

# 2021-09-16T09:30:20 update

# 2021-10-14T20:42:33 update

# 2021-12-28T16:39:14 update

# 2022-01-26T19:07:27 update

# 2022-01-28T08:03:41 update

# 2022-03-23T12:17:02 update

# 2022-04-06T12:12:27 update

# 2022-04-21T14:53:01 update

# 2022-06-30T08:37:32 update

# 2022-07-06T10:44:45 update

# 2022-11-02T11:12:47 update

# 2022-11-15T20:54:21 update

# 2022-11-23T14:13:34 update

# 2023-01-26T10:03:44 update

# 2023-02-09T17:08:10 update

# 2023-02-16T10:04:00 update

# 2023-03-14T11:52:03 update

# 2023-04-10T12:42:07 update

# 2023-04-26T10:43:39 update

# 2023-06-27T08:18:07 update

# 2023-08-30T15:30:40 update

# 2023-08-30T14:10:05 update

# 2023-10-09T18:32:46 update

# 2023-11-21T20:35:55 update

# 2024-03-07T19:17:39 update

# 2024-04-01T18:06:19 update

# 2024-07-18T15:37:34 update

# 2024-07-25T09:21:53 update

# 2024-08-12T14:24:22 update

# 2024-11-18T08:50:54 update

# 2025-04-08T12:43:05 update

# 2025-06-03T08:10:47 update

# 2025-06-12T08:37:52 update

# 2025-06-17T08:36:56 update

# 2025-07-02T18:09:42 update

# 2025-07-22T12:39:21 update

# 2025-10-13T12:13:46 update

# 2025-12-05T09:44:22 update

# 2025-12-22T18:34:47 update

# 2026-01-26T15:36:23 update

# 2026-02-13T12:36:40 update

# 2026-02-26T11:07:15 update

# 2026-03-19T11:00:17 update

# 2026-03-27T12:58:53 update

# 2026-05-12T17:19:36 update
