"""Which character the tool reads, and whether it admits how it decided.

The bug this file exists for was measured against the live account and looked like
nothing at all:

    PlaceholderWarden        league=Standard          current=False
    PlaceholderHierophant    league=Allflame          current=False
    PlaceholderJuggernaut    league=Solo Self-Found   current=False
    the default picked: PlaceholderWarden

``current`` is a field **GGG does not send** — it is absent from every entry of a
roster read out of game — and the resolver's last rung was ``characters[0]``. So a
parked Standard character was read for weeks while its owner played a league one,
and the panel header said ``character: <name>`` with exactly the confidence it has
when somebody chose it.

Two things are asserted here and they are separable. The first is that the *right*
character is now picked, from ``lastLoginTime``, which GGG does send and which is
what the PoE website's own top bar reads. The second, and the one that survives GGG
changing the payload again, is that when nothing can be read the answer is labelled
a guess — in the model, on the bag, and in the CLI.

Offline, like everything else: the roster comes from ``tests/fixtures/poeapi/``.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from modules.poeapi.backend.api import (
    CHARACTER_CHANGED,
    CHARACTER_ENV,
    Character,
    CharacterList,
    CharacterSource,
    CharacterUnknownError,
    Meta,
    PoeApi,
)
from modules.poeapi.backend.models import utcnow
from modules.poeapi.backend.module import PoeApiModule, _characters_from, _last_login
from runtime.registry import Registry
from runtime.settings import SETTINGS_FILENAME, SettingsStore
from tests.conftest import Server, payload

RECENT = "PlaceholderWarden"
"""Highest ``lastLoginTime`` in the fixture, and **second** in GGG's ordering — so
nothing here can pass by accidentally reading ``characters[0]``."""

FIRST = "PlaceholderJuggernaut"
"""First in the fixture's ordering, and the character the old fallback would have
picked. Its league is Solo Self-Found; `RECENT` is in Standard."""

ANOTHER = "PlaceholderHierophant"


def roster(*characters: Character) -> CharacterList:
    return CharacterList(characters=list(characters), meta=Meta(fetched_at=utcnow()))


def at(iso: str) -> datetime:
    return datetime.fromisoformat(iso).replace(tzinfo=UTC)


# -- parsing ---------------------------------------------------------------------


def test_last_login_is_read_as_seconds_not_milliseconds():
    """The unit was checked against the live values, not assumed.

    The largest one on the account decoded to the day before it was read. The same
    number read as milliseconds lands in January 1970, which is how you can tell.
    """
    when = _last_login(1786484400)
    assert when is not None
    assert when.year == 2026
    # The same number understood as milliseconds is January 1970, which is how the
    # unit was settled rather than assumed.
    assert datetime.fromtimestamp(1786484400 / 1000, UTC).year == 1970
    # And there is no unit-guessing branch: a millisecond-sized value is refused
    # rather than quietly rescaled. A parser that guesses units is the same class of
    # confident invention as the fallback this change removes.
    assert _last_login(1786484400 * 1000) is None


def test_a_character_with_no_last_login_is_unknown_rather_than_the_epoch():
    """Absent, zero and negative all mean *unknown*.

    Whether a never-played character omits the key or sends ``0`` is unmeasured, so
    both have to read the same way. An entry that sorted at the epoch would be an
    unknown quietly winning a comparison against another unknown.
    """
    assert _last_login(None) is None
    assert _last_login(0) is None
    assert _last_login(-1) is None
    assert _last_login("2026-08-11") is None
    assert _last_login(True) is None


def test_the_recorded_roster_carries_no_current_flag():
    """The recorded shape is the measured one, and it has no ``current`` in it."""
    characters = _characters_from(payload("get-characters.json"))
    assert [character.current for character in characters] == [False, False, False]
    assert all(character.last_login is not None for character in characters)


# -- ranking ---------------------------------------------------------------------


def test_the_most_recently_played_character_wins_over_ggg_ordering():
    picked, source = roster(
        Character(name=FIRST, last_login=at("2026-05-02T18:20")),
        Character(name=RECENT, last_login=at("2026-08-11T21:40")),
    ).resolve()
    assert picked is not None and picked.name == RECENT
    assert source is CharacterSource.LAST_LOGIN


def test_a_marked_current_character_beats_the_most_recently_played_one():
    """``current`` says who is *playing*; ``last_login`` says who played *last*.

    Out of game only the second exists, which is every measurement so far. In game
    they answer different questions and the first is the truth about now — so it
    wins, and somebody playing a character other than the one they played last is
    ordinary rather than a conflict to report.
    """
    picked, source = roster(
        Character(name=RECENT, last_login=at("2026-08-11T21:40")),
        Character(name=ANOTHER, last_login=at("2026-07-14T07:05"), current=True),
    ).resolve()
    assert picked is not None and picked.name == ANOTHER
    assert source is CharacterSource.CURRENT


def test_an_entry_with_no_timestamp_cannot_win_by_tying_at_zero():
    picked, source = roster(
        Character(name=FIRST),
        Character(name=RECENT, last_login=at("2026-05-02T18:20")),
    ).resolve()
    assert picked is not None and picked.name == RECENT
    assert source is CharacterSource.LAST_LOGIN


def test_with_nothing_readable_the_pick_is_labelled_a_guess():
    """The report's exact state, and the only one where the tool is guessing."""
    picked, source = roster(Character(name=FIRST), Character(name=RECENT)).resolve()
    assert picked is not None and picked.name == FIRST
    assert source is CharacterSource.FALLBACK


def test_default_has_no_positional_fallback_at_all():
    """`default()` is what a league and a realm read, and it must return nothing
    rather than GGG's first entry — a stash read aimed at a guessed character's
    league is the four-fold pricing error with an extra step."""
    assert roster(Character(name=FIRST), Character(name=RECENT)).default() is None
    assert roster().default() is None


def test_an_empty_roster_is_none_and_not_an_exception_in_the_model():
    picked, source = roster().resolve()
    assert picked is None
    assert source is CharacterSource.NONE


# -- how it is said --------------------------------------------------------------


@pytest.mark.parametrize(
    ("source", "fragment"),
    [
        (CharacterSource.LAST_LOGIN, "most recently played"),
        (CharacterSource.CURRENT, "the character you are playing"),
        (CharacterSource.SETTING, "pinned by the poeapi.character setting"),
        (CharacterSource.FALLBACK, "GUESSED"),
    ],
)
def test_every_source_says_what_it_is(source: CharacterSource, fragment: str):
    from modules.poeapi.backend.api import CharacterChoice

    assert fragment in CharacterChoice(name=RECENT, source=source).describe()


def test_only_the_fallback_counts_as_a_guess():
    from modules.poeapi.backend.api import CharacterChoice

    guessed = [
        source
        for source in CharacterSource
        if CharacterChoice(name=RECENT, source=source).guessed
    ]
    assert guessed == [CharacterSource.FALLBACK]


# -- the module ------------------------------------------------------------------


async def test_the_default_character_is_the_most_recently_played_one(api: PoeApi):
    selection = await api.character_choice()
    assert selection.choice.name == RECENT
    assert selection.choice.source is CharacterSource.LAST_LOGIN
    assert selection.choice.guessed is False
    assert selection.configured is None
    # The whole roster, with the column that disambiguates it.
    assert {c.name: c.league for c in selection.characters} == {
        FIRST: "Solo Self-Found",
        RECENT: "Standard",
        ANOTHER: "Allflame",
    }


async def test_the_bag_is_fetched_for_the_most_recently_played_character(
    api: PoeApi, server: Server
):
    bag = await api.get_items()
    assert bag.character == RECENT
    assert bag.character_source is CharacterSource.LAST_LOGIN
    items = [r for r in server.requests if r.url.path.endswith("get-items")]
    assert items[-1].url.params["character"] == RECENT


async def test_a_marked_current_character_is_what_the_bag_reads(
    api: PoeApi, server: Server
):
    """Hypothetical, and labelled as such: nobody here has read a roster from inside
    the game. The ordering is correct either way — if GGG sets the field it wins, and
    if it never does then `last_login` silently carries the whole job."""
    server.characters = payload("get-characters-in-game.json")
    bag = await api.get_items()
    assert bag.character == ANOTHER
    assert bag.character_source is CharacterSource.CURRENT


async def test_with_nothing_marked_the_bag_says_it_guessed(api: PoeApi, server: Server):
    server.characters = payload("get-characters-unplayed.json")
    bag = await api.get_items()
    assert bag.character == FIRST
    assert bag.character_source is CharacterSource.FALLBACK
    # Which is the field a header has to render differently. A name with a source of
    # `fallback` is the tool admitting it picked.
    assert bag.to_json()["character_source"] == "fallback"


async def test_a_guess_is_shouted_into_the_log_as_well(
    api: PoeApi, server: Server, caplog
):
    """A Deck's plugin log is what somebody has after the fact, and a silent guess
    leaves nothing in it."""
    server.characters = payload("get-characters-unplayed.json")
    with caplog.at_level("WARNING"):
        await api.get_items()
    assert any("only because GGG listed it first" in record.message for record in caplog.records)


async def test_an_explicit_argument_outranks_everything(api: PoeApi):
    selection = await api.character_choice(ANOTHER)
    assert selection.choice.name == ANOTHER
    assert selection.choice.source is CharacterSource.ARGUMENT
    # And it says what it is overriding, because a name alone cannot be checked.
    assert selection.choice.played_last == RECENT
    assert selection.choice.overriding is True


async def test_the_environment_variable_outranks_the_setting(
    api: PoeApi, stack: Registry, monkeypatch
):
    stack.settings.view("poeapi").set("character", ANOTHER)
    monkeypatch.setenv(CHARACTER_ENV, FIRST)
    selection = await api.character_choice()
    assert selection.choice.name == FIRST
    assert selection.choice.source is CharacterSource.ENVIRONMENT


async def test_the_setting_stays_above_a_marked_current_character(
    api: PoeApi, server: Server
):
    """The decision, asserted rather than assumed.

    `poeapi` resolves the account, the league and the realm as *stated, then
    derived*, and an override a lookup can beat is not an override. What makes it
    safe is that the disagreement is now visible and one press undoes it: before the
    picker existed, a pin on a Deck could not be cleared by any surface.
    """
    server.characters = payload("get-characters-in-game.json")
    await api.set_character(RECENT)
    selection = await api.character_choice()
    assert selection.choice.name == RECENT
    assert selection.choice.source is CharacterSource.SETTING
    # ...and it says the account points somewhere else. Reported, not warned about.
    assert selection.choice.played_last == ANOTHER
    assert selection.choice.overriding is True


# -- pinning ---------------------------------------------------------------------


async def test_the_picker_persists_the_choice_and_it_survives_a_restart(
    api: PoeApi, stack: Registry, tmp_path
):
    """Written to the settings file, not held in memory.

    A pin that only lives in the running process is a pin the Deck loses on the next
    suspend, and "it worked when I set it" is the worst way to find that out. The
    assertion is deliberately against a *fresh* store reading the same file, which is
    what a restart actually is.
    """
    await api.set_character(ANOTHER)
    assert stack.settings.view("poeapi").get("character") == ANOTHER

    reopened = SettingsStore(tmp_path / "config" / SETTINGS_FILENAME)
    reopened.register("poeapi", PoeApiModule().settings_schema())
    assert reopened.get("poeapi", "character") == ANOTHER


async def test_clearing_the_pin_goes_back_to_following_the_account(api: PoeApi):
    """An override you cannot undo from the surface that set it is a trap, and on a
    Deck this surface is the only one there is."""
    await api.set_character(ANOTHER)
    selection = await api.set_character(None)
    assert selection.configured is None
    assert selection.choice.name == RECENT
    assert selection.choice.source is CharacterSource.LAST_LOGIN
    assert selection.choice.overriding is False


async def test_a_character_not_on_the_roster_is_refused(api: PoeApi):
    """A picker may pin and may not invent. On a Deck a wrong name is unfixable by
    the surface that wrote it."""
    with pytest.raises(CharacterUnknownError) as caught:
        await api.set_character("NotOnThisAccount")
    assert RECENT in str(caught.value)


async def test_pinning_announces_itself(api: PoeApi, stack: Registry):
    """The pick is made on one screen and spent on another: a bag left showing the
    previous character's items is the bug arriving by a different door."""
    seen: list[dict] = []
    stack.events.subscribe(CHARACTER_CHANGED, lambda event: seen.append(event.payload))
    await api.set_character(ANOTHER)
    assert seen and seen[-1]["choice"]["name"] == ANOTHER


async def test_the_picker_costs_no_extra_request(api: PoeApi, server: Server):
    await api.character_choice()
    before = len([r for r in server.requests if "get-characters" in r.url.path])
    await api.character_choice()
    await api.set_character(ANOTHER)
    await api.character_choice()
    after = len([r for r in server.requests if "get-characters" in r.url.path])
    assert after == before


async def test_both_picker_methods_go_through_the_one_rpc_door(stack: Registry):
    """The panel reaches these through ``transports/dispatch.call_method``, which is
    the same function the FastAPI route calls. Plain JSON, both ways."""
    import json

    from transports.dispatch import call_method

    listed = await call_method(stack, "poeapi.character_choice")
    assert listed.ok
    json.dumps(listed.result)
    assert listed.result["choice"]["source"] == "last_login"

    pinned = await call_method(stack, "poeapi.set_character", {"name": ANOTHER})
    assert pinned.ok
    assert pinned.result["choice"]["name"] == ANOTHER
    assert pinned.result["configured"] == ANOTHER

    refused = await call_method(stack, "poeapi.set_character", {"name": "NotOnThisAccount"})
    assert refused.ok is False
    assert "NotOnThisAccount" in refused.error["message"]
