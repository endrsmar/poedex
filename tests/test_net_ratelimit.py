"""The rate limiter: header parsing, buckets, backfill, backoff, refusal.

This is the part of PoEDex that can get the user's account restricted, so the tests
are written to catch the limiter being *too permissive*. Where a behaviour has two
plausible readings, the test pins the conservative one.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from modules.net.backend.api import LimitSnapshot
from modules.net.backend.ratelimit import (
    BACKOFF_BASE,
    DEFAULT_PERIOD_PAD,
    SEED_MAX_HITS,
    Bucket,
    RateLimiter,
    margin_for,
    parse_rules,
    parse_specs,
    parse_states,
)
from tests.conftest import FIXTURES, FakeClock, headers

ROUTE = "character-window:items"


@pytest.fixture
def limiter(clock: FakeClock) -> RateLimiter:
    return RateLimiter(clock=clock)


# -- header parsing ------------------------------------------------------------


def test_parses_the_three_field_shape():
    specs = parse_specs("30:60:60,100:1800:600")
    assert [(s.max_hits, s.period, s.restriction) for s in specs] == [
        (30, 60, 60),
        (100, 1800, 600),
    ]


def test_parses_the_two_field_shape():
    """`get-characters` sends `10:60` with no restriction field."""
    specs = parse_specs("10:60,50:1800")
    assert [(s.max_hits, s.period, s.restriction) for s in specs] == [(10, 60, 0), (50, 1800, 0)]


def test_parses_state_headers():
    states = parse_states("4:60:0,11:1800:600")
    assert [(s.hits, s.period, s.restriction) for s in states] == [(4, 60, 0), (11, 1800, 600)]


def test_parses_rule_names():
    assert parse_rules("Account,Ip") == ["Account", "Ip"]
    assert parse_rules(" Account , Ip ") == ["Account", "Ip"]
    assert parse_rules(None) == []


@pytest.mark.parametrize("raw", ["", None, "garbage", "x:y:z", "5", ":::"])
def test_unparseable_headers_yield_nothing_rather_than_no_limit(raw):
    """A header we cannot read must leave the model empty, never wide open."""
    assert parse_specs(raw) == []
    assert parse_states(raw) == []


def test_zero_period_entries_are_dropped():
    assert parse_specs("10:0:0,5:60:0") == parse_specs("5:60:0")


# -- policies are learned, never assumed ---------------------------------------


def test_learns_the_authenticated_item_policy_from_headers(limiter: RateLimiter):
    limiter.observe(ROUTE, 200, headers("headers-items-authenticated.json"))
    learned = {(s.policy, s.rule, s.period, s.max_hits) for s in limiter.snapshots()}
    assert learned == {
        ("backend-item-request-limit", "Account", 60, 30),
        ("backend-item-request-limit", "Account", 1800, 100),
        ("backend-item-request-limit", "Ip", 60, 45),
        ("backend-item-request-limit", "Ip", 1800, 180),
    }


def test_learns_the_two_field_character_policy(limiter: RateLimiter):
    limiter.observe("chars", 200, headers("headers-characters-authenticated.json"))
    learned = {(s.rule, s.period, s.max_hits) for s in limiter.snapshots()}
    assert learned == {
        ("Account", 60, 10),
        ("Account", 1800, 50),
        ("Ip", 60, 30),
        ("Ip", 1800, 120),
    }


def test_anonymous_and_authenticated_differ_on_the_same_endpoint(clock: FakeClock):
    """research-notes §3: the strongest argument against hardcoding.

    Two limiters, the same policy *name*, entirely different numbers and rules. A
    limiter that had the authenticated values baked in would sail past the
    anonymous 6-in-4-seconds bucket.
    """
    authed = RateLimiter(clock=clock)
    anon = RateLimiter(clock=clock)
    authed.observe(ROUTE, 200, headers("headers-items-authenticated.json"))
    anon.observe(ROUTE, 200, headers("headers-items-anonymous.json"))

    assert {s.rule for s in authed.snapshots()} == {"Account", "Ip"}
    assert {s.rule for s in anon.snapshots()} == {"Ip"}
    assert {s.period for s in anon.snapshots()} == {4, 120, 3600}
    assert authed.snapshots() != anon.snapshots()


def test_a_response_without_a_policy_header_does_not_erase_what_we_learned(limiter):
    limiter.observe(ROUTE, 200, headers("headers-items-authenticated.json"))
    before = limiter.snapshots()
    limiter.observe(ROUTE, 200, {"Content-Type": "application/json"})
    assert [s.policy for s in limiter.snapshots()] == [s.policy for s in before]


def test_a_missing_rules_header_keeps_the_known_rules(limiter: RateLimiter):
    limiter.observe(ROUTE, 200, headers("headers-items-authenticated.json"))
    partial = {
        "X-Rate-Limit-Policy": "backend-item-request-limit",
        "X-Rate-Limit-Account": "30:60:60,100:1800:600",
        "X-Rate-Limit-Account-State": "2:60:0,2:1800:0",
    }
    limiter.observe(ROUTE, 200, partial)
    assert {s.rule for s in limiter.snapshots()} == {"Account", "Ip"}


# -- the margin ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("max_hits", "expected"),
    [(100, 3), (30, 3), (20, 3), (10, 2), (6, 2), (5, 1), (1, 1)],
)
def test_margin_is_two_or_three_below_max(max_hits, expected):
    """SPEC §4.4 asks for 2 or 3. Small buckets get 1 rather than being unusable."""
    assert margin_for(max_hits) == expected


def test_effective_max_is_below_the_server_maximum(limiter: RateLimiter):
    limiter.observe(ROUTE, 200, headers("headers-items-authenticated.json"))
    for snap in limiter.snapshots():
        assert snap.effective_max < snap.max_hits
        assert snap.effective_max >= 1


def test_effective_max_never_reaches_zero():
    assert Bucket(policy="p", rule="r", period=10, max_hits=1).effective_max == 1


def test_period_is_padded_for_clock_skew():
    bucket = Bucket(policy="p", rule="r", period=60, max_hits=30)
    assert bucket.window == 60 + DEFAULT_PERIOD_PAD
    assert DEFAULT_PERIOD_PAD >= 1.0
    assert DEFAULT_PERIOD_PAD <= 3.0


# -- bucket behaviour at the limit ---------------------------------------------


def test_bucket_refuses_at_the_effective_max_not_at_the_server_max(clock: FakeClock):
    bucket = Bucket(policy="p", rule="Account", period=60, max_hits=30)
    assert bucket.effective_max == 27
    for _ in range(27):
        assert bucket.allows(clock.t)
        bucket.record(clock.t)
    assert not bucket.allows(clock.t)
    assert bucket.retry_after(clock.t) == pytest.approx(60 + DEFAULT_PERIOD_PAD)


def test_bucket_recovers_as_the_window_slides(clock: FakeClock):
    bucket = Bucket(policy="p", rule="Account", period=10, max_hits=3)  # effective 2
    bucket.record(clock.t)
    clock.advance(5)
    bucket.record(clock.t)
    assert not bucket.allows(clock.t)
    # The first hit ages out at t0 + 10 + pad.
    clock.advance(5 + DEFAULT_PERIOD_PAD + 0.01)
    assert bucket.allows(clock.t)


def test_limiter_refuses_when_any_bucket_is_full(limiter: RateLimiter, clock: FakeClock):
    limiter.observe(ROUTE, 200, headers("headers-items-authenticated.json"))
    # Account/60s is the tightest: max 30, margin 3, and the fixture's `-State`
    # header already reports one hit, so 26 remain.
    for _ in range(26):
        assert limiter.check(ROUTE).allowed
        limiter.record_request(ROUTE)
    decision = limiter.check(ROUTE)
    assert not decision.allowed
    assert decision.policy == "backend-item-request-limit"
    assert decision.rule == "Account"
    assert decision.period == 60
    assert decision.retry_after > 0


def test_refusal_reports_the_longest_wait_of_all_full_buckets(clock: FakeClock):
    limiter = RateLimiter(clock=clock)
    limiter.observe(
        ROUTE,
        200,
        {
            "X-Rate-Limit-Policy": "p",
            "X-Rate-Limit-Rules": "Account",
            "X-Rate-Limit-Account": "2:10,2:600",
        },
    )
    # margin 1 on a max of 2 leaves an effective max of 1; one request fills both.
    limiter.record_request(ROUTE)
    decision = limiter.check(ROUTE)
    assert not decision.allowed
    assert decision.period == 600
    assert decision.retry_after == pytest.approx(600 + DEFAULT_PERIOD_PAD)


def test_check_is_a_verdict_and_never_sleeps(limiter: RateLimiter, clock: FakeClock):
    """SPEC §4.4: refuse rather than silently queue.

    The whole contract is that `check` is synchronous and returns immediately, so
    there is nowhere for a hidden queue to live.
    """
    before = clock.t
    for _ in range(50):
        limiter.record_request(ROUTE)
        limiter.check(ROUTE)
    assert clock.t == before


# -- seeding -------------------------------------------------------------------


def test_an_unknown_route_starts_with_a_tiny_seed_budget(limiter: RateLimiter):
    snap = limiter.check(ROUTE)
    assert snap.allowed
    limiter.record_request(ROUTE)
    refused = limiter.check(ROUTE)
    assert not refused.allowed
    assert "seed" in refused.reason
    assert SEED_MAX_HITS == 1


def test_the_seed_is_reported_as_not_learned(limiter: RateLimiter):
    limiter.record_request(ROUTE)
    seeds = [s for s in limiter.snapshots() if not s.learned]
    assert len(seeds) == 1
    assert seeds[0].rule == "seed"


def test_the_seed_is_dropped_once_the_policy_is_learned(limiter: RateLimiter):
    limiter.record_request(ROUTE)
    limiter.observe(ROUTE, 200, headers("headers-items-authenticated.json"))
    assert all(s.learned for s in limiter.snapshots())


def test_two_routes_converge_on_shared_buckets(limiter: RateLimiter):
    """`get-items` and `get-stash-items` share `backend-item-request-limit`."""
    limiter.observe("items", 200, headers("headers-items-authenticated.json"))
    limiter.observe("stash", 200, headers("headers-items-authenticated.json"))
    limiter.record_request("items")
    account_60 = next(
        s for s in limiter.snapshots() if s.rule == "Account" and s.period == 60
    )
    assert account_60.hits >= 1
    # A request on the other route counts against the same bucket.
    limiter.record_request("stash")
    account_60 = next(
        s for s in limiter.snapshots() if s.rule == "Account" and s.period == 60
    )
    assert account_60.hits >= 2


# -- trusting the server's count -----------------------------------------------


def test_server_state_backfills_upward(limiter: RateLimiter):
    """The same session is spending budget in the user's browser."""
    limiter.observe(
        ROUTE,
        200,
        {
            "X-Rate-Limit-Policy": "backend-item-request-limit",
            "X-Rate-Limit-Rules": "Account",
            "X-Rate-Limit-Account": "30:60:60",
            "X-Rate-Limit-Account-State": "17:60:0",
        },
    )
    snap = limiter.snapshots()[0]
    assert snap.hits == 17


def test_server_state_never_revises_downward(limiter: RateLimiter):
    """Their counter can lag a request we already sent; ours must not shrink."""
    limiter.observe(ROUTE, 200, headers("headers-items-authenticated.json"))
    for _ in range(10):
        limiter.record_request(ROUTE)
    before = next(s for s in limiter.snapshots() if s.rule == "Account" and s.period == 60).hits
    limiter.observe(
        ROUTE,
        200,
        {
            "X-Rate-Limit-Policy": "backend-item-request-limit",
            "X-Rate-Limit-Rules": "Account",
            "X-Rate-Limit-Account": "30:60:60",
            "X-Rate-Limit-Account-State": "2:60:0",
        },
    )
    after = next(s for s in limiter.snapshots() if s.rule == "Account" and s.period == 60).hits
    assert after == before


def test_backfill_converges_instead_of_compounding(limiter: RateLimiter):
    """Repeating the same server count must not keep adding hits."""
    state = {
        "X-Rate-Limit-Policy": "p",
        "X-Rate-Limit-Rules": "Account",
        "X-Rate-Limit-Account": "30:60:60",
        "X-Rate-Limit-Account-State": "12:60:0",
    }
    for _ in range(5):
        limiter.observe(ROUTE, 200, state)
    assert limiter.snapshots()[0].hits == 12


def test_backfilled_hits_expire_with_the_window(limiter: RateLimiter, clock: FakeClock):
    limiter.observe(
        ROUTE,
        200,
        {
            "X-Rate-Limit-Policy": "p",
            "X-Rate-Limit-Rules": "Account",
            "X-Rate-Limit-Account": "30:60:60",
            "X-Rate-Limit-Account-State": "29:60:0",
        },
    )
    assert not limiter.check(ROUTE).allowed
    clock.advance(60 + DEFAULT_PERIOD_PAD + 1)
    assert limiter.check(ROUTE).allowed


def test_an_active_server_restriction_blocks_the_policy(limiter: RateLimiter, clock: FakeClock):
    limiter.observe(ROUTE, 200, headers("headers-items-restricted.json"))
    decision = limiter.check(ROUTE)
    assert not decision.allowed
    assert decision.retry_after >= 60
    assert "restriction" in decision.reason
    # The restriction lapses after 60s, but the 31 hits the server reported are
    # still inside the padded 60s window — both have to clear.
    clock.advance(60 + DEFAULT_PERIOD_PAD + 1)
    assert limiter.check(ROUTE).allowed


# -- backoff -------------------------------------------------------------------


def test_a_429_blocks_for_at_least_retry_after(limiter: RateLimiter, clock: FakeClock):
    limiter.observe(ROUTE, 200, headers("headers-items-authenticated.json"))
    wait = limiter.observe(ROUTE, 429, {"Retry-After": "45"})
    assert wait >= 45
    clock.advance(44)
    assert not limiter.check(ROUTE).allowed


def test_backoff_is_exponential_and_retry_after_is_only_a_floor(clock: FakeClock):
    limiter = RateLimiter(clock=clock)
    limiter.observe(ROUTE, 200, headers("headers-items-authenticated.json"))
    waits = []
    for _ in range(4):
        waits.append(limiter.observe(ROUTE, 429, {"Retry-After": "1"}))
        clock.advance(1000)  # let the block lapse so the next 429 is measurable
    assert waits == [BACKOFF_BASE, BACKOFF_BASE * 2, BACKOFF_BASE * 4, BACKOFF_BASE * 8]


def test_a_success_resets_the_backoff_counter(clock: FakeClock):
    limiter = RateLimiter(clock=clock)
    limiter.observe(ROUTE, 200, headers("headers-items-authenticated.json"))
    first = limiter.observe(ROUTE, 429, {"Retry-After": "0"})
    clock.advance(1000)
    limiter.observe(ROUTE, 200, headers("headers-items-authenticated.json"))
    again = limiter.observe(ROUTE, 429, {"Retry-After": "0"})
    assert first == again == BACKOFF_BASE


def test_a_server_error_also_backs_off(limiter: RateLimiter):
    limiter.observe(ROUTE, 200, headers("headers-items-authenticated.json"))
    wait = limiter.observe(ROUTE, 503, {})
    assert wait >= BACKOFF_BASE
    assert not limiter.check(ROUTE).allowed


def test_a_429_before_any_policy_is_known_blocks_for_the_full_retry_after(
    limiter: RateLimiter, clock: FakeClock
):
    """The seed window is ~10s; a `Retry-After: 300` must not be shortened to that.

    Before any policy is learned there is no bucket to hold the block, so it is
    recorded against the route. Getting this wrong means a restriction the server
    asked us to sit out for five minutes is honoured for ten seconds.
    """
    wait = limiter.observe(ROUTE, 429, {"Retry-After": "300"})
    assert wait >= 300
    decision = limiter.check(ROUTE)
    assert not decision.allowed
    assert decision.retry_after == pytest.approx(300, abs=1)
    clock.advance(299)
    assert not limiter.check(ROUTE).allowed
    clock.advance(2)
    assert limiter.check(ROUTE).allowed


def test_a_date_form_retry_after_is_ignored_rather_than_crashing(limiter: RateLimiter):
    limiter.observe(ROUTE, 200, headers("headers-items-authenticated.json"))
    wait = limiter.observe(ROUTE, 429, {"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"})
    assert wait == BACKOFF_BASE


def test_backoff_is_capped(clock: FakeClock):
    limiter = RateLimiter(clock=clock)
    limiter.observe(ROUTE, 200, headers("headers-items-authenticated.json"))
    for _ in range(20):
        wait = limiter.observe(ROUTE, 429, {})
        clock.advance(2000)
    assert wait <= 900.0


# -- snapshots -----------------------------------------------------------------


def test_snapshot_json_is_plain_types(limiter: RateLimiter):
    limiter.observe(ROUTE, 200, headers("headers-items-authenticated.json"))
    for snap in limiter.snapshots():
        assert isinstance(snap, LimitSnapshot)
        payload = snap.to_json()
        json.dumps(payload)  # must not raise
        assert set(payload) >= {"policy", "rule", "period", "hits", "effective_max"}


def test_fixture_headers_exist():
    """Guard against tests passing because a fixture silently vanished."""
    for name in (
        "headers-items-authenticated.json",
        "headers-characters-authenticated.json",
        "headers-items-anonymous.json",
        "headers-items-restricted.json",
    ):
        assert (FIXTURES / name).is_file(), name
    assert Path(FIXTURES / "README.md").is_file()
