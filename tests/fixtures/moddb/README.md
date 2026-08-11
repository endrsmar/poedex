# `tests/fixtures/moddb/`

## `source/`

A 304 KiB sample of the four documents `scripts/build_moddb.py` builds from — 211 mods,
150 bases, 109 translation entries and 439 of GGG's stat entries, cut down from 30 MB.
Public game data, verbatim; nothing here identifies anybody and nothing is rewritten.

It exists so `tests/test_moddb_build.py` can build the artifact **twice from the same
bytes and compare them**, which is the property that was missing and the reason a
regeneration could not be trusted. The real sources are 30 MB fetched from two live
endpoints, one of which is neither versioned nor immutable; neither belongs in a test
run or in a repository.

**Selection is by coverage, not by frequency**, because a determinism test over easy
cases proves nothing. It carries both readings of the ambiguous local/global sentences
(94 ambiguous lines, 47 of them local — a sample that answered one way would pass with
`line_locality` hardwired), the `-#` sentences that reach the signed-key fallback, the
influence pools, essence-only mods, and at least one prefix and one suffix in every
surviving domain. `test_the_sample_exercises_the_parts_where_an_ordering_bug_could_hide`
asserts all of that, so the sample cannot quietly decay into a rubber stamp.

### Refreshing it

Every league, from the same documents the artifact is built from, in the same run:

    ./.venv/bin/python scripts/build_moddb.py --sample-to tests/fixtures/moddb/source

It is a mode of the build script rather than a script of its own on purpose: a sampler
that drifts from the builder proves nothing about the builder.

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
