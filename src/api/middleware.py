"""API middleware components."""

import time
import logging
from typing import Callable, Dict, Iterable, Optional, Tuple
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

logger = logging.getLogger(__name__)

DOCUMENTATION_PATHS = {
    "/api/docs",
    "/api/redoc",
    "/api/openapi.json",
    "/openapi.json",
}
PUBLIC_API_PATHS = {"/api/v2/auth/token"}
DOC_SCOPE = "docs:read"
DOC_ROLES = {"admin", "developer", "maintainer", "owner"}


class AuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(
        self,
        request: Request,
        call_next: Callable,
    ) -> Response:
        requirement = self._auth_requirement(request.url.path)
        if requirement:
            error = self._authorize(request, *requirement)
            if error:
                status_code, content = error
                return Response(status_code=status_code, content=content)
        return await call_next(request)

    def _auth_requirement(
        self, path: str
    ) -> Optional[Tuple[Optional[str], Optional[Iterable[str]]]]:
        if path in DOCUMENTATION_PATHS:
            return DOC_SCOPE, DOC_ROLES
        if path.startswith("/api/v2") and path not in PUBLIC_API_PATHS:
            return None, None
        return None

    def _authorize(
        self,
        request: Request,
        required_scope: Optional[str],
        allowed_roles: Optional[Iterable[str]],
    ) -> Optional[Tuple[int, str]]:
        token, malformed = self._extract_token(request)
        if malformed or not token:
            return 401, "Unauthorized"

        token_store = getattr(request.app.state, "auth_tokens", None)
        if token_store is None:
            if required_scope:
                return 401, "Unauthorized"
            return None

        principal = token_store.get(token)
        if principal is None:
            return 401, "Unauthorized"

        if principal.get("revoked"):
            return 401, "Unauthorized"

        expires_at = principal.get("expires_at")
        if expires_at is not None and self._is_expired(expires_at):
            return 401, "Unauthorized"

        if required_scope and not self._has_scope(principal, required_scope):
            return 403, "Forbidden"

        if allowed_roles and not self._has_workspace_role(
            request, principal, allowed_roles
        ):
            return 403, "Forbidden"

        return None

    def _extract_token(self, request: Request) -> Tuple[Optional[str], bool]:
        authorization = request.headers.get("Authorization", "")
        if authorization:
            if not authorization.startswith("Bearer "):
                return None, True
            token = authorization.removeprefix("Bearer ").strip()
            return token or None, not bool(token)

        session_token = request.cookies.get("ao_session")
        if session_token:
            session_token = session_token.strip()
        return session_token or None, False

    def _is_expired(self, expires_at) -> bool:
        try:
            return float(expires_at) <= time.time()
        except (TypeError, ValueError):
            return True

    def _has_scope(self, principal: Dict, required_scope: str) -> bool:
        scopes = principal.get("scopes", [])
        if isinstance(scopes, str):
            scopes = [scopes]
        return required_scope in set(scopes)

    def _has_workspace_role(
        self,
        request: Request,
        principal: Dict,
        allowed_roles: Iterable[str],
    ) -> bool:
        workspace_id = (
            request.headers.get("X-Workspace-Id")
            or principal.get("workspace_id")
            or "default"
        )
        roles_by_workspace = principal.get("workspace_roles") or {}
        if isinstance(roles_by_workspace, dict):
            roles = roles_by_workspace.get(
                workspace_id,
                principal.get("roles", []),
            )
        else:
            roles = principal.get("roles", [])
        roles = roles or []
        if isinstance(roles, str):
            roles = [roles]
        return bool(set(roles).intersection(allowed_roles))


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
            "%s %s %s %.3fs",
            request.method,
            request.url.path,
            response.status_code,
            duration,
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
