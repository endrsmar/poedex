"""`poedex stash` — the shippable increment of Phase 10.

The CLI is the priority the phase brief set: a working backend plus these four
commands is a usable tool with no panel in front of it. So the output is asserted on
directly, and hardest where it would be easiest to lie:

* a tab nobody has read prints ``not read yet`` and a dash, never ``0c``;
* a map tab prints ``not supported yet``, never ``0c``;
* the crawl prints its cost and **stops** unless ``--yes``.
"""

from __future__ import annotations

import pytest

from cli.main import build_parser
from cli.stash import (
    cmd_stash_crawl,
    cmd_stash_list,
    cmd_stash_plan,
    cmd_stash_tab,
)
from modules.appraisal.backend.api import AppraisalApi
from modules.poeapi.backend.api import PoeApi
from modules.prices.backend.api import PricesApi
from tests.conftest import Server, stash_requests


def flat(text: str) -> str:
    """The report with its line wrapping undone.

    Sentences in this report hang-indent across lines, and an assertion that broke
    when a sentence moved by one word would be testing the terminal width rather than
    what the tool says.
    """
    return " ".join(text.split())


async def run_list(stack, capsys, **kwargs):
    code = await cmd_stash_list(
        stack.api(AppraisalApi), stack.api(PricesApi), league="Standard", **kwargs
    )
    return code, capsys.readouterr().out


async def run_tab(stack, capsys, index: int, **kwargs):
    kwargs.setdefault("colour", False)
    code = await cmd_stash_tab(
        stack.api(AppraisalApi),
        stack.api(PricesApi),
        index,
        league="Standard",
        **kwargs,
    )
    return code, capsys.readouterr().out


# -- the tab list ---------------------------------------------------------------


async def test_the_list_shows_every_tab_with_its_kind_shape_and_freshness(
    stash_stack, capsys
):
    code, out = await run_list(stash_stack, capsys)
    assert code == 0
    assert "quad" in out and "24x24" in out
    assert "currency" in out and "special" in out
    assert "not read yet" in out
    assert "R remove-only" in out


async def test_the_list_spends_no_item_requests(stash_stack, capsys, server: Server):
    """One tab-list read, and nothing else. 117 tabs cost the same as 8."""
    await run_list(stash_stack, capsys)
    assert stash_requests(server) == [0]  # the tab list itself


async def test_an_unread_tab_prints_a_dash_and_never_a_zero(stash_stack, capsys):
    """`0c` next to a tab nobody has opened is the failure mode of this whole phase.

    The row would look identical to a genuinely empty tab, and the reader would stop
    walking to the stash on the strength of a number nobody computed.
    """
    _, out = await run_list(stash_stack, capsys)
    unread = [line for line in out.splitlines() if "not read yet" in line]
    assert unread
    for line in unread:
        assert "0c" not in line
        assert "—" in line


async def test_a_map_tab_says_not_supported_rather_than_zero(stash_stack, capsys):
    _, out = await run_list(stash_stack, capsys)
    row = next(line for line in out.splitlines() if "not supported yet" in line)
    assert "0c" not in row
    assert "cannot be read" in flat(out)


async def test_the_list_says_what_a_full_refresh_would_cost(stash_stack, capsys):
    _, out = await run_list(stash_stack, capsys)
    assert "to refresh: 7 tab request(s)" in out
    assert "pause your inventory syncing" in flat(out)


async def test_the_total_is_a_floor_while_tabs_are_unread(stash_stack, capsys):
    _, out = await run_list(stash_stack, capsys)
    assert "stash:      ≥" in out
    assert "have never been read" in flat(out)
    assert "unknown, not zero" in flat(out)


# -- one tab --------------------------------------------------------------------


async def test_reading_one_tab_costs_one_request(stash_stack, capsys, server: Server):
    """SPEC §6.6's primary path: lazy per-tab fetch on open, one request, ~1s."""
    before = len(stash_requests(server))
    code, out = await run_tab(stash_stack, capsys, 1)
    assert code == 0
    # One for the tab, plus the tab list the digest needed to know what tab 1 is.
    assert len(stash_requests(server)) - before <= 2
    assert stash_requests(server).count(1) == 1
    assert "Corpse Guardian" in out
    assert "highlight:  strict (stash)" in out


async def test_a_tab_prints_its_layout_and_its_composition(stash_stack, capsys):
    _, out = await run_tab(stash_stack, capsys, 2)
    assert "layout:     24x24" in out
    assert "bulk" in out


async def test_a_remove_only_tab_says_its_copy_is_permanent(stash_stack, capsys):
    _, out = await run_tab(stash_stack, capsys, 3)
    assert "(remove-only)" in out
    assert "Veiled Scarab" in out
    # A removed item is unpriceable, not trash, and the units are named.
    assert "unpriceable" in out.lower()
    assert "170" in out


async def test_a_map_tab_prints_the_reason_and_exits_nonzero(stash_stack, capsys):
    code, out = await run_tab(stash_stack, capsys, 4)
    assert code == 1
    assert "not supported yet" in out
    assert "0c" not in out


async def test_the_strictness_flag_changes_what_is_highlighted(stash_stack, capsys):
    _, strict = await run_tab(stash_stack, capsys, 1)
    _, generous = await run_tab(stash_stack, capsys, 1, strictness="generous")
    assert "generous (stash)" in generous
    assert strict.count("check") <= generous.count("check")


async def test_the_tab_output_says_how_to_ask_about_one_item(stash_stack, capsys):
    _, out = await run_tab(stash_stack, capsys, 1)
    assert "poedex price <uid> --tab 1" in out


# -- plan and crawl -------------------------------------------------------------


async def test_plan_states_the_cost_and_spends_nothing(stash_stack, capsys, server: Server):
    code = await cmd_stash_plan(
        stash_stack.api(AppraisalApi), stash_stack.api(PricesApi), league="Standard"
    )
    out = capsys.readouterr().out
    assert code == 0
    assert "nothing crawls by itself" in flat(out)
    # And never a bare league name — `stash list` already said where its league came
    # from and `stash plan` used to print the word on its own beside it.
    assert "OVERRIDE" in out
    assert stash_requests(server) == [0]


async def test_a_crawl_refuses_without_yes(stash_stack, capsys, server: Server):
    """The refusal *is* the feature. A crawl is the tool declining to sync the bag
    for as long as it runs, and that is a thing to be asked for."""
    code = await cmd_stash_crawl(
        stash_stack.api(PoeApi), stash_stack.api(AppraisalApi), league="Standard"
    )
    out = capsys.readouterr().out
    assert code == 1
    assert "nothing was fetched" in flat(out)
    assert "--yes" in out
    assert stash_requests(server) == [0]


async def test_a_crawl_with_yes_walks_the_tabs_and_reports_what_it_spent(
    stash_stack, capsys, server: Server
):
    code = await cmd_stash_crawl(
        stash_stack.api(PoeApi), stash_stack.api(AppraisalApi), league="Standard", yes=True
    )
    out = capsys.readouterr().out
    assert code == 0
    assert "[1/7]" in out and "[7/7]" in out
    assert "request(s) spent" in out
    # The map tab is not walked at all.
    assert 4 not in stash_requests(server)


async def test_a_resumed_crawl_says_how_much_was_already_done(
    stash_stack, capsys, server: Server
):
    await cmd_stash_crawl(
        stash_stack.api(PoeApi),
        stash_stack.api(AppraisalApi),
        league="Standard",
        yes=True,
        limit=3,
    )
    capsys.readouterr()
    await cmd_stash_crawl(
        stash_stack.api(PoeApi), stash_stack.api(AppraisalApi), league="Standard"
    )
    out = capsys.readouterr().out
    assert "resume:     3 tab(s) already done" in out


async def test_a_second_crawl_reports_the_cached_tabs_as_cached(
    stash_stack, capsys, server: Server
):
    """The remove-only rule, visible in the output rather than only in a docstring."""
    await cmd_stash_crawl(
        stash_stack.api(PoeApi), stash_stack.api(AppraisalApi), league="Standard", yes=True
    )
    capsys.readouterr()
    await cmd_stash_crawl(
        stash_stack.api(PoeApi),
        stash_stack.api(AppraisalApi),
        league="Standard",
        yes=True,
        resume=False,
    )
    out = capsys.readouterr().out
    assert "cached" in out
    assert out.count("cached") >= 2  # both remove-only tabs


# -- the parser -----------------------------------------------------------------


def test_the_parser_accepts_every_form():
    parser = build_parser()
    assert parser.parse_args(["stash"]).action == "list"
    tab = parser.parse_args(["stash", "tab", "3"])
    assert (tab.action, tab.index) == ("tab", 3)
    assert parser.parse_args(["stash", "crawl", "--yes"]).yes is True
    assert parser.parse_args(["stash", "plan"]).action == "plan"


def test_the_league_flag_survives_on_either_side_of_the_action():
    """Not an aesthetic choice: argparse subparsers re-apply their own defaults over
    the parent namespace, so `stash --league X tab 3` would have dropped the league.
    A silently wrong league is the most expensive bug this project has had."""
    parser = build_parser()
    assert parser.parse_args(["stash", "--league", "Standard", "tab", "3"]).league == "Standard"
    assert parser.parse_args(["stash", "tab", "3", "--league", "Standard"]).league == "Standard"


def test_price_accepts_a_tab():
    parser = build_parser()
    assert parser.parse_args(["price", "abc", "--tab", "2"]).tab == 2
    assert parser.parse_args(["price", "abc"]).tab is None


async def test_stash_tab_without_an_index_is_refused(capsys):
    from cli.main import main

    with pytest.raises(SystemExit):
        main(["stash", "tab", "not-a-number"])
