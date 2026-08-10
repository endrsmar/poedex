"""The header-driven token bucket.

This is the piece of PoEDex that can hurt the user outside the app. GGG restricts,
and eventually revokes, API access for sustained rate-limit violations, and the
credential we hold is the user's own website session — a restriction lands on their
browser too. Everything below is biased towards refusing a request we could probably
have made, and never towards making one we probably should not have.

## What the server tells us

Every response carries, for one *policy*:

    X-Rate-Limit-Policy:        backend-item-request-limit
    X-Rate-Limit-Rules:         Account,Ip
    X-Rate-Limit-Account:       30:60:60,100:1800:600
    X-Rate-Limit-Account-State: 4:60:0,11:1800:0
    X-Rate-Limit-Ip:            45:60,180:1800
    X-Rate-Limit-Ip-State:      4:60:0,11:1800:0

Each entry is ``max_hits:period_seconds:restriction_seconds``; the ``-State`` header
is positionally aligned with its rule header and reports ``hits:period:active``.
The third field is optional — both two-field and three-field forms occur in the
wild, and the anonymous and authenticated policies for the *same* endpoint differ.
Nothing here is hardcoded (SPEC §4.4). Policy names, rule names and periods are all
learned from whatever the server sends.

## The five rules we implement

1. **Bucket by ``(policy, rule, period)``.** Not by endpoint. ``get-items`` and
   ``get-stash-items`` share ``backend-item-request-limit``, so they must share
   buckets or the pair together will bust a limit neither busts alone.
2. **Trust the server's ``-State`` count over local counting, upward only.** The
   same session is spending budget in the user's browser. When the server says we
   are at 11 and we counted 3, we are at 11. We never revise *down*, because the
   server's count can lag a request we have already sent.
3. **Keep a margin below the stated maximum** and **pad the period**, so clock skew
   and an in-flight browser request cannot push us over the real edge.
4. **Seed pessimistically.** Before any response has taught us the policy for a
   route, a deliberately tiny budget applies. Learning costs one request; guessing
   wrong costs a restriction.
5. **Refuse, never queue.** :meth:`RateLimiter.check` returns a verdict. There is no
   sleep in this file.
"""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field

from modules.net.backend.api import LimitSnapshot
from runtime.log import get_logger

__all__ = [
    "Bucket",
    "Decision",
    "LimitSpec",
    "RateLimiter",
    "StateSpec",
    "margin_for",
    "parse_rules",
    "parse_specs",
    "parse_states",
]

_log = get_logger("module.net.ratelimit")

Clock = Callable[[], float]

POLICY_HEADER = "x-rate-limit-policy"
RULES_HEADER = "x-rate-limit-rules"
RETRY_AFTER_HEADER = "retry-after"

# Padding added to every learned period, in seconds. Our clock and GGG's disagree,
# and a window that we think closed 200 ms ago may still be open on their side.
# SPEC §4.4 says 1 to 3 s.
DEFAULT_PERIOD_PAD = 2.0

# Until a route's policy is known we allow this much and no more. One request buys
# the real numbers; the rest of the seed budget exists only so a burst at startup
# cannot outrun the first response.
SEED_MAX_HITS = 1
SEED_PERIOD = 10

# 429 backoff. The Retry-After header is a *floor*, not the answer: GGG's restriction
# periods are per policy, and a second 429 while one is active means our model of the
# world is wrong, so we back off harder than the header asks.
BACKOFF_BASE = 5.0
BACKOFF_CAP = 900.0


def margin_for(max_hits: int) -> int:
    """How far below ``max_hits`` we stop. SPEC §4.4 asks for 2 or 3.

    Three for the larger buckets, two for the small ones — a margin of three out of
    a maximum of five would spend more than half the budget on headroom.
    """
    if max_hits >= 20:
        return 3
    if max_hits >= 6:
        return 2
    return 1


@dataclass(frozen=True, slots=True)
class LimitSpec:
    """One entry of an ``X-Rate-Limit-<Rule>`` header."""

    max_hits: int
    period: int
    restriction: int = 0


@dataclass(frozen=True, slots=True)
class StateSpec:
    """One entry of an ``X-Rate-Limit-<Rule>-State`` header."""

    hits: int
    period: int
    restriction: int = 0


def _parse_triples(raw: str) -> list[tuple[int, int, int]]:
    """``"30:60:60,100:1800"`` → ``[(30, 60, 60), (100, 1800, 0)]``.

    Tolerant on purpose. A header we cannot parse must not take the process down,
    and it must not silently become "no limit" either — the caller treats an empty
    result as "learned nothing", which leaves the seed budget in force.
    """
    out: list[tuple[int, int, int]] = []
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        parts = chunk.split(":")
        if len(parts) < 2:
            _log.warning("unparseable rate-limit entry %r", chunk)
            continue
        try:
            first = int(parts[0])
            period = int(parts[1])
            third = int(parts[2]) if len(parts) > 2 and parts[2] != "" else 0
        except ValueError:
            _log.warning("non-numeric rate-limit entry %r", chunk)
            continue
        if period <= 0:
            continue
        out.append((first, period, third))
    return out


def parse_specs(raw: str | None) -> list[LimitSpec]:
    if not raw:
        return []
    return [LimitSpec(a, b, c) for a, b, c in _parse_triples(raw)]


def parse_states(raw: str | None) -> list[StateSpec]:
    if not raw:
        return []
    return [StateSpec(a, b, c) for a, b, c in _parse_triples(raw)]


def parse_rules(raw: str | None) -> list[str]:
    """``"Account,Ip"`` → ``["Account", "Ip"]``. Rule names are learned, not assumed."""
    if not raw:
        return []
    return [part.strip() for part in raw.split(",") if part.strip()]


@dataclass
class Bucket:
    """A sliding window of our own request timestamps for one ``(rule, period)``."""

    policy: str
    rule: str
    period: int
    max_hits: int
    restriction: int = 0
    pad: float = DEFAULT_PERIOD_PAD
    margin: int | None = None
    learned: bool = True
    hits: deque[float] = field(default_factory=deque)

    @property
    def window(self) -> float:
        """The period we actually enforce: the server's, plus clock-skew padding."""
        return self.period + self.pad

    @property
    def effective_max(self) -> int:
        """The ceiling we enforce. Always at least 1, or nothing could ever be sent."""
        margin = margin_for(self.max_hits) if self.margin is None else self.margin
        return max(1, self.max_hits - margin)

    def prune(self, now: float) -> None:
        cutoff = now - self.window
        while self.hits and self.hits[0] <= cutoff:
            self.hits.popleft()

    def count(self, now: float) -> int:
        self.prune(now)
        return len(self.hits)

    def allows(self, now: float) -> bool:
        return self.count(now) < self.effective_max

    def retry_after(self, now: float) -> float:
        """Seconds until this bucket would admit one more request."""
        count = self.count(now)
        if count < self.effective_max:
            return 0.0
        # `count - effective_max + 1` of the oldest hits must age out; the last of
        # those is at index `count - effective_max`.
        index = count - self.effective_max
        return max(0.0, self.hits[index] + self.window - now)

    def record(self, now: float) -> None:
        self.prune(now)
        self.hits.append(now)

    def sync_from_state(self, now: float, observed: int) -> int:
        """Reconcile with the server's count. Returns how many hits were backfilled.

        Upward only, and the synthetic hits are stamped ``now`` so they expire last.
        Both choices are the pessimistic one: we would rather believe we have spent
        budget we have not than the reverse. The cost is bounded — the next response
        re-states the truth, and the count converges because we compare against the
        local total *including* previous backfill.
        """
        local = self.count(now)
        if observed <= local:
            return 0
        missing = observed - local
        for _ in range(missing):
            self.hits.append(now)
        return missing

    def update_limits(self, spec: LimitSpec) -> None:
        self.max_hits = spec.max_hits
        self.restriction = spec.restriction
        self.learned = True

    def snapshot(self, now: float) -> LimitSnapshot:
        return LimitSnapshot(
            policy=self.policy,
            rule=self.rule,
            period=self.period,
            max_hits=self.max_hits,
            effective_max=self.effective_max,
            hits=self.count(now),
            retry_after=self.retry_after(now),
            learned=self.learned,
        )


@dataclass
class Policy:
    """Everything learned about one named policy."""

    name: str
    buckets: dict[tuple[str, int], Bucket] = field(default_factory=dict)
    blocked_until: float = 0.0
    blocked_reason: str = ""
    consecutive_429: int = 0

    def bucket(
        self, rule: str, spec: LimitSpec, *, pad: float, margin: int | None
    ) -> Bucket:
        key = (rule, spec.period)
        existing = self.buckets.get(key)
        if existing is None:
            existing = Bucket(
                policy=self.name,
                rule=rule,
                period=spec.period,
                max_hits=spec.max_hits,
                restriction=spec.restriction,
                pad=pad,
                margin=margin,
            )
            self.buckets[key] = existing
        else:
            existing.update_limits(spec)
        return existing


@dataclass(frozen=True, slots=True)
class Decision:
    """The verdict for one prospective request. Never a promise to wait.

    Deliberately *not* truthy-by-``allowed``. An earlier version defined
    ``__bool__`` and ``worst or ALLOWED`` then silently turned every refusal into
    an approval, because a refusing Decision is falsey. Callers read ``.allowed``.
    """

    allowed: bool
    retry_after: float = 0.0
    reason: str = ""
    policy: str | None = None
    rule: str | None = None
    period: int | None = None


ALLOWED = Decision(allowed=True)


class RateLimiter:
    """Learns GGG's policies from response headers and enforces them locally.

    Not thread-safe and not asyncio-locked; :class:`~modules.net.backend.client.
    NetClient` serialises access with a lock, which is also what makes
    "check then record then send" atomic.
    """

    def __init__(
        self,
        *,
        clock: Clock | None = None,
        pad: float = DEFAULT_PERIOD_PAD,
        margin: int | None = None,
        seed_max_hits: int = SEED_MAX_HITS,
        seed_period: int = SEED_PERIOD,
    ) -> None:
        self._clock = clock or time.monotonic
        self.pad = pad
        self.margin = margin
        self._seed_max_hits = seed_max_hits
        self._seed_period = seed_period
        self.policies: dict[str, Policy] = {}
        self.routes: dict[str, str] = {}
        self._seeds: dict[str, Bucket] = {}
        # A route can be blocked before its policy is known — a 429 answered by an
        # edge, say, with no X-Rate-Limit-Policy header on it. Without somewhere to
        # record that, a `Retry-After: 300` would only hold us back for the length of
        # the seed window.
        self._route_blocks: dict[str, tuple[float, str]] = {}

    # -- introspection ---------------------------------------------------------

    def now(self) -> float:
        return self._clock()

    def policy_for(self, route: str) -> Policy | None:
        name = self.routes.get(route)
        return self.policies.get(name) if name else None

    def seed_bucket(self, route: str) -> Bucket:
        bucket = self._seeds.get(route)
        if bucket is None:
            bucket = Bucket(
                policy=f"seed:{route}",
                rule="seed",
                period=self._seed_period,
                max_hits=self._seed_max_hits,
                pad=self.pad,
                margin=0,
                learned=False,
            )
            self._seeds[route] = bucket
        return bucket

    def snapshots(self) -> list[LimitSnapshot]:
        now = self.now()
        out: list[LimitSnapshot] = []
        for policy in self.policies.values():
            blocked = max(0.0, policy.blocked_until - now)
            for bucket in policy.buckets.values():
                snap = bucket.snapshot(now)
                if blocked:
                    snap = LimitSnapshot(
                        policy=snap.policy,
                        rule=snap.rule,
                        period=snap.period,
                        max_hits=snap.max_hits,
                        effective_max=snap.effective_max,
                        hits=snap.hits,
                        retry_after=max(snap.retry_after, blocked),
                        restricted_for=blocked,
                        learned=snap.learned,
                    )
                out.append(snap)
        for route, bucket in self._seeds.items():
            if route in self.routes:
                continue  # superseded by a learned policy
            out.append(bucket.snapshot(now))
        out.sort(key=lambda s: (s.policy, s.rule, s.period))
        return out

    # -- the verdict -----------------------------------------------------------

    def check(self, route: str) -> Decision:
        """May we send a request on ``route`` right now?"""
        now = self.now()

        blocked_until, blocked_reason = self._route_blocks.get(route, (0.0, ""))
        if blocked_until > now:
            return Decision(
                allowed=False,
                retry_after=blocked_until - now,
                reason=blocked_reason,
                policy=self.routes.get(route),
            )

        policy = self.policy_for(route)
        if policy is None:
            bucket = self.seed_bucket(route)
            if bucket.allows(now):
                return ALLOWED
            return Decision(
                allowed=False,
                retry_after=bucket.retry_after(now),
                reason="policy not learned yet; seed budget in force",
            )

        if policy.blocked_until > now:
            return Decision(
                allowed=False,
                retry_after=policy.blocked_until - now,
                reason=policy.blocked_reason or "backing off",
                policy=policy.name,
            )

        worst: Decision | None = None
        for bucket in policy.buckets.values():
            if bucket.allows(now):
                continue
            candidate = Decision(
                allowed=False,
                retry_after=bucket.retry_after(now),
                reason=(
                    f"{bucket.count(now)}/{bucket.effective_max} used "
                    f"(server max {bucket.max_hits})"
                ),
                policy=bucket.policy,
                rule=bucket.rule,
                period=bucket.period,
            )
            if worst is None or candidate.retry_after > worst.retry_after:
                worst = candidate
        return ALLOWED if worst is None else worst

    def retry_after(self, route: str) -> float:
        return self.check(route).retry_after

    # -- accounting ------------------------------------------------------------

    def record_request(self, route: str) -> None:
        """Count a request as spent *before* it is sent.

        Optimistic accounting in the safe direction: a request that fails in transit
        may still have reached GGG's counter, so it stays counted.
        """
        now = self.now()
        policy = self.policy_for(route)
        if policy is None:
            self.seed_bucket(route).record(now)
            return
        for bucket in policy.buckets.values():
            bucket.record(now)

    def observe(self, route: str, status: int, headers: Mapping[str, str]) -> float:
        """Learn from a response. Returns the seconds this route is now blocked for.

        ``headers`` is read case-insensitively, because httpx hands us a
        case-insensitive mapping and tests hand us a plain dict.
        """
        now = self.now()
        get = _header_reader(headers)

        policy_name = (get(POLICY_HEADER) or "").strip()
        policy: Policy | None = None
        if policy_name:
            policy = self.policies.get(policy_name)
            if policy is None:
                policy = Policy(name=policy_name)
                self.policies[policy_name] = policy
                _log.info("learned rate-limit policy %s", policy_name)
            previous = self.routes.get(route)
            if previous != policy_name:
                if previous:
                    _log.info(
                        "route %s moved from policy %s to %s", route, previous, policy_name
                    )
                self.routes[route] = policy_name
                # The learned buckets take over; the seed has done its job.
                self._seeds.pop(route, None)
            self._learn_buckets(policy, get, now)
        else:
            policy = self.policy_for(route)

        if status == 429 or status >= 500:
            return self._back_off(policy, route, status, get, now)
        if policy is not None and 200 <= status < 400:
            policy.consecutive_429 = 0
        return max(0.0, policy.blocked_until - now) if policy else 0.0

    def _learn_buckets(self, policy: Policy, get: Callable[[str], str | None], now: float) -> None:
        rules = parse_rules(get(RULES_HEADER))
        if not rules:
            # No Rules header: fall back to whatever rule names we already know, so a
            # partial response cannot erase the model we built from a complete one.
            rules = sorted({rule for rule, _ in policy.buckets})
        for rule in rules:
            specs = parse_specs(get(f"x-rate-limit-{rule.lower()}"))
            states = parse_states(get(f"x-rate-limit-{rule.lower()}-state"))
            if not specs:
                continue
            by_period = {state.period: state for state in states}
            for index, spec in enumerate(specs):
                bucket = policy.bucket(rule, spec, pad=self.pad, margin=self.margin)
                # The `-State` header is positionally aligned with its rule header,
                # but matching on the period is safer than trusting the order.
                state = by_period.get(spec.period)
                if state is None and index < len(states):
                    state = states[index]
                if state is None:
                    continue
                added = bucket.sync_from_state(now, state.hits)
                if added:
                    _log.debug(
                        "backfilled %d hit(s) into %s/%s/%ds from server state",
                        added,
                        policy.name,
                        rule,
                        spec.period,
                    )
                if state.restriction > 0:
                    self._block(
                        policy,
                        now + state.restriction,
                        f"server reports an active {state.restriction}s restriction on "
                        f"{rule}/{spec.period}s",
                    )

    def _back_off(
        self,
        policy: Policy | None,
        route: str,
        status: int,
        get: Callable[[str], str | None],
        now: float,
    ) -> float:
        retry_after = _parse_retry_after(get(RETRY_AFTER_HEADER))
        if policy is None:
            # No policy learned for this route, so there is no bucket to hold the
            # block. Record it against the route itself, and fill the seed bucket
            # too so the state is consistent from either direction.
            wait = max(retry_after, BACKOFF_BASE)
            self._block_route(route, now + wait, f"HTTP {status} before any policy was learned")
            bucket = self.seed_bucket(route)
            for _ in range(bucket.effective_max):
                bucket.record(now)
            return wait

        if status == 429:
            policy.consecutive_429 += 1
            attempt = policy.consecutive_429
            backoff = min(BACKOFF_CAP, BACKOFF_BASE * (2 ** (attempt - 1)))
            wait = max(retry_after, backoff)
            reason = (
                f"HTTP 429 (#{attempt} in a row); "
                f"Retry-After {retry_after:.0f}s, backoff {backoff:.0f}s"
            )
            _log.warning("rate limited by the server on %s: %s", route, reason)
        else:
            wait = max(retry_after, BACKOFF_BASE)
            reason = f"HTTP {status}; pausing {wait:.0f}s"
        self._block(policy, now + wait, reason)
        return wait

    def _block(self, policy: Policy, until: float, reason: str) -> None:
        if until > policy.blocked_until:
            policy.blocked_until = until
            policy.blocked_reason = reason

    def _block_route(self, route: str, until: float, reason: str) -> None:
        current, _ = self._route_blocks.get(route, (0.0, ""))
        if until > current:
            self._route_blocks[route] = (until, reason)


def _header_reader(headers: Mapping[str, str]) -> Callable[[str], str | None]:
    """Case-insensitive lookup over any mapping, without copying httpx's own."""
    try:
        lowered = {str(k).lower(): v for k, v in headers.items()}
    except AttributeError:  # pragma: no cover - defensive
        lowered = {}

    def get(name: str) -> str | None:
        return lowered.get(name.lower())

    return get


def _parse_retry_after(raw: str | None) -> float:
    """``Retry-After`` in seconds. GGG sends an integer; a date form is ignored."""
    if not raw:
        return 0.0
    try:
        return max(0.0, float(raw.strip()))
    except ValueError:
        _log.warning("unparseable Retry-After header")
        return 0.0
