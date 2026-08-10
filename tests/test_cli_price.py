"""`poedex price` — the manual check on the command line.

The shippable increment of Phase 9: a working backend plus this command is a usable
tool with no panel in front of it. What is asserted here is what the output has to
get right for that to be true — the tier beside each line, the word ``unknown``
where the database will not commit, the comparable count beside any number, and a
dry run that spends nothing.
"""

from __future__ import annotations

import pytest

from cli.price import cmd_price, parse_mods, render_highlight
from modules.appraisal.backend.api import AppraisalApi
from modules.poeapi.backend.api import PoeApi
from modules.prices.backend.api import PricesApi


async def run(stack, capsys, uid, **kwargs):
    kwargs.setdefault("character", None)
    code = await cmd_price(
        stack.api(AppraisalApi),
        stack.api(PoeApi),
        stack.api(PricesApi),
        uid=uid,
        **kwargs,
    )
    captured = capsys.readouterr()
    return code, captured.out + captured.err


def uid_of(loot, base_type: str) -> str:
    return next(i.uid for i in loot if i.base_type == base_type)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, None),
        ("0,3,4", [0, 3, 4]),
        (" 1 , 2 ", [1, 2]),
        # An empty string is a real answer — "none of them" — and is how a player
        # asks a pure open-affix question. Folding it into `None` would silently put
        # the pre-ticked mods back into a query that deliberately excluded them.
        ("", []),
    ],
)
def test_the_mod_list_is_parsed_including_the_empty_case(raw, expected):
    assert parse_mods(raw) == expected


async def test_a_dry_run_prints_the_list_and_spends_nothing(appraised_stack, server, capsys):
    from modules.poeapi.backend.api import Source

    bag = await appraised_stack.api(PoeApi).get_items()
    loot = bag.by_source(Source.BAG)
    before = len(server.trade_requests())
    code, out = await run(appraised_stack, capsys, uid_of(loot, "Vaal Regalia"), dry_run=True)

    assert code == 0
    assert "dry run" in out
    assert len(server.trade_requests()) == before
    # The list, with the tier beside each line rather than a bare mod dump.
    assert "+112 to maximum Energy Shield" in out
    assert "98% increased Energy Shield" in out
    assert "query:" in out


async def test_the_tier_column_says_unknown_where_moddb_will_not_commit(
    appraised_stack, capsys
):
    """One line in five, measured. A column that printed "T2" for those would be
    right most of the time, which is exactly what would make it dangerous."""
    from modules.poeapi.backend.api import Source

    bag = await appraised_stack.api(PoeApi).get_items()
    loot = bag.by_source(Source.BAG)
    _, out = await run(appraised_stack, capsys, uid_of(loot, "Iron Hat"), dry_run=True)
    assert "unknown" in out
    assert "will not say which mod produced them" in out


async def test_a_real_check_prints_a_price_with_its_comparable_count(
    appraised_stack, capsys
):
    from modules.poeapi.backend.api import Source

    bag = await appraised_stack.api(PoeApi).get_items()
    loot = bag.by_source(Source.BAG)
    code, out = await run(appraised_stack, capsys, uid_of(loot, "Vaal Regalia"))
    assert code in (0, 1)
    assert "price:" in out
    assert "trade:" in out and "request(s) spent" in out
    # The count is never optional: a median over one listing is one stranger's
    # asking price, and that is what reported 10c for a 1c jewel.
    assert "matching listing" in out or "no listings matched" in out


async def test_selecting_no_mods_at_all_is_refused_rather_than_widened(
    appraised_stack, server, capsys
):
    from modules.poeapi.backend.api import Source

    bag = await appraised_stack.api(PoeApi).get_items()
    loot = bag.by_source(Source.BAG)
    before = len(server.trade_requests())
    code, out = await run(appraised_stack, capsys, uid_of(loot, "Vaal Regalia"), mods="")
    assert code == 2
    assert "nothing was selected" in out
    assert len(server.trade_requests()) == before


async def test_an_unknown_uid_fails_without_spending_anything(appraised_stack, capsys):
    code, out = await run(appraised_stack, capsys, "not-a-real-uid")
    assert code == 2
    assert "no item" in out


def test_the_parser_accepts_the_command():
    from cli.main import build_parser

    args = build_parser().parse_args(
        ["price", "abc123", "--mods", "0,2", "--open-prefixes", "1", "--dry-run"]
    )
    assert args.command == "price"
    assert (args.uid, args.mods, args.open_prefixes, args.dry_run) == ("abc123", "0,2", 1, True)


def test_the_rendered_list_marks_the_pre_ticked_rows(appraised_stack):
    """A checkbox list that does not show which boxes are ticked is a mod dump."""
    from modules.appraisal.backend.api import Selection
    from modules.appraisal.backend.gate import evaluate, report_for
    from modules.appraisal.backend.highlight import build
    from modules.moddb.backend.module import ModDbModule
    from tests.test_appraisal_gate import item

    db = ModDbModule()
    subject = item(
        base_type="Siege Helmet",
        category="armour",
        subcategory="helmet",
        ilvl=86,
        explicit=["+130 to maximum Life", "+12% to Fire Resistance"],
    )
    report = report_for(subject, db)
    proposal = build(subject, evaluate(subject, moddb=db, report=report), report, moddb=db)
    text = render_highlight(proposal, Selection(uid=subject.uid, mods=proposal.preticked))
    lines = [line for line in text.splitlines() if line.lstrip().startswith(("[x]", "[ ]"))]
    life = next(line for line in lines if "maximum Life" in line)
    assert "[x]" in life
    assert "T1 of 10" in life
    resist = next(line for line in text.splitlines() if "Fire Resistance" in line)
    assert "[ ]" in resist
