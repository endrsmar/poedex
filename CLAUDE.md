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

**Phases 1, 2, 3, 4, 4b, 5, 6, 8, 9 and 9b done.** `runtime/` (registry, context, events, storage,
settings, methods, redacting log); core modules `credentials`, `net` (header-driven limiter +
httpx), `poeapi` (endpoints, normalization, cache), `gamelog` (read-only Client.txt tail),
`moddb` (a trimmed mod database: real tiers per base, affix counts, influence pools);
feature modules `prices` (poe.ninja bulk tables with per-league type discovery, tier-0 notes, a
bulk exchange fallback, a trade client) and `appraisal` (a `moddb`-backed **highlighter**,
five-state verdicts, and a player-driven price check); a `poedex` CLI; and **the first usable
surface** — `ui-kit/` (`@poedex/ui`, two profiles behind a build-time alias), `frontend/core/`
(transports, stores, TS types generated from the pydantic models), `transports/http/` (FastAPI
on 127.0.0.1 + SSE + the built SPA), `surfaces/web/` and `modules/appraisal/ui/`. Boundaries are
enforced in both languages: the Python AST tests, and an ESLint rule over `modules/*/ui`. Next
action is **Phase 7** (compact profile against `@decky/ui`, Decky transport, panel), then
**Phase 10** (stash tabs).

    poedex serve      # http://127.0.0.1:7331 — the priced bag, with verdicts and provenance
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
much weaker test than it sounds — see the phase note in the plan. Three things need a human and
would close most of the open risk:

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
```

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

- **The pre-tick is a claim about rolls, and roughly one tick in seven is on a mod nobody
  prices.** Measured on twenty real identified rares from public listings: a median of 3 ticks per
  item, and 8 of 53 ticks landed on genuine T1/T2 rolls of groups no one searches — stun and block
  recovery, light radius, global accuracy. That is not a bug in `NEAR_TOP_TIER`; it is §5b's own
  conclusion, that which mods matter is player knowledge. **A junk-mod list is `MOD_GROUPS` coming
  back** and must not be added. Two things that *are* worth fixing: on an item where every roll is
  high the pre-tick ticks all six, which is the conjunction that returned zero listings live; and
  where `moddb` mis-tiers, the pre-tick misses the mod that makes the item expensive.

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
  render one sentence, hybrids write several, and essence, influence and bench-craft tiers are
  counted from different ladders. `Attribution` is `exact`/`group`/`ambiguous`/`unknown` and the
  last two expose no tier. On the live fixtures 79% of affix lines resolve confidently; showing
  "T2" for the rest would be right most of the time, which is exactly what makes it dangerous.
- **The QAM is 300 CSS px** (268 inside a `PanelSection`). It is a verdict surface, not a browser.
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
- **2D grid navigation is free** — Steam's focus system is geometric. Lay out a CSS grid of
  `Focusable` cells and it works.
- **PoE 2 has no data path.** GGG removed inventory from its character endpoint in 3.27.0.

## Conventions

- Backend is Python 3.11+ (Decky plugin backends are Python). Vendor pure-Python deps into
  `py_modules/` — there is no pip at install time.
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
