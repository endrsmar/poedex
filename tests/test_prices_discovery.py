"""Which poe.ninja tables exist for a league — asked, not assumed.

The bug this replaces is worth stating in a test file, because every assertion here
is shaped by it. ``CATALOGUE`` was twenty-six type names typed in by hand. ``Ducat``
was not one of them, had been a live exchange type all along, and so every ducat in
a real bag came back ``unpriceable`` while poe.ninja was publishing eleven priced
lines for them.

Two properties matter, and only one of them is obvious:

* discovery finds what a league actually serves, and skips what it does not;
* discovery can find a type **nobody has typed into the catalogue**. A mechanism
  that only validated known names against a league would have confirmed all
  twenty-six and reproduced the bug exactly.
"""

from __future__ import annotations

import pytest

from modules.prices.backend.discovery import (
    PROBED,
    STATIC,
    CatalogueStore,
    LeagueCatalogue,
    candidates_from_slugs,
)
from modules.prices.backend.module import PricesModule
from modules.prices.backend.ninja import (
    CANDIDATES,
    CATALOGUE,
    NEVER_PREFETCH,
    PREFETCH,
    key_for_type,
    slug_to_type,
    types_from_sitemap,
)
from runtime.storage import Storage
from tests.conftest import DISCOVERY_REQUESTS, ninja_sitemap

NINJA = "poe.ninja"


# -- the catalogue itself ---------------------------------------------------------


def test_every_documented_type_is_in_the_catalogue():
    """All 44 poe.ninja documents, cross-checked against its sitemap.

    This is the assertion that would have failed before the fix, and the reason it
    is written against a count rather than a list is that the list is the thing that
    was wrong.
    """
    assert len(CATALOGUE) == 44
    assert "ducat" in CATALOGUE, "the type whose absence started this"
    assert CATALOGUE["ducat"].kind == "exchange"
    assert set(PREFETCH) <= set(CATALOGUE)


def test_the_heavy_and_variant_keyed_tables_are_excluded_with_a_reason():
    """An unpriced item is honest; an item priced as the wrong variant is not."""
    assert set(NEVER_PREFETCH) <= set(CATALOGUE)
    assert "skill_gem" in NEVER_PREFETCH and "base_type" in NEVER_PREFETCH
    assert all(reason.strip() for reason in NEVER_PREFETCH.values())
    assert set(CANDIDATES) == set(CATALOGUE) - set(NEVER_PREFETCH)


# -- slugs to types ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("slug", "expect"),
    [
        ("ducats", "Ducat"),
        ("currency", "Currency"),
        ("delirium-orbs", "DeliriumOrb"),
        ("unique-accessories", "UniqueAccessory"),
        ("memories", "Memory"),
        ("blight-ravaged-maps", "BlightRavagedMap"),
        ("djinn-coins", "DjinnCoin"),
        ("base-types", "BaseType"),
        # The one the rule cannot derive, and the only one, of forty-four.
        ("temples", "IncursionTemple"),
    ],
)
def test_a_page_slug_becomes_the_api_type(slug, expect):
    assert slug_to_type(slug) == expect


def test_every_sitemap_slug_resolves_to_a_type_the_catalogue_knows():
    """The whole mechanism in one assertion: forty-four slugs in, forty-four working
    types out, with no per-slug table beyond a single irregular entry."""
    slugs = types_from_sitemap(ninja_sitemap("Standard"), "Standard")
    assert len(slugs) == 44
    new, unmapped = candidates_from_slugs(slugs)
    assert unmapped == []
    assert new == [], "a slug produced a category the catalogue already covers"


def test_a_slug_the_catalogue_has_never_heard_of_becomes_a_candidate():
    """Next league's mechanic, arriving without a code change.

    This is the property the ``Ducat`` failure demands. A discovery that could only
    confirm names already in the catalogue would return nothing here.
    """
    new, unmapped = candidates_from_slugs(["soul-cores", "ducats"])
    assert unmapped == []
    assert [(c.key, c.type) for c in new] == [("soul_core", "SoulCore")]


def test_a_slug_that_derives_nothing_is_reported_rather_than_dropped():
    new, unmapped = candidates_from_slugs(["a/b", "---"])
    assert new == []
    assert unmapped == ["a/b", "---"]


def test_the_key_for_a_type_is_derived_not_looked_up():
    assert key_for_type("UniqueAccessory") == "unique_accessory"
    assert key_for_type("Ducat") == "ducat"
    assert key_for_type("BlightRavagedMap") == "blight_ravaged_map"


def test_the_sitemap_parser_ignores_everything_that_is_not_this_league():
    xml = ninja_sitemap("Standard", "Allflame")
    assert "ducats" in types_from_sitemap(xml, "Allflame")
    assert types_from_sitemap(xml, "Hardcore") == []
    # The per-item URLs under a category, and the docs pages, are not categories.
    assert "chaos-orb" not in types_from_sitemap(xml, "Standard")
    assert "api" not in types_from_sitemap(xml, "Standard")


# -- the record -------------------------------------------------------------------


def test_the_record_round_trips(tmp_path):
    store = CatalogueStore(Storage(tmp_path / "cache", "prices"))
    record = LeagueCatalogue(
        league="Allflame",
        discovered_at=100.0,
        served=["currency"],
        empty=["ducat"],
        failed={"oil": "HTTP 500"},
        found={"soul_core": {"kind": "exchange", "type": "SoulCore", "label": "Soul Cores"}},
        unmapped=["mystery"],
        source=PROBED,
    )
    store.save(record)
    back = store.load("Allflame")
    assert back is not None
    assert back.to_json() == record.to_json()
    assert back.category("soul_core") is not None
    assert back.category("currency") is CATALOGUE["currency"]
    assert store.load("Standard") is None, "one league's record must not answer another's"


def test_a_static_record_is_never_treated_as_fresh():
    """``static`` means "we did not ask". It must not suppress the next attempt."""
    record = LeagueCatalogue(league="Standard", discovered_at=1000.0, source=STATIC)
    assert not record.fresh(1000.0)
    record.source = PROBED
    assert record.fresh(1000.0)
    assert not record.fresh(1000.0 + 86401)


# -- through the module -----------------------------------------------------------


async def test_a_first_refresh_asks_the_league_and_keeps_only_what_it_serves(
    priced_stack, prices_module, server
):
    record = prices_module.catalogue()
    assert record is not None and record.source == PROBED
    assert set(record.served) == set(PREFETCH)
    # The fixture server answers 200-with-no-lines for everything it has no table
    # for, exactly as poe.ninja does for a type a league does not have.
    assert set(record.empty) == set(CANDIDATES) - set(PREFETCH)
    assert record.failed == {} and record.unmapped == []
    assert len(server.to_host(NINJA)) == DISCOVERY_REQUESTS


async def test_the_second_refresh_only_asks_about_what_the_league_serves(
    priced_stack, prices_module, server, cache_clock, clock
):
    """The saving. Empty tables are asked about once a day, not twice an hour."""
    before = len(server.to_host(NINJA))
    cache_clock.advance(1801)
    clock.advance(1801)
    await prices_module.refresh()
    asked = server.to_host(NINJA)[before:]
    assert len(asked) == len(PREFETCH) < DISCOVERY_REQUESTS
    assert not any(r.url.path == "/sitemap.xml" for r in asked)


async def test_the_record_expires_and_the_league_is_asked_again(
    priced_stack, prices_module, server, cache_clock, clock
):
    before = len(server.to_host(NINJA))
    cache_clock.advance(86401)
    clock.advance(86401)
    await prices_module.refresh()
    assert len(server.to_host(NINJA)) - before == DISCOVERY_REQUESTS


async def test_discovery_is_per_league_and_never_shared(
    priced_stack, prices_module, server
):
    await prices_module.ensure_tables("Allflame")
    allflame = prices_module.catalogue("Allflame")
    standard = prices_module.catalogue("Standard")
    assert allflame is not None and standard is not None
    assert allflame.league == "Allflame" and standard.league == "Standard"
    assert allflame is not standard
    # Two full passes, one per league, each naming its own league on the wire.
    leagues = {
        r.url.params.get("league")
        for r in server.to_host(NINJA)
        if r.url.path != "/sitemap.xml"
    }
    assert leagues == {"Standard", "Allflame"}


async def test_a_restart_reuses_the_record_rather_than_re_probing(
    stack_factory, registry, server, cache_clock
):
    first = PricesModule(clock=cache_clock, prefetch=False)
    await stack_factory(first)
    await first.refresh()
    await registry.stop_all()
    spent = len(server.to_host(NINJA))

    from runtime.registry import Registry

    second = PricesModule(clock=cache_clock, prefetch=False)
    fresh = Registry(
        events=registry.events, storage=registry.storage, settings=registry.settings
    )
    for module in (registry.get("credentials"), registry.get("net"), registry.get("poeapi")):
        fresh.register(module)
    fresh.register(second)
    await fresh.start_all()
    try:
        record = second.catalogue("Standard")
        assert record is not None and record.source == PROBED
        assert len(server.to_host(NINJA)) == spent, "a warm start re-probed"
    finally:
        await fresh.stop_all()


# -- failure ----------------------------------------------------------------------


async def test_without_a_sitemap_discovery_still_probes_the_known_types(
    stack_factory, server, cache_clock
):
    """The sitemap is a supplement. Losing it costs the types nobody typed in, not
    the thirty-eight that are."""
    server.sitemap = None
    module = PricesModule(clock=cache_clock, prefetch=False)
    await stack_factory(module)
    status = await module.refresh()
    assert status.loaded == len(PREFETCH)
    record = module.catalogue()
    assert record is not None and record.source == PROBED
    assert record.found == {}


async def test_with_poe_ninja_down_the_built_in_list_is_used_and_says_so(
    stack_factory, server, cache_clock
):
    server.ninja_status = 503
    module = PricesModule(clock=cache_clock, prefetch=False)
    await stack_factory(module)
    status = await module.refresh()
    record = module.catalogue()

    assert record is not None and record.source == STATIC
    assert set(record.served) == set(PREFETCH)
    assert status.discovery and "built-in list" in status.discovery
    # ...and nothing was recorded to disk, so the next attempt is a real one.
    assert CatalogueStore(module._require_store()._storage).load("Standard") is None


async def test_discovery_can_be_switched_off(stack_factory, server, cache_clock, registry):
    module = PricesModule(clock=cache_clock, prefetch=False)
    await stack_factory(module)
    registry.settings.set("prices", "discover_categories", False)
    await module.refresh()
    assert module.catalogue() is None
    assert not any(r.url.path == "/sitemap.xml" for r in server.to_host(NINJA))
    assert module.status().loaded == len(PREFETCH)
    assert "discovery is switched off" in module.status().discovery


async def test_a_table_that_errors_is_retried_rather_than_recorded_as_absent(
    stack_factory, server, cache_clock
):
    """A 500 is not evidence that a league has no scarabs."""
    module = PricesModule(clock=cache_clock, prefetch=False)
    await stack_factory(module)
    await module.refresh()
    record = module.catalogue()
    assert record is not None
    assert "scarab" in record.served
    assert "scarab" not in record.empty
