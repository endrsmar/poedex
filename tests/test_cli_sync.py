"""`poedex sync` and `poedex selftest freshness`, driven offline.

Phase 2's exit criterion is "`poedex sync` prints the normalized bag". These tests
are how that is checked without spending rate-limit budget: the same `PoeApi` the
CLI would get, backed by the same fixture-driven mock transport as everything else.
"""

from __future__ import annotations

import pytest

from cli import main as cli
from cli.selftest import MIN_INTERVAL, cmd_freshness
from cli.sync import cmd_sync, render_freshness, render_item
from modules.poeapi.backend.api import PoeApi, Source
from tests.conftest import Server

# -- argument parsing ----------------------------------------------------------


def test_sync_is_a_command():
    args = cli.build_parser().parse_args(["sync"])
    assert args.command == "sync"
    assert args.character is None
    assert args.force is False


def test_selftest_freshness_is_a_command():
    args = cli.build_parser().parse_args(["selftest", "freshness", "--seconds", "30"])
    assert (args.command, args.selftest_command) == ("selftest", "freshness")
    assert args.seconds == 30


def test_limits_is_a_command():
    assert cli.build_parser().parse_args(["limits"]).command == "limits"


def test_no_command_accepts_a_credential_value():
    """Phase 1's rule, re-checked now that there are more commands."""
    parser = cli.build_parser()
    for action in parser._subparsers._group_actions[0].choices.values():
        for option in action._actions:
            assert "poesessid" not in " ".join(option.option_strings).lower()


# -- rendering -----------------------------------------------------------------


async def test_sync_prints_the_normalized_bag(api: PoeApi, capsys: pytest.CaptureFixture):
    code = await cmd_sync(api, character="PlaceholderWarden", refresh=True, equipment=False)
    out = capsys.readouterr().out
    assert code == 0
    assert "character:  PlaceholderWarden" in out
    assert "freshness:  fresh" in out
    assert "bag —" in out
    # Every category the fixture contains should be visible in the tally.
    for category in ("currency", "card", "map", "fragment", "gem", "jewel", "accessory"):
        assert category in out
    # And the limiter's state, because "why is it not refreshing" is the next question.
    assert "backend-item-request-limit" in out


async def test_sync_can_include_worn_gear(api: PoeApi, capsys: pytest.CaptureFixture):
    await cmd_sync(api, character="PlaceholderWarden", refresh=True, equipment=True)
    out = capsys.readouterr().out
    assert "equipment —" in out
    assert "Placeholder Shroud" in out  # the body armour, rendered by name


async def test_sync_reports_stale_data_as_a_failure(
    api: PoeApi, server: Server, capsys: pytest.CaptureFixture
):
    """Exit code 1 and a line on stderr, so a script cannot mistake cache for fresh."""
    await api.get_items("PlaceholderWarden")
    server.status = 500
    code = await cmd_sync(api, character="PlaceholderWarden", refresh=True, equipment=False)
    captured = capsys.readouterr()
    assert code == 1
    assert "STALE" in captured.out
    assert "cached data" in captured.err


async def test_the_rendered_item_line_carries_what_phase_3_will_price(api: PoeApi):
    result = await api.get_items("PlaceholderWarden")
    amulet = next(i for i in result.by_source(Source.BAG) if i.base_type == "Onyx Amulet")
    line = render_item(amulet)
    assert "unique" in line
    assert "note=\"~price 3 divine\"" in line
    assert "elder,shaper" in line
    assert "C" in line  # corrupted


async def test_freshness_is_reported_in_words(api: PoeApi):
    result = await api.get_items("PlaceholderWarden")
    assert render_freshness(result).startswith("fresh, fetched ")


# -- the freshness self-test ---------------------------------------------------


async def test_the_freshness_procedure_is_printed_before_polling(
    api: PoeApi, capsys: pytest.CaptureFixture
):
    """It is useless without the instructions; a human has to act on them."""
    await cmd_freshness(api, character="PlaceholderWarden", interval=0.0, seconds=0.0)
    out = capsys.readouterr().out
    assert "PICK UP AN ITEM" in out
    assert "PORTAL TO YOUR HIDEOUT" in out
    assert "SPEC §4.3" in out
    assert "spends real rate-limit budget" in out.lower() or "Cost:" in out


async def test_the_poll_interval_has_a_floor(api: PoeApi, capsys: pytest.CaptureFixture):
    """5s is already 12 requests a minute against a 30:60 bucket."""
    await cmd_freshness(api, character="PlaceholderWarden", interval=0.1, seconds=0.0)
    assert f"polling every {MIN_INTERVAL:.0f}s" in capsys.readouterr().out


async def test_a_refusal_is_printed_rather_than_slept_through(
    api: PoeApi, server: Server, capsys: pytest.CaptureFixture
):
    server.header_file = "headers-items-restricted.json"
    await api.get_items("PlaceholderWarden")  # learns the restricted policy
    await cmd_freshness(api, character="PlaceholderWarden", interval=0.0, seconds=0.001)
    out = capsys.readouterr().out
    assert "stale" in out or "REFUSED" in out
