#!/usr/bin/env python3
"""Build the frontend's bag fixture from the real backend classes.

Two properties matter more than the content:

1. **It is produced by the real code path.** ``appraise_bag`` decides the verdicts,
   ``BagAppraisal.to_json`` writes the payload. A hand-written JSON fixture drifts
   from the backend the first time a key is renamed, and every frontend test then
   passes against a shape that no longer exists.
2. **It is regenerable and checked.** ``--check`` fails when the checked-in file
   differs, and ``tests/test_wire_and_types.py`` runs it. So a backend change that
   alters the wire shows up as a failing Python test, not as a frontend bug.

The *content* is chosen from the findings this phase was told to respect, and every
row is here because something real made it necessary:

* all four verdicts, with `unpriceable` non-zero
* ``Dead Man's Sulphur`` at a stack of 40296 — five digits, which is what a price column
  and a quantity column have to survive
* two rows with the same name and different stack sizes, unmerged
* a gated rare with no price and visible gate reasoning
* a rare whose tier-3 query is still outstanding, so the total is a floor
* a rare whose tier-3 query **finished and matched nothing** — a terminal answer that
  must not render as `pricing…` (the first live appraisal's bug 2)
* a quest item, which is not a loot decision and must never appear under `vendor`
  (the first live appraisal's bug 3)
* one row per price provenance: poe.ninja, the bulk exchange, a trade search, and
  the player's own ``~price`` note
* a `check` row that is cheap-but-priced next to `check` rows that are gate hits —
  SPEC §11's open question, which the bag screen has to draw

Everything is invented. Nothing here came off the live account: the repo is public
and a fixture is a lasting artefact.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from modules.appraisal.backend.api import (  # noqa: E402
    GateResult,
    GateSignal,
    Strictness,
)
from modules.appraisal.backend.verdict import appraise_bag  # noqa: E402
from modules.poeapi.backend.models import (  # noqa: E402
    Grid,
    Location,
    Mods,
    NormalizedItem,
    Rarity,
    Sockets,
    Source,
)
from modules.prices.backend.api import (  # noqa: E402
    BagValuation,
    LeagueSource,
    Price,
    PriceSource,
    TableStatus,
    Tier3,
    Valuation,
)

# Inside the module, not in a shared fixtures directory. A module is a vertical
# slice (IMPLEMENTATION-PLAN §1.1) and this is an *appraisal* payload; putting it
# under `frontend/` would also make the bag screen's own test reach outside its
# module, which is the thing `poedex/module-ui-boundary` exists to refuse.
FIXTURES = REPO_ROOT / "modules" / "appraisal" / "ui" / "fixtures"
OUTPUT = FIXTURES / "bag-appraisal.json"
HIGHLIGHT_OUTPUT = FIXTURES / "item-highlight.json"
CHECK_OUTPUT = FIXTURES / "price-check.json"

LEAGUE = "Allflame"
DIVINE_RATE = 214.0
AS_OF = datetime(2026, 8, 10, 14, 32, 0, tzinfo=UTC)

# Bag order, left to right and top to bottom, honouring the footprint of a
# multi-slot item. A generator that just counted would place a 2x3 chest on top of
# the next three rows, and the grid drops covered cells — so the fixture would
# quietly lose rows the frontend tests are asserting on.
_occupied: set[tuple[int, int]] = set()


def _place(w: int, h: int) -> tuple[int, int]:
    for y in range(12):
        for x in range(12 - w + 1):
            footprint = {(x + dx, y + dy) for dx in range(w) for dy in range(h)}
            if footprint & _occupied:
                continue
            _occupied.update(footprint)
            return x, y
    raise RuntimeError("the fixture bag no longer fits in a bag")


def item(
    uid: str,
    name: str,
    *,
    base_type: str | None = None,
    category: str = "currency",
    rarity: Rarity = Rarity.CURRENCY,
    stack: int = 1,
    ilvl: int = 0,
    influences: tuple[str, ...] = (),
    links: int = 0,
    corrupted: bool = False,
    note: str | None = None,
    explicit: tuple[str, ...] = (),
    width: int = 1,
    height: int = 1,
) -> NormalizedItem:
    x, y = _place(width, height)
    return NormalizedItem(
        uid=uid,
        name=name,
        base_type=base_type or name,
        category=category,
        rarity=rarity,
        ilvl=ilvl,
        stack_size=stack,
        grid=Grid(x=x, y=y, w=width, h=height),
        sockets=Sockets(count=6 if links else 0, links=links),
        corrupted=corrupted,
        influences=list(influences),
        mods=Mods(explicit=list(explicit)),
        note=note,
        location=Location(source=Source.BAG),
    )


def valued(
    row: NormalizedItem,
    *,
    chaos: float | None,
    source: PriceSource = PriceSource.BULK,
    detail: str | None = None,
    listings: int | None = None,
    sample: int | None = None,
    note_chaos: float | None = None,
    market_chaos: float | None = None,
    tier3: Tier3 = Tier3.NONE,
    reason: str | None = None,
) -> Valuation:
    price = (
        Price(
            chaos,
            source,
            category=row.category,
            detail=detail,
            listing_count=listings,
            sample_size=sample,
            as_of=AS_OF,
        )
        if chaos is not None
        else None
    )
    return Valuation(
        uid=row.uid,
        name=row.name,
        base_type=row.base_type,
        category=row.category,
        stack_size=row.stack_size,
        price=price,
        note_price=(
            Price(note_chaos, PriceSource.NOTE, detail=row.note, as_of=AS_OF)
            if note_chaos is not None
            else None
        ),
        market=(
            Price(market_chaos, PriceSource.BULK, detail="poe.ninja line", as_of=AS_OF)
            if market_chaos is not None
            else None
        ),
        tier3=tier3,
        reason=reason,
    )


def gate(*signals: GateSignal, considered: bool = True) -> GateResult:
    return GateResult(signals, strictness=Strictness.GENEROUS, considered=considered)


NOT_CONSIDERED = gate(considered=False)


def build() -> dict:
    rows: list[tuple[NormalizedItem, Valuation, GateResult]] = []

    # -- keep: the obvious ones, at four different provenances ---------------
    divine = item("u-divine", "Divine Orb", stack=3)
    rows.append(
        (
            divine,
            valued(divine, chaos=214.0, source=PriceSource.BULK, detail="Currency line"),
            NOT_CONSIDERED,
        )
    )

    ducat = item("u-ducat", "Merrick's Ducat", category="currency", stack=88)
    rows.append(
        (
            ducat,
            valued(
                ducat,
                chaos=3.0,
                source=PriceSource.EXCHANGE,
                detail="median of the cheapest 10 offers",
                listings=39,
                sample=10,
            ),
            NOT_CONSIDERED,
        )
    )

    # The player's own asking price beat the index, and the index is kept beside it
    # so the surface can show the difference (SPEC §6.3).
    yoke = item(
        "u-yoke",
        "Yoke of Suffering",
        base_type="Onyx Amulet",
        category="accessory",
        rarity=Rarity.UNIQUE,
        ilvl=86,
        note="~price 2 divine",
        explicit=("Your Elemental Damage can Shock", "+27% to all Elemental Resistances"),
    )
    rows.append(
        (
            yoke,
            valued(
                yoke,
                chaos=428.0,
                source=PriceSource.NOTE,
                detail="~price 2 divine",
                note_chaos=428.0,
                market_chaos=257.0,
            ),
            NOT_CONSIDERED,
        )
    )

    # A gated rare that tier 3 actually priced. The number came from a trade search
    # and the row says so, because "428c (poe.ninja)" and "428c (six live listings)"
    # are different claims.
    wand = item(
        "u-wand",
        "Doom Bane",
        base_type="Convoking Wand",
        category="weapon",
        rarity=Rarity.RARE,
        ilvl=86,
        influences=("shaper",),
        explicit=("+1 to Level of all Minion Skill Gems", "88% increased Spell Damage"),
    )
    rows.append(
        (
            wand,
            valued(
                wand,
                chaos=192.0,
                source=PriceSource.TRADE,
                detail="median of 8 online listing(s)",
                listings=64,
                sample=8,
            ),
            gate(
                GateSignal("influence", "shaper-influenced", hard=True),
                GateSignal("base", "Convoking Wand is on the allowlist", hard=True),
                GateSignal("ilvl", "ilvl 86 base", hard=True),
            ),
        )
    )

    # -- check, job one: priced, below the keep threshold, above trivial -----
    cheap = [
        ("Greater Eldritch Ember", 18.0, 1),
        ("Orb of Annulment", 6.5, 2),
        ("Awakened Sextant", 4.0, 11),
    ]
    for index, (name, chaos, stack) in enumerate(cheap):
        row = item(f"u-check-{index}", name, stack=stack)
        rows.append((row, valued(row, chaos=chaos, detail="Currency line"), NOT_CONSIDERED))

    # -- check, job two: the gate flagged it and there is no number ----------
    helm = item(
        "u-helm",
        "Corpse Ward",
        base_type="Hubris Circlet",
        category="armour",
        rarity=Rarity.RARE,
        ilvl=86,
        influences=("hunter",),
        explicit=("+79 to maximum Life", "+38% to Cold Resistance"),
    )
    rows.append(
        (
            helm,
            valued(helm, chaos=None, reason="no bulk table prices rares"),
            gate(
                GateSignal("influence", "hunter-influenced", hard=True),
                GateSignal("ilvl", "ilvl 86 base", hard=True),
                GateSignal("mods", "life roll in the top group", hard=False),
            ),
        )
    )

    chest = item(
        "u-chest",
        "Rift Shroud",
        base_type="Astral Plate",
        category="armour",
        rarity=Rarity.RARE,
        ilvl=84,
        links=6,
        width=2,
        height=3,
        explicit=("+112 to maximum Life",),
    )
    rows.append(
        (
            chest,
            # Started and still outstanding when the pass returned: the bag total is
            # a floor while this row exists, and the row shows `⋯`, never `0c`.
            valued(
                chest,
                chaos=None,
                tier3=Tier3.PENDING,
                reason="tier 3 query outstanding",
            ),
            gate(GateSignal("links", "6-linked", hard=True)),
        )
    )

    # The other half of that word. This search ran, broadened, and matched nothing
    # in the league — a *finished* answer. It must render as terminal, never as
    # `pricing…`, and it must not be counted in "still pricing" under the total.
    gauntlets = item(
        "u-gauntlets",
        "Dire Grasp",
        base_type="Dragonscale Gauntlets",
        category="armour",
        rarity=Rarity.RARE,
        ilvl=81,
        width=2,
        height=2,
        explicit=("+109 to maximum Life", "10% increased Attack Speed"),
    )
    rows.append(
        (
            gauntlets,
            valued(
                gauntlets,
                chaos=None,
                tier3=Tier3.NO_LISTINGS,
                reason="Dragonscale Gauntlets · rare · max life ≥ 87",
            ),
            gate(
                GateSignal("fractured", "fractured", hard=True),
                GateSignal(
                    "roll:life",
                    "max life 109>=80",
                    mods=["+109 to maximum Life"],
                    value=109.0,
                    label="max life",
                ),
            ),
        )
    )

    # -- not loot: no verdict block may tell the player to sell this ---------
    tome = item(
        "u-quest",
        "",
        base_type="The Mortinomicon Exitio Immortalis",
        category="quest",
        rarity=Rarity.QUEST,
        ilvl=0,
    )
    rows.append((tome, valued(tome, chaos=None), NOT_CONSIDERED))

    # -- unpriceable: the index should carry these and does not --------------
    scarab = item("u-veiled", "Veiled Scarab", category="fragment", stack=174)
    rows.append(
        (
            scarab,
            valued(scarab, chaos=None, reason="not in Allflame's poe.ninja index"),
            NOT_CONSIDERED,
        )
    )

    relic = item(
        "u-relic",
        "Bottled Faith",
        base_type="Sulphur Flask",
        category="flask",
        rarity=Rarity.RELIC,
        ilvl=80,
    )
    rows.append(
        (
            relic,
            valued(relic, chaos=None, reason="relic variants are not indexed"),
            NOT_CONSIDERED,
        )
    )

    # -- the five-digit stack, and the duplicate-name pair -------------------
    # Two rows, one name, different stacks. They are not merged: they are two stacks
    # in two slots, and merging them behind the player's back moves an item.
    sulphur_big = item("u-sulphur-a", "Dead Man's Sulphur", category="currency", stack=40296)
    sulphur_small = item("u-sulphur-b", "Dead Man's Sulphur", category="currency", stack=7)
    for row in (sulphur_big, sulphur_small):
        rows.append((row, valued(row, chaos=0.08, detail="Ultimatum line"), NOT_CONSIDERED))

    # -- trash: the largest block, and the least informative -----------------
    trash = [
        ("Orb of Alchemy", 0.4, 31),
        ("Jeweller's Orb", 0.12, 2615),
        ("Orb of Fusing", 0.5, 9),
        ("Chaos Orb", 1.0, 6),
        ("Portal Scroll", 0.02, 24),
        ("Scroll of Wisdom", 0.01, 13),
        ("Blacksmith's Whetstone", 0.05, 40),
    ]
    for index, (name, chaos, stack) in enumerate(trash):
        row = item(f"u-trash-{index}", name, stack=stack)
        rows.append((row, valued(row, chaos=chaos, detail="Currency line"), NOT_CONSIDERED))

    # A rare the gate looked at and had nothing to say about. This is the answer the
    # gate exists to give, and it is a different `trash` from a 1c orb.
    ring = item(
        "u-ring",
        "Blight Loop",
        base_type="Iron Ring",
        category="accessory",
        rarity=Rarity.RARE,
        ilvl=61,
        explicit=("+12 to Strength", "+9% to Fire Resistance"),
    )
    rows.append((ring, valued(ring, chaos=None, reason="no bulk table prices rares"), gate()))

    items = [row for row, _valuation, _gate in rows]
    valuation = BagValuation(
        [value for _row, value, _gate in rows],
        league=LEAGUE,
        league_source=LeagueSource.CHARACTER,
        divine_rate=DIVINE_RATE,
        table=TableStatus(
            league=LEAGUE,
            loaded=36,
            requested=38,
            oldest=datetime(2026, 8, 10, 14, 2, 0, tzinfo=UTC),
            newest=AS_OF,
            stale=False,
            note=None,
            discovery="36 of 38 candidate types served by Allflame (asked, not assumed)",
        ),
        lookups=17,
        trade_requests=2,
    )
    appraisal = appraise_bag(
        items,
        valuation,
        [result for _row, _valuation, result in rows],
        keep_chaos=20.0,
        check_chaos=1.0,
        strictness=Strictness.GENEROUS,
    )
    payload = appraisal.to_json()
    payload["character"] = "Gladefall"
    payload["stale"] = False
    return payload


# -- Phase 9: the checkbox list ------------------------------------------------
#
# Built through the **real `moddb`**, not written out by hand. The point of the
# fixture is that `unknown` appears in it because the database genuinely refuses to
# attribute a line, rather than because somebody typed the word: a frontend test
# asserting that the panel renders `unknown` is only worth anything if the payload
# it renders came from the thing that decides.

HIGHLIGHT_ITEM = NormalizedItem(
    uid="u-helm",
    name="Corpse Ward",
    base_type="Siege Helmet",
    category="armour",
    subcategory="helmet",
    rarity=Rarity.RARE,
    ilvl=86,
    grid=Grid(x=0, y=0, w=2, h=2),
    sockets=Sockets(),
    identified=True,
    mods=Mods(
        explicit=[
            # T1 of 10 here, and T1 of 13 nowhere near this value on a body armour.
            "+130 to maximum Life",
            # T7 of 8 — a real tier, and a bad one.
            "+12% to Fire Resistance",
            # Ambiguous: several ladders reach it, so `moddb` names no tier at all
            # and the panel must print `unknown`.
            "10% increased Rarity of Items found",
            # Phase 9b: a line GGG's own filter list has no entry for — it publishes
            # only "Arrows Pierce an additional Target", which is a different stat.
            # The panel annotates it and still lets the player tick it, and the
            # "not searchable" notice needs a real example to be about. Typed here
            # rather than into the fixture so that `tradeable: false` in the payload
            # is `moddb`'s answer and not somebody's assumption about it.
            "Projectiles Pierce an additional Target",
        ],
        crafted=[
            # The bench pool is not in the artifact, so this is `unknown` for a
            # second and different reason. Both render the same way on purpose.
            "+15 to maximum Mana",
        ],
    ),
    location=Location(source=Source.BAG, slot="MainInventory"),
)


def build_highlight() -> dict:
    from modules.appraisal.backend.gate import evaluate, report_for
    from modules.appraisal.backend.highlight import build as build_item_highlight
    from modules.moddb.backend.module import ModDbModule

    db = ModDbModule()
    report = report_for(HIGHLIGHT_ITEM, db)
    result = evaluate(HIGHLIGHT_ITEM, moddb=db, report=report)
    return build_item_highlight(HIGHLIGHT_ITEM, result, report, moddb=db).to_json()


def build_check() -> dict:
    """A finished check, with a **thin** sample — the shape that has to be labelled.

    Four comparables is a real outcome for a specific rare and it is exactly the
    case the first live appraisal got wrong: it reported the median of one listing
    as though it were a market price. The count travels with the number.
    """
    from modules.appraisal.backend.api import ItemHighlight, PriceCheck, Selection
    from modules.appraisal.backend.gate import evaluate, report_for
    from modules.appraisal.backend.highlight import build as build_item_highlight
    from modules.moddb.backend.module import ModDbModule
    from modules.prices.backend.api import TradeQuote

    db = ModDbModule()
    report = report_for(HIGHLIGHT_ITEM, db)
    result = evaluate(HIGHLIGHT_ITEM, moddb=db, report=report)
    highlight: ItemHighlight = build_item_highlight(
        HIGHLIGHT_ITEM, result, report, moddb=db
    )
    selection: Selection = highlight.selection()
    quote = TradeQuote(
        62.0,
        considered=4,
        online=4,
        total=4,
        listings=[48.0, 57.0, 67.0, 91.0],
        query="Siege Helmet · rare · +130 to maximum Life ≥ 104",
        attempts=1,
        query_url="https://www.pathofexile.com/trade/search/Allflame/EXAMPLE",
    )
    return PriceCheck(
        highlight=highlight,
        selection=selection,
        league=LEAGUE,
        quote=quote,
        spent=2,
        divine_rate=DIVINE_RATE,
    ).to_json()


def render() -> str:
    return json.dumps(build(), indent=2, sort_keys=False) + "\n"


def render_highlight() -> str:
    return json.dumps(build_highlight(), indent=2, sort_keys=False) + "\n"


def render_check() -> str:
    return json.dumps(build_check(), indent=2, sort_keys=False) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="exit 1 if a file is stale")
    args = parser.parse_args(argv)
    wanted = [
        (OUTPUT, render()),
        (HIGHLIGHT_OUTPUT, render_highlight()),
        (CHECK_OUTPUT, render_check()),
    ]
    if args.check:
        stale = [
            target
            for target, generated in wanted
            if (target.read_text(encoding="utf-8") if target.exists() else "") != generated
        ]
        if not stale:
            return 0
        for target in stale:
            print(
                f"{target.relative_to(REPO_ROOT)} is stale.\n"
                "Run: python3 scripts/make_ui_fixtures.py",
                file=sys.stderr,
            )
        return 1
    for target, generated in wanted:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(generated, encoding="utf-8")
        print(f"wrote {target.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
