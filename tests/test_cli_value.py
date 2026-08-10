"""`poedex value` — Phase 3's exit criterion.

The output is the deliverable, so it is asserted on directly: a total, per-item
values, and unpriceable called out separately rather than folded in as zero.
"""

from __future__ import annotations

import pytest

from cli.value import cmd_value, format_chaos, render_bag, render_total
from modules.poeapi.backend.api import PoeApi
from modules.prices.backend.api import PricesApi
from modules.prices.backend.ninja import PREFETCH
from tests.conftest import DISCOVERY_REQUESTS


@pytest.mark.parametrize(
    ("value", "text"),
    [
        # A currency table spans nine orders of magnitude; one format cannot serve it.
        (1387737.0, "1,387,737"),
        (897.7, "897.7"),
        (30.0, "30.0"),
        (2.19, "2.19"),
        (0.11, "0.11"),
        (0.0025, "0.0025"),
        (0.0, "0"),
    ],
)
def test_chaos_is_formatted_readably_at_every_scale(value, text):
    assert format_chaos(value) == text


async def test_it_prints_the_bag_with_values_and_a_total(priced_stack, capsys):
    code = await cmd_value(
        priced_stack.api(PricesApi),
        priced_stack.api(PoeApi),
        character=None,
        refresh=False,
        refresh_prices=False,
    )
    out = capsys.readouterr().out
    assert code == 0

    # Per-item values, with the stack and the line total kept apart.
    assert "Divine Orb" in out
    assert "Jeweller's Orb" in out
    assert "x2615" in out
    assert "Tabula Rasa" in out

    # A total, in both denominations.
    assert "total:" in out
    assert "chaos" in out and "divine" in out

    # Unpriceable, called out separately and by unit count.
    assert "unpriceable" in out
    assert "Veiled Scarab" in out
    # Units, not rows: 170 Veiled Scarab plus two unpriced rares.
    assert "3 item(s), 172 unit(s)" in out
    assert "not the same as worthless" in out

    # And the provenance of each number.
    assert "note" in out and "bulk" in out
    assert f"{len(PREFETCH)}/{len(PREFETCH)} loaded" in out
    # `value` never runs a trade search; the bulk-exchange line beside it is tier 1b
    # and is reported separately rather than folded into one "trade" number.
    assert "trade:      0 search(es)" in out
    assert "bulk-exchange request(s)" in out


async def test_it_reports_how_much_deduplication_saved(priced_stack, capsys):
    await cmd_value(
        priced_stack.api(PricesApi),
        priced_stack.api(PoeApi),
        character=None,
        refresh=False,
        refresh_prices=False,
    )
    out = capsys.readouterr().out
    line = next(entry for entry in out.splitlines() if entry.startswith("items:"))
    rows, lookups = (int(part) for part in line.replace("(", " ").split() if part.isdigit())
    assert lookups < rows


async def test_pricing_the_bag_spends_one_ggg_request_and_no_more(
    priced_stack, server, capsys
):
    """The whole point of poe.ninja being a different host."""
    before = len(server.to_host("www.pathofexile.com"))
    await cmd_value(
        priced_stack.api(PricesApi),
        priced_stack.api(PoeApi),
        character=None,
        refresh=False,
        refresh_prices=False,
    )
    capsys.readouterr()
    after = server.to_host("www.pathofexile.com")[before:]
    # get-characters resolves the default character and is cached hard; get-items is
    # the only other one. Nothing else may touch the *account* endpoints.
    account = [r for r in after if r.url.path.startswith("/character-window/")]
    assert {r.url.path for r in account} <= {
        "/character-window/get-items",
        "/character-window/get-characters",
    }
    # The trade host is the same host, and that is the whole reason this assertion
    # is about policies rather than hostnames: the tier-1b fallback below goes to
    # `trade-exchange-request-limit`, which is Ip-ruled and cannot spend the
    # account's `backend-item-request-limit` budget. What must never happen is a
    # tier-3 *search*, which is the thing SPEC §5.3 calls eager pricing.
    paths = [r.url.path for r in server.trade_requests()]
    assert not any(p.startswith(("/api/trade/search/", "/api/trade/fetch/")) for p in paths)
    assert all("cookie" not in {k.lower() for k in r.headers} for r in server.trade_requests())


async def test_refresh_prices_forces_a_table_refetch(priced_stack, server, capsys, clock):
    before = len(server.to_host("poe.ninja"))
    clock.advance(61)
    await cmd_value(
        priced_stack.api(PricesApi),
        priced_stack.api(PoeApi),
        character=None,
        refresh=False,
        refresh_prices=True,
    )
    capsys.readouterr()
    # A forced refresh re-runs discovery: "give me today's prices" and "and check
    # the table list is still right" are the same instruction from a user's side.
    assert len(server.to_host("poe.ninja")) == before + DISCOVERY_REQUESTS


async def test_without_tables_it_says_so_and_fails(stack_factory, registry, server, cache_clock,
                                                   capsys):
    from modules.prices.backend.module import PricesModule

    server.bag_fixture = "bag.json"
    server.ninja_status = 503
    module = PricesModule(clock=cache_clock, prefetch=False)
    await stack_factory(module)
    try:
        code = await cmd_value(
            registry.api(PricesApi),
            registry.api(PoeApi),
            character=None,
            refresh=False,
            refresh_prices=False,
        )
        captured = capsys.readouterr()
        assert code == 1
        assert "no price tables loaded" in captured.err
        # And it still lists the bag rather than printing nothing.
        assert "unpriceable" in captured.out
    finally:
        await registry.stop_all()


def test_an_empty_bag_renders_without_crashing():
    from modules.prices.backend.api import BagValuation

    empty = BagValuation([], league="Standard")
    assert "(nothing priced)" in render_bag(empty)
    assert "0 chaos" in render_total(empty)


def test_the_parser_accepts_the_command():
    from cli.main import build_parser

    args = build_parser().parse_args(["value", "--refresh-prices", "--character", "Someone"])
    assert args.command == "value"
    assert args.refresh_prices is True
    assert args.character == "Someone"
