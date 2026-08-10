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

**Phases 1, 2, 3, 4, 4b and 6 done.** `runtime/` (registry, context, events, storage, settings,
methods, redacting log); core modules `credentials`, `net` (header-driven limiter + httpx),
`poeapi` (endpoints, normalization, cache), `gamelog` (read-only Client.txt tail); feature
modules `prices` (poe.ninja bulk tables with per-league type discovery, tier-0 notes, a bulk
exchange fallback, a trade client) and `appraisal` (the strictness-parameterized tier-2 gate,
four-state verdicts, eager tier 3 for a bag); a `poedex` CLI; and the boundary tests, which now
have both a core→feature rejection and a real feature→feature edge to enforce against. Next
action is **Phase 5** (UI kit, web surface, appraisal UI).

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
poedex appraise         # the bag, judged. one account request; a few trade requests for rares
poedex appraise --no-escalate   # ...or none at all
poedex limits           # what the limiter has learned
```

Module layout is fixed by the registry: `modules/<id>/backend/module.py` exports `MODULE`, a
module instance; `api.py` is the only file dependents may import.

## Module architecture in one paragraph

Everything is a module, and **a module is a vertical slice** — backend logic and its own UI in one
directory. *Core* modules (`credentials`, `net`, `poeapi`, `gamelog`) are PoE infrastructure with
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
- **Tier 3 is eager for a bag and never for a stash.** SPEC §5.3's original "never eager" was a
  stash rule; a bag holds 3–5 rares and the search budget is `5:10:60`. The switch is the
  existing `Strictness`, and `strict` cannot be overridden into escalating.
- **Batching the bulk exchange is not free.** The response caps at 100 rows sorted cheapest-first
  across the whole batch, so ten ids in one request returns everyone's floor. Starved wants are
  re-queried alone. research-notes §11 has the table.
- **`unpriceable` is never zero.** A removed item absent from the price index is a hole in the
  total, and reporting it as worthless understates the bag badly (SPEC §5.4). `appraisal` splits
  it further: an item the index *should* carry and does not is `unpriceable`; a rare, which no
  bulk table has ever priced, is a tier-2 question and not a gap.
- **The tier-2 mod thresholds are not mod tiers.** `modules/appraisal/backend/gate.py` scores
  mods with one regex and one number per group, with no knowledge of base type, item class or
  item level. It says so at the top of the file. Real tiers need a mod database this project
  does not have; if one ever lands, delete those constants rather than tuning them.
- **The QAM is 300 CSS px** (268 inside a `PanelSection`). It is a verdict surface, not a browser.
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
