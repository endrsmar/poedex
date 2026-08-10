"""Tier 2 — the local heuristic gate, strictness-parameterized (SPEC §5.2).

The gate answers one question: *is this item worth spending a trade request on?* It
never produces a price. Everything here is local arithmetic over the normalized
model, which is what makes it safe to run over a whole bag on a zone transition and
over 818 stash items on demand.

## Same code, opposite biases

`prices` cannot value a rare — no bulk table ever has — so for rares the gate is the
only thing standing between "vendor it" and "look at it". The two contexts want the
error to fall on opposite sides:

* **Bag → generous.** A false negative here tells the player to vendor something
  good, which is unrecoverable. A false positive costs one trade query they can
  decline. So the generous gate adds the soft signals: a desirable mod group being
  present at all, and rolls above a threshold.
* **Stash → strict.** The item already survived bag triage and is sitting in
  storage, not about to be vendored, so a false negative costs nothing today. A
  generous gate over a real stash produces hundreds of hits and the digest becomes
  noise. So the strict gate accepts **only** the hard requirements, and the
  "a desirable mod group is present" signal is dropped entirely — research-notes and
  SPEC §5.2 both name it as the false-positive engine, and it is: on a rare with six
  mods, *something* is always in a desirable group.

The strict hard requirements, verbatim from SPEC §5.2 — must hit at least one:
influence / fractured / synthesised, six-link, ilvl 86 on a base where it matters,
or a base on an explicit high-value allowlist.

## The approximation, stated plainly

**These are not mod tiers.** Real tier data — "T1 life is +120 to +129 at ilvl 86 on
a body armour" — lives in a mod database keyed by base type, item class and item
level. This project does not have one, will not ship one (it is tens of thousands of
rows that go stale every league), and cannot derive one from anything the API
returns.

So :data:`MOD_GROUPS` approximates it: a regex per mod group, a single numeric
threshold per group, and no awareness of base type, item class or item level. A
``+80 to maximum Life`` roll is treated identically on a helmet, where it is
near-top-tier, and on a body armour, where it is mediocre. Percentage rolls are worse
still, because their real tiers scale with the base.

That is a **deliberate, documented approximation and nothing later should mistake it
for tier data.** It is defensible only because of what it is used for: a *generous*
gate whose false positives cost one optional trade query, and which is switched off
entirely at strict. If a future phase wants real tiers, it needs a mod database and
these constants should be deleted rather than tuned.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence

from modules.appraisal.backend.api import (
    GateResult,
    GateSignal,
    Strictness,
)
from modules.poeapi.backend.api import Mods, NormalizedItem, Rarity

__all__ = [
    "HIGH_VALUE_BASES",
    "ILVL86_BASE_CATEGORIES",
    "ILVL86_EXCLUDED_SUBCATEGORIES",
    "MOD_GROUPS",
    "SIX_LINK",
    "ModGroup",
    "evaluate",
    "gate_applies",
    "mod_hits",
]

SIX_LINK = 6
ILVL86 = 86

# ilvl 86 is the breakpoint where the top tier of a large number of prefixes and
# suffixes unlocks, and "ilvl 86+" is the filter every crafter types into the trade
# site. It is meaningful on gear that *rolls* those mods and meaningless elsewhere:
# a jewel has no item-level tiers worth the name, a flask's mods do not scale with
# ilvl, and a map's tier is its level.
ILVL86_BASE_CATEGORIES: frozenset[str] = frozenset({"weapon", "armour", "accessory"})
ILVL86_EXCLUDED_SUBCATEGORIES: frozenset[str] = frozenset({"talisman", "trinket"})

# The explicit high-value allowlist SPEC §5.2 asks for. These are bases that are
# worth something *as bases*, before any mod is read — the ones people buy blank to
# craft on. Kept short and explicit on purpose: a long list is a mod database with
# extra steps, and every entry here is a claim someone has to re-check each league.
# Extendable at runtime through the `extra_high_value_bases` setting.
HIGH_VALUE_BASES: frozenset[str] = frozenset(
    {
        # Jewellery — the ilvl-86 crafting bases with the best implicits.
        "Vermillion Ring",
        "Cerulean Ring",
        "Amethyst Ring",
        "Opal Ring",
        "Steel Ring",
        "Marble Amulet",
        "Citrine Amulet",
        "Turquoise Amulet",
        "Onyx Amulet",
        "Stygian Vise",
        "Crystal Belt",
        # Armour bases whose implicit or base stats carry the price.
        "Hubris Circlet",
        "Bone Helmet",
        "Vaal Regalia",
        "Astral Plate",
        "Sacrificial Garb",
        "Titanium Spirit Shield",
        "Fingerless Silk Gloves",
        "Gripped Gloves",
        "Spiked Gloves",
        "Two-Toned Boots",
        # Weapon bases bought blank to craft on.
        "Convoking Wand",
        "Imbued Wand",
        "Opal Sceptre",
        "Vaal Rapier",
        "Jewelled Foil",
    }
)


class ModGroup:
    """One approximated mod group: a regex, a threshold, and a name.

    ``pattern`` must expose at least one numeric capture group. When it exposes two
    (``Adds 12 to 30 …``) the midpoint is used, which is how the trade site's own
    "damage" filters read a flat range.
    """

    __slots__ = ("label", "name", "pattern", "sum_matches", "threshold")

    def __init__(
        self,
        name: str,
        pattern: str,
        threshold: float,
        *,
        label: str | None = None,
        sum_matches: bool = False,
    ) -> None:
        self.name = name
        self.pattern = re.compile(pattern, re.IGNORECASE)
        self.threshold = threshold
        self.label = label or name.replace("_", " ")
        self.sum_matches = sum_matches
        """Add every match together before comparing — the only honest way to read
        resistances, which arrive as three or four separate lines."""

    def measure(self, texts: Iterable[str]) -> float | None:
        """The group's roll across ``texts``, or ``None`` if it is absent."""
        values: list[float] = []
        for text in texts:
            for match in self.pattern.finditer(text):
                numbers = [float(g) for g in match.groups() if g is not None]
                if not numbers:
                    continue
                values.append(sum(numbers) / len(numbers))
        if not values:
            return None
        return sum(values) if self.sum_matches else max(values)

    def __repr__(self) -> str:
        return f"ModGroup({self.name!r}, >= {self.threshold:g})"


# The approximation. See the module docstring before changing a number here: these
# are not tiers, they are one threshold per group with no knowledge of base type,
# item class or item level.
MOD_GROUPS: tuple[ModGroup, ...] = (
    ModGroup("life", r"\+(\d+) to maximum Life", 80, label="max life"),
    ModGroup(
        "energy_shield",
        r"\+(\d+) to maximum Energy Shield",
        90,
        label="max energy shield",
    ),
    ModGroup(
        "energy_shield_pct",
        r"(\d+)% increased (?:maximum )?Energy Shield",
        80,
        label="increased energy shield",
    ),
    ModGroup(
        "resistances",
        r"\+(\d+)% to (?:Fire|Cold|Lightning|Chaos|all Elemental) Resistances?",
        110,
        label="total resistances",
        sum_matches=True,
    ),
    ModGroup(
        "physical_damage",
        r"(\d+)% increased Physical Damage",
        150,
        label="increased physical damage",
    ),
    ModGroup("spell_damage", r"(\d+)% increased Spell Damage", 80),
    ModGroup("attack_speed", r"(\d+)% increased Attack Speed", 25),
    ModGroup("cast_speed", r"(\d+)% increased Cast Speed", 25),
    ModGroup("movement_speed", r"(\d+)% increased Movement Speed", 30),
    ModGroup(
        "critical_multiplier",
        r"\+(\d+)% to (?:Global )?Critical Strike Multiplier",
        30,
        label="crit multiplier",
    ),
    ModGroup(
        "added_elemental",
        r"Adds (\d+) to (\d+) (?:Fire|Cold|Lightning) Damage",
        30,
        label="added elemental damage",
    ),
    ModGroup(
        "added_physical",
        r"Adds (\d+) to (\d+) Physical Damage",
        20,
        label="added physical damage",
    ),
    # A gem-level mod is desirable at any roll; the threshold is "it exists".
    ModGroup(
        "gem_level",
        r"\+(\d+) to Level of all .*Skill Gems",
        1,
        label="+level to skill gems",
    ),
    ModGroup(
        "attributes",
        r"\+(\d+) to (?:Strength|Dexterity|Intelligence|all Attributes)",
        45,
        label="attribute roll",
    ),
)

# Rarities the gate has anything useful to say about. Currency, cards, fragments and
# gems are tier-1 territory; SPEC §5.2's "bulk tabs never enter tier 2 or 3" is the
# same rule seen from the stash side.
_GATED_RARITIES: frozenset[Rarity] = frozenset({Rarity.RARE, Rarity.MAGIC})

# ...and the categories on which "is this worth a trade query" is a real question.
_GATED_CATEGORIES: frozenset[str] = frozenset(
    {"weapon", "armour", "accessory", "jewel", "flask", "tincture"}
)


def gate_applies(item: NormalizedItem) -> bool:
    """Whether tier 2 has any business looking at this item.

    A Chaos Orb does not get a heuristic opinion — it has a bulk price, and running
    the gate over it would be spending cycles to reach a foregone conclusion.
    """
    return item.rarity in _GATED_RARITIES and item.category in _GATED_CATEGORIES


def mod_texts(mods: Mods) -> list[str]:
    """Every mod line that can carry a roll.

    Enchants and utility (flask) mods are included; ``veiled`` lines are not, because
    a veiled mod's text is a placeholder (``Prefix Unveil``) with no number in it —
    its value is captured by the veiled signal instead.
    """
    return [*mods.explicit, *mods.implicit, *mods.crafted, *mods.fractured, *mods.enchant]


def mod_hits(item: NormalizedItem, *, require_threshold: bool) -> list[tuple[ModGroup, float]]:
    """Mod groups present on ``item``, optionally only those over their threshold."""
    texts = mod_texts(item.mods)
    hits: list[tuple[ModGroup, float]] = []
    for group in MOD_GROUPS:
        value = group.measure(texts)
        if value is None:
            continue
        if require_threshold and value < group.threshold:
            continue
        hits.append((group, value))
    return hits


def _hard_signals(item: NormalizedItem, allowlist: frozenset[str]) -> list[GateSignal]:
    """SPEC §5.2's hard requirements. The whole of the strict gate."""
    signals: list[GateSignal] = []

    # Details are terse on purpose: they are concatenated into one line of a 300 px
    # panel, and "shaper-influenced" says everything "shaper influenced item" does.
    if item.influences:
        signals.append(
            GateSignal("influence", "/".join(sorted(item.influences)) + "-influenced", hard=True)
        )
    if item.fractured or item.mods.fractured:
        signals.append(GateSignal("fractured", "fractured", hard=True))
    if item.synthesised:
        signals.append(GateSignal("synthesised", "synthesised", hard=True))
    if item.sockets.links >= SIX_LINK:
        signals.append(GateSignal("six_link", f"{item.sockets.links}-link", hard=True))
    if item.ilvl >= ILVL86 and _ilvl_matters(item):
        signals.append(GateSignal("ilvl_86", f"ilvl {item.ilvl}", hard=True))
    if _base_matches(item, allowlist):
        signals.append(GateSignal("high_value_base", "sought-after base", hard=True))
    return signals


def _ilvl_matters(item: NormalizedItem) -> bool:
    if item.category not in ILVL86_BASE_CATEGORIES:
        return False
    return (item.subcategory or "") not in ILVL86_EXCLUDED_SUBCATEGORIES


def _base_matches(item: NormalizedItem, allowlist: frozenset[str]) -> bool:
    folded = {name.casefold() for name in allowlist}
    return item.base_type.casefold() in folded


def _soft_signals(item: NormalizedItem) -> list[GateSignal]:
    """The generous gate's additions. Dropped entirely at strict — SPEC §5.2 names
    "a desirable mod group is present" as the false-positive engine, and on a rare
    with six mods something is always in a desirable group."""
    signals: list[GateSignal] = []

    if not item.identified:
        # Unidentified means the mods cannot be read at all, so nothing below can
        # fire. In a bag that is exactly the item you must not vendor by accident.
        signals.append(GateSignal("unidentified", "unidentified"))
        return signals

    if item.mods.veiled:
        signals.append(GateSignal("veiled", f"{len(item.mods.veiled)} veiled"))

    over = mod_hits(item, require_threshold=True)
    for group, value in over:
        signals.append(
            GateSignal(f"roll:{group.name}", f"{group.label} {value:g}>={group.threshold:g}")
        )

    if not over:
        present = mod_hits(item, require_threshold=False)
        if present:
            labels = ", ".join(group.label for group, _ in present[:3])
            signals.append(GateSignal("mod_group", f"mods present: {labels}"))
    return signals


def evaluate(
    item: NormalizedItem,
    *,
    strictness: Strictness = Strictness.GENEROUS,
    allowlist: Sequence[str] | frozenset[str] = HIGH_VALUE_BASES,
) -> GateResult:
    """Run tier 2 over one item.

    The one function both strictness levels go through, so "the strict gate is the
    generous gate minus the soft signals" is a property of the code rather than a
    claim about two parallel implementations.
    """
    bases = allowlist if isinstance(allowlist, frozenset) else frozenset(allowlist)
    if not gate_applies(item):
        return GateResult((), strictness=strictness, considered=False)

    signals = _hard_signals(item, bases)
    if strictness is Strictness.GENEROUS:
        signals.extend(_soft_signals(item))
    return GateResult(signals, strictness=strictness, considered=True)
