"""``poedex moddb`` — inspect the mod database.

Small on purpose. It exists for one job the module cannot do for itself: let a human
see **how old the data is**, because a stale mod database is the failure mode this
whole module has to be defended against. It does not fail, it answers confidently and
wrongly, and nothing on screen would look unusual.

The mod and base lookups are there so that "why does it say T3?" can be answered in
one command instead of a Python session.
"""

from __future__ import annotations

from modules.moddb.backend.api import Attribution, ModDbApi, ModDbError, Origin
from runtime.registry import Registry

__all__ = ["cmd_moddb"]

STALE_DAYS = 120.0


async def cmd_moddb(
    registry: Registry,
    *,
    base: str | None = None,
    mod: str | None = None,
    ilvl: int = 0,
    origin: str = Origin.EXPLICIT.value,
    influence: list[str] | None = None,
) -> int:
    moddb = registry.api(ModDbApi)
    try:
        version = moddb.version()
    except ModDbError as exc:
        print(f"unavailable: {exc}")
        return 1

    print(f"source:     repoe-fork, Path of Exile {version.game_version}")
    print(f"built:      {version.generated_at.isoformat()} ({version.age_days():.0f} days ago)")
    print(f"contents:   {version.mods} affixes, {version.bases} bases, {version.texts} mod texts")
    if version.age_days() > STALE_DAYS:
        print("warning:    older than a league. Re-run scripts/build_moddb.py — a stale")
        print("            mod database answers confidently and wrongly.")

    if base:
        found = moddb.base(base)
        if found is None:
            print(f"\n{base!r} is not a base that rolls affixes")
            return 1
        print(f"\nbase:       {found.name} ({found.item_class})")
        print(f"drops from: item level {found.drop_level}")
        print(f"top affix:  item level {found.top_affix_level}")
        print(f"ilvl matters: {'yes' if found.ilvl_matters else 'no'}")
        print(f"top-tier base: {'yes' if found.is_top_tier else 'no'}")
        print(f"tags:       {', '.join(sorted(found.tags))}")

    if mod:
        if not base:
            print("\n--mod needs --base: a tier only means something on a base type")
            return 1
        match = moddb.identify(
            mod,
            base_type=base,
            ilvl=ilvl,
            origin=Origin(origin),
            influences=influence or (),
        )
        print(f"\nmod:        {match.text}")
        print(f"normalized: {match.normalized}")
        print(f"verdict:    {match.attribution.value} — {match.describe()}")
        if match.group:
            print(f"group:      {match.group} ({match.affix.value if match.affix else '?'})")
        if match.ceiling is not None:
            print(f"ceiling:    {match.ceiling:g} on this base")
        if match.influences:
            print(f"influence:  {', '.join(sorted(i.value for i in match.influences))}")
        trade = moddb.trade_stat_id(mod, origin=Origin(origin))
        print(f"trade id:   {trade or '-'}")
        print(f"game stats: {', '.join(moddb.game_stat_ids(mod)) or '-'}")
        if match.note:
            print(f"note:       {match.note}")
        if match.attribution is not Attribution.EXACT and match.candidates:
            print("candidates:")
            for candidate in match.candidates:
                span = f"{candidate.low:g}-{candidate.high:g}" if candidate.low is not None else "-"
                print(
                    f"  {candidate.group} T{candidate.tier} of {candidate.tiers} "
                    f"(ilvl {candidate.required_level}, {span})"
                )
        return 0 if match.attribution.is_confident else 1
    return 0
