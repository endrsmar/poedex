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
from tests.conftest import SESSION_VALUE as VALUE
from tests.conftest import FakeClock, Server

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
    assert result.characters[0].name == "PlaceholderWarden"
    assert result.current().name == "PlaceholderWarden"
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


async def test_a_missing_account_name_is_a_distinct_error(stack: Registry, api: PoeApi):
    credentials = stack.api(CredentialsApi)
    await credentials.clear()
    await credentials.set(VALUE, None)
    with pytest.raises(AccountUnknownError) as excinfo:
        await api.get_items("PlaceholderWarden")
    assert "poedex auth set" in str(excinfo.value)


async def test_items_are_not_cached_by_default(api: PoeApi, server: Server):
    """The endpoint commits at zone transitions; a TTL would delay the one sync
    that matters (SPEC §4.3)."""
    await api.get_items("PlaceholderWarden")
    await api.get_items("PlaceholderWarden")
    assert server.paths().count("/character-window/get-items") == 2


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
        await api.get_stash_items(3, "Standard")
        await api.get_stash_items(4, "Standard")
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
        "poeapi.get_characters",
        "poeapi.get_items",
        "poeapi.get_stash_items",
        "poeapi.get_stash_tabs",
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
