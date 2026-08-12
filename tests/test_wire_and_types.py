"""The Python↔TypeScript contract.

Three checks, and they close a loop that neither half closes alone:

1. **The real `to_json()` output validates against the wire models.** `extra="forbid"`
   means an added key fails, and a required field means a removed key fails. This is
   what makes `transports/wire.py` a contract rather than a wish.
2. **The generated TS is not stale.** IMPLEMENTATION-PLAN §3 asks for a build step
   that CI fails on; this is that failure.
3. **The frontend fixture is not stale, and still contains the cases it exists for.**
   A five-digit stack, a duplicate name, a gated rare with no price, all four
   verdicts. The frontend tests assert on those; if a backend change quietly drops
   one, the frontend keeps passing against a bag that no longer poses the question.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from scripts import generate_types, make_ui_fixtures
from transports.dispatch import server_meta
from transports.wire import (
    BagAppraisalPayload,
    ItemHighlightPayload,
    PriceCheckPayload,
    ServerMeta,
    StashDigestPayload,
    TabAppraisalPayload,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURES = REPO_ROOT / "modules" / "appraisal" / "ui" / "fixtures"
FIXTURE = FIXTURES / "bag-appraisal.json"


@pytest.fixture
def bag_payload_json() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def test_the_checked_in_highlight_fixture_validates():
    ItemHighlightPayload.model_validate(
        json.loads((FIXTURES / "item-highlight.json").read_text(encoding="utf-8"))
    )


def test_the_checked_in_price_check_fixture_validates():
    PriceCheckPayload.model_validate(
        json.loads((FIXTURES / "price-check.json").read_text(encoding="utf-8"))
    )


def test_the_checked_in_stash_fixtures_validate():
    StashDigestPayload.model_validate(
        json.loads((FIXTURES / "stash-digest.json").read_text(encoding="utf-8"))
    )
    TabAppraisalPayload.model_validate(
        json.loads((FIXTURES / "stash-tab.json").read_text(encoding="utf-8"))
    )


async def test_a_real_stash_digest_validates_against_the_wire_model(stash_appraiser):
    """The live code path, over the fixture stash, as a browser would receive it."""
    digest = await stash_appraiser.stash_digest("Standard")
    StashDigestPayload.model_validate(digest.to_json())


async def test_a_real_tab_appraisal_validates_against_the_wire_model(stash_appraiser):
    for index in (0, 1, 4):  # bulk, gear, and the one that cannot be read
        result = await stash_appraiser.appraise_tab(index, league="Standard")
        TabAppraisalPayload.model_validate(result.to_json())


def test_the_stash_fixture_carries_every_state_the_screen_must_draw(
):
    """A digest fixture with no holes in it would let the screen pass its tests while
    quietly rendering an unread tab as 0c — which is this phase's whole failure mode."""
    digest = json.loads((FIXTURES / "stash-digest.json").read_text(encoding="utf-8"))
    rows = digest["tabs"]
    assert any(row["known"] is False and row["supported"] for row in rows), "an unread tab"
    assert any(row["supported"] is False for row in rows), "a map tab"
    assert any(row["grid"] and row["cols"] == 24 for row in rows), "a quad"
    assert any(row["grid"] is False for row in rows), "a special tab"
    assert any(row["permanent"] for row in rows), "a remove-only tab"
    assert any(row["unpriceable_count"] > 0 for row in rows), "a tab with a hole in it"
    assert any(row["highlighted"] > 0 for row in rows), "a tab with something to check"
    assert digest["total_is_floor"] is True


def test_a_real_highlight_validates_against_the_wire_model(appraiser, loot):
    """The live code path, over the fixture bag, as a browser would receive it."""
    for row in loot:
        ItemHighlightPayload.model_validate(appraiser.highlight(row).to_json())


# -- the wire models describe what the backend actually emits --------------------


def test_the_checked_in_fixture_validates_against_the_wire_model(bag_payload_json: dict):
    BagAppraisalPayload.model_validate(bag_payload_json)


async def test_a_real_appraisal_validates_against_the_wire_model(appraiser, loot):
    """The fixture is generated, so validating it proves the generator. This
    validates the output of the *live* code path over the Phase 4 loot bag, which is
    what a browser would actually receive."""
    result = await appraiser.appraise(loot)
    payload = result.to_json()
    payload["character"] = "PlaceholderWarden"
    payload["stale"] = False
    BagAppraisalPayload.model_validate(payload)


async def test_an_extra_key_in_to_json_is_a_failure_not_a_shrug(bag_payload_json: dict):
    """`extra="forbid"` is the whole mechanism: a key the frontend types do not
    carry must break here, not silently reach a screen that ignores it."""
    with pytest.raises(ValidationError):
        BagAppraisalPayload.model_validate({**bag_payload_json, "surprise": 1})


async def test_server_meta_validates(started_meta_registry):
    ServerMeta.model_validate(server_meta(started_meta_registry, version="0.1.0"))


@pytest.fixture
async def started_meta_registry(registry, fake_module):
    async def ping() -> str:
        return "pong"

    registry.register(fake_module("demo", methods={"ping": ping}))
    await registry.start_all()
    yield registry
    await registry.stop_all()


# -- generated artefacts are not stale -------------------------------------------


def test_the_generated_typescript_is_current():
    assert generate_types.main(["--check"]) == 0, (
        "frontend/core/src/types/generated.ts is stale — run "
        "'python3 scripts/generate_types.py'"
    )


def test_the_frontend_bag_fixture_is_current():
    assert make_ui_fixtures.main(["--check"]) == 0, (
        "frontend/fixtures/bag-appraisal.json is stale — run "
        "'python3 scripts/make_ui_fixtures.py'"
    )


def test_the_generator_refuses_a_schema_it_cannot_express():
    """It raises rather than emitting `any`. A generator that guesses produces types
    that compile and lie."""
    with pytest.raises(generate_types.UnsupportedSchema):
        generate_types.ts_type({"type": "not-a-json-schema-type"})


# -- the fixture still poses the questions the frontend tests ask -------------------


def test_the_fixture_carries_every_verdict(bag_payload_json: dict):
    counts = bag_payload_json["counts"]
    assert set(counts) == {"keep", "check", "trash", "unpriceable", "not_loot"}
    assert all(count > 0 for count in counts.values())


def test_the_fixture_has_a_five_digit_stack(bag_payload_json: dict):
    """A stack of 40296 is a real shape a real bag produced. A quantity
    column that cannot hold it is a column that reflows on the one row that matters."""
    stacks = [item["stack_size"] for item in bag_payload_json["items"]]
    assert max(stacks) >= 10_000


def test_the_fixture_has_two_rows_with_the_same_name_and_different_stacks(
    bag_payload_json: dict,
):
    """Nothing merges them. Two stacks in two slots are two things to pick up."""
    pairs = [(item["name"], item["stack_size"]) for item in bag_payload_json["items"]]
    names = [name for name, _stack in pairs]
    duplicated = {name for name in names if names.count(name) > 1}
    assert duplicated
    for name in duplicated:
        sizes = {stack for candidate, stack in pairs if candidate == name}
        assert len(sizes) > 1


def test_the_fixture_has_a_gated_rare_with_no_price(bag_payload_json: dict):
    gated = [
        item
        for item in bag_payload_json["items"]
        if item["gate"]["passed"] and item["valuation"]["unpriceable"]
    ]
    assert gated, "the tier-2 gate's own case is the one the bag screen must draw"
    assert any(signal["hard"] for item in gated for signal in item["gate"]["signals"])


def test_the_fixture_total_is_a_floor_and_says_why(bag_payload_json: dict):
    assert bag_payload_json["total_is_floor"] is True
    assert bag_payload_json["pricing_count"] >= 1
    assert bag_payload_json["unpriceable_count"] >= 1
    assert bag_payload_json["unpriceable_stack"] >= bag_payload_json["unpriceable_count"]


def test_the_fixture_covers_every_price_provenance(bag_payload_json: dict):
    """poe.ninja, the bulk exchange, a trade search and the player's own note are
    four different claims, and the screen labels all four."""
    sources = {item["valuation"]["source"] for item in bag_payload_json["items"]}
    assert {"bulk", "exchange", "trade", "note"} <= sources


def test_every_fixture_row_knows_where_it_sits(bag_payload_json: dict):
    """The grid is a *map* (SPEC §6.3) — a coloured cell is the slot the cursor has
    to find. A row without a slot cannot be drawn on it."""
    assert all(item["slot"] is not None for item in bag_payload_json["items"])
