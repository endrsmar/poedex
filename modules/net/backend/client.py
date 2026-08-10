"""The httpx client. One instance, one limiter, one place the credential is used.

Two things in here are load-bearing beyond "it makes requests":

* **Redaction is installed before httpx exists.** ``runtime.log.install_redaction``
  attaches its filter to the handlers that are present when it runs, so a handler
  created later — by ``decky.logger``, which reconfigures the root logger with
  ``force=True``, or by httpx's own logging — is not covered. Phase 1 flagged this.
  :func:`prepare_logging` is called from ``NetModule.start`` *before* the client is
  constructed, and it silences the httpx and httpcore loggers outright, because
  their DEBUG records contain full request URLs.
* **The credential goes in exactly one place**: the ``Cookie`` request header, built
  per request and never stored on the client. It is never a query parameter (query
  strings reach access logs and referrers) and never part of an error message.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Mapping
from typing import Any

import httpx

from modules.credentials.backend.api import CredentialsApi
from modules.net.backend.api import (
    AuthRejected,
    HttpStatusError,
    LimitSnapshot,
    NetError,
    NetworkError,
    RateLimited,
    Response,
)
from modules.net.backend.ratelimit import (
    DEFAULT_COURTESY_MAX_HITS,
    DEFAULT_COURTESY_PERIOD,
    Decision,
    RateLimiter,
)
from runtime.log import get_logger, install_redaction, silence_noisy_loggers
from runtime.secrets import redact, register_secret

__all__ = ["NetClient", "build_user_agent", "prepare_logging"]

_log = get_logger("module.net.client")

DEFAULT_BASE_URL = "https://www.pathofexile.com"
DEFAULT_TIMEOUT = 20.0
PROJECT_URL = "https://github.com/endrsmar/poedex"

# Enough of a body to diagnose a 4xx, little enough that a surprise payload cannot
# turn a log line into a data dump. Redacted before it reaches an exception anyway.
BODY_EXCERPT_LIMIT = 200


def prepare_logging() -> None:
    """Cover every existing handler, then silence the HTTP libraries.

    Order matters and is the whole point of the function: install the redacting
    filter first so anything already configured is scrubbed, then turn the URL-
    logging libraries down to WARNING so the common case never produces a record
    that needs scrubbing in the first place. Defence in depth, cheap.
    """
    install_redaction()
    silence_noisy_loggers()
    # Named explicitly as well as via silence_noisy_loggers(): this is the leak
    # CLAUDE.md calls out by name, and it should not stop working if that helper is
    # ever refactored.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def build_user_agent(version: str, contact: str | None) -> str:
    """GGG requires a ``User-Agent`` with contact details on every call (SPEC §4.2).

    The project URL is always present; ``contact`` adds an email when the user has
    supplied one. A tool that cannot be contacted is a tool GGG blocks without
    asking, so this is not decoration.
    """
    parts = [f"+{PROJECT_URL}"]
    if contact and contact.strip():
        parts.append(f"contact: {contact.strip()}")
    return f"PoEDex/{version} ({'; '.join(parts)})"


def _login_redirect(response: httpx.Response) -> bool:
    """A 30x to the login page is a dead session wearing a different status code."""
    if response.status_code not in (301, 302, 303, 307, 308):
        return False
    location = response.headers.get("location", "")
    return "/login" in location or "/auth/" in location


class NetClient:
    """Serialises every request through one limiter and one httpx client."""

    def __init__(
        self,
        *,
        limiter: RateLimiter,
        user_agent: str,
        credentials: CredentialsApi | None = None,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = DEFAULT_TIMEOUT,
        transport: httpx.AsyncBaseTransport | None = None,
        foreign_max_hits: int = DEFAULT_COURTESY_MAX_HITS,
        foreign_period: int = DEFAULT_COURTESY_PERIOD,
    ) -> None:
        self._limiter = limiter
        self._user_agent = user_agent
        self._credentials = credentials
        self._base_url = base_url.rstrip("/")
        self._host = httpx.URL(self._base_url).host
        self._timeout = timeout
        self._foreign_max_hits = foreign_max_hits
        self._foreign_period = foreign_period
        # A single connection is plenty: the limiter refuses concurrency long before
        # the pool would, and one connection keeps our footprint honest.
        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            timeout=timeout,
            follow_redirects=False,
            limits=httpx.Limits(max_connections=2, max_keepalive_connections=2),
            headers={
                "User-Agent": user_agent,
                "Accept": "application/json",
                "Accept-Encoding": "gzip, deflate",
            },
            transport=transport,
        )
        # The check/record/send sequence must be atomic or two coroutines both pass a
        # check that only one of them should have.
        self._lock = asyncio.Lock()

    # -- NetApi ----------------------------------------------------------------

    @property
    def user_agent(self) -> str:
        return self._user_agent

    @property
    def base_url(self) -> str:
        return self._base_url

    def limits(self) -> list[LimitSnapshot]:
        return self._limiter.snapshots()

    def retry_after(self, route: str) -> float:
        return self._limiter.retry_after(route)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        json: Any = None,
        headers: Mapping[str, str] | None = None,
        authenticated: bool = True,
        route: str | None = None,
        timeout: float | None = None,
    ) -> Response:
        route = route or path
        foreign_host = self._foreign_host(path)
        request_headers = {str(k): str(v) for k, v in (headers or {}).items()}
        if authenticated and foreign_host is None:
            cookie = await self._cookie()
            if cookie is None:
                raise AuthRejected(401, path, "no credential stored")
            request_headers["Cookie"] = cookie
        elif authenticated:
            # Not an error, and deliberately not silent either: a caller that asked
            # for authentication against another host has a bug, and the useful
            # outcome is the request going out *without* the account credential.
            _log.warning(
                "dropping the session credential: %s is not %s", foreign_host, self._host
            )
        if foreign_host is not None:
            # Its own policy, keyed by hostname. Nothing this host does can move a
            # GGG bucket, and nothing GGG does can hold this host back.
            self._limiter.declare_budget(
                f"host:{foreign_host}",
                [route],
                max_hits=self._foreign_max_hits,
                period=self._foreign_period,
            )

        async with self._lock:
            decision = self._limiter.check(route)
            if not decision.allowed:
                raise RateLimited(
                    decision.retry_after,
                    reason=decision.reason,
                    policy=decision.policy,
                    rule=decision.rule,
                    period=decision.period,
                )
            self._limiter.record_request(route)
            started = time.monotonic()
            try:
                raw = await self._client.request(
                    method.upper(),
                    path,
                    params=dict(params) if params else None,
                    json=json,
                    headers=request_headers,
                    timeout=timeout if timeout is not None else self._timeout,
                )
            except httpx.HTTPError as exc:
                # The request is left counted: it may well have reached the server.
                raise NetworkError(f"{path}: {type(exc).__name__}: {redact(str(exc))}") from None
            elapsed = time.monotonic() - started
            self._limiter.observe(route, raw.status_code, raw.headers)
            # Ask the limiter what it would now say, rather than reading one bucket:
            # after a 429 the block may live on the policy or on the route.
            after = self._limiter.check(route)

        _log.info("%s %s -> %s in %.0f ms", method.upper(), path, raw.status_code, elapsed * 1000)
        return self._interpret(path, route, raw, elapsed, after)

    async def get(
        self,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
        authenticated: bool = True,
        route: str | None = None,
        timeout: float | None = None,
    ) -> Response:
        return await self.request(
            "GET",
            path,
            params=params,
            headers=headers,
            authenticated=authenticated,
            route=route,
            timeout=timeout,
        )

    async def get_json(
        self,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        authenticated: bool = True,
        route: str | None = None,
        timeout: float | None = None,
    ) -> Any:
        response = await self.get(
            path,
            params=params,
            authenticated=authenticated,
            route=route,
            timeout=timeout,
        )
        return self._decode(response)

    async def post_json(
        self,
        path: str,
        *,
        json: Any = None,
        params: Mapping[str, Any] | None = None,
        authenticated: bool = True,
        route: str | None = None,
        timeout: float | None = None,
    ) -> Any:
        response = await self.request(
            "POST",
            path,
            params=params,
            json=json,
            authenticated=authenticated,
            route=route,
            timeout=timeout,
        )
        return self._decode(response)

    # -- internals -------------------------------------------------------------

    @staticmethod
    def _decode(response: Response) -> Any:
        try:
            return response.json()
        except ValueError:
            raise NetError(
                f"{response.path}: expected JSON, got "
                f"{response.headers.get('content-type', 'nothing')}"
            ) from None

    def _foreign_host(self, path: str) -> str | None:
        """The hostname, when ``path`` points somewhere other than ``base_url``.

        ``None`` means "this is the PoE API host", which is the only host allowed to
        see the credential.

        Matched on the scheme prefix rather than on a substring: a *relative* path
        whose query string happens to contain ``://`` must not be mistaken for an
        absolute URL, because the consequence of that mistake is silently dropping
        the credential from a request that needed it.
        """
        if not path.startswith(("http://", "https://")):
            return None
        host = httpx.URL(path).host
        return None if host == self._host else host

    async def _cookie(self) -> str | None:
        if self._credentials is None:
            return None
        value = await self._credentials.session_id()
        if not value:
            return None
        # Belt and braces: `credentials` registers it when it loads the record, but a
        # value that reaches us by any other route must still never survive a log.
        register_secret(value)
        return f"POESESSID={value}"

    def _interpret(
        self,
        path: str,
        route: str,
        raw: httpx.Response,
        elapsed: float,
        after: Decision,
    ) -> Response:
        status = raw.status_code
        if status == 429:
            raise RateLimited(
                after.retry_after,
                reason=after.reason or "server returned 429",
                policy=self._limiter.routes.get(route),
                server_rejected=True,
            )
        if status in (401, 403) or _login_redirect(raw):
            reported = status if status in (401, 403) else 401
            raise AuthRejected(reported, path, "session rejected")
        if status >= 400:
            raise HttpStatusError(status, path, self._excerpt(raw))
        if status == 304:
            # "Your copy is current." A success with no body, and the only reason
            # conditional requests are worth making.
            return Response(
                status=304,
                path=path,
                headers={
                    key: value
                    for key, value in raw.headers.items()
                    if key.lower() != "set-cookie"
                },
                elapsed=elapsed,
            )
        if 300 <= status < 400:
            raise HttpStatusError(
                status, path, f"unexpected redirect to {raw.headers.get('location', '?')}"
            )

        headers = {
            key: value for key, value in raw.headers.items() if key.lower() != "set-cookie"
        }
        return Response(
            status=status,
            path=path,
            headers=headers,
            content=raw.content,
            elapsed=elapsed,
        )

    @staticmethod
    def _excerpt(raw: httpx.Response) -> str:
        try:
            body = raw.text
        except Exception:  # pragma: no cover - undecodable body
            return ""
        return redact(" ".join(body.split())[:BODY_EXCERPT_LIMIT])
