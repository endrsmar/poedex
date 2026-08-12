# PoEDex

A modular Path of Exile assistant for Steam Deck **gaming mode** and desktop. First feature is
loot appraisal: "is any of this worth a stash trip, or is it all vendor trash?"

## Start here

- [`docs/IMPLEMENTATION-PLAN.md`](docs/IMPLEMENTATION-PLAN.md) — module architecture, the UI
  framework, build phases. **Read this first.**
- [`docs/SPEC.md`](docs/SPEC.md) — data layer, pricing tiers, constraints, open questions.
- [`docs/research-notes.md`](docs/research-notes.md) — the evidence. Read before proposing an
  alternative architecture; most obvious ones are already ruled out with measurements.
- [`docs/ui-mockups.html`](docs/ui-mockups.html) — UI at true Deck proportions. Open in a browser.

## Project state

**Phases 1, 2, 3, 4, 4b, 5, 6, 7, 8, 9, 9b, 9c, 10 and 11 done** — with **Phase 7 built and never run
on a Steam Deck**, which is a different kind of done and is tracked as such in
[`docs/deck-checklist.md`](docs/deck-checklist.md). `runtime/` (registry, context, events, storage,
settings, methods, redacting log); core modules `credentials`, `net` (header-driven limiter +
httpx), `poeapi` (endpoints, normalization, cache), `gamelog` (read-only Client.txt tail),
`moddb` (a trimmed mod database: real tiers per base, affix counts, influence pools);
feature modules `prices` (poe.ninja bulk tables with per-league type discovery, tier-0 notes, a
bulk exchange fallback, a trade client) and `appraisal` (a `moddb`-backed **highlighter**,
five-state verdicts, and a player-driven price check); a `poedex` CLI; and **the first usable
surface** — `ui-kit/` (`@poedex/ui`, two profiles behind a build-time alias), `frontend/core/`
(transports, stores, TS types generated from the pydantic models), `transports/http/` (FastAPI
on 127.0.0.1 + SSE + the built SPA), `surfaces/web/` and `modules/appraisal/ui/`. Boundaries are
enforced in both languages: the Python AST tests, and an ESLint rule over `modules/*/ui`.
**Phase 10** added the stash: tab enumeration with per-tab layouts and staleness, the remove-only
cache rule, a resumable user-initiated crawl, the strict gate over a tab, `poedex stash`, and a
`full`-only stash screen. **Phase 7** added the Deck: the `compact` profile against `@decky/ui`
behind a guard, `transports/decky/` + `plugin/`, `DeckyTransport`, the QAM panel with a screen
stack, LAN pairing in `modules/credentials/`, and `scripts/build_plugin.py` producing a 3.4 MB
Release zip. **Phase 11** closed the last three defects: `scripts/build_moddb.py` is reproducible
and proved so by a test that builds twice and compares bytes (the artifact was regenerated, and
the diff is four sentences); skill gems are priced on an exact `level/quality/corrupted` variant
match against a table fetched **lazily**, only when a bag or tab holds a gem; and every message
that tells a user to set a setting names `poedex config set <module>.<key>`, checked by a sweep
over the whole tree rather than a list of four names.

**The first real Deck run closed a blocker**: pairing succeeded and then every request failed
with *"no account name on record"*. `get-items` needs an `accountName`, no other account
endpoint returns one, and the LAN pairing form has two fields — so the Deck path was dead from
the moment it worked. The name is now **derived** from `GET /api/profile`, which answers from
the session cookie alone; `poeapi.get_profile()` caches it for a day, the precedence runs
explicit → `poeapi.account` → the credential record → profile → raise, and a pair files the name
with the credential as it lands (and still succeeds if that lookup fails, saying so). This is
also more correct than asking: a *wrong* account name came back as the same 403 as an expired
session. **The rest of `docs/deck-checklist.md` still needs running on hardware** — eleven items,
about twelve minutes, and nothing else above the line has been checked against a real Deck.

**The second real Deck finding was the wrong character, read silently.** `get-characters`
does **not** send `current` to a client that is out of game — it is absent from every entry of
the live roster — so the old default fell through to `characters[0]`, which is GGG's own
ordering and picked a parked Standard character on an account whose owner plays a league one.
Nothing looked wrong: the header read `character: <name>` with exactly the confidence it has
when somebody chose it. GGG *does* publish **`lastLoginTime`** per entry, which is what the PoE
website's own top bar reads and what the tool now ranks on. Precedence is explicit →
`POEDEX_CHARACTER` → `poeapi.character` → a character marked `current` → highest
`lastLoginTime` → **a visible guess**. There is a `poeapi/ui` character picker, on both
profiles, because on a Deck the panel was the only place this could be fixed and it could not:
the plugin reads `DECKY_PLUGIN_SETTINGS_DIR`, not `~/.config/poedex`, and there is no usable
CLI inside the plugin tree.

    poedex characters # the roster with leagues, and which one is read — and why
    poedex serve      # http://127.0.0.1:7331 — the priced bag, with verdicts and provenance
    python scripts/build_plugin.py   # dist/poedex.zip — the Decky plugin, ~3.4 MB
    poedex stash      # the tab list: freshness, contents, value. Zero item requests
    poedex stash tab N   # one tab, judged at stash strictness. One request, ~1s
    poedex price UID  # one item's mods with real tiers, then the query you chose
    poedex moddb      # how old the mod database is, and what it says about a mod
    pnpm install && pnpm build && pnpm run check

## The design pivot

**Automatic rare pricing is abandoned.** It failed twice against the live account: querying every
mod gave zero listings; querying one loose mod matched a single listing and reported 10c for an
item worth 1c. No heuristic recovers *which mods make an item interesting* — that is player
knowledge.

The model is Awakened PoE Trade's: the tool **highlights items that are potentially expensive**,
shows their mods as a checkbox list with the significant ones pre-ticked, and the player triggers
the price check. `moddb` (Phase 8) supplies the facts; **Phase 9 rebuilt `gate.py` on them and
deleted every constant** — `MOD_GROUPS`, `HIGH_VALUE_BASES`, `ILVL86_BASE_CATEGORIES`,
`ILVL86_EXCLUDED_SUBCATEGORIES`. An appraise now makes one account request and **zero** trade
requests; `AppraisalApi.price_check` is the only thing that spends, and it runs the player's
selection.

**Read Phase 4's validation finding before building UI on the bag screen**
(IMPLEMENTATION-PLAN §5, Phase 4). The four-state verdict works and the totals are honest, but
`check` currently absorbs everything between 1c and the keep threshold *and* everything the gate
flags, which are different questions sharing a colour. Consider splitting them before the panel
is drawn, not after.

**Nothing here has run against the live API, a real Client.txt, or a Deck.** Phase 4's
validation checkpoint was therefore answered against a fixture bag somebody wrote, which is a
much weaker test than it sounds — see the phase note in the plan. **Phase 7 built the whole Deck
surface without one either**, so the geometry, the D-pad, suspend/resume and the pairing flow are
written and unverified. Four things need a human and would close most of the open risk:

- **`docs/deck-checklist.md`, on a Deck.** Eleven items, about twelve minutes, ordered by how likely each is
  to be wrong. Every one of them fails by looking slightly off rather than by throwing, which is
  why each item says what the failure looks like.

- `poedex appraise` against a real backpack after a real map. Until then nobody knows whether
  the verdicts are surprising or obvious, and that is the question Phase 4 was supposed to settle.

- `poedex selftest freshness` — needs someone in the game (research-notes §2.1).
- `poedex gamelog status` and `poedex gamelog watch` on the Deck through one portal-to-hideout
  and one map entry — would confirm the path probes, the town/hub area ids, and whether
  `Generating` and `You have entered` really arrive back-to-back.

The rate limiter has never seen a real GGG response header. If parsing degrades it falls back to
a 1-request-per-10s seed budget — safe, but it looks like the tool is broken rather than
mis-parsing. `poedex limits` still showing seed buckets after several requests is the symptom.

```bash
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'   # 3.11+
.venv/bin/pytest        # everything offline
.venv/bin/ruff check .
poedex sync             # normalized bag; spends real rate-limit budget
poedex value            # the bag, priced. one GGG request; poe.ninja is free
poedex appraise         # the bag, judged. one account request, zero trade requests
poedex price <uid> --dry-run    # an item's mods with real tiers; spends nothing
poedex price <uid> --mods 0,3 --open-prefixes 1   # ...and the query you chose
poedex limits           # what the limiter has learned
poedex config list      # every setting, its value, and whether it is stored or the default
poedex config set net.contact you@example.com    # the setting three messages used to ask for
poedex stash                    # tab list, freshness and value. Spends no item requests
poedex stash tab 3              # one tab, judged strict. One request, or none if cached
poedex stash plan               # what a full refresh costs right now, in requests and minutes
poedex stash crawl --yes        # the cold crawl. Minutes. Resumable. Never automatic
poedex price <uid> --tab 3      # the same manual check, on an item in a stash tab
```

**Every message that names a setting names the command that sets it.** `poedex config`
(list/get/set/unset) is driven by the schema registry, so a new setting is listed, described and
validated without touching the CLI. The POESESSID is **not** a setting — `credentials` keeps it
in its own file and registers one integer — so no config command can reach it, and a test walks
every key to prove it.

Module layout is fixed by the registry: `modules/<id>/backend/module.py` exports `MODULE`, a
module instance; `api.py` is the only file dependents may import.

## Module architecture in one paragraph

Everything is a module, and **a module is a vertical slice** — backend logic and its own UI in one
directory. *Core* modules (`credentials`, `net`, `poeapi`, `gamelog`, `moddb`) are PoE infrastructure with
no feature opinion and may not depend on feature modules. *Feature* modules (`prices`,
`appraisal`, later crafting and guides) hold the opinions and may depend on each other. Modules
depend on interfaces (`api.py` Protocols), never implementations. Module UI is written once
against the `@poedex/ui` kit and reshaped by **surface profiles** — `compact` (300 px, gamepad,
`@decky/ui`) and `full` (web, unconstrained) — so one `BagScreen.tsx` renders on both. Boundaries
are enforced by tests in both languages, not by discipline.

## The one thing to understand

The tool never touches the game client. It has exactly two inputs, both passive:

1. **HTTPS reads** of account data from `pathofexile.com`.
2. **A read-only tail** of `Client.txt`, the log the game writes to the Linux filesystem.

No clipboard, no keystroke injection, no memory reading, no OCR, no overlay on the game. This is
why it works in gaming mode when Awakened PoE Trade does not, and it is what keeps the project
inside GGG's terms. Any proposal reintroducing client interaction is a regression.

The compliance risk is **API request volume**, not the log file. GGG revokes API access for
sustained rate-limit violations.

## The interpreter gap — read before trusting a green suite

**Tests run on CPython 3.12. The Decky plugin runs on 3.11.** The plugin backend is a
fork of the frozen Decky Loader, which is PyInstaller-built against **3.11.7** — not
SteamOS's Python, which is 3.13.

That gap has already produced one fatal bug that 1369 green tests could not see:
`runtime_checkable` Protocol `isinstance` calls `hasattr` on 3.11 and earlier, and
`hasattr` only swallows `AttributeError`. A module exposing state through a property
that raises before `start()` therefore detonated during *registration*. Python 3.12
rewrote that check to use `inspect.getattr_static`, so it is silent here and fatal
there.

When a change touches descriptors, protocol conformance, import machinery or
anything else with version-dependent semantics, exercise it under `python3.10` or
`python3.11` in a subprocess — `tests/test_registry_protocols.py` and
`tests/test_plugin_shadowing.py` are the pattern. Both are skipped, loudly, when no
such interpreter exists.

## Facts that constrain every design decision

- **Inventory commits at zone transitions, not live.** Measured, ~90% confidence. Real-time drop
  feedback is physically impossible; the tool is event-driven off zone changes.
- **Stash is near-live** and updates without zoning — the fresher data source.
- **One item-endpoint request per 18 seconds, sustained.** `get-items` and `get-stash-items`
  share that bucket; trade endpoints do not. Never poll on a timer.
- **Never hardcode rate limits.** Parse `X-Rate-Limit-*`; policies differ between authenticated
  and anonymous on the same endpoint. A host that sends none — poe.ninja — gets a fixed courtesy
  budget keyed by hostname, pinned so no response header can merge it into GGG's buckets.
- **poe.ninja's routes moved and will move again.** They are measured in research-notes §9, not
  guessed, and the operator states there is no stability guarantee. Re-measure before debugging.
- **Never hardcode which price tables exist.** The list was 26 types typed in by hand, `Ducat`
  was not one of them, and a whole item class was `unpriceable` for a league while poe.ninja
  published prices for it the entire time. `modules/prices/backend/discovery.py` asks the league:
  sitemap slugs → derived type names → probe → per-league record. It must stay able to find a
  type nobody has typed into `CATALOGUE`; a discovery that only confirms known names is the same
  bug with more steps. There is no type-index endpoint — research-notes §9.6 has the 404s.
- **Tier 3 is never automatic, for a bag or a stash.** Phase 4b's eager pass is deleted: it failed
  twice against the live account, once by ANDing every mod and matching zero listings, once by
  matching a single listing and reporting 10c for an item worth 1.00c across 438 comparables.
  There is no `escalate` parameter and no code path one could switch on. A rare is highlighted;
  `poedex price <uid>` or the panel's **Check price** is how a number is asked for.
- **Batching the bulk exchange is not free.** The response caps at 100 rows sorted cheapest-first
  across the whole batch, so ten ids in one request returns everyone's floor. Starved wants are
  re-queried alone. research-notes §11 has the table.
- **`unpriceable` is never zero.** A removed item absent from the price index is a hole in the
  total, and reporting it as worthless understates the bag badly (SPEC §5.4). `appraisal` splits
  it further: an item the index *should* carry and does not is `unpriceable`; a rare, which no
  bulk table has ever priced, is a tier-2 question and not a gap.
- **There are no mod thresholds left in `gate.py`, and there must not be new ones.** Phase 9
  deleted all four constants. `moddb` answers per base and per pool: `+95 to maximum Life` is
  T4 of 10 on a helmet and T7 of 13 on a body armour, ceilings 144 and 189. Two of the deleted
  constants were factually wrong — a **Hubris Circlet tops out at affix level 85**, so the old
  `ilvl >= 86` was noise on a third of the gear it fired for, and flasks were excluded on the
  grounds that their mods do not scale with item level when the top flask suffixes need 84–85.
  Anything reintroducing a number to compare a roll against is reintroducing the bug.

- **"Which mods make this item interesting" is a question, not a derivation.** The query is built
  from `Selection` — the indexes the player ticked — and from nothing else. A ticked roll searches
  `min = roll * 0.8`, never the exact value; a manual check never broadens itself, because
  broadening answers a different question and reports the answer under the player's heading.

- **The same sentence is two different stats, and the item decides which.** GGG publishes 22
  sentences twice — `#% increased Armour` is a global stat on a ring and `#% increased Armour
  (Local)` on a body armour. Phase 9's bridge sent the global id for both, and measured live that
  matched **0** rare body armours against 10 000+ for the local one. Correct bridge coverage was
  87.1%, not the 94.3% that "resolved to an id" suggested; it is 96.9% now. Which reading a line is
  comes from the *mod's* own `local_*` stat id, is decided at build time, and travels
  `ModMatch.local` → `ModOption.local` → `ModFocus.local` → `StatIndex.stat_id(local=…)`. It is
  never re-derived from the text, because the text is exactly what cannot say.

- **`moddb`'s build has a fourth source, and it is GGG's own filter list.**
  `/api/trade/data/stats` is fetched by `scripts/build_moddb.py` because it is the only published
  place the local stat ids exist. It is a *supplement*: RePoE stays primary, since GGG's document
  carries 77 sentences under two ids with nothing to choose between them and only RePoE knows
  which game stat wrote the line. Still no runtime download, still not committed.

- **The trade stat index keys by `(text, group)`, and `pseudo` never wins.** `StatIndex` used
  `setdefault` and GGG puts `pseudo` first, so `Adds 12 to 30 Physical Damage` resolved to
  `pseudo.pseudo_adds_physical_damage` — an aggregate over the whole item — rather than
  `explicit.stat_960081730`. Measured live, the aggregate matches a strict superset: 161 listings
  against 160 for the same movement-speed filter on Two-Toned Boots.

- **The pre-tick proposes at most two filters, and 2 is a measurement.** On twenty real rares it
  used to tick a median of 3 and, on the best item in the sample, **6 of 6** — the conjunction
  Phase 9 measured returning zero listings, sent by the default press with no broadening behind
  it. Live against the fixture's 2-divine Soldier Gloves: **6 filters → 0, 3 → 0, 2 → 3, 1 → 35**,
  so `MAX_PRETICKED` is 2 and not the 3 the median suggested. The order is
  `highlight.significance`, ranked on facts `moddb` already has — influence pool, then **the item
  level the game demands for the roll**, then proximity to the base's ceiling, then ladder depth.
  **A junk-mod list is `MOD_GROUPS` coming back** and must not be added; the noise count fell 8 →
  4 without one, and the four survivors are genuine T1 rolls of groups nobody searches, which is
  §5b's conclusion and not a bug. The cap is on the *proposal*: `MAX_QUERY_FILTERS` is still 6, no
  row is disabled, and the note says how many high rolls were left unticked.

- **A checkbox list is the one list that may not truncate.** Every other kit list takes a `limit`
  and reports what it hid; `CheckList` takes none. A hidden row is a filter the player can neither
  see nor switch off, and an item's six affixes already bound it.

- **A mod database goes stale silently, and that is its whole risk.** It does not fail; it
  answers confidently and wrongly about tiers GGG re-levelled. So `modules/moddb/data/moddb.json`
  is a **committed build artifact** stamped with its game version and build date, regenerated by
  `python scripts/build_moddb.py` **every league**, and four surfaces show its age (the CLI's
  first two lines, `ModDbApi.version()`, the `moddb.version` method, and a warning in the log
  past 120 days). `--check` answers "is the committed one current?".

- **Nothing downloads game data at runtime.** 30 MB of repoe-fork JSON trims to 566 KiB at build
  time. A Decky plugin installs from a zip with no pip, and a runtime fetch would put startup on
  a GitHub Pages site. `moddb` imports no networking module at all, and a test walks its AST to
  keep it that way.

- **`moddb` says "unknown" rather than guessing which mod produced a line.** Several groups can
  render one sentence, hybrids write several, essence, influence and bench-craft tiers are counted
  from different ladders, and **the game adds two affixes of the same stat into one displayed
  line** — `+161 to Evasion Rating` is a hybrid plus a prefix, and the single-affix reading of it
  was `T1 of 8`, asserted. `Attribution` is `exact`/`group`/`ambiguous`/`unknown` and the last two
  expose no tier. Showing "T2" for the rest would be right most of the time, which is exactly what
  makes it dangerous. The summed reading is refused only where the item has a **free affix slot**
  for the second mod — per line the sum is conceivable on 12 of 99 fixture lines, against the
  affix budget on 5, and the difference is a refusal rather than a policy of not answering.

- **A widened floor is only a floor if up is good.** About ten gear sentences roll negative
  (`-9 to Total Mana Cost of Skills`) and for those a lower number is the better item, so
  `widened(-9) = -7.2` goes out as `max`, never `min` — `min: -7.2` excludes the item the filter
  was built from and matches every worse one, which is a search that returns listings and is
  still wrong. Which way a mod runs is `ModMatch.higher_is_better`, read off the mod's own
  reachable range, and `moddb`'s ranges are flipped into the units the item displays on load.
- **The QAM is 300 CSS px** (268 inside a `PanelSection`). It is a verdict surface, not a browser.
  `ItemGrid` backs out of the inset with `margin: 0 -16px` — 300 minus 268 is 32, sixteen a side —
  which is also how a 22 px cell becomes 24. `PANEL_INSET` is that number and checklist item 3 is
  where it gets confirmed.
- **A compact grid cell selects on *focus*, and has no `onActivate`.** Wiring both fires
  `onSelect` twice for one interaction, and the stash tab list spends a request per selection.
  Focus is selection here; that is what makes detail fit in 268 px without spending a press.
- **`@decky/ui` is imported in exactly one file, behind a guard.** It finds Steam's components by
  regex over minified code and `@decky/rollup` externalises it to the global `DFL`, so a Steam
  update makes a component `undefined` rather than an import error — and `<undefined>` in the QAM
  is a **white panel with no message**. `ui-kit/src/profiles/compact/steam.tsx` substitutes
  five-line plain-DOM stand-ins and names what was missing; the shell puts that on screen. Do not
  make the stand-ins prettier: a fake that looked right would hide the failure it exists to expose.
- **The plugin's Python is Decky Loader's 3.11, not the Deck's.** A plugin backend is a
  `multiprocessing.Process` fork of the loader, which is frozen against CPython 3.11.7; SteamOS
  ships 3.13 from 3.7 onward and the loader splices its `site-packages` onto the path anyway.
  `pydantic-core` publishes **no abi3 wheel**, so `py_modules/` is ABI-pinned and
  `scripts/build_plugin.py` refuses a mismatched tag. Measured against the real loader binary —
  research-notes §5.1. When Decky moves, `TARGET_PYTHON` is the whole fix, and the symptom is a
  plugin that starts and does nothing.
- **The Decky transport has one RPC door.** `Plugin.call(method, params)` hands to
  `transports/dispatch.call_method`, the same function the FastAPI route calls. Dispatch is never
  duplicated: `FORBIDDEN_METHODS` and the redaction are one implementation with two doors.
- **The pairing socket exists only during a pairing window**, refuses non-RFC1918 sources before
  reading a body, caps wrong codes at three, times out in three minutes, and closes the instant a
  credential arrives. It is a full-account credential intake on a network and is built like one.
  There is no `pair_submit` method — the value arrives over that socket from the *other* machine,
  never over the RPC channel a CEF console can reach.
- **A module's UI writes no CSS and imports no Decky API.** It composes `@poedex/ui` primitives
  and declares density with per-profile hints (`limit={{compact: 5, full: null}}`). The rule is
  enforced by `eslint-plugin-poedex`, whose own test proves it fails on each violation.
- **The same component tests run against both profiles.** `pnpm run test` resolves `#profile` to
  `full`, `pnpm run test:compact` to `compact`. That harness is what turns "designed for both"
  into a fact; the first run of it found five places where `compact` had quietly dropped content.
- **`check` is drawn as two blocks, not one.** SPEC §5.4's "below threshold **or** the gate
  flagged it" is two questions; the bag screen splits them on whether a number exists. The split
  is in `modules/appraisal/ui/model.ts`, not in `Verdict` — it is a layout decision, and a fifth
  verdict would change the CLI, the event payload and every test to express it. `not_loot` **is**
  a fifth verdict, and clears that bar for the opposite reason: a quest item is not asking the
  player for an action at all, so no existing block's headline is true of it. Anything proposing a
  sixth should have to answer the same question — is this about layout, or about the decision?

- **Tier 3 has three ways of not producing a number, and they are three words.** Outstanding
  (`pricing…`, `⋯`), searched-and-empty (`∅`), and could-not-ask. Collapsing them into one
  boolean is what made the first live appraisal render `pricing…` forever beside two finished
  searches. `Valuation.tier3` is the state; `pricing` is now a derived property of it.

- **A tier-3 query asks about the mods the gate flagged, not every mod on the item.** ANDing all
  of them turns a six-mod rare into a near-exact-match search: measured, that returned 0, 0 and 1
  listings for the three rares in a real bag. `GateResult.focus()` is the join, and it exists
  because `prices` cannot import `appraisal` without making a cycle.
- **A stash tab nobody has read is worth *unknown*, and never `0c`.** The same rule
  `unpriceable` follows for an item, one level up. An unread tab, an unreadable one (a map tab)
  and a genuinely empty one look identical in a total and mean three different things, so
  `TabSummary.known` and `StashTab.supported` are separate fields, the stash total says `≥`, and
  `poedex stash` prints `—` rather than a number nobody computed. Phase 10's whole failure mode
  was a screen that quietly summed holes as zero.

- **Remove-only tabs are fetched once, ever, and map tabs are not fetched at all.** 101 of the
  measured 117 Standard tabs cannot gain items, so their TTL is literally infinite; and a map tab
  returns nothing through this endpoint on every one of five samples across two leagues, so
  spending a request to receive a zero we would then have to disbelieve is the worst of both.
  research-notes §7.1 has the evidence and the fix (OAuth `children` traversal).

- **A crawl states its cost from the buckets, not from the sustained rate.** `requests × 18 s` is
  right for "forever" and wrong by an order of magnitude for anything small: the same policy
  allows 30 requests in the first minute, so a 15-tab refresh is fifteen seconds and not four and
  a half minutes. Measured both ends in research-notes §7.2 — cold 107 requests / ~30 min, steady
  state 15 / ~15 s. And **nothing crawls by itself**: there is no registered method that could
  start one, only `poedex stash crawl --yes`.

- **The stash gate is strict and has its own setting.** SPEC §5.2's two biases are two settings —
  `appraisal.strictness` (bag, generous) and `appraisal.stash_strictness` (stash, strict) —
  because a player who loosens the bag panel has said nothing about whether they want 818 stash
  items flagged. Strict is generous minus the *soft* signals, and it is the same `evaluate()`.

- **`current` is not "the most recently played character", and GGG does not send it.** It is
  absent from every entry of a roster read out of game, so it is understood to mark whoever is
  *logged in* — a hypothesis nobody here has been able to test, and nothing depends on it. What
  is published is **`lastLoginTime`, Unix seconds** (checked: the largest live value decoded to
  the day before it was read; milliseconds would put it in January 1970). The two answer
  different questions — `current` is who is playing, `lastLoginTime` is who played last — so
  `current` wins when it exists and `lastLoginTime` carries the job when it does not, which is
  every measurement so far. **A `lastLoginTime` of 0 or absent is *unknown*, never the epoch**:
  an unknown that sorts must not win a comparison. And the last rung is a **labelled guess**:
  `CharacterList.default()` has no positional fallback at all, because the fallback it replaced
  is what read the wrong character for weeks. `poedex characters` prints the whole chain.
- **A stated choice outranks a derived one, in all four chains.** Account, league, realm and now
  character all resolve as argument → environment/setting → derived, and `poeapi.character`
  therefore sits **above** a character GGG marks as being played. An override a lookup can beat
  is not an override. What makes it safe rather than a trap is that the disagreement is visible
  (`CharacterChoice.played_last`, rendered as "you last played X") and that the panel's picker
  clears it in one press — before that picker existed, a pin on a Deck could not be undone by
  any surface, which is why this ordering would have been the wrong call a week ago.
- **2D grid navigation is free** — Steam's focus system is geometric. Lay out a CSS grid of
  `Focusable` cells and it works.
- **PoE 2 has no data path.** GGG removed inventory from its character endpoint in 3.27.0.

## Conventions

- Backend is Python 3.11+ (Decky plugin backends are Python). Vendor deps into `py_modules/` —
  there is no pip at install time — with `python scripts/build_plugin.py`, which targets
  **CPython 3.11 / manylinux_2_17_x86_64** because that is what the frozen loader runs.
  `pydantic-core` is compiled and has no abi3 wheel, so the pin is real; `fastapi`/`uvicorn` stay
  out, because the Decky transport uses Decky's RPC and the pairing listener is
  `asyncio.start_server`.
- The normalized item model (SPEC §4.5) is the boundary. Pricing consumes normalized items,
  never raw API JSON. Everything crossing to the frontend is JSON-serializable.
- Pricing logic must be testable offline against recorded fixtures. No tests that hit live APIs.
- Frontend→backend methods are `async def` and must not start with `_`.
- `plugin.json`: keep `"debug"` (hot reload needs it), never enable `root`.

## Credentials — important

`POESESSID` is a **full-account website credential**, not a scoped token. Store under
`DECKY_PLUGIN_SETTINGS_DIR` at `0600`. Never commit, log, or let it reach error output —
exceptions cross into the CEF console.

`decky.logger` reconfigures the **root** logger with `force=True`, so httpx debug logging will
write request URLs into the plugin log unless you explicitly silence it.

## Development environment

- Development on **Ubuntu 22.04**; the Deck is a deploy target over SSH. M1–M2 need no hardware.
- A `poe` MCP server is configured with a working POESESSID and can query the live account —
  useful for probing during development. The app must not depend on it.
- The user's own `poe_mcp` project contains a working Auth-Code+PKCE-against-GGG implementation
  (`src/auth/oauth.ts`) to port when OAuth becomes available.

## Distribution

**Sideload via GitHub Release.** The Decky store rejects AI-assisted plugins outright and
requires an attestation; this project is AI-assisted, so the store is a non-goal. Decky's
install-from-URL is first-class and permanent.

## Unresolved, needs the user

SPEC §11. The one worth raising early: the **keep threshold default** (~20c gives a busy panel,
divine-tier a quiet one).

**"Which league is primary?" (SPEC §11, row 2) is answered: none of them.** A bag is priced
against the league of the character it came from, carried on `ItemSet.league` and read off
`get-characters`. `prices.league` is an *override*, empty by default; `poedex value|appraise
--league` overrides it for one run. When nothing knows the league the tool raises
`LeagueUnknownError` rather than falling back to Standard — which is what it used to do,
silently, while the character was in Allflame and a Divine Orb cost 897.7c in one and 209.0c in
the other. Price tables are per league, and tables loaded for one are never used for another.

**`REALM = "pc"` is gone for the same reason, one rung down.** Every character-window request
carried a hardcoded realm, and `get-characters` returns one per character — in the same roster
entry the league is read from. Precedence is argument, then `poeapi.realm`, then the roster; an
unresolvable realm **omits the parameter** and logs a warning naming the command, because
"leave it off" and "it is pc" are different claims. Lower stakes than the league (a console
account gets an empty roster or a 403, not wrong numbers) and **unverified**: nobody here has a
console account, and whether the legacy endpoints require the parameter at all is unmeasured.
