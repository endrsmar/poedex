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
| Rares | 8 | The only rows the gate can speak to. Deliberately one per branch — see below. |
| Worn `Headhunter` | 1 | In the `Belt` slot, worth 8,977c. If it ever appears in a bag total, the `Source.BAG` filter has broken, and at that value it is impossible to miss. |
| `Book of Skill` | 1 | A quest item (`frameType: 7`). Cannot be traded and cannot be vendored, so it is `not_loot`. The first live appraisal filed a quest item under `TRASH`, whose headline is *vendor* — an instruction the game will not let the player follow. |

### The rares, and which gate each one is for

| Item | Strict | Generous | Why |
|---|---|---|---|
| Unidentified `Hubris Circlet` ilvl 86 | pass | pass | Two hard requirements: ilvl 86 on a base where it matters, and an allowlisted base. Generous adds `unidentified` — the mods cannot be read, so nothing else can fire. |
| `Vaal Regalia`, 6-link | pass | pass | Six-link, plus an allowlisted base. |
| `Leather Belt`, Shaper | pass | pass | Influence. |
| `Two-Stone Ring`, fractured | pass | pass | Fractured. |
| `Coral Ring` ilvl 81 | **fail** | pass | 118% total resistance clears the roll threshold, and nothing else does. The clearest divergence: a genuinely sellable ring the strict gate throws away and the generous gate catches. |
| `Rusted Sword` ilvl 62 | **fail** | pass | Life, attack speed and flat physical all *present*, all mediocre. This is SPEC §5.2's "false-positive engine" in one item — the reason the mod-group-present signal is dropped at strict. |
| `Iron Hat` ilvl 41 | fail | fail | Armour, rarity and stun recovery. No group matches at all. |
| `Onyx Amulet`, `~b/o 40 chaos` | pass | pass | An allowlisted base, so the gate fires — and it does not matter, because tier 0 prices it at 40c and `keep` outranks `check`. Proves the ordering. |

One magic `Diamond Flask` is included so the gated-category rule is exercised by
something other than a rare.
