"""API middleware components."""

import hashlib
import logging
import time
from contextvars import ContextVar
from typing import Callable, Dict, Optional
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

logger = logging.getLogger(__name__)

_REQUEST_AGENT_CONTEXT: ContextVar[Optional[Dict[str, str]]] = ContextVar(
    "request_agent_context",
    default=None,
)
_AGENT_CONTEXT_HEADER = "X-Agent-Context-Cleared"


def get_request_agent_context() -> Optional[Dict[str, str]]:
    context = _REQUEST_AGENT_CONTEXT.get()
    return dict(context) if context else None


def _hash_reference(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


def _agent_id_from_path(path: str) -> Optional[str]:
    parts = [part for part in path.split("/") if part]
    try:
        api_index = parts.index("api")
    except ValueError:
        return None
    expected_prefix = parts[api_index:api_index + 3]
    if expected_prefix != ["api", "v2", "agents"]:
        return None
    if len(parts) <= api_index + 3:
        return None
    agent_id = parts[api_index + 3]
    if agent_id in {"count", ""}:
        return None
    return agent_id


def _context_for_request(request: Request) -> Optional[Dict[str, str]]:
    agent_id = _agent_id_from_path(request.url.path)
    if not agent_id:
        return None
    return {
        "agent_ref": _hash_reference(agent_id),
        "source": "path",
    }


def sanitize_request_path(path: str) -> str:
    parts = [part for part in path.split("/") if part]
    try:
        api_index = parts.index("api")
    except ValueError:
        return path
    if parts[api_index:api_index + 3] != ["api", "v2", "agents"]:
        return path
    if len(parts) <= api_index + 3 or parts[api_index + 3] == "count":
        return path
    sanitized = list(parts)
    sanitized[api_index + 3] = "{agent_id}"
    return "/" + "/".join(sanitized)


class RequestContextMiddleware(BaseHTTPMiddleware):
    async def dispatch(
        self,
        request: Request,
        call_next: Callable,
    ) -> Response:
        context = _context_for_request(request)
        token = _REQUEST_AGENT_CONTEXT.set(context)
        safe_path = sanitize_request_path(request.url.path)
        status_code = 500
        try:
            response = await call_next(request)
            status_code = response.status_code
            response.headers[_AGENT_CONTEXT_HEADER] = "true"
            return response
        except Exception:
            logger.error(
                "%s %s %s request_context_cleared=true error=true",
                request.method,
                safe_path,
                status_code,
            )
            response = Response(
                status_code=500,
                content="Internal Server Error",
            )
            response.headers[_AGENT_CONTEXT_HEADER] = "true"
            return response
        finally:
            _REQUEST_AGENT_CONTEXT.reset(token)
            logger.info(
                "%s %s %s request_context_cleared=true",
                request.method,
                safe_path,
                status_code,
            )


class AuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(
        self,
        request: Request,
        call_next: Callable,
    ) -> Response:
        if (
            request.url.path.startswith("/api/v2")
            and request.url.path != "/api/v2/auth/token"
        ):
            token = request.headers.get("Authorization", "")
            if not token.startswith("Bearer "):
                return Response(status_code=401, content="Unauthorized")
        return await call_next(request)


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
        safe_path = sanitize_request_path(request.url.path)
        logger.info(
            "%s %s %s %.3fs",
            request.method,
            safe_path,
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
