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


async def appraised(stack, **kwargs):
    """The same appraisal the CLI just printed, as an object.

    The fixtures make this deterministic — same bag, same tables, same scripted
    trade responses — so asserting on the object and on the text together is
    checking the renderer rather than re-running the engine and hoping.
    """
    from modules.poeapi.backend.api import Source

    bag = await stack.api(PoeApi).get_items()
    return await stack.api(AppraisalApi).appraise(bag.by_source(Source.BAG), **kwargs)


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
    # rows[0] since Phase 9: nothing in `keep` is highlighted any more, so the
    # gate-hits-first tie-break no longer floats an unpriced row above the money.
    assert "Choking Guilt" in rows[0], rows[:3]  # 2 x 9,874c, the biggest line
    assert "19,748" in rows[0]


async def test_highlighted_rares_sort_above_priced_rows_inside_check(
    appraised_stack, capsys
):
    """A `check` block mixes "worth 6 chaos" with "worth asking about". The second is
    the one the player cannot work out alone, so it goes first."""
    _, out = await run(appraised_stack, capsys)
    result = await appraised(appraised_stack)
    rows = [item for item in result.ranked() if item.verdict is Verdict.CHECK]
    flagged = [i for i, item in enumerate(rows) if item.highlighted]
    plain = [i for i, item in enumerate(rows) if not item.highlighted]
    assert flagged and plain
    assert max(flagged) < min(plain)
    # ...and the rendering preserves that order.
    printed = block(out, Verdict.CHECK)
    assert len(printed) == len(rows)
    assert rows[0].name[:12] in printed[0]


async def test_each_verdict_has_its_own_glyph_so_the_output_survives_greyscale(
    appraised_stack, capsys
):
    """SPEC §5.4 asks for shape as well as colour. With colour off — which is what a
    pipe, a log and this test all get — the glyph is the only visual channel left."""
    _, out = await run(appraised_stack, capsys, show_all=True)
    assert len(set(GLYPH.values())) == len(Verdict)
    for verdict in Verdict:
        assert any(row.strip().startswith(GLYPH[verdict]) for row in block(out, verdict)), verdict
    assert "\033[" not in out, "colour must be off when stdout is not a terminal"


# -- the number that must not be a lie -----------------------------------------


async def test_the_output_never_prints_zero_chaos_for_an_item_of_unknown_value(
    appraised_stack, capsys
):
    _, out = await run(appraised_stack, capsys, show_all=True)
    unknown = ("no bulk price", "poe.ninja index", "pricing…", "bulk-exchange offers")
    seen = 0
    for verdict in Verdict:
        for row in block(out, verdict):
            if not any(phrase in row for phrase in unknown):
                continue
            seen += 1
            # No number at all, and never a zero. The two ways of having no number
            # are distinct: "—" is "we have none", "⋯" is "we asked and it has not
            # arrived yet".
            assert " 0c " not in row and not row.rstrip().endswith(" 0c"), row
            assert "—" in row or "⋯" in row, row
    assert seen, "the bag no longer exercises the unknown-value path at all"


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
        # `≥`, because a highlighted rare has no price and nobody has asked yet.
        .lstrip("≥ ")
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
    # `Soul Bind` is the divergence: T2 of 10 life on a Siege Helmet. Near the top
    # of a long ladder, which is a generous-only signal and could not be expressed
    # at all while one threshold per mod group was the only tool.
    assert any("Soul Bind" in row for row in block(generous, Verdict.CHECK))
    assert any("Soul Bind" in row for row in block(strict, Verdict.TRASH))


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


async def test_it_reports_that_it_spent_nothing_and_says_how_to_ask(
    appraised_stack, server, capsys
):
    """The line used to carry a number that could be non-zero. It cannot any more —
    an appraise makes no trade request at all — so the line's job is now to say that
    and to point at the command that does."""
    _, out = await run(appraised_stack, capsys)
    assert "trade:      0 request(s)" in out
    assert "poedex price <uid>" in out
    assert not [r for r in server.trade_requests() if _is_tier3(r.url.path)]


async def test_the_summary_names_the_rows_the_total_leaves_out(appraised_stack, capsys):
    """The honest replacement for the eager pass. The old output made this hole small
    by spending requests on it; the new one says the hole is there."""
    _, out = await run(appraised_stack, capsys)
    result = await appraised(appraised_stack)
    assert result.unchecked
    assert f"highlighted:{len(result.unchecked):>3} item(s) worth asking about" in out
    assert "unknown, not zero" in out
    assert "bag total:  ≥" in out


def _is_tier3(path: str) -> bool:
    return path.startswith(
        ("/api/trade/search/", "/api/trade/fetch/", "/api/trade/data/stats")
    )


async def test_without_price_tables_it_says_so_and_still_prints_the_bag(
    stack_factory, registry, server, cache_clock, capsys
):
    from modules.appraisal.backend.module import AppraisalModule
    from modules.moddb.backend.module import ModDbModule
    from modules.prices.backend.module import PricesModule

    server.bag_fixture = "loot-bag.json"
    server.ninja_status = 503
    await stack_factory(
        PricesModule(clock=cache_clock, prefetch=False), ModDbModule(), AppraisalModule()
    )
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


# -- bug 2 and bug 3, in the renderer ------------------------------------------


def _row(out: str, name: str) -> str:
    return next(line for line in out.splitlines() if name in line)


async def test_bug2_the_three_ways_of_having_no_number_still_render_apart(capsys):
    """The renderer half of bug 2, now that no appraise produces these states itself.

    A tier-3 query can still be outstanding (``⋯``), finished-and-empty (``∅``) or
    simply never asked (``—``), because a manual check writes one of them onto a
    valuation. Collapsing them is what drew ``pricing…`` forever beside two searches
    that had already come back with nothing, so the distinction is asserted directly
    on the row renderer rather than through a path that can no longer reach it.
    """
    from cli.appraise import render_row
    from modules.appraisal.backend.api import GateResult, ItemVerdict
    from modules.prices.backend.api import Tier3, Valuation

    def row(tier3: Tier3) -> str:
        valuation = Valuation(uid="u", name="Rare Thing", base_type="Coral Ring",
                              category="accessory", stack_size=1, price=None)
        valuation.tier3 = tier3
        return render_row(
            ItemVerdict(
                uid="u",
                name="Rare Thing",
                base_type="Coral Ring",
                category="accessory",
                rarity="rare",
                verdict=Verdict.CHECK,
                valuation=valuation,
                gate=GateResult(),
                reason="worth asking about",
            ),
            colour=False,
        )

    assert "⋯" in row(Tier3.PENDING)
    assert "∅" in row(Tier3.NO_LISTINGS) and "⋯" not in row(Tier3.NO_LISTINGS)
    assert "—" in row(Tier3.NONE) and "∅" not in row(Tier3.NONE)
    assert " 0c" not in row(Tier3.NONE)


async def test_bug3_a_quest_item_never_appears_under_an_instruction_to_sell_it(
    appraised_stack, capsys
):
    """`Book of Skill` is in the fixture bag. It cannot be traded and cannot be
    vendored, and the first live appraisal filed one under TRASH — headline
    "vendor"."""
    _code, out = await run(appraised_stack, capsys, show_all=True)

    assert "Book of Skill" in out
    assert "Book of Skill" not in "\n".join(block(out, Verdict.TRASH))
    assert "Book of Skill" not in "\n".join(block(out, Verdict.UNPRICEABLE))
    assert "Book of Skill" in "\n".join(block(out, Verdict.NOT_LOOT))

    # The block it *is* in gives no instruction, and its own line says why.
    heading = next(line for line in out.splitlines() if line.startswith("NOT_LOOT"))
    assert "vendor" in heading and "nothing to vendor" in heading
    line = _row(out, "Book of Skill")
    assert "cannot be traded or vendored" in line
    # No money column at all: a `0c`, or even a `—`, invites the value question back.
    assert "0c" not in line


async def test_bug3_the_not_loot_row_is_still_on_the_screen(appraised_stack, capsys):
    """Excluding it from the verdict would have been the other way to fix this, and
    it would have made the bag grid — which SPEC §6.3 calls a *map* — incomplete."""
    result = await appraised(appraised_stack)
    quest = next(row for row in result.items if row.name == "Book of Skill")
    assert quest.slot is not None, "the row still knows where it is in the bag"
    assert quest.verdict is Verdict.NOT_LOOT
    assert quest.to_json()["verdict"] == "not_loot"
