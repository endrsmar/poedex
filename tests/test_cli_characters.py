"""`poedex characters` — the fastest way to see this class of problem.

The bug it diagnoses produced no error, no warning and no wrong-looking output. The
only thing that would have exposed it in one command is a list of the account's
characters with their leagues next to the *reason* one of them is being read, which
is what this command is. Offline, against the recorded roster.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from cli import main as cli
from cli.characters import cmd_characters, describe_character, render, render_last_login
from modules.poeapi.backend.api import PoeApi
from tests.conftest import Server, payload

NOW = datetime(2026, 8, 12, 12, 0, tzinfo=UTC)


def test_characters_is_a_command():
    args = cli.build_parser().parse_args(["characters"])
    assert args.command == "characters"
    assert args.force is False


# -- the report ------------------------------------------------------------------


async def test_it_lists_every_character_with_its_league(
    api: PoeApi, capsys: pytest.CaptureFixture
):
    code = await cmd_characters(api)
    out = capsys.readouterr().out
    assert code == 0
    for name in ("PlaceholderWarden", "PlaceholderHierophant", "PlaceholderJuggernaut"):
        assert name in out
    # The league is the disambiguator and the reason the wrong pick was expensive.
    for league in ("Standard", "Allflame", "Solo Self-Found"):
        assert league in out
    assert "Hierophant" in out and "97" in out


async def test_it_names_the_winner_and_the_rule_it_won_under(
    api: PoeApi, capsys: pytest.CaptureFixture
):
    await cmd_characters(api)
    out = capsys.readouterr().out
    assert "reading: PlaceholderWarden" in out
    assert "most recent lastLoginTime" in out


async def test_the_chosen_row_is_marked(api: PoeApi, capsys: pytest.CaptureFixture):
    await cmd_characters(api)
    lines = capsys.readouterr().out.splitlines()
    marked = [line for line in lines if line.startswith(">")]
    assert len(marked) == 1
    assert "PlaceholderWarden" in marked[0]


async def test_a_guess_says_so_and_exits_non_zero(
    api: PoeApi, server: Server, capsys: pytest.CaptureFixture
):
    """The report's own state: nothing marked current, no timestamps to read.

    Non-zero because a script should be able to notice it without parsing prose —
    the command did what was asked, and what it found is that nobody has said.
    """
    server.characters = payload("get-characters-unplayed.json")
    code = await cmd_characters(api)
    out = capsys.readouterr().out
    assert code == 1
    assert "GUESSED" in out
    assert "Nothing about this pick is an observation" in out
    assert "poedex config set poeapi.character" in out


async def test_a_pinned_character_says_who_you_last_played(
    api: PoeApi, capsys: pytest.CaptureFixture
):
    await api.set_character("PlaceholderHierophant")
    await cmd_characters(api)
    out = capsys.readouterr().out
    assert "the poeapi.character setting is pinned to it" in out
    assert "you last played PlaceholderWarden" in out
    assert "poedex config unset poeapi.character" in out


async def test_it_costs_one_cached_request(api: PoeApi, server: Server):
    await cmd_characters(api)
    before = len([r for r in server.requests if "get-characters" in r.url.path])
    await cmd_characters(api)
    after = len([r for r in server.requests if "get-characters" in r.url.path])
    assert after == before == 1


async def test_an_empty_roster_is_reported_rather_than_crashed(
    api: PoeApi, server: Server, capsys: pytest.CaptureFixture
):
    server.characters = []
    code = await cmd_characters(api)
    out = capsys.readouterr().out
    assert code == 1
    assert "this account has no characters" in out


# -- the pieces ------------------------------------------------------------------


def test_last_played_is_relative_because_nobody_remembers_a_timestamp():
    assert render_last_login(None) == "never played"
    assert "(today)" in render_last_login(datetime(2026, 8, 12, 9, 0, tzinfo=UTC), now=NOW)
    assert "(yesterday)" in render_last_login(datetime(2026, 8, 11, 9, 0, tzinfo=UTC), now=NOW)
    assert "(9 days ago)" in render_last_login(datetime(2026, 8, 3, 9, 0, tzinfo=UTC), now=NOW)
    assert "months ago" in render_last_login(datetime(2025, 8, 3, 9, 0, tzinfo=UTC), now=NOW)
    # The absolute form is still there — a relative one alone cannot be checked
    # against anything.
    assert "2026-08-11 09:00" in render_last_login(
        datetime(2026, 8, 11, 9, 0, tzinfo=UTC), now=NOW
    )


async def test_no_header_prints_a_bare_character_name(api: PoeApi):
    """The property that closes the bug, stated as a property.

    Every CLI header goes through `describe_character`, and it cannot return a name
    on its own for any bag `poeapi` produces.
    """
    bag = await api.get_items()
    described = describe_character(bag)
    assert described != bag.character
    assert "most recently played" in described


async def test_render_is_pure_enough_to_read_as_a_sentence(api: PoeApi):
    selection = await api.character_choice()
    text = render(selection, now=NOW)
    assert "LAST PLAYED" in text
    assert text.strip().endswith(".")
