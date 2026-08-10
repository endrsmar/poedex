# The mod database artifact

`moddb.json` is a **committed build artifact**. It is not fetched, not cached, and
not downloadable at runtime — it ships in the repository and in the plugin zip,
because a Decky plugin installs from a zip with no pip and has no business pulling
30 MB of JSON over the user's connection at first launch.

## Regenerate it every league

```bash
./.venv/bin/python scripts/build_moddb.py            # fetch upstream, write the artifact
./.venv/bin/python scripts/build_moddb.py --check    # is the committed one current? (exit 1 if not)
```

**This is the maintenance obligation of the whole module, and skipping it does not
produce an error.** A mod database one league old still answers every question, still
sounds certain, and is wrong about tiers GGG re-levelled, mods GGG added and spawn
weights GGG moved. That is this project's worst failure mode — a confident wrong
answer the player cannot check — so:

- the artifact stamps `source.game_version` (upstream's `version.txt`) and
  `source.generated_at`;
- `ModDbApi.version()` exposes both, and `DbVersion.describe()` is written to be
  shown to a player;
- `poedex moddb` prints them as its **first two lines**, before anything else;
- `modules/moddb/backend/module.py` logs a warning at start once the artifact is more
  than `STALE_AFTER_DAYS` (120, roughly a league) old.

`--check` is what a release script should run. It compares content and ignores only
`generated_at`, so a rebuild that changed nothing but the clock does not read as a
change.

## Where it comes from

[repoe-fork](https://github.com/repoe-fork/repoe-fork), the maintained fork of RePoE
(which was archived in December 2024), publishing to `repoe-fork.github.io`:

| upstream file | size | what survives the trim |
|---|---|---|
| `mods.min.json` | ~22 MB | prefix/suffix mods in the affix domains, with their rendered text, roll ranges, required level, spawn weights and influence pool |
| `base_items.min.json` | ~2.9 MB | bases that roll affixes: item class, tags, drop level, domain |
| `stat_translations.min.json` | ~4.6 MB | the text ↔ trade-stat-id ↔ game-stat-id bridge, for the texts that survived |

**Do not commit the upstream files.** `scripts/build_moddb.py --source-dir` exists so
they can be kept outside the repository (`/tmp`, a scratch directory) and the build
re-run offline.

## What was dropped, and why it is safe

Of ~40 000 upstream mods, ~7 800 survive. Everything dropped is a thing the consumers
in `api.py` cannot ask about:

- `generation_type: unique` (15 886) — unique-item mods are fixed by the unique, not
  rolled, so there is no tier to report.
- Non-affix domains: `area` (map mods), `monster`, `atlas`, `watchstone`, `heist_*`,
  `sanctum_relic`, `crucible_*`, `delve_area`, `expedition_relic`, `necropolis_*`,
  `brequel_graft`, `synthesis_*`, `memory_lines`, `mercenary`, `chest`,
  `primordial_altar`, `map_device`, `map_relic`, `leaguestone`, `sentinel`,
  `deepwater_*`, `templar_relic`, `affliction_charm`, `ducat_crafted`, `dummy`.
- Non-affix generation types inside kept domains: `enchantment` (a lab enchant is not
  an affix and occupies no slot), `corrupted` implicits, the Eater/Exarch implicits,
  `crucible_tree`, everything `scourge_*`, `talisman`, `bestiary`, `torment`,
  `tempest`, `nemesis`, `bloodlines`.
- Per mod: the stat id list, `adds_tags`, `implicit_tags`, `generation_weights`,
  `grants_effects`, the mod's own id and its `name` ("of the Bear"). Group plus tier
  is the identity a consumer needs; the display name is never shown.
- Per base: `properties`, `visual_identity`, `requirements`, `inherits_from`,
  `implicits`, and every base whose domain cannot carry affixes.
- Trade namespaces `pseudo` (an aggregate no single mod produces) and `scourge`.

## The compact encoding

Every repeated string is interned into `vocab`; spawn-weight vectors are shared
between the tiers of a group (7 800 mods, 743 distinct vectors); a mod's rendered
lines are one flat `[text, lo, hi, lo, hi, text, …]` list whose arity is recovered
from the number of `#` in each text. `modules/moddb/backend/database.py` gives the
integers their names back. Nothing about the encoding is load-bearing for
correctness — `modules/moddb/tests/test_artifact.py` checks every index resolves.
