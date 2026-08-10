# stash fixtures

An eight-tab stash, for Phase 10. Every test that touches the stash runs against these;
**no test may hit a live API.**

## Provenance, stated honestly

The *shape* is the wire format of `/character-window/get-stash-items` — `{numTabs, tabs, items}`
— and the tab set is modelled on what research-notes §7 measured on the live account. The
*contents* are not a capture: every item is copied from an already-scrubbed fixture
(`tests/fixtures/poeapi/get-stash-items.json`, `tests/fixtures/appraisal/loot-bag.json`) and
re-placed, and every id is `sha256("poedex-fixture:<label>")`. No account name, no character
name, no real item id, no real tab id.

Tab names follow the real account's *style* — one-letter names for special tabs, a
`(Remove-only) …` prefix GGG writes itself — because the remove-only rule is a **string test**
on that prefix and a fixture that spelled it differently would not exercise it.

## What each tab is for

| Tab | Type | Why it exists |
|---|---|---|
| 0 `C` | `CurrencyStash` | A special tab: bespoke layout, no lattice, and `Vaal Orb 163/20` — `stackSize` legitimately above `maxStackSize`. Also `Jeweller's Orb ×2615`, because value lives in stack sizes |
| 1 `Gear` | `PremiumStash` | Where the rares are (they are spatially segregated on a real account). `Soul Bind` is the **strict/generous divergence**: flagged by the generous gate, silent under strict |
| 2 `Sext` | `QuadStash` | 24x24, with an item at `(23, 23)` — on a 12x12 lattice it is off the board and silently disappears |
| 3 | `NormalStash`, remove-only | The fetch-once-forever rule, holding 170 `Veiled Scarab` — a removed item the price index does not carry, so `unpriceable` rather than `trash` |
| 4 `M` | `MapStash` | Returns nothing, as five real map tabs did across two leagues. Must be reported *not supported*, never as an empty tab worth 0c |
| 5 `D` | `DivinationCardStash` | A bulk tab: nothing in it enters the tier-2 gate |
| 6 | `NormalStash`, remove-only | A second permanent tab, so "how many were cached for free" is more than one |
| 7 `Later` | `PremiumStash` | **No file at all.** A tab nobody has read, which must never render as empty or as 0c |

`tab-N.json` carries only `items`; the scripted server in `tests/conftest.py` attaches the tab
list, because the real endpoint returns both from one request and that is what makes a cold
open of one tab cost one request rather than two.

Regenerating means re-scrubbing. The generator lived in a scratch directory and is not committed.
