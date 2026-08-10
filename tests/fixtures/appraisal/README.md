# appraisal fixtures

## `loot-bag.json` — synthetic, and that matters

A backpack in GGG's wire format, normalized by the real `normalize.py` and priced by
the real poe.ninja tables in `tests/fixtures/prices/`. **No item in it came off
anybody's account.** It was written for this phase because
`tests/fixtures/prices/bag.json` was written to exercise the *pricing* engine, and
its four rares carry `ilvl: 0`, no influence, no sockets and one mod each — a bag on
which the tier-2 gate has nothing whatever to read, and against which both strictness
levels would trivially agree.

Read that limitation before drawing conclusions from anything this file produces.
A hand-written bag cannot surprise the person who wrote it: every verdict it yields
is one somebody chose in advance. It is good enough to prove the two gates *diverge*,
that every branch is reachable, and that the numbers add up. It is not evidence that
the gate is right about real loot, and Phase 4's validation checkpoint should not be
answered from it alone.

Item ids are `sha256("poedex-fixture:<label>")`. Names are real in-game names,
because they have to match the price tables.

### What each group is for

| Group | Rows | Point |
|---|---|---|
| Bulk currency | 14 | Realistic composition: most of a map's drops are worth under a chaos. Spans `Orb of Alchemy` at 0.0025c to `Divine Orb` at 897.7c, which is the range the verdict thresholds have to survive. |
| `Veiled Scarab x23`, `Blighted Scouting Report x4` | 2 | `unpriceable`, the two ways it happens: a removed item absent from the league index (research-notes §7) and a current item the prefetched tables do not cover. Both are holes in the total; neither is trash. |
| `Choking Guilt`, `Grotto Map` | 2 | A card and a map — priced by name and by *tier* respectively (SPEC §5.1). |
| `Goldrim`, `Quill Rain` | 2 | Uniques: indexable, cheap, and therefore genuine `trash`/`check` rather than `unpriceable`. |
| Rares | 9 | The only rows the gate can speak to. Deliberately one per branch — see below. |
| Worn `Headhunter` | 1 | In the `Belt` slot, worth 8,977c. If it ever appears in a bag total, the `Source.BAG` filter has broken, and at that value it is impossible to miss. |
| `Book of Skill` | 1 | A quest item (`frameType: 7`). Cannot be traded and cannot be vendored, so it is `not_loot`. The first live appraisal filed a quest item under `TRASH`, whose headline is *vendor* — an instruction the game will not let the player follow. |

### The rares, and which highlighter each one is for

Rewritten for Phase 9. The table below is what the **`moddb`-backed** highlighter
says, which is not always what the deleted constants said — and where they differ,
the difference is the point.

| Item | Strict | Generous | Why |
|---|---|---|---|
| Unidentified `Hubris Circlet` ilvl 86 | pass | pass | `top_tier_base_item_type` **and** fully rolled. Note the old gate needed ilvl 86 for this; the base tops out at affix level **85**, so it now fires a level earlier. Generous adds `unidentified` — the mods cannot be read, so nothing else can. |
| `Vaal Regalia`, 6-link | pass | pass | Six-link. Generous also reads `98% increased Energy Shield` as a top-tier roll. |
| `Leather Belt`, Shaper | **fail** | **fail** | **Changed deliberately.** The item carries the Shaper *tag* and no mod from the Shaper pool, so §5b's fourth criterion — influence **mods** — is not met. The old gate flagged the tag, which is a highlight on nothing. |
| `Two-Stone Ring`, fractured | pass | pass | Fractured. Retained beyond §5b's four criteria; see `gate.py`. |
| `Siege Helmet` ilvl 86, `+120 to maximum Life` | **fail** | pass | **T2 of 10 on this base.** Near-top but not top, on a ladder long enough for the distinction to mean something — the generous-only signal, and the one that could not exist while a threshold was the only tool. |
| `Coral Ring` ilvl 81 | fail | fail | **Changed deliberately.** 118% total resistance used to clear a hand-typed threshold. On a Coral Ring the individual rolls are T3–T4 of 8, and there is no such thing as a "total resistance" tier. |
| `Rusted Sword` ilvl 62 | fail | fail | The old "false-positive engine": life, attack speed and flat physical, all present, all mediocre. The whole signal is gone, so the item is gone with it. |
| `Iron Hat` ilvl 41 | fail | fail | Armour, rarity and stun recovery. Nothing high-tier, nothing factual. |
| `Onyx Amulet`, `~b/o 40 chaos` | fail | fail | **Changed deliberately.** An Onyx Amulet is sought after as an *opinion* only at full item level; this one is ilvl 84 against a ceiling of 86. It does not matter: tier 0 prices it at 40c and `keep` outranks `check`. |

One magic `Diamond Flask` is included so the gated-category rule is exercised by
something other than a rare.
