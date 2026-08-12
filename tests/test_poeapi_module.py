"""The `poeapi` module: caching, honest degradation, and the 401 → mark_rejected path.

The whole module is exercised through a real `credentials` and a real `net`, with an
``httpx.MockTransport`` underneath. That way the wiring these tests describe — a 401
reaching `credentials`, a refusal reaching the cache — is the wiring that ships,
rather than a mock's idea of it.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from modules.credentials.backend.api import (
    CREDENTIAL_CHANGED,
    CredentialsApi,
    CredentialState,
)
from modules.poeapi.backend.api import (
    SYNC_COMPLETE,
    AccountUnknownError,
    PoeApi,
    RateLimitedError,
    SessionRejectedError,
    Source,
)
from modules.poeapi.backend.cache import ResponseCache
from modules.poeapi.backend.module import CHARACTERS_MIN_INTERVAL, PoeApiModule
from runtime.registry import Registry
from runtime.storage import Storage
from tests.conftest import ACCOUNT, PROFILE_ACCOUNT, FakeClock, Server
from tests.conftest import SESSION_VALUE as VALUE

# -- wiring --------------------------------------------------------------------


def test_poeapi_is_core_and_requires_only_core_modules():
    assert PoeApiModule.kind == "core"
    assert PoeApiModule.requires == ["credentials", "net"]


async def test_it_provides_its_protocol(api: PoeApi):
    assert isinstance(api, PoeApiModule)


async def test_start_order_is_credentials_then_net_then_poeapi(stack: Registry):
    assert stack.order == ["credentials", "net", "poeapi"]


# -- characters ----------------------------------------------------------------


async def test_get_characters_normalizes(api: PoeApi):
    result = await api.get_characters()
    # Second in GGG's ordering and first by recency. The fixture is arranged that
    # way so nothing can pass by reading `characters[0]`, which is what the default
    # used to do.
    assert result.characters[1].name == "PlaceholderWarden"
    assert result.default().name == "PlaceholderWarden"
    assert result.meta.from_cache is False


async def test_get_characters_is_cached_hard(api: PoeApi, server: Server):
    await api.get_characters()
    for _ in range(5):
        result = await api.get_characters()
        assert result.meta.from_cache is True
    assert server.paths().count("/character-window/get-characters") == 1


async def test_refresh_cannot_beat_the_character_endpoint_floor(
    api: PoeApi, server: Server, cache_clock: FakeClock
):
    """SPEC §4.4: `get-characters` is 10:60. `refresh=True` is not a licence to poll."""
    await api.get_characters()
    cache_clock.advance(CHARACTERS_MIN_INTERVAL / 2)
    result = await api.get_characters(refresh=True)
    assert result.meta.from_cache is True
    assert "refresh ignored" in (result.meta.note or "")
    assert server.paths().count("/character-window/get-characters") == 1

    cache_clock.advance(CHARACTERS_MIN_INTERVAL + 1)
    result = await api.get_characters(refresh=True)
    assert result.meta.from_cache is False
    assert server.paths().count("/character-window/get-characters") == 2


# -- items ---------------------------------------------------------------------


async def test_get_items_returns_the_normalized_bag(api: PoeApi):
    result = await api.get_items()
    assert result.source is Source.BAG
    assert result.character == "PlaceholderWarden"
    assert len(result.by_source(Source.BAG)) > 0
    assert len(result.by_source(Source.EQUIPMENT)) == 3
    assert result.content_hash


async def test_get_items_uses_the_current_character_by_default(api: PoeApi, server: Server):
    await api.get_items()
    items_request = next(
        r for r in server.requests if r.url.path == "/character-window/get-items"
    )
    assert items_request.url.params["character"] == "PlaceholderWarden"
    assert items_request.url.params["accountName"] == "ExampleAccount"


async def test_the_account_name_comes_from_the_credential_record(api: PoeApi, server: Server):
    await api.get_items("PlaceholderWarden")
    request = next(r for r in server.requests if r.url.path == "/character-window/get-items")
    assert request.url.params["accountName"] == "ExampleAccount"


async def test_items_are_not_cached_by_default(api: PoeApi, server: Server):
    """The endpoint commits at zone transitions; a TTL would delay the one sync
    that matters (SPEC §4.3)."""
    await api.get_items("PlaceholderWarden")
    await api.get_items("PlaceholderWarden")
    assert server.paths().count("/character-window/get-items") == 2


# -- the account name ----------------------------------------------------------
#
# Phase 2 called this un-inferable and asked for it. It is inferable, and asking was
# the bug: the LAN pairing form has two fields — a code and a credential — so on a
# Deck there was no way to answer, and a successful pair was followed by
# "no account name on record" forever. `/api/profile` answers from the cookie alone.


@pytest.fixture
async def unattributed(stack: Registry) -> Registry:
    """The Deck's state after pairing: a good credential, and no name with it."""
    credentials = stack.api(CredentialsApi)
    await credentials.clear()
    await credentials.set(VALUE, None)
    return stack


async def test_get_profile_reads_the_account_off_the_session(api: PoeApi, server: Server):
    profile = await api.get_profile()
    assert profile.account == PROFILE_ACCOUNT
    assert profile.uuid == "00000000-0000-4000-8000-000000000000"
    assert profile.meta.from_cache is False
    request = next(r for r in server.requests if r.url.path == "/api/profile")
    # The point of the endpoint: it is the one account request that does not already
    # need to know whose account it is.
    assert "accountName" not in request.url.params


async def test_get_profile_is_cached_hard(api: PoeApi, server: Server):
    """An account name changes when somebody pays GGG to change it."""
    for _ in range(5):
        await api.get_profile()
    assert server.paths().count("/api/profile") == 1


async def test_the_account_is_derived_when_nothing_states_one(
    unattributed: Registry, api: PoeApi, server: Server
):
    """The bug, as a test: pair, then read a bag, with nothing typed anywhere."""
    await api.get_items("PlaceholderWarden")
    request = next(r for r in server.requests if r.url.path == "/character-window/get-items")
    assert request.url.params["accountName"] == PROFILE_ACCOUNT
    assert "/api/profile" in server.paths()


async def test_a_derived_account_is_filed_with_the_credential(
    unattributed: Registry, api: PoeApi
):
    """So `poedex auth status` can answer it, and so it survives a restart."""
    await api.get_items("PlaceholderWarden")
    assert (await unattributed.api(CredentialsApi).status()).account == PROFILE_ACCOUNT


async def test_the_derived_account_is_not_looked_up_again_per_call(
    unattributed: Registry, api: PoeApi, server: Server
):
    """One request per session, not one per bag.

    Two mechanisms have to hold for this, and both are load-bearing: the response
    cache stops a second request, and filing the name with the credential means the
    resolution never even reaches the profile rung again.
    """
    for _ in range(4):
        await api.get_items("PlaceholderWarden")
    assert server.paths().count("/api/profile") == 1


async def test_an_explicit_account_beats_a_derived_one(
    unattributed: Registry, api: PoeApi, server: Server
):
    """`--account` is the escape hatch for a derived answer that is wrong. An
    override a lookup can beat is not an override — and it must not even *ask*,
    because a request spent to be ignored is a request spent."""
    await api.get_items("PlaceholderWarden", account="SomebodyElse#1234")
    request = next(r for r in server.requests if r.url.path == "/character-window/get-items")
    assert request.url.params["accountName"] == "SomebodyElse#1234"
    assert "/api/profile" not in server.paths()


async def test_the_setting_beats_a_derived_one(
    unattributed: Registry, api: PoeApi, server: Server
):
    unattributed.settings.view("poeapi").set("account", "SettingAccount#5678")
    await api.get_items("PlaceholderWarden")
    request = next(r for r in server.requests if r.url.path == "/character-window/get-items")
    assert request.url.params["accountName"] == "SettingAccount#5678"
    assert "/api/profile" not in server.paths()


async def test_the_credential_record_beats_a_derived_one(api: PoeApi, server: Server):
    """The default stack pairs *with* a name, and that name is used unasked."""
    await api.get_items("PlaceholderWarden")
    request = next(r for r in server.requests if r.url.path == "/character-window/get-items")
    assert request.url.params["accountName"] == ACCOUNT
    assert "/api/profile" not in server.paths()


async def test_an_unreachable_profile_says_what_went_wrong(
    unattributed: Registry, api: PoeApi, server: Server
):
    """The message names the failure and the one command that ends it.

    Not "run 'poedex auth set --account <name>'", which is what it used to say: that
    is an instruction to type an account name on a device with no keyboard, for a
    value the tool can nearly always read for itself.
    """
    server.profile = None  # the endpoint 404s; the credential is fine
    with pytest.raises(AccountUnknownError) as excinfo:
        await api.get_items("PlaceholderWarden")
    message = str(excinfo.value)
    assert "404" in message
    assert "poedex config set poeapi.account" in message
    assert "poedex auth set --account" not in message


async def test_a_profile_with_no_name_in_it_is_not_an_account(
    unattributed: Registry, api: PoeApi, server: Server
):
    """A 200 that names nobody is not a licence to send an empty ``accountName``."""
    server.profile = {"uuid": "00000000-0000-4000-8000-000000000000"}
    with pytest.raises(AccountUnknownError):
        await api.get_items("PlaceholderWarden")
    assert "/character-window/get-items" not in server.paths()


async def test_a_rejected_session_on_the_profile_endpoint_is_not_a_missing_name(
    unattributed: Registry, api: PoeApi, server: Server
):
    """401/403 goes down the same path as every other endpoint's.

    `credentials` is told first, and the caller gets ``SessionRejectedError`` — not
    ``AccountUnknownError``. Reporting a dead session as a missing account name would
    reintroduce, from the other direction, exactly the confusion that deriving the
    name removed.
    """
    server.status = 403
    with pytest.raises(SessionRejectedError):
        await api.get_items("PlaceholderWarden")
    assert (
        await unattributed.api(CredentialsApi).status()
    ).state is CredentialState.REJECTED


async def test_the_started_stack_lets_a_pair_attribute_itself(
    stack: Registry, server: Server
):
    """The wiring, end to end, over the real registry.

    `poeapi` hands `credentials` a resolver at start; `credentials` calls it when a
    credential lands over the pairing socket. Neither module imports the other's
    implementation and the dependency still points one way — `poeapi` requires
    `credentials`, not the reverse — which is the only reason this can be a callback
    rather than an import.

    Driven through ``_pair_store`` rather than a real socket because the alternative
    is binding ``0.0.0.0`` in a unit test; ``tests/test_credentials_pairing.py``
    covers the socket, on loopback, with a stub resolver. Between the two, every hop
    is exercised.
    """
    credentials = stack.get("credentials")
    await credentials.clear()

    status = await credentials._pair_store(VALUE)

    assert status.account == PROFILE_ACCOUNT
    assert status.usable is True
    assert server.paths().count("/api/profile") == 1
    # And the bag that follows spends nothing more finding out whose it is.
    await stack.api(PoeApi).get_items("PlaceholderWarden")
    request = next(r for r in server.requests if r.url.path == "/character-window/get-items")
    assert request.url.params["accountName"] == PROFILE_ACCOUNT
    assert server.paths().count("/api/profile") == 1


async def test_a_stopped_poeapi_leaves_no_resolver_behind(stack: Registry, registry: Registry):
    """A closure over a stopped module would answer `ModuleNotStartedError` rather
    than a name, which is a worse failure than not answering at all."""
    credentials = stack.get("credentials")
    assert credentials._resolve_account is not None
    await stack.get("poeapi").stop()
    assert credentials._resolve_account is None


# -- stash ---------------------------------------------------------------------


async def test_get_stash_tabs(api: PoeApi):
    result = await api.get_stash_tabs("Standard")
    assert [t.name for t in result.tabs][:2] == ["C", "Gear"]
    assert result.league == "Standard"


async def test_get_stash_items(api: PoeApi):
    result = await api.get_stash_items(1, "Standard")
    assert result.source is Source.STASH
    assert result.tab_index == 1
    assert all(i.location.source is Source.STASH for i in result.items)


async def test_stash_and_items_share_the_rate_limit_bucket(api: PoeApi):
    """research-notes §3: they are the same policy, so they must be one bucket."""
    await api.get_items("PlaceholderWarden")
    before = _account_hits(api)
    await api.get_stash_items(1, "Standard")
    assert _account_hits(api) > before


def _account_hits(api: PoeApi) -> int:
    return next(
        entry["hits"]
        for entry in api.limits()
        if entry["policy"] == "backend-item-request-limit"
        and entry["rule"] == "Account"
        and entry["period"] == 60
    )


# -- the 401 path --------------------------------------------------------------


async def test_a_401_marks_the_credential_rejected(stack: Registry, api: PoeApi, server: Server):
    credentials = stack.api(CredentialsApi)
    assert (await credentials.status()).state is CredentialState.SET

    server.status = 401
    with pytest.raises(SessionRejectedError):
        await api.get_characters()

    status = await credentials.status()
    assert status.state is CredentialState.REJECTED
    assert status.usable is False
    assert VALUE not in (status.note or "")


async def test_a_403_marks_the_credential_rejected(stack: Registry, api: PoeApi, server: Server):
    server.status = 403
    with pytest.raises(SessionRejectedError):
        await api.get_characters()
    assert (await stack.api(CredentialsApi).status()).state is CredentialState.REJECTED


async def test_a_success_marks_the_credential_ok(stack: Registry, api: PoeApi):
    await api.get_characters()
    status = await stack.api(CredentialsApi).status()
    assert status.state is CredentialState.OK
    assert status.last_ok_at is not None


async def test_the_rejection_note_never_contains_the_credential(
    stack: Registry, api: PoeApi, server: Server
):
    server.status = 403
    server.error_body = {"error": f"bad cookie POESESSID={VALUE}"}
    with pytest.raises(SessionRejectedError) as excinfo:
        await api.get_characters()
    note = (await stack.api(CredentialsApi).status()).note or ""
    assert VALUE not in note
    assert VALUE not in str(excinfo.value)


# -- degradation ---------------------------------------------------------------


async def test_a_refusal_falls_back_to_cache_and_says_so(api: PoeApi, server: Server):
    await api.get_items("PlaceholderWarden")
    sent = len(server.requests)
    # Burn the Account/60s bucket.
    for _ in range(40):
        try:
            await api.get_items("PlaceholderWarden")
        except RateLimitedError:  # pragma: no cover - only if there is no cache
            pytest.fail("cached data should have been served")
        result = await api.get_items("PlaceholderWarden")
        if result.meta.stale:
            break
    else:  # pragma: no cover
        pytest.fail("the limiter never refused")

    assert result.meta.from_cache is True
    assert result.meta.stale is True
    assert result.meta.retry_after and result.meta.retry_after > 0
    assert len(server.requests) > sent
    # The refused calls did not reach the server.
    assert len(result.items) > 0


async def test_a_refusal_with_no_cache_raises_rather_than_queueing(api: PoeApi, server: Server):
    """SPEC §4.4: refuse rather than silently queue."""
    server.header_file = "headers-items-restricted.json"
    with pytest.raises(RateLimitedError) as excinfo:
        # First call learns the (already restricted) policy, second is refused.
        # Two *ordinary* tabs: a remove-only one would be served from cache the
        # second time and a map tab would never be requested at all, and neither of
        # those is the path this test is about.
        await api.get_stash_items(1, "Standard")
        await api.get_stash_items(2, "Standard")
    assert excinfo.value.retry_after > 0


async def test_a_500_falls_back_to_cache(api: PoeApi, server: Server):
    await api.get_items("PlaceholderWarden")
    server.status = 500
    result = await api.get_items("PlaceholderWarden")
    assert result.meta.stale is True
    assert "500" in (result.meta.note or "")


async def test_a_500_with_no_cache_raises(api: PoeApi, server: Server):
    from modules.poeapi.backend.api import PoeApiError

    server.status = 500
    with pytest.raises(PoeApiError):
        await api.get_stash_items(2, "Standard")


# -- events --------------------------------------------------------------------


async def test_a_live_sync_emits_sync_complete(stack: Registry, api: PoeApi):
    seen = []
    stack.events.subscribe(SYNC_COMPLETE, lambda event: seen.append(event.payload))
    await api.get_items("PlaceholderWarden")
    assert len(seen) == 1
    assert seen[0]["changed"] is True
    assert seen[0]["source"] == "bag"
    assert seen[0]["content_hash"]


async def test_an_unchanged_sync_says_so(stack: Registry, api: PoeApi):
    seen = []
    stack.events.subscribe(SYNC_COMPLETE, lambda event: seen.append(event.payload))
    await api.get_items("PlaceholderWarden")
    await api.get_items("PlaceholderWarden")
    assert [entry["changed"] for entry in seen] == [True, False]
    assert seen[0]["content_hash"] == seen[1]["content_hash"]


async def test_cached_data_does_not_emit_a_sync(stack: Registry, api: PoeApi):
    seen = []
    stack.events.subscribe(SYNC_COMPLETE, lambda event: seen.append(event.payload))
    await api.get_characters()
    await api.get_characters()
    assert seen == []


# -- the cache -----------------------------------------------------------------


def test_the_cache_survives_a_restart(tmp_path: Path, cache_clock: FakeClock):
    storage = Storage(tmp_path / "cache", "poeapi")
    first = ResponseCache(storage, clock=cache_clock)
    first.put("k", {"hello": "world"})
    second = ResponseCache(Storage(tmp_path / "cache", "poeapi"), clock=cache_clock)
    entry = second.get("k")
    assert entry is not None
    assert entry.payload == {"hello": "world"}


def test_a_corrupt_cache_file_is_discarded_not_fatal(tmp_path: Path):
    storage = Storage(tmp_path / "cache", "poeapi")
    cache = ResponseCache(storage)
    storage.write_bytes(ResponseCache.filename("k"), b"{not json")
    assert cache.get("k") is None


def test_cache_keys_with_awkward_characters_are_safe(tmp_path: Path):
    cache = ResponseCache(Storage(tmp_path / "cache", "poeapi"))
    cache.put("items:Some Account/../etc:Character Name", [1, 2, 3])
    assert cache.get("items:Some Account/../etc:Character Name").payload == [1, 2, 3]


def test_cache_files_are_owner_only(tmp_path: Path):
    storage = Storage(tmp_path / "cache", "poeapi")
    cache = ResponseCache(storage)
    cache.put("k", {"a": 1})
    path = storage.path(ResponseCache.filename("k"))
    assert oct(path.stat().st_mode & 0o777) == "0o600"


# -- JSON surface --------------------------------------------------------------


async def test_every_registered_method_returns_plain_json(stack: Registry, api: PoeApi):
    methods = stack.methods
    assert set(methods.for_module("poeapi")) == {
        # A surface may ask whose account this is. It may not ask *with* a
        # credential — nothing here takes one — and the answer is a name the user
        # already knows.
        "poeapi.get_profile",
        "poeapi.get_characters",
        # The picker's two. `set_character` is the only method in the tool that
        # writes a setting, and it exists because on a Deck the panel is the only
        # place the character can be changed at all.
        "poeapi.character_choice",
        "poeapi.set_character",
        "poeapi.get_items",
        "poeapi.get_stash_items",
        "poeapi.get_stash_tabs",
        # Phase 10. `crawl_stash` is deliberately **not** registered: a crawl spends
        # the whole item budget over minutes, and a registered method is something any
        # surface can start with one dispatch (SPEC §6.6, "never auto-crawl").
        "poeapi.stash_state",
        "poeapi.limits",
    }
    payload = await methods.call("poeapi.get_items", "PlaceholderWarden")
    json.dumps(payload)
    assert payload["content_hash"]
    assert payload["meta"]["from_cache"] is False


async def test_never_paired_is_not_reported_as_an_expired_session(
    stack: Registry, api: PoeApi
):
    """"Pair with the tool" and "pair again" are different instructions."""
    await stack.api(CredentialsApi).clear()
    with pytest.raises(SessionRejectedError) as excinfo:
        await api.get_characters()
    assert "no credential is stored" in str(excinfo.value)
    assert (await stack.api(CredentialsApi).status()).state is CredentialState.NEVER_SET


async def test_repeated_syncs_do_not_rewrite_the_credential_every_time(
    stack: Registry, api: PoeApi
):
    """`mark_ok` is a disk write plus a `credential_changed` event. Throttle it."""
    seen: list[object] = []
    stack.events.subscribe(CREDENTIAL_CHANGED, lambda event: seen.append(event.payload))
    await api.get_items("PlaceholderWarden")
    assert len(seen) == 1  # SET -> OK, worth recording
    for _ in range(3):
        await api.get_items("PlaceholderWarden")
    assert len(seen) == 1
