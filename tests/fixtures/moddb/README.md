# `tests/fixtures/moddb/`

## `live_trade_rares.json`

Twenty **real, identified rares** taken from public trade listings in Allflame on
2026-08-10 — ten priced at one fusing, ten at two divine — recorded because Phase 9
shipped the checkbox list without ever having seen it run on an item with readable
mods. The live account's highlighted items were all unidentified, and no test may hit
the network, so this is the substitute: public listing data, frozen.

**Scrubbed.** No account name, no listing id, no price, no item id, no icon URL, no
`~b/o` note. What is kept is what mods exist in the league and what GGG says about
them. Nothing here identifies a seller or a sale.

### Why it is worth a fixture rather than a one-off script

The trade API answers the same questions `moddb` does, and it is the authority on both
of them:

* **`ggg[].trade_stat_id`** — the opaque id GGG's own filter list uses for that
  sentence, on that item. This is the only external check the text→id bridge has ever
  had. Phase 9b's whole finding is that the bridge was returning the *global* id for
  local mods, and the check that would have caught it is exactly this one.
* **`ggg[].tier`** — GGG's own tier label, `P1`/`S5` for a dropped affix and `R2` for
  a bench craft. It gives `moddb`'s tier ladders a ground truth per base.
* **`ggg[].affixes`** — how many affixes the game **summed** into that one displayed
  line. Two of the twenty items have one. This is a case `moddb` does not model at
  all: `+161 to Evasion Rating` is a P2 hybrid plus a P3 prefix, and asked about the
  sum `moddb` finds the one tier whose range contains 161 and answers `T1 of 8`
  confidently. The fixture records the trap rather than hiding it.

### Refreshing it

Not required — this is evidence, not a mirror. If it is ever refreshed, note that the
trade API's `fetch` flattens crafted and fractured mods into `explicitMods` with a
`domain` field, while the account API keeps them in separate arrays. They have to be
split back apart or `moddb` is asked to attribute a bench craft as a dropped affix,
which it correctly refuses to do, and every count comes out wrong.
