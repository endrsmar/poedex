"""The `net` core module.

Every HTTP request PoEDex makes goes through here, because one limiter has to see
all of them (IMPLEMENTATION-PLAN §4). A module that opens its own socket can get the
user's account restricted, which is the one failure that hurts them outside the app.

`net` is core: it holds no opinion about *what* to fetch. It knows how to send a
request politely, how much budget is left, and how to say no.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any, ClassVar

from modules.credentials.backend.api import CredentialsApi
from modules.net.backend.api import LimitSnapshot, NetApi, Response
from modules.net.backend.client import (
    DEFAULT_BASE_URL,
    DEFAULT_TIMEOUT,
    NetClient,
    build_user_agent,
    prepare_logging,
)
from modules.net.backend.ratelimit import DEFAULT_PERIOD_PAD, RateLimiter
from runtime.context import ModuleContext
from runtime.errors import ModuleNotStartedError

VERSION = "0.1.0"


class NetModule:
    id = "net"
    name = "Network"
    kind = "core"
    requires: ClassVar[list[str]] = ["credentials"]
    provides: type | None = NetApi

    def __init__(self, client: NetClient | None = None) -> None:
        # An injectable client keeps the tests off the network entirely.
        self._client = client
        self._owns_client = client is None
        self._ctx: ModuleContext | None = None

    # -- lifecycle -------------------------------------------------------------

    async def start(self, ctx: ModuleContext) -> None:
        self._ctx = ctx
        # Before httpx exists. See client.prepare_logging.
        prepare_logging()
        if self._client is None:
            credentials = ctx.require(CredentialsApi)
            margin = int(ctx.settings.get("rate_limit_margin"))
            limiter = RateLimiter(
                pad=float(ctx.settings.get("period_pad_seconds")),
                margin=None if margin <= 0 else margin,
            )
            self._client = NetClient(
                limiter=limiter,
                user_agent=build_user_agent(VERSION, str(ctx.settings.get("contact"))),
                credentials=credentials,
                base_url=str(ctx.settings.get("base_url")),
                timeout=float(ctx.settings.get("timeout_seconds")),
            )
            self._owns_client = True
            if not str(ctx.settings.get("contact")).strip():
                ctx.logger.warning(
                    "no contact address configured; GGG asks for one in the User-Agent. "
                    "Set it with the net.contact setting."
                )
        ctx.logger.info("net ready: %s", self._client.user_agent)

    async def stop(self) -> None:
        if self._client is not None and self._owns_client:
            await self._client.aclose()
        self._ctx = None

    def methods(self) -> dict[str, Callable[..., Any]]:
        # `get` is deliberately absent. The method registry is what a transport
        # exposes to the frontend, and an arbitrary-URL fetch reachable from the CEF
        # console is a credential-bearing proxy for whoever can reach it.
        return {"limits": self.limits_json}

    def settings_schema(self) -> dict[str, Any]:
        return {
            "base_url": {
                "type": "str",
                "default": DEFAULT_BASE_URL,
                "label": "API host",
            },
            "contact": {
                "type": "str",
                "default": "",
                "label": "Contact address",
                "description": (
                    "Included in the User-Agent. GGG requires contact details on "
                    "every call and blocks tools it cannot reach."
                ),
            },
            "timeout_seconds": {
                "type": "float",
                "default": DEFAULT_TIMEOUT,
                "min": 1.0,
                "max": 120.0,
                "label": "Request timeout",
            },
            "rate_limit_margin": {
                "type": "int",
                "default": 0,
                "min": 0,
                "max": 20,
                "label": "Rate-limit margin",
                "description": (
                    "How many requests below the server's stated maximum to stop. "
                    "0 picks 2 or 3 automatically by bucket size. Raising it is "
                    "always safe; lowering it spends the headroom that the user's "
                    "own browser session needs."
                ),
            },
            "period_pad_seconds": {
                "type": "float",
                "default": DEFAULT_PERIOD_PAD,
                "min": 0.0,
                "max": 10.0,
                "label": "Period padding",
                "description": "Clock-skew allowance added to every learned period.",
            },
        }

    # -- NetApi ----------------------------------------------------------------

    @property
    def user_agent(self) -> str:
        return self._require_client().user_agent

    async def get(
        self,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        authenticated: bool = True,
        route: str | None = None,
        timeout: float | None = None,
    ) -> Response:
        return await self._require_client().get(
            path,
            params=params,
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
        return await self._require_client().get_json(
            path,
            params=params,
            authenticated=authenticated,
            route=route,
            timeout=timeout,
        )

    def limits(self) -> list[LimitSnapshot]:
        return self._require_client().limits()

    def retry_after(self, route: str) -> float:
        return self._require_client().retry_after(route)

    async def limits_json(self) -> list[dict[str, Any]]:
        return [snapshot.to_json() for snapshot in self.limits()]

    # -- internals -------------------------------------------------------------

    def _require_client(self) -> NetClient:
        if self._client is None:
            raise ModuleNotStartedError("net has not been started")
        return self._client

    def __repr__(self) -> str:
        return f"NetModule(started={self._client is not None})"


MODULE = NetModule()
