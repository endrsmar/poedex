"""The public surface of the `net` module.

**This is the only file in this module that other modules may import** (plan §1.4,
enforced by ``tests/test_boundaries.py``). Protocols, plain data and errors only.

The shape of the contract is deliberate in one respect: there is no ``wait``
parameter and no queue. When the limiter says a request would breach the budget,
:meth:`NetApi.get` raises :class:`RateLimited` carrying the number of seconds to
wait. SPEC §4.4 — *refuse rather than silently queue*. A hidden queue turns "the
tool is slow" into "the tool got my account restricted an hour after I stopped
looking at it", and it denies the UI the one thing it can honestly show: *not yet,
N seconds*.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from runtime.errors import PoedexError

__all__ = [
    "AuthRejected",
    "HttpStatusError",
    "LimitSnapshot",
    "NetApi",
    "NetError",
    "NetworkError",
    "RateLimited",
    "Response",
    "describe_limits",
]


class NetError(PoedexError):
    """Base for every failure the `net` module reports.

    Messages are built from status codes, paths and durations. The credential is
    never one of the ingredients, and response bodies are truncated and passed
    through :func:`runtime.secrets.redact` before they reach a message.
    """


class NetworkError(NetError):
    """The request never got an HTTP answer: DNS, TLS, connect or read timeout."""


class RateLimited(NetError):
    """The request was **refused** to protect the budget, or the server returned 429.

    ``retry_after`` is the number of seconds after which the request is expected to
    be allowed. It is advisory — the limiter re-checks at the next attempt.
    """

    def __init__(
        self,
        retry_after: float,
        *,
        reason: str = "",
        policy: str | None = None,
        rule: str | None = None,
        period: int | None = None,
        server_rejected: bool = False,
    ) -> None:
        self.retry_after = max(0.0, float(retry_after))
        self.reason = reason
        self.policy = policy
        self.rule = rule
        self.period = period
        self.server_rejected = server_rejected
        """``True`` when the server itself answered 429; ``False`` when we refused
        locally before sending. The difference matters: the first is a violation
        already on record with GGG, the second is the limiter doing its job."""
        where = f"{policy}/{rule}/{period}s" if policy else "seed budget"
        detail = f" ({reason})" if reason else ""
        super().__init__(
            f"rate limited by {where}: retry after {self.retry_after:.1f}s{detail}"
        )


class HttpStatusError(NetError):
    """A 4xx or 5xx that is not a 429."""

    def __init__(self, status: int, path: str, body_excerpt: str = "") -> None:
        self.status = status
        self.path = path
        self.body_excerpt = body_excerpt
        suffix = f": {body_excerpt}" if body_excerpt else ""
        super().__init__(f"HTTP {status} from {path}{suffix}")


class AuthRejected(HttpStatusError):
    """401, 403, or a redirect to the login page. The session is dead or revoked.

    A distinct type because exactly one caller reaction is correct — telling
    `credentials` to mark the session rejected so the UI can say so — and it must
    not be reachable by string-matching an error message.
    """


@dataclass(frozen=True, slots=True)
class Response:
    """What a successful request produced.

    ``headers`` has ``set-cookie`` removed: a response that rotates the session
    would otherwise put a live credential into a value that callers treat as inert
    metadata.
    """

    status: int
    path: str
    headers: Mapping[str, str] = field(default_factory=dict)
    content: bytes = b""
    elapsed: float = 0.0

    @property
    def text(self) -> str:
        return self.content.decode("utf-8", errors="replace")

    def json(self) -> Any:
        import json as _json

        return _json.loads(self.content or b"null")


@dataclass(frozen=True, slots=True)
class LimitSnapshot:
    """One learned bucket, for display and for tests.

    ``hits``/``effective_max`` are what the limiter enforces; ``max_hits`` is what
    the server said. The gap between them is the safety margin (SPEC §4.4) — the
    same POESESSID is also the user's live browser session, and the browser does not
    consult us before spending budget.
    """

    policy: str
    rule: str
    period: int
    max_hits: int
    effective_max: int
    hits: int
    retry_after: float
    restricted_for: float = 0.0
    learned: bool = True

    def to_json(self) -> dict[str, Any]:
        return {
            "policy": self.policy,
            "rule": self.rule,
            "period": self.period,
            "max_hits": self.max_hits,
            "effective_max": self.effective_max,
            "hits": self.hits,
            "retry_after": round(self.retry_after, 2),
            "restricted_for": round(self.restricted_for, 2),
            "learned": self.learned,
        }


@runtime_checkable
class NetApi(Protocol):
    """What dependents get from ``ctx.require(NetApi)``."""

    @property
    def user_agent(self) -> str:
        """The exact ``User-Agent`` sent. GGG requires contact details in it."""
        ...

    async def get(
        self,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        authenticated: bool = True,
        route: str | None = None,
        timeout: float | None = None,
    ) -> Response:
        """Perform a GET, or raise :class:`RateLimited` rather than queue.

        ``route`` names the rate-limit route this request belongs to; it defaults to
        ``path``. Two paths that the server governs with the same policy converge on
        the same buckets automatically once the policy name has been learned, so the
        only thing ``route`` does is decide which seed budget applies *before* the
        first response teaches us anything.
        """
        ...

    async def get_json(
        self,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        authenticated: bool = True,
        route: str | None = None,
        timeout: float | None = None,
    ) -> Any:
        """:meth:`get`, decoded. Raises :class:`NetError` if the body is not JSON."""
        ...

    def limits(self) -> list[LimitSnapshot]:
        """Every bucket the limiter currently knows about."""
        ...

    def retry_after(self, route: str) -> float:
        """Seconds until ``route`` would be allowed. ``0.0`` means "go now"."""
        ...


def describe_limits(snapshots: Iterable[LimitSnapshot]) -> str:
    """One line per bucket, for a CLI or a diagnostics panel.

    Lives in ``api.py`` rather than beside the limiter because the only callers are
    outside the module, and a surface may import nothing else from here.
    """
    lines = []
    for snap in snapshots:
        seed = "" if snap.learned else "  (seed - policy not learned yet)"
        wait = f"  wait {snap.retry_after:.0f}s" if snap.retry_after else ""
        lines.append(
            f"{snap.policy:<34} {snap.rule:<8} {snap.period:>6}s  "
            f"{snap.hits:>4}/{snap.effective_max:<4} of {snap.max_hits}{wait}{seed}"
        )
    return "\n".join(lines)
