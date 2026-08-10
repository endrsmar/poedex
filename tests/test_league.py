"""Which league a bag is priced against, and how the tool refuses to guess.

## The bug these tests exist for

``poedex appraise --character Pallinadar`` printed ``league: Standard`` for a
character in Allflame, and priced the bag against Standard's tables. Three links,
each individually reasonable:

1. ``poeapi.get_items`` built an ``ItemSet`` with no ``league``, although
   ``get-characters`` had just told it which league the character was in.
2. ``prices.league`` read only its own setting, whose default was ``"Standard"``,
   and never looked at the bag it was pricing.
3. ``appraisal`` reported and totalled whatever ``prices`` said.

The damage is not the label. A Divine Orb was measured at 897.7c in Standard and
209.0c in Allflame on the same day, so a divine-denominated total was out by ~4x;
league-specific items were absent from the wrong index and came back
``unpriceable``; and the ``unpriceable`` count was padded with items that price
perfectly well in the right league. Every number was plausible and none was right.

``test_a_bag_from_a_non_standard_league_is_priced_against_that_league`` is the
regression test for exactly that report. The rest of this file pins the rules that
make the failure impossible to reintroduce quietly: the setting is an override and
says so, an unknown league raises instead of defaulting, and tables loaded for one
league are never spent on another.

Every test here runs with ``league_override = ""`` — no configured league at all,
which is the shape a fresh install has and the shape the bug needed.
"""

from __future__ import annotations

import pytest

from cli.appraise import cmd_appraise
from cli.value import cmd_value
from modules.appraisal.backend.api import AppraisalApi
from modules.poeapi.backend.api import LeagueUnknownError, PoeApi, Source
from modules.prices.backend.api import LeagueSource, PricesApi, PriceSource
from modules.prices.backend.ninja import PREFETCH

NINJA = "poe.ninja"
GGG = "www.pathofexile.com"

ALLFLAME_CHARACTER = "PlaceholderHierophant"
"""``tests/fixtures/poeapi/get-characters.json``: in Allflame, and not the current
character. Standing in for the report's Pallinadar."""

STANDARD_CHARACTER = "PlaceholderWarden"
"""The current character, in Standard. The one whose league the old default
accidentally agreed with, which is why nothing looked wrong for so long."""


@pytest.fixture
def league_override() -> str:
    """No override. The bag decides, which is the whole point."""
    return ""


@pytest.fixture
async def stack_with_prices(stack_factory, registry, server, prices_module):
    """`prices` started with no league at all: no override, so no prefetch.

    Deliberately *not* ``priced_stack``. That fixture pins Standard and loads its
    tables before the test starts, which is precisely the state that hid the bug.
    """
    server.bag_fixture = "bag.json"
    result = await stack_factory(prices_module)
    yield result
    await registry.stop_all()


@pytest.fixture
async def full_stack(stack_factory, registry, server, prices_module, appraisal_module):
    """`prices` and `appraisal`, no league configured, on the loot bag."""
    server.bag_fixture = "loot-bag.json"
    result = await stack_factory(prices_module, appraisal_module)
    yield result
    await registry.stop_all()


def ninja_leagues(server) -> set[str]:
    return {r.url.params.get("league") for r in server.to_host(NINJA)}


def character_calls(server) -> int:
    return server.paths().count("/character-window/get-characters")


# -- link 1: the bag knows its own league ---------------------------------------


async def test_a_bag_carries_the_league_of_the_character_it_came_from(api: PoeApi):
    """The fact was always available on ``get-characters`` and always dropped."""
    bag = await api.get_items(ALLFLAME_CHARACTER)
    assert bag.character == ALLFLAME_CHARACTER
    assert bag.league == "Allflame"

    default = await api.get_items()
    assert default.character == STANDARD_CHARACTER
    assert default.league == "Standard"


async def test_resolving_the_league_costs_no_extra_character_request(
    api: PoeApi, server
):
    """``get-characters`` is the tightest endpoint on the account (10:60, 50:1800).

    The league has to come from it, so the rule is that it is read from the *cache*
    the default-character lookup already fills: one call for any number of bags.
    """
    await api.get_items(ALLFLAME_CHARACTER)
    assert character_calls(server) == 1
    for _ in range(4):
        await api.get_items(ALLFLAME_CHARACTER)
        await api.get_items()
    assert character_calls(server) == 1


async def test_a_bag_whose_league_cannot_be_established_says_none_not_standard(
    api: PoeApi
):
    """A character the roster does not list. Unknown is a state; Standard is a lie."""
    bag = await api.get_items("NobodyOfThatName")
    assert bag.league is None
    assert bag.items, "the bag itself is still perfectly usable"


async def test_the_stash_refuses_to_read_a_league_it_had_to_invent(api: PoeApi, server):
    server.characters = []
    with pytest.raises(LeagueUnknownError) as excinfo:
        await api.get_stash_tabs()
    assert "poeapi.league" in str(excinfo.value)


# -- link 2: the bag's league wins ----------------------------------------------


async def test_a_bag_from_a_non_standard_league_is_priced_against_that_league(
    stack_with_prices, server
):
    """**The regression test for the reported bug.**

    A character in Allflame, no configured league, nothing passed on the command
    line: the tables fetched are Allflame's, the valuation says Allflame, and it
    says the league came from the character rather than from a default.
    """
    poeapi = stack_with_prices.api(PoeApi)
    prices = stack_with_prices.api(PricesApi)

    bag = await poeapi.get_items(ALLFLAME_CHARACTER)
    assert bag.league == "Allflame"

    await prices.ensure_tables(prices.league_choice(bag.league).league)
    result = await prices.value_all(bag.by_source(Source.BAG), league=bag.league)

    assert result.league == "Allflame"
    assert result.league_source is LeagueSource.CHARACTER
    assert result.to_json()["league_overridden"] is False
    # Every table request went to Allflame's economy, and none to Standard's.
    assert ninja_leagues(server) == {"Allflame"}
    assert prices.tables_league == "Allflame"
    # And it is a real valuation, not an empty one that happens to be labelled right.
    assert result.total_chaos > 0
    assert result.priced


async def test_the_setting_overrides_the_bag_league_when_it_is_set(
    stack_with_prices, server
):
    """The escape hatch: "price my Allflame bag as if it were Standard".

    Legitimate to ask for, disastrous by accident — so it only happens when
    something explicitly asked, and the answer carries the fact that it did.
    """
    prices = stack_with_prices.api(PricesApi)
    poeapi = stack_with_prices.api(PoeApi)
    stack_with_prices.settings.set("prices", "league", "Standard")

    bag = await poeapi.get_items(ALLFLAME_CHARACTER)
    assert bag.league == "Allflame"

    choice = prices.league_choice(bag.league)
    assert choice.league == "Standard"
    assert choice.source is LeagueSource.SETTING
    assert choice.overridden
    assert "OVERRIDE" in choice.describe()

    await prices.ensure_tables(choice.league)
    result = await prices.value_all(bag.by_source(Source.BAG), league=bag.league)
    assert result.league == "Standard"
    assert result.league_source is LeagueSource.SETTING
    assert ninja_leagues(server) == {"Standard"}


async def test_an_explicit_argument_outranks_the_setting(stack_with_prices):
    """``--league`` is per-run and beats a standing instruction; both beat the bag."""
    prices = stack_with_prices.api(PricesApi)
    stack_with_prices.settings.set("prices", "league", "Standard")

    choice = prices.league_choice("Allflame", explicit="Hardcore")
    assert (choice.league, choice.source) == ("Hardcore", LeagueSource.ARGUMENT)
    assert "--league" in choice.describe()


async def test_an_empty_setting_is_not_an_override(stack_with_prices):
    """A blank string in the settings file must not shadow the character."""
    prices = stack_with_prices.api(PricesApi)
    stack_with_prices.settings.set("prices", "league", "   ")
    choice = prices.league_choice("Allflame")
    assert (choice.league, choice.source) == ("Allflame", LeagueSource.CHARACTER)


# -- link 3: never guess ---------------------------------------------------------


async def test_an_indeterminate_league_fails_loudly_rather_than_defaulting(
    stack_with_prices,
):
    """No bag league, no override: the one case the old code answered "Standard"."""
    prices = stack_with_prices.api(PricesApi)
    poeapi = stack_with_prices.api(PoeApi)
    bag = await poeapi.get_items("NobodyOfThatName")
    assert bag.league is None

    with pytest.raises(LeagueUnknownError) as excinfo:
        await prices.value_all(bag.by_source(Source.BAG), league=bag.league)
    message = str(excinfo.value)
    assert "--league" in message and "prices.league" in message
    # The message says why refusing is better than assuming, in numbers.
    assert "897.7" in message and "209.0" in message


async def test_the_cli_refuses_rather_than_appraising_against_a_guess(
    full_stack, capsys
):
    with pytest.raises(LeagueUnknownError):
        await cmd_appraise(
            full_stack.api(AppraisalApi),
            full_stack.api(PoeApi),
            full_stack.api(PricesApi),
            character="NobodyOfThatName",
            refresh=False,
            colour=False,
        )
    assert "bag total" not in capsys.readouterr().out


async def test_prices_starts_with_no_league_and_no_tables_when_none_is_configured(
    stack_with_prices, server
):
    """No override means nothing to prefetch — and no Standard tables fetched "just
    in case", which is how the wrong economy used to be sitting there ready."""
    prices = stack_with_prices.api(PricesApi)
    assert prices.tables_league is None
    assert prices.status().loaded == 0
    assert server.to_host(NINJA) == []


# -- tables belong to a league ---------------------------------------------------


async def test_tables_loaded_for_one_league_are_not_used_for_another(
    stack_with_prices, server
):
    """The subtle half of the bug: right label, wrong numbers underneath.

    Standard's tables are loaded. Asked to price an Allflame bag, the index answers
    *nothing* rather than answering out of Standard — and the table status names
    both leagues so the emptiness is explained rather than mysterious.
    """
    prices = stack_with_prices.api(PricesApi)
    poeapi = stack_with_prices.api(PoeApi)
    await prices.ensure_tables("Standard")
    assert prices.status().loaded == len(PREFETCH)

    bag = await poeapi.get_items(ALLFLAME_CHARACTER)
    result = await prices.value_all(bag.by_source(Source.BAG), league=bag.league)

    assert result.league == "Allflame"
    # Not one number came out of Standard's tables. The only survivors are tier 0 —
    # the player's own chaos-denominated ``~price`` notes, which are their claim
    # about their own item and belong to no economy's index.
    assert {v.source for v in result.priced} <= {PriceSource.NOTE}
    assert all(v.market is None for v in result.items)
    assert result.divine_rate is None, "a divine rate is the most league-specific"
    assert result.table is not None
    assert result.table.loaded == 0
    assert result.table.league == "Standard"
    assert "Standard" in result.table.note and "Allflame" in result.table.note


async def test_status_reports_which_league_the_loaded_tables_are_for(
    stack_with_prices,
):
    prices = stack_with_prices.api(PricesApi)
    await prices.ensure_tables("Allflame")
    status = prices.status()
    assert status.league == "Allflame"
    assert status.loaded == len(PREFETCH)
    assert status.to_json()["league"] == "Allflame"
    # And asked about a league it does not hold, it says so instead of counting.
    mismatch = prices.status("Standard")
    assert mismatch.loaded == 0
    assert "Standard" in mismatch.note


async def test_switching_league_refetches_and_keeps_the_two_apart(
    stack_with_prices, server, cache_clock
):
    prices = stack_with_prices.api(PricesApi)
    await prices.ensure_tables("Standard")
    standard_divine = prices.index().chaos_per_divine

    await prices.ensure_tables("Allflame")
    assert prices.tables_league == "Allflame"
    assert prices.status().loaded == len(PREFETCH)
    assert ninja_leagues(server) == {"Standard", "Allflame"}
    # The fixture server serves the same body for both, so the point being made is
    # about routing, not values: the Allflame index was fetched under Allflame.
    assert prices.index().chaos_per_divine == standard_divine

    # Switching back is free — each league's tables were cached on disk separately.
    before = len(server.to_host(NINJA))
    await prices.ensure_tables("Standard")
    assert len(server.to_host(NINJA)) == before
    assert prices.tables_league == "Standard"


# -- the surface -----------------------------------------------------------------


async def test_appraise_prints_the_league_and_where_it_came_from(full_stack, capsys):
    code = await cmd_appraise(
        full_stack.api(AppraisalApi),
        full_stack.api(PoeApi),
        full_stack.api(PricesApi),
        character=ALLFLAME_CHARACTER,
        refresh=False,
        colour=False,
    )
    out = capsys.readouterr().out
    assert code == 0
    assert "league:     Allflame (from the character)" in out
    assert "loaded for Allflame" in out
    # And it said what it was doing before spending several seconds on poe.ninja.
    assert "fetching from poe.ninja" in out


async def test_appraise_marks_an_overridden_league_as_an_override(full_stack, capsys):
    code = await cmd_appraise(
        full_stack.api(AppraisalApi),
        full_stack.api(PoeApi),
        full_stack.api(PricesApi),
        character=ALLFLAME_CHARACTER,
        refresh=False,
        colour=False,
        league="Standard",
    )
    out = capsys.readouterr().out
    assert code == 0
    assert "league:     Standard (OVERRIDE via --league" in out
    assert "not the character's league" in out


async def test_value_prints_the_league_and_where_it_came_from(
    stack_with_prices, capsys
):
    code = await cmd_value(
        stack_with_prices.api(PricesApi),
        stack_with_prices.api(PoeApi),
        character=ALLFLAME_CHARACTER,
        refresh=False,
        refresh_prices=False,
    )
    out = capsys.readouterr().out
    assert code == 0
    assert "league:     Allflame (from the character)" in out


async def test_value_marks_an_overridden_league_as_an_override(
    stack_with_prices, capsys
):
    code = await cmd_value(
        stack_with_prices.api(PricesApi),
        stack_with_prices.api(PoeApi),
        character=ALLFLAME_CHARACTER,
        refresh=False,
        refresh_prices=False,
        league="Standard",
    )
    out = capsys.readouterr().out
    assert code == 0
    assert "OVERRIDE via --league" in out


async def test_appraising_a_second_bag_spends_no_further_character_request(
    full_stack, server, capsys
):
    """The non-negotiable: the fix must not add a ``get-characters`` per appraise."""
    for _ in range(3):
        await cmd_appraise(
            full_stack.api(AppraisalApi),
            full_stack.api(PoeApi),
            full_stack.api(PricesApi),
            character=ALLFLAME_CHARACTER,
            refresh=False,
            colour=False,
        )
    capsys.readouterr()
    assert character_calls(server) == 1


def test_both_pricing_commands_accept_a_league_override():
    from cli.main import build_parser

    parser = build_parser()
    assert parser.parse_args(["value"]).league is None
    assert parser.parse_args(["value", "--league", "Standard"]).league == "Standard"
    assert parser.parse_args(["appraise", "--league", "Allflame"]).league == "Allflame"


async def test_the_json_surface_reports_the_league_and_its_source(full_stack):
    """Phase 5's panel reads these, so the fact has to survive serialization."""
    payload = await full_stack.methods.call(
        "appraisal.appraise_bag", ALLFLAME_CHARACTER
    )
    assert payload["league"] == "Allflame"
    assert payload["league_source"] == "character"
    assert payload["league_overridden"] is False

    priced = await full_stack.methods.call("prices.value_bag", ALLFLAME_CHARACTER)
    assert priced["league"] == "Allflame"
    assert priced["league_source"] == "character"
