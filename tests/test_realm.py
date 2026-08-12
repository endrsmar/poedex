"""Which realm the account is on, and how the tool refuses to guess.

`poeapi` carried ``REALM = "pc"`` and put it in the query string of every
character-window request. It is the league bug one rung down: a constant standing in
for an account fact the API already returns. ``get-characters`` reports a ``realm``
per character, in the same roster entry the league is read from, so on an Xbox or
Sony account the tool was asking about the pc realm and would have got an empty
roster or a 403 — never *wrong* data, which is why it is lower stakes than the
league, but the same class of mistake and the same fix.

The rules pinned here:

* the realm is read off the roster, like the league;
* an explicit argument outranks it, and the ``poeapi.realm`` setting sits between;
* **an unresolvable realm never becomes ``pc``** — the parameter is left off the
  request and a warning names the command that fixes it.

**What is unverified.** Nobody on this project has a console account, so no test
here proves what GGG does with a console request; the fixtures put ``xbox`` in a
roster and assert what *we* send. Nor is it measured whether the legacy endpoints
require the parameter or default when it is absent — the tests assert only that a
realm nobody can state is not invented, which is true either way.
"""

from __future__ import annotations

import copy
import logging

import httpx
import pytest

from modules.poeapi.backend.api import CHARACTERS_PATH, ITEMS_PATH, STASH_PATH, PoeApi
from modules.poeapi.backend.module import _characters_from
from tests.conftest import Server, headers, payload

CHARACTER = "PlaceholderWarden"
"""The current character in ``get-characters.json``, whose realm is ``pc``."""


def request_to(server: Server, path: str) -> httpx.Request:
    matches = [r for r in server.requests if r.url.path == path]
    assert matches, f"no request to {path}"
    return matches[-1]


def console_roster() -> list[dict]:
    """The recorded roster, moved to Xbox. Every character, so nothing is mixed."""
    roster = copy.deepcopy(payload("get-characters.json"))
    for entry in roster:
        entry["realm"] = "xbox"
    return roster


def realmless_roster() -> list[dict]:
    """A roster with no ``realm`` key at all — an older or trimmed response."""
    roster = copy.deepcopy(payload("get-characters.json"))
    for entry in roster:
        entry.pop("realm", None)
    return roster


# -- the roster is where the answer lives ---------------------------------------


def test_the_realm_is_parsed_off_each_roster_entry():
    characters = _characters_from(payload("get-characters.json"))
    assert [c.realm for c in characters] == ["pc", "pc", "pc"]


def test_a_roster_entry_without_a_realm_is_none_not_pc():
    characters = _characters_from(realmless_roster())
    assert [c.realm for c in characters] == [None, None, None]


async def test_the_character_list_carries_the_realm_to_its_callers(api: PoeApi):
    roster = await api.get_characters()
    assert roster.default().realm == "pc"


# -- what goes on the wire ------------------------------------------------------


async def test_the_character_request_asserts_no_realm_of_its_own(api: PoeApi, server: Server):
    """The one request that cannot read the realm off the roster: it *is* the roster.

    Asking for ``realm=pc`` here is what made a console account's roster come back
    empty, and there is nothing to consult that would say otherwise — so nothing is
    claimed.
    """
    await api.get_characters()
    assert "realm" not in request_to(server, CHARACTERS_PATH).url.params


async def test_a_configured_realm_reaches_the_character_request(stack, server: Server):
    stack.settings.set("poeapi", "realm", "xbox")
    await stack.api(PoeApi).get_characters()
    assert request_to(server, CHARACTERS_PATH).url.params["realm"] == "xbox"


async def test_the_bag_is_fetched_for_the_realm_the_roster_reported(api: PoeApi, server: Server):
    await api.get_items()
    assert request_to(server, ITEMS_PATH).url.params["realm"] == "pc"


async def test_a_console_roster_moves_the_bag_request_to_its_realm(api: PoeApi, server: Server):
    server.characters = console_roster()
    await api.get_items()
    request = request_to(server, ITEMS_PATH)
    assert request.url.params["realm"] == "xbox"
    assert "pc" not in str(request.url)


async def test_the_stash_follows_the_roster_too(api: PoeApi, server: Server):
    server.characters = console_roster()
    await api.get_stash_tabs("Standard")
    assert request_to(server, STASH_PATH).url.params["realm"] == "xbox"


# -- precedence -----------------------------------------------------------------


async def test_an_explicit_realm_outranks_the_roster(api: PoeApi, server: Server):
    await api.get_items(realm="sony")
    assert request_to(server, ITEMS_PATH).url.params["realm"] == "sony"


async def test_an_explicit_realm_outranks_the_setting(stack, server: Server):
    stack.settings.set("poeapi", "realm", "xbox")
    await stack.api(PoeApi).get_items(realm="sony")
    assert request_to(server, ITEMS_PATH).url.params["realm"] == "sony"


async def test_the_setting_outranks_the_roster(stack, server: Server):
    """A pc roster and an xbox setting: the stated answer wins.

    The setting exists for the case the roster cannot be reached or names something
    this build has never heard of, so it has to be able to override what was read.
    """
    stack.settings.set("poeapi", "realm", "xbox")
    await stack.api(PoeApi).get_items()
    assert request_to(server, ITEMS_PATH).url.params["realm"] == "xbox"


# -- an unresolvable realm is never "pc" ----------------------------------------


async def test_a_roster_with_no_realm_does_not_become_pc(
    api: PoeApi, server: Server, caplog: pytest.LogCaptureFixture
):
    server.characters = realmless_roster()
    with caplog.at_level(logging.WARNING):
        await api.get_items()
    request = request_to(server, ITEMS_PATH)
    assert "realm" not in request.url.params
    assert "realm=pc" not in str(request.url)
    assert "poedex config set poeapi.realm" in caplog.text


async def test_an_unreachable_roster_does_not_become_pc(
    stack, server: Server, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
):
    """The character endpoint refuses; the bag still loads, without a claimed realm.

    A named character short-circuits the roster lookup for *which* character to
    read, so this exercises the path where the realm is the only thing the roster
    was wanted for — and it is simply not answered.
    """
    original = server._ggg

    def refuse_characters(request: httpx.Request) -> httpx.Response:
        if request.url.path == CHARACTERS_PATH:
            return httpx.Response(
                429,
                json={"error": {"code": 8, "message": "Rate limit exceeded"}},
                headers=headers("headers-items-authenticated.json"),
            )
        return original(request)

    monkeypatch.setattr(server, "_ggg", refuse_characters)
    with caplog.at_level(logging.WARNING):
        items = await stack.api(PoeApi).get_items(CHARACTER)

    assert len(items.items) > 0
    request = request_to(server, ITEMS_PATH)
    assert "realm" not in request.url.params
    assert items.league is None, "an unknown league is not invented either"
    assert "poedex config set poeapi.realm" in caplog.text


def test_the_module_holds_no_realm_literal_at_all():
    """The whole point, stated as a property of the source.

    ``REALM = "pc"`` was one token, and a plausible one to type again. So: no such
    name, and no bare ``"pc"`` anywhere in the module's syntax tree. Prose is
    exempt by construction — a docstring is one long constant, not the string
    ``"pc"``.
    """
    import ast
    import inspect

    from modules.poeapi.backend import module as poeapi_module

    assert not hasattr(poeapi_module, "REALM")
    tree = ast.parse(inspect.getsource(poeapi_module))
    literals = {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }
    assert literals.isdisjoint({"pc", "xbox", "sony"})
