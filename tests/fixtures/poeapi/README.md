# poeapi fixtures

Recorded shapes for the account endpoints of SPEC §4.2 and the rate-limit headers of
SPEC §4.4. Every test in this repository runs against these; **no test may hit a
live API**.

## Provenance, stated honestly

The *structure* was read off the live account on 2026-08-10 through the `poe` MCP
server, which is configured with a working POESESSID. What is written here is not a
verbatim capture, for two reasons:

1. **The MCP server reshapes the payload.** It adds `rarity` and `frameTypeId`, and
   it converts `implicitMods`/`explicitMods` from the API's `["+59 to Armour"]` into
   `[{"description": "+59 to Armour", "flags": {"crafted": true}}]`. The fixtures are
   written back to the **wire format** — plain strings, crafted mods in their own
   `craftedMods` array — because that is what `net` will actually receive.
   `normalize.py` accepts both forms, so a future verbatim capture will still work.
2. **Everything identifying is replaced.** Account name, character names, item ids
   and stash tab ids are obvious placeholders; item ids are `sha256("poedex-fixture:
   <label>")`, not real ones. `note` values keep the *format* the user actually uses
   (`~price 3 divine`, `~b/o 25 chaos`) because Phase 3 depends on parsing it, but
   the prices are invented.

The generator lived in a scratch directory and is not committed; regenerating means
re-capturing, which means re-scrubbing. If you do that, re-read this file first.

## What each file pins down

| File | Purpose |
|---|---|
| `profile.json` | What `/api/profile` answers from the session cookie alone. The **account name** comes from here: it is the only account endpoint that takes no `accountName`, which is what lets it supply one to the two that require it. `name` carries a discriminator, as every live one now does. |
| `get-characters.json` | The bare array `get-characters` returns. One entry has `current: true`. |
| `get-items.json` | `{items, character}`. Deliberately covers every category branch in `normalize.py`. |
| `get-stash-tabs.json` | `tabs=1` response: tab metadata, including a remove-only and a hidden map tab. |
| `get-stash-items.json` | One tab's items, including `stackSize` legitimately above `maxStackSize`. |
| `headers-items-authenticated.json` | `backend-item-request-limit`, three fields per entry. |
| `headers-characters-authenticated.json` | `backend-character-request-limit`, **two** fields per entry on the rule header. |
| `headers-items-anonymous.json` | The same endpoint, unauthenticated: same policy name, different rules and numbers. |
| `headers-items-restricted.json` | A 429 with `Retry-After` and a non-zero restriction in the `-State` header. |

`get-items.json` is the one to extend when a normalization bug is found. The items in
it are chosen for their awkwardness, not their realism:

- a rare with `craftedMods` split out of `explicitMods`
- a magic flask with `utilityMods` (dropping those makes it look white)
- a 6-socket body armour with two link groups and a nested `socketedItems` gem
- currency with the stack size in `stackSize` *and* in a `Stack Size` property
- a map (`Map Tier`) and a fragment, which share the `2DItems/Maps` art tree
- an item whose `name`/`typeLine` carry `<<set:…>>` localization markers
- an unidentified, fractured rare with a `note`
- an item with **no `id`**, so the synthetic-uid path is exercised
- a legacy literal-path icon instead of a `/gen/image/<base64>` one
- an item with no icon, no known slot and an invented base type, which must come out
  `unknown` rather than being guessed
