"""`poedex appraise` — Phase 4's exit criterion.

The output *is* the deliverable, so it is asserted on directly and fairly harshly:
the four blocks, the subtotals, the sort order, the total, and the fact that the
unpriceable rows are called out with a unit count and excluded from the money rather
than folded in as zero.

One assertion here is worth more than the rest: `test_the_output_never_prints_zero_
chaos_for_an_item_of_unknown_value`. A gate-flagged rare has no price, and the first
draft of this renderer printed ``0c`` beside it — which reads as "worth nothing" next
to a line saying "look before you vendor". It is the kind of bug a passing test suite
misses entirely, because every number in it is correct.
"""

from __future__ import annotations

import re

import pytest

from cli.appraise import (
    GLYPH,
    cmd_appraise,
    render_appraisal,
    render_summary,
    use_colour,
)
from modules.appraisal.backend.api import (
    AppraisalApi,
    BagAppraisal,
    Strictness,
    Verdict,
)
from modules.poeapi.backend.api import PoeApi
from modules.prices.backend.api import PricesApi


async def run(stack, capsys, **kwargs):
    kwargs.setdefault("character", None)
    kwargs.setdefault("refresh", False)
    kwargs.setdefault("colour", False)
    code = await cmd_appraise(
        stack.api(AppraisalApi), stack.api(PoeApi), stack.api(PricesApi), **kwargs
    )
    return code, capsys.readouterr().out


def block(out: str, verdict: Verdict) -> list[str]:
    """The rows under one heading, up to the blank line that ends the block."""
    lines = out.splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith(verdict.value.upper()))
    rows = []
    for line in lines[start + 1 :]:
        if not line.strip():
            break
        rows.append(line)
    return rows


# -- the shape of the answer ---------------------------------------------------


async def test_it_prints_four_blocks_a_total_and_the_counts(appraised_stack, capsys):
    code, out = await run(appraised_stack, capsys, show_all=True)
    assert code == 0

    for verdict in Verdict:
        assert f"{verdict.value.upper():<12}" in out, verdict

    assert "bag total:" in out
    assert "chaos" in out and "divine" in out
    assert "verdicts:   keep " in out
    for verdict in Verdict:
        assert f"{verdict.value} " in out.split("verdicts:")[1]


async def test_the_blocks_come_in_the_order_the_player_acts_on_them(
    appraised_stack, capsys
):
    _, out = await run(appraised_stack, capsys, show_all=True)
    positions = [out.index(v.value.upper() + " ") for v in
                 (Verdict.KEEP, Verdict.CHECK, Verdict.UNPRICEABLE, Verdict.TRASH)]
    assert positions == sorted(positions)


async def test_the_most_valuable_item_is_the_first_row_of_the_keep_block(
    appraised_stack, capsys
):
    _, out = await run(appraised_stack, capsys)
    rows = block(out, Verdict.KEEP)
    assert "Choking Guilt" in rows[1], rows[:3]  # 2 x 9,874c, the biggest line
    assert "19,748" in rows[1]


async def test_gate_flagged_rares_sort_above_priced_rows_inside_check(
    appraised_stack, capsys
):
    """A `check` block mixes "worth 6 chaos" with "worth an unknown amount, and here
    is why you should look". The second is the one the player cannot work out alone,
    so it goes first."""
    _, out = await run(appraised_stack, capsys)
    rows = block(out, Verdict.CHECK)
    flagged = [i for i, row in enumerate(rows) if "—" in row]
    priced = [i for i, row in enumerate(rows) if "c   " in row]
    assert flagged and priced
    assert max(flagged) < min(priced)


async def test_each_verdict_has_its_own_glyph_so_the_output_survives_greyscale(
    appraised_stack, capsys
):
    """SPEC §5.4 asks for shape as well as colour. With colour off — which is what a
    pipe, a log and this test all get — the glyph is the only visual channel left."""
    _, out = await run(appraised_stack, capsys, show_all=True)
    assert len(set(GLYPH.values())) == 4
    for verdict in Verdict:
        assert any(row.strip().startswith(GLYPH[verdict]) for row in block(out, verdict)), verdict
    assert "\033[" not in out, "colour must be off when stdout is not a terminal"


# -- the number that must not be a lie -----------------------------------------


async def test_the_output_never_prints_zero_chaos_for_an_item_of_unknown_value(
    appraised_stack, capsys
):
    _, out = await run(appraised_stack, capsys, show_all=True)
    for verdict in Verdict:
        for row in block(out, verdict):
            if "no bulk price" in row or "poe.ninja index" in row or "6-link" in row:
                assert " 0c " not in row and not row.split()[-1].endswith(" 0c"), row
                assert "—" in row, row


async def test_unpriceable_is_a_block_with_a_unit_count_not_a_footnote(
    appraised_stack, capsys
):
    _, out = await run(appraised_stack, capsys)
    heading = next(
        line for line in out.splitlines() if line.startswith("UNPRICEABLE")
    )
    assert "row(s)" in heading and "unit(s)" in heading
    assert "not worthless" in heading
    rows = block(out, Verdict.UNPRICEABLE)
    assert any("Veiled Scarab" in row and "x23" in row for row in rows)


async def test_the_total_says_it_excludes_the_unpriceable_rows(appraised_stack, capsys):
    _, out = await run(appraised_stack, capsys)
    assert "excludes 2 unpriceable row(s), 27 unit(s)" in out
    assert "the total is a floor, not a value" in out


async def test_the_printed_total_is_the_sum_of_the_printed_blocks(appraised_stack, capsys):
    """Read back off the rendered text rather than off the object, because the bug
    this catches is a rendering one: a block subtotal that quietly omits a row, or a
    grand total computed from something other than what was shown."""
    _, out = await run(appraised_stack, capsys, show_all=True)

    def money(line: str) -> float:
        """The ``N item(s), Xc`` figure off a block heading."""
        return float(re.search(r"item\(s\), ([\d,.]+)c", line).group(1).replace(",", ""))

    subtotals = 0.0
    for verdict in (Verdict.KEEP, Verdict.CHECK, Verdict.TRASH):
        heading = next(
            line for line in out.splitlines() if line.startswith(verdict.value.upper())
        )
        subtotals += money(heading)

    printed = float(
        next(line for line in out.splitlines() if line.startswith("bag total:"))
        .split(":")[1]
        .split("chaos")[0]
        .strip()
        .replace(",", "")
    )
    assert printed == pytest.approx(subtotals, rel=1e-3)
    assert printed > 0


async def test_the_worn_headhunter_never_appears(appraised_stack, capsys):
    """8,977c of equipped belt. If the bag filter breaks, the total moves visibly."""
    _, out = await run(appraised_stack, capsys, show_all=True)
    assert "Headhunter" not in out


# -- the knobs -----------------------------------------------------------------


async def test_trash_is_collapsed_by_default_and_says_how_much_it_hid(
    appraised_stack, capsys
):
    _, collapsed = await run(appraised_stack, capsys)
    _, expanded = await run(appraised_stack, capsys, show_all=True)
    assert "rows hidden — pass --all" in collapsed
    assert "rows hidden" not in expanded
    assert "Orb of Alchemy" in expanded and "Orb of Alchemy" not in collapsed
    # Collapsed or not, the subtotal is on the heading either way.
    heading = next(line for line in collapsed.splitlines() if line.startswith("TRASH"))
    assert "item(s)" in heading and "c " in heading


async def test_strictness_changes_the_printed_verdicts(appraised_stack, capsys):
    _, generous = await run(appraised_stack, capsys, strictness="generous", show_all=True)
    _, strict = await run(appraised_stack, capsys, strictness="strict", show_all=True)

    assert "generous (bag)" in generous and "strict (stash)" in strict
    # `Loath Grip` is the divergence: good resistances, worthless base.
    assert any("Loath Grip" in row for row in block(generous, Verdict.CHECK))
    assert any("Loath Grip" in row for row in block(strict, Verdict.TRASH))


async def test_the_threshold_flag_changes_the_headline_and_the_blocks(
    appraised_stack, capsys
):
    _, low = await run(appraised_stack, capsys, threshold=1.0, show_all=True)
    _, high = await run(appraised_stack, capsys, threshold=5000.0, show_all=True)
    assert "keep at:    1.00 chaos" in low
    assert "keep at:    5,000 chaos" in high
    assert _count(low, Verdict.KEEP) > _count(high, Verdict.KEEP)
    # ...and neither moves a single unpriceable row.
    assert _count(low, Verdict.UNPRICEABLE) == _count(high, Verdict.UNPRICEABLE) == 2


def _count(out: str, verdict: Verdict) -> int:
    heading = next(line for line in out.splitlines() if line.startswith(verdict.value.upper()))
    return int(heading.split()[1])


# -- honest failure ------------------------------------------------------------


async def test_it_reports_no_trade_requests_were_made(appraised_stack, server, capsys):
    before = len(server.trade_requests())
    _, out = await run(appraised_stack, capsys)
    assert "trade:      0 request(s)" in out
    assert len(server.trade_requests()) == before


async def test_without_price_tables_it_says_so_and_still_prints_the_bag(
    stack_factory, registry, server, cache_clock, capsys
):
    from modules.appraisal.backend.module import AppraisalModule
    from modules.prices.backend.module import PricesModule

    server.bag_fixture = "loot-bag.json"
    server.ninja_status = 503
    await stack_factory(PricesModule(clock=cache_clock, prefetch=False), AppraisalModule())
    try:
        code = await cmd_appraise(
            registry.api(AppraisalApi),
            registry.api(PoeApi),
            registry.api(PricesApi),
            character=None,
            refresh=False,
            colour=False,
        )
        captured = capsys.readouterr()
        assert code == 1
        assert "no price tables loaded" in captured.err
        assert "UNPRICEABLE" in captured.out
        assert "Divine Orb" in captured.out
    finally:
        await registry.stop_all()


def test_an_empty_bag_renders_without_crashing():
    empty = BagAppraisal(
        [], league="Standard", threshold_chaos=20.0, strictness=Strictness.GENEROUS
    )
    assert "the bag is empty" in render_appraisal(empty)
    assert "0 chaos" in render_summary(empty)
    assert "keep 0" in render_summary(empty)


def test_colour_is_off_unless_stdout_is_a_terminal(monkeypatch):
    class Tty:
        @staticmethod
        def isatty() -> bool:
            return True

    monkeypatch.delenv("NO_COLOR", raising=False)
    assert use_colour(Tty()) is True
    monkeypatch.setenv("NO_COLOR", "1")
    assert use_colour(Tty()) is False


def test_the_parser_accepts_the_command():
    from cli.main import build_parser

    args = build_parser().parse_args(
        ["appraise", "--strictness", "strict", "--threshold", "150", "--all"]
    )
    assert args.command == "appraise"
    assert args.strictness == "strict"
    assert args.threshold == 150.0
    assert args.all is True
