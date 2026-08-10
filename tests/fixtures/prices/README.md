# prices fixtures

Recorded shapes for poe.ninja's economy overviews (SPEC §5.1) and the official trade
endpoints (SPEC §5.3). Every pricing test in this repository runs against these;
**no test may hit a live API.**

## Provenance, stated honestly

Captured **2026-08-10** from the live services by plain HTTPS request, then trimmed.
Routes and shapes are recorded in `docs/research-notes.md` §9 and §10.

The poe.ninja files are **verbatim subsets**: the `lines` array is filtered down to a
handful of entries and the surrounding structure — `core`, `items`, sparklines, field
names, value types — is exactly what the server sent. Nothing was rewritten, because
the point of a recorded fixture is to catch the case where the parser agrees with my
assumptions and disagrees with the server. The prices are therefore real Standard
prices from that morning and will not match today's; no test asserts an absolute
value, only relationships between values in the same file.

The trade files are **scrubbed**, because trade listings carry other people's data:

- `trade-fetch.json` — every `account.name`, `lastCharacterName` and `whisper` is
  replaced with `SellerN#0001` / `CharacterN`, the item is flattened to a plain Tabula
  Rasa, and the prices are invented (`4c 5c 6c 8c 10c 12c 15c 1div 2div 40c`). What is
  kept from the capture is the *structure*: where the price lives, that
  `account.online` is present only for online sellers, and that the ten results come
  back in the order the ids were requested.
- `trade-search.json` — the result hashes are replaced with `0000…0001`-style
  placeholders and the query id with `FIXTUREQID`. `total` and `complexity` are real.
- `trade-stats.json` — a real subset of GGG's stat document. Public data, no scrubbing
  needed; trimmed from 409 kB to four mod texts across the groups that carry them.

`bag.json` is **synthetic**, written for this phase rather than captured. It is in the
GGG wire format and goes through the real `normalize.py`, but no item in it came off
anybody's account. Names are real in-game names because they have to match the price
tables; item ids are `sha256("poedex-fixture:<label>")`.

The generators lived in a scratch directory and are not committed. Regenerating means
re-capturing, which means re-scrubbing. Read this file first if you do.

## What each file pins down

| File | Purpose |
|---|---|
| `leagues.json` | The bare `[{id, name}]` array, challenge league first. |
| `exchange-*.json` | The exchange overview shape: opaque `lines[].id`, `primaryValue`, names in a sibling `items[]`, unit in `core.primary`. One per category the prefetch covers. |
| `item-*.json` | The stash item overview shape: `name`, `baseType`, `chaosValue`, `links`, `variant`. |
| `trade-stats.json` | `{result: [{id, label, entries: [{id, text, type}]}]}` — the opaque stat ids. |
| `trade-search.json` | `{id, complexity, result: [hash…], total}`. |
| `trade-fetch.json` | `{result: [{id, listing, item}]}`, ten listings, five online. |
| `bag.json` | A backpack chosen to exercise every branch of the pricing engine. |

## Why these particular lines

The trimming is not arbitrary — each file keeps the awkward cases:

- `item-uniqueweapon.json` — `Pillar of the Caged God` **six times**: two base types
  (Long Staff, Iron Staff) times three link counts (6, 5, none), spanning 0.96c to
  718,160c. This is the ambiguity `choose_line` exists to resolve, and getting it
  wrong is a 750,000-fold error.
- `item-map.json` — `Map (Tier 16)` **thirteen times**, once per map series, spanning
  1c to 898c. Tests that liquidity picks the current series.
- `exchange-currency.json` — `Divine Orb` (the rate every other conversion needs),
  `Mirror of Kalandra` at 10⁶ chaos and `Orb of Alchemy` at 10⁻³, which is the range
  the CLI's number formatting has to survive.
- `exchange-*.json` for scarab, fossil, essence, oil and delirium orb — all of these
  arrive from the API as `frameType: 5` and are categorised `currency`, so each one is
  a test that routing falls through to the right table.
- `bag.json` — three separate `Chaos Orb` stacks (deduplication), `Jeweller's Orb
  x2615` next to `Divine Orb x5` (stack maths), `Veiled Scarab x170` (a removed item
  absent from the index — the `unpriceable` case from research-notes §7), a note that
  parses, a note in a fraction, a note naming an unknown currency, a note that is a
  sentence, and one worn belt that must never reach the bag total.
