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
    Valuation,
)

# Inside the module, not in a shared fixtures directory. A module is a vertical
# slice (IMPLEMENTATION-PLAN §1.1) and this is an *appraisal* payload; putting it
# under `frontend/` would also make the bag screen's own test reach outside its
# module, which is the thing `poedex/module-ui-boundary` exists to refuse.
OUTPUT = REPO_ROOT / "modules" / "appraisal" / "ui" / "fixtures" / "bag-appraisal.json"

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
    pricing: bool = False,
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
        pricing=pricing,
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
            valued(chest, chaos=None, pricing=True, reason="tier 3 query outstanding"),
            gate(GateSignal("links", "6-linked", hard=True)),
        )
    )

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


def render() -> str:
    return json.dumps(build(), indent=2, sort_keys=False) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="exit 1 if the file is stale")
    args = parser.parse_args(argv)
    generated = render()
    if args.check:
        current = OUTPUT.read_text(encoding="utf-8") if OUTPUT.exists() else ""
        if current == generated:
            return 0
        print(
            f"{OUTPUT.relative_to(REPO_ROOT)} is stale.\n"
            "Run: python3 scripts/make_ui_fixtures.py",
            file=sys.stderr,
        )
        return 1
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(generated, encoding="utf-8")
    print(f"wrote {OUTPUT.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
