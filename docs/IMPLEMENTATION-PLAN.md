# PoEDex — Prototype Implementation Plan

**Scope:** module runtime + core modules + loot appraisal as the first feature module, reaching a
usable desktop web surface, then the Decky panel.
**Created:** 2026-08-10 · Revised v3 — modules are vertical slices, UI included
**Companion to** [`SPEC.md`](SPEC.md)

---

## 1. Architecture

### 1.1 A module is a vertical slice

A module owns everything about its feature: backend logic, exposed API, and **its own UI**. It is
one directory. Nothing about appraisal lives anywhere else.

```
modules/appraisal/
  backend/     module.py  api.py  gate.py  verdict.py
  ui/          index.ts  BagScreen.tsx  ...
  tests/
```

The two surfaces — a 300 px gamepad panel and an unconstrained desktop page — are reconciled by a
shared **UI kit with surface profiles** (§2), not by writing the feature twice.

### 1.2 Layers

```
┌──────────────────────────────────────────────────────────────┐
│  SURFACES     web shell  ·  decky shell                       │
│               discover and mount module screens               │
├──────────────────────────────────────────────────────────────┤
│  UI KIT       primitives × 2 profile implementations          │
│               (@poedex/ui)                                    │
├──────────────────────────────────────────────────────────────┤
│  TRANSPORTS   http (FastAPI + SSE)  ·  decky (RPC + emit)     │
├──────────────────────────────────────────────────────────────┤
│  FEATURE      prices · appraisal · (crafting) · (guides)      │
│  MODULES      backend + ui. toggleable. may depend on each     │
│               other and on core.                              │
├──────────────────────────────────────────────────────────────┤
│  CORE         credentials · net · poeapi · gamelog · moddb    │
│  MODULES      PoE infrastructure, zero feature opinion.       │
│               always loaded. may NOT depend on features.      │
├──────────────────────────────────────────────────────────────┤
│  RUNTIME      registry · events · storage · settings · log    │
│               hosts modules. knows nothing about PoE.         │
└──────────────────────────────────────────────────────────────┘
```

Core and feature modules use the **identical mechanism** — same registry, lifecycle, dependency
resolution. `kind` is a policy label:

| | core | feature |
|---|---|---|
| May depend on core | yes | yes |
| May depend on features | **no** | yes |
| User can disable | no | yes |
| Holds PoE *feature* opinion | no | yes |

Core modules may still ship UI — `credentials` needs a pairing screen. "Core" constrains what a
module may *depend on* and *decide*, not whether it has a face.

The runtime is not modules. Event bus, storage, settings and logging are what `ModuleContext` is
made of; `requires: ["storage"]` on every module would be pure ceremony.

### 1.3 Why `prices` is a feature module

Pricing carries policy — which source, which league, median-of-cheapest-N versus minimum, what
qualifies as `unpriceable`, how stale a table may be. Opinions in the core are how a contained
core stops being contained. So:

```
crafting ──┐
           ├──> prices ──> poeapi ──> net ──> credentials
appraisal ─┘
```

Crafting wants *"what is this worth"*, not *"should I keep it"*. Routing it through `prices`
rather than through `appraisal` avoids depending on verdict policy and thresholds to obtain a
number.

### 1.4 Modules depend on interfaces

Each module exports a Protocol as its public surface; dependents get that type, never the
concrete class, and may import **only** the dependency's `api.py`.

```python
class PricesApi(Protocol):
    async def value(self, item: Item) -> Valuation: ...
    async def bulk(self, category: str) -> Mapping[str, Price]: ...
```

```python
class AppraisalModule:
    id, kind, requires = "appraisal", "feature", ["prices"]
    async def start(self, ctx: ModuleContext) -> None:
        self.prices: PricesApi = ctx.require(PricesApi)
```

### 1.5 Registry behaviour

Topological sort on `requires`; **cycles are a hard startup error**. Start in dependency order,
stop in reverse. A missing or disabled dependency disables the dependent with a stated reason
surfaced in the UI — never a silent partial start. Methods namespaced by module id
(`appraisal.get_bag`). A `kind: "core"` module declaring a feature dependency is a startup error.

---

## 2. The UI framework

### 2.1 The problem it solves

The two surfaces differ in more than styling. At 300 px the bag screen shows a total, a tally, the
grid, and ~5 rows. At 1400 px it shows all of that *plus* a detail pane with full mods and the
comparable listings behind a price. That is a difference in **how much content**, which no amount
of responsive CSS on one component tree expresses honestly.

So a module declares *what* it wants shown and *how much at each density*. The framework decides
*how* to draw it.

### 2.2 Surface profiles

```ts
interface Profile {
  id: 'compact' | 'full'
  width: number | 'fluid'      // 300 | 'fluid'
  input: 'gamepad' | 'pointer'
  density: 'compact' | 'comfortable'
}
```

- **`compact`** — Decky QAM. 300 CSS px, gamepad focus, `@decky/ui` components, inline styles
  only, Steam's React 19.
- **`full`** — web. Unconstrained, pointer and keyboard, real CSS, routing.

### 2.3 The kit

Module UI is written against `@poedex/ui` primitives and never touches raw DOM, `@decky/ui`, or
CSS files directly. Each primitive has two implementations, selected at **build time** via alias
(Rollup resolves `compact`, Vite resolves `full`) so neither bundle carries the other.

| Group | Primitives |
|---|---|
| Layout | `Screen` `Section` `Stack` `Row` |
| Data | `Stat` `Tally` `ItemGrid` `ItemRow` `ValueBar` `VerdictPill` |
| Interaction | `Action` `Focus` `Detail` |
| State | `Pending` `Empty` `ErrorState` `StaleBanner` |

Per-profile hints are how density is declared:

```tsx
<Screen id="bag">
  <Stat label="Bag value" value={total} />
  <Tally counts={counts} />
  <ItemGrid items={items} cols={12} rows={5} onFocus={setFocused} />
  <ItemList items={ranked} limit={{ compact: 5, full: null }} />
  <Detail
    item={focused}
    fields={{ compact: ['name', 'value'],
              full:    ['name', 'value', 'mods', 'comparables'] }}
  />
</Screen>
```

**Navigation is the kit's job, not the module's.** `ItemGrid` renders `Focusable` cells under
`compact` — which gets 2D D-pad navigation free from Steam's geometric focus system — and plain
clickable cells under `full`. The module never imports `Focusable` or writes a focus handler.

### 2.4 Screen registration

```ts
export default defineModuleUI({
  id: 'appraisal',
  screens: [
    { id: 'bag',   title: 'Bag',   component: BagScreen,   profiles: ['compact', 'full'] },
    { id: 'stash', title: 'Stash', component: StashScreen, profiles: ['full'] },
  ],
  settings: SettingsScreen,
})
```

`profiles` lets a screen declare it only makes sense somewhere. A dense stash browser genuinely
does not belong at 300 px, and saying so is better than shipping a cramped version of it.

Surface shells discover registered module UIs and mount them: web as routes/tabs, Decky as a
screen stack with **B** to go back.

### 2.5 Escape hatch

```
modules/appraisal/ui/overrides/compact.tsx
```

Opt-in, per screen. When the shared vocabulary genuinely cannot express something, a module
provides a profile-specific rendering rather than forking the kit. The framework logs which
screens use overrides, so drift stays visible instead of quietly becoming the norm.

Without this, the first genuinely surface-specific need breaks the framework. With it, the
common case stays shared.

### 2.6 Enforcement, both languages

Python — pytest walks the AST:
- `runtime` imports nothing from `modules` or `transports`
- core modules import no feature module
- feature modules import only `<dep>/api.py` of declared `requires`
- no module imports a Decky API

TypeScript — lint rule on `modules/*/ui/**`:
- may import `@poedex/ui`, its own module's types, and `frontend` runtime
- may **not** import `@decky/ui`, `react-dom`, CSS files, or another module's internals

Plus a runtime check on the assembled registry for cycles and core→feature edges. Static analysis
catches the import; the registry check catches the declaration.

---

## 3. Repo layout

```
runtime/                      python: registry, context, events, storage, settings, methods
modules/
  credentials/  [core]   backend/  ui/          pairing + session state
  net/          [core]   backend/               httpx + header-driven limiter
  poeapi/       [core]   backend/               endpoints, normalization, cache
  gamelog/      [core]   backend/               locate, tail, parse → events
  moddb/        [core]   backend/  data/       trimmed mod database; requires: []
  prices/       [feature] backend/              requires: poeapi
  appraisal/    [feature] backend/  ui/         requires: prices
ui-kit/                       @poedex/ui
  primitives/                 contracts + shared types
  profiles/compact/           @decky/ui-backed
  profiles/full/              CSS-backed
surfaces/
  web/                        shell, routing, mounts module screens
  decky/                      panel shell, screen stack
transports/
  http/                       FastAPI + SSE + static
  decky/                      Plugin class → method registry
plugin/                       plugin.json, main.py, py_modules/
tests/
docs/
```

Modules mix Python and TypeScript in one directory deliberately — that is what "the module is the
unit" means. Python packaging globs `modules/*/backend`; the TS build globs `modules/*/ui`.

Pydantic models are the single source of truth for types; a build step generates TS from their
JSON Schema and CI fails if it is stale.

---

## 4. Module contract

```python
class Module(Protocol):
    id: str
    name: str
    kind: Literal["core", "feature"]
    requires: list[str]
    provides: type | None

    async def start(self, ctx: ModuleContext) -> None: ...
    async def stop(self) -> None: ...
    def methods(self) -> dict[str, Callable]: ...
    def settings_schema(self) -> dict: ...
```

```python
@dataclass
class ModuleContext:
    require: Callable[[type[T]], T]
    events: EventBus            # zone_changed, sync_complete, credential_changed
    storage: Storage            # namespaced to this module
    settings: SettingsView
    logger: Logger              # redacting
```

**No module fetches directly.** All network access goes through the `net` core module so one rate
limiter sees every request. A module bypassing this can get the account restricted — the one
failure that hurts the user outside the app.

---

## 5. Phases

### Phase 1 — Runtime, boundaries, credentials
Registry with toposort, cycle detection, kind validation. `ModuleContext`, event bus, storage,
settings. `credentials` core module: `~/.config/poedex/session.json` at `0600` storing value plus
`account`, `added_at`, `last_ok_at`. `poedex auth set` reads a hidden stdin prompt, never argv.
Python boundary tests in place.

**Done:** credential stored; a trivial second module resolves a dependency on it; boundary tests pass.

### Phase 2 — `net` and `poeapi`
Header-driven token bucket: bucket by `(policy, rule, period)`, learn policy names at runtime,
margin 2–3, pad periods 1–3 s, seed pessimistically, backoff with a `Retry-After` floor,
**refuse rather than queue**. Compliant User-Agent, credential redaction, `httpx` logger silenced.
Endpoints, normalization, pydantic models, response cache. Fixtures recorded from the live account.

Run the **60-second freshness self-test** (SPEC §4.3); record results and the volatile-fields list.

**Done:** `poedex sync` prints the normalized bag. All tests run offline.

### Phase 3 — `prices` — **done**
poe.ninja bulk tables on the routes measured in research-notes §9 (`/poe1/api/economy/
{exchange|stash}/current/.../overview?league=&type=`), ETag conditional requests, 30-minute
cache, prefetched at start. Trade client wired against its own bucket, not called eagerly.
`PricesApi` published.

The first `kind: "feature"` module, which is what makes the core→feature rule falsifiable: the
boundary tests now run the static checker over a copy of the real tree with `poeapi` edited to
depend on `prices`, and assemble the real registry with a core module that requires it.

`requires` is `["net", "poeapi"]`, not the `["poeapi"]` of §1.3's diagram. The diagram describes
data flow; poe.ninja and the trade endpoints are not account endpoints, so `poeapi` neither
fetches nor should fetch them, and §4 forbids a module opening its own socket. `net` grew what
that needs: absolute URLs, POST, request headers, `304` as a success, a per-hostname courtesy
budget for hosts that publish no rate-limit headers, and a hard rule that the account credential
never leaves the PoE API host.

**Done:** `poedex value` prints per-item values and a total.

### Phase 4 — `appraisal` backend — **done**
Tier-2 gate with the strictness parameter (`gate.py`), four-state verdicts (`verdict.py`),
`AppraisalApi`, `appraisal.*` methods, and `poedex appraise`.

`requires` is `["poeapi", "prices"]`, not the `["prices"]` of §1.4's sketch — the same kind of
stated deviation Phase 3 made with `net`. The API is defined over `NormalizedItem`, which is
`poeapi`'s type and reaching it through `prices`' import list is not a public surface; and
`PricesApi` has no bag accessor, so without the edge `appraisal.appraise_bag` cannot exist and
the Phase 5 bag screen would have to post the whole normalized bag back to the backend to have
it judged. `poeapi` is core, so a feature module depending on it is the ordinary direction.

Two decisions beyond the brief, both stated in `api.py`:

- **"Not in the index" and "bulk was never going to price this" are separated.** `prices` returns
  one `unpriceable` state for both. `appraisal.indexable()` splits them: a missing `Veiled
  Scarab` is a hole in the total; a rare ring with no bulk price is a tier-2 question, and
  flooding the panel with question marks for every rare would bury the real gaps.
- **The mod thresholds are an approximation and the file says so.** One regex and one number per
  mod group, with no knowledge of base type, item class or item level. Defensible only because
  they feed a *generous* gate whose false positives cost one optional query, and because they
  are switched off entirely at strict.

**Done:** `poedex appraise` prints keep/check/trash/unpriceable with a total.

**Validation checkpoint — answered weakly, and the weakness is the finding.**

The output is legible, correctly sorted and honest about what it does not know. But it was run
against a **synthetic bag**: `poedex appraise` has never seen a real backpack, so the checkpoint's
actual question — *does this tell the player something they did not already know?* — has not been
answered by evidence. A fixture cannot surprise the person who wrote it.

What can be said from the code and from the real account data that *was* read:

1. **On bulk items the answer is mostly obvious.** Currency, cards, fragments and scarabs are
   ~98% of a real bag (research-notes §7), every one of them resolves at tier 1, and a player
   already knows a Divine Orb is worth keeping. For those rows the tool adds a *total* and a
   sort, not a discovery.
2. **The non-obvious answers all live in two places** — the tier-2 gate on rares, and the
   `unpriceable` row. Both are real value the player cannot get at a glance. Both are also the
   parts with the least evidence behind them.
3. **The measured account has almost no rares.** Every active Standard and Allflame tab read
   during this phase (six tabs, ~300 items) was currency, essences, fossils, fragments and
   splinters — not one rare. If that is representative of how this player plays, the gate is
   machinery for a case that rarely arises, and the honest headline feature is the *total* plus
   the `unpriceable` callout rather than rare triage.
4. **`check` is doing two jobs.** SPEC §5.4 defines it as "below threshold but non-trivial, **or**
   tier-3 pending". Those are different questions — "worth 6 chaos" and "worth an unknown amount,
   here is why you should look" — and on a real bag the first will swamp the second. The renderer
   sorts gate hits to the top of the block to compensate, which is a workaround. **Split them
   before drawing the panel**, or raise the check floor a long way.

**Before Phase 5 ships a bag screen, run `poedex appraise` on a real backpack after a real map.**
It is one GGG request. If the answer is "I knew all that", the panel to build is a total and a
tally, not a grid.

### Phase 4b — pricing coverage — **done**

Three gaps the user found against their real account, in the order they matter.

**Table discovery, and the bug behind it.** `CATALOGUE` was twenty-six poe.ninja types typed in
by hand. `Ducat` was not one of them — it has been a live exchange type all along, with eleven
priced lines in Allflame — so for a whole league every ducat in a bag came back `unpriceable`.
The catalogue is now the full documented 44, and, more importantly, **which of them a league
serves is asked rather than assumed**: `sitemap.xml` for the category slugs, a derivation rule
for the API type name (43 of 44 by rule, one irregular), a probe per candidate, a per-league
record cached for a day, and the static list as a stated fallback. There is no per-league type
endpoint — the four 404s that establish that are in research-notes §9.6.

The property worth protecting in review: **discovery can find a type nobody has typed into the
catalogue.** A mechanism that only validated known names against a league would have confirmed
all twenty-six and reproduced the bug exactly.

**Tier 1b, the bulk exchange.** `POST /api/trade/exchange/{league}` for currency-class items
poe.ninja does not index *at all*. Demoted from its original brief once discovery turned out to
be the real ducat fix: it is a safety net for the case discovery cannot reach. Batched (10 ids
max) with a re-query for starved wants — the response caps at 100 rows sorted cheapest-first
across the whole batch, so a naive ten-id batch priced Merrick's Ducat at a third of its rate
(research-notes §11).

**Eager tier 3 for a bag.** SPEC §5.3's "never eager" was written against stash scale. A bag of
three to five rares fits inside `5:10:60` comfortably, and without escalation every rare in a bag
came back with a gate opinion and no number. Parameterised on the existing `Strictness`:
`generous` escalates, capped and timed out; `strict` never does, and an explicit `escalate=True`
cannot override it. `prices` gained `quote_many`; the decision to spend lives in `appraisal`,
because when to spend a shared budget is a feature opinion.

`net`'s foreign-host courtesy budget went from 40/min to 90/min — sized, as before, to the worst
honest burst, which is now one discovery pass plus a forced refresh.

**Done:** ducats price at tier 1; a bag's rares come back with numbers; 821 tests, all offline.

### Phase 5 — UI kit, web surface, appraisal UI — **done**

`ui-kit/` (`@poedex/ui`), `frontend/core/` (`@poedex/core`), `transports/http/`, `surfaces/web/`
and `modules/appraisal/ui/`. `poedex serve` starts it; `localhost:7331` is the priced bag.

**The stated risk was answered rather than deferred.** The mitigation in the original text —
"write both profile signatures before either implementation" — turned out to be too weak to
check anything, because a signature nobody runs is a signature nobody has tested. So `compact`
was written as a *working* implementation (inline styles, no `@decky/ui` yet; Phase 7 swaps the
elements) and `vitest.compact.config.ts` runs **the same 123 tests** through it by flipping the
`#profile` export condition. No test names a profile.

That harness earned its keep on its first run: 15 failures, every one a place where `compact`
had quietly dropped content rather than a place where a contract did not fit. The sync wording
had already forked between the two profiles before anything shipped (`cached` vs
`cached — nothing was fetched`; `Resync` vs `Refresh`), which is why `ui-kit/src/sync.ts` now
owns the sentences and a profile only decides how much of one fits.

Two deviations from §2.3, both stated where they live:

- **`limit` is a prop on `Section`, not a separate `ItemList` primitive.** §2.3's example puts
  `limit={{compact: 5, full: null}}` on `ItemList`; the primitive *table* lists `ItemRow`. Making
  the container own truncation means the same hint works for rows, bars and anything else, and
  the "N more not shown" footer is written once.
- **`ItemVerdict` gained a `slot`.** The bag grid is a *map* (§6.3) and the appraisal payload
  carried no coordinates, so the screen could not draw one. Four ints, optional, defaulted.

**Done:** open `localhost:7331`, see the priced bag. **First usable surface.**

### Phase 6 — `gamelog`
`libraryfolders.vdf` resolution, EOF-seek tailing with truncation handling, parsing with the
`] : ` anchor, `<<set:>>` stripping, area-id classification. Zone entry → debounced sync →
`sync_complete` → SSE push. Visible degradation if the log cannot be found.

**Done:** portal to hideout; the web UI updates on its own.

### Phase 7 — Compact profile, Decky transport, panel
`compact` profile of the UI kit: `@decky/ui`-backed primitives, `Focusable` grid, inline styles.
`plugin.json` with `debug` on and `root` **off**; `Plugin` class delegating to the method
registry; `_main()` hosting the runtime; vendored httpx in `py_modules/`. `DeckyTransport`. Decky
shell with the screen stack. `credentials` LAN pairing screen. Hardware checks: suspend/resume,
real geometry, hot-reload loop.

**Done:** installs from a Release zip; the same `BagScreen.tsx` renders at 300 px with D-pad
navigation.

### Phase 8 — `moddb` — **done**

A trimmed Path of Exile mod database as a `kind: "core"` module with `requires: []`. Core because
game data is factual; whether a T1 roll makes an item worth keeping stays `appraisal`'s job.

It exists for a design pivot. **Automatic rare pricing is abandoned** — it failed twice against
the live account, once by ANDing every mod and finding zero listings, once by querying one loose
mod and reporting 10c for a 1c item. No heuristic recovers *which mods make an item interesting*;
that is player knowledge. The replacement is Awakened PoE Trade's model: highlight items that are
*potentially* expensive, show their mods as a checkbox list with the significant ones pre-ticked,
and let the player trigger the price check. This module supplies the facts that make the
highlight and the pre-tick correct rather than guessed.

**A build step, not a download.** `scripts/build_moddb.py` fetches repoe-fork (the maintained
fork of RePoE, archived Dec 2024) and emits a committed 566 KiB artifact from 30 MB of upstream
JSON. `--check` answers "is the committed one current?". No test touches the network and there is
no runtime fetch path — `modules/moddb/tests/test_module.py` walks the AST to prove there is not
even an unused one.

**Regeneration is the module's maintenance obligation, and skipping it fails silently.** A
league-old mod database still answers everything and still sounds certain. So the artifact stamps
its game version and build date, `ModDbApi.version()` exposes both, `poedex moddb` prints them
first, and the module logs a warning past 120 days.

Three things beyond a straight port of the data:

- **Attribution is a return type.** Deciding "this +85 life is T2" means deciding which mod
  produced it, which is often undecidable: several groups render one sentence, hybrids write
  several, and pools overlap. `Attribution` is `exact` / `group` / `ambiguous` / `unknown`, and
  the last two expose no tier at all. Nothing ever picks the most likely candidate — the most
  likely candidate is right *most* of the time, which is exactly what makes it dangerous.
- **A tier ladder belongs to a base and to a pool, not to a group.** `+95 to maximum Life` is T4
  of 10 on a helmet and T7 of 13 on a body armour. Candidates spanning two ladders — a dropped
  affix and a bench craft, an ordinary roll and an essence one — are `ambiguous`, because their
  tier numbers are counted from different places.
- **Whole-item context is a second pass.** A mod that writes two sentences can only be the answer
  if both are on the item. That turns `+26 to Armour` from ambiguous into exact, and it is what
  makes the affix count right: a hybrid takes one slot, not two.

Measured on the live-derived fixtures: **79% of affix lines resolve confidently** (13 exact, 2
group, 2 ambiguous, 2 unknown, of 19). Both "unknown"s are rolls the fixture scrubbing invented.

**Done:** `poedex moddb --base 'Hubris Circlet' --mod '+95 to maximum Life' --ilvl 86` prints
`T4 of 10`, a ceiling of 144, and both stat ids. 1047 Python tests, all offline.

---

## 5b. The pivot: highlight, don't price

**Decided 2026-08-10, after the first live appraisals. This supersedes automatic rare pricing.**

### What failed

Two live runs against a real account, two failure modes, same root cause:

- Querying **every** resolvable mod made a six-mod rare a near-exact-match search. Two of three
  flagged rares returned **zero listings**.
- Querying **one loose mod** on a jewel matched **exactly one item in the league** — a worse jewel
  someone was asking 10c for. "Median of the cheapest N" over n=1 reported 10c for an item worth
  1.00c across 438 comparables.

Narrowing produced a missing answer; widening produced a confidently wrong one. There is no
setting between them that is right, because the question the query encodes — *which mods make
**this** item interesting* — is not answerable from the item alone. It is player knowledge.

### What replaces it

Awakened PoE Trade's model, and the reason that model won:

1. The gate **highlights** items that are *potentially* expensive — valuable base at high ilvl,
   high-tier rolls, six-link, rare influence mods — and claims no number.
2. Selecting an item shows its mods as a **checkbox list**, with the significant ones **pre-ticked**,
   plus an **open-affix count** filter.
3. The **player triggers** the price check. The query is built from their selection.

The gate stops being a decision-maker and becomes a proposal. Being wrong costs a tick, not a
wrong number.

### What this deletes

Eager tier 3, `max_eager_quotes`, `eager_timeout_seconds`, and the whole escalate-on-appraise
path. With no automatic searches the `600:21600:3600` ceiling stops binding, which retires both
the quote cache and the budget-aware cap that were queued to make it sustainable.

**Bulk pricing stays automatic.** poe.ninja works, is free, is accurate, and needs no input. The
pivot is about rares only.

### What it makes load-bearing

Three of the four highlight criteria need facts the project does not have: high-tier rolls need
real tiers, influence mods need the influence pools, and open-affix counting needs prefix/suffix
classification. All three live in RePoE. `moddb` moves from "later" to **prerequisite**.

### Phase 8 — `moddb` core module
RePoE-derived: mod → tier for a base and ilvl, prefix/suffix, open-affix counts, influence-mod
identification, base tags, max achievable ranges. Build-time trim from 21 MB to a committed
artifact; regeneration documented and required each league. Attribution ambiguity returned
honestly rather than guessed.

**Done:** `gate.py`'s hand-typed thresholds and 26-base allowlist have a factual replacement.

### Phase 9 — highlight-not-price — **done**

`gate.py` is a highlighter built on `moddb`, eager tier 3 is gone, and the query comes from a
player's ticks. `poedex price <uid>` is the shippable increment; the panel is the same thing with
checkboxes.

**What `gate.py` lost.** 407 lines to 400, but the count is the least of it. Deleted: `MOD_GROUPS`
(14 regex+threshold entries), the `ModGroup` class and `mod_hits`, `HIGH_VALUE_BASES` (26 names),
`ILVL86_BASE_CATEGORIES`, `ILVL86_EXCLUDED_SUBCATEGORIES`. **Every number the file used to compare
against is gone.** What is left is `SIX_LINK`, `ILVL86` as a fallback for a base the database does
not know, and `NEAR_TOP_TIER`/`NEAR_TOP_TIER_LADDER` — which are not thresholds on *rolls* but on
tier positions the database supplies.

The two factual errors the plan predicted are both confirmed against the artifact: a **Hubris
Circlet tops out at affix level 85**, so a third of the old ilvl-86 flags on helmets, gloves and
rings were noise; and of the 26 hand-typed bases, **seven** carry GGG's own
`top_tier_base_item_type` tag. The other nineteen survive as `SOUGHT_AFTER_BASES`, labelled an
opinion and demoted to a *soft* signal, which makes the strict gate entirely factual for the first
time.

**One criterion changed a live fixture's verdict, and that is the finding.** §5b's fourth criterion
is influence *mods*, not influenced items. The fixture's Shaper-tagged `Leather Belt` carries no
mod from the Shaper pool, so it is no longer flagged — the old gate was highlighting a tag, which
is a highlight on nothing.

**The `StatIndex` bug was real and is fixed.** `entries.setdefault()` made the *first* group win
and `pseudo` comes first, so `Adds 12 to 30 Physical Damage` resolved to
`pseudo.pseudo_adds_physical_damage` — an aggregate over every source of physical damage on the
item — instead of `explicit.stat_960081730`. The index now keeps every group per sentence and
chooses at lookup time by the line's origin, with `pseudo` last always. Measured live: the same
`30% increased Movement Speed` filter on Two-Toned Boots matches **161** listings through the
pseudo id and **160** through the explicit one. Small, and in the direction the bug predicts — an
aggregate is a superset and never a subset.

`stat_id("20% increased Attack Speed")` returning `None` was **not** a normalization gap.
`normalize_stat_text` produces `#% increased Attack Speed`, which is exactly GGG's spelling; the
recorded fixture simply carried no attack-speed entry. It carries both ids now, and the explicit
one wins.

**The 300 px problem was solved in the kit, with no override.** A checkbox row at `compact` is two
lines — mod text, then the tier under it — inside a 30 px focus target, rather than a row of
columns with a 13 px input. The tier goes *under* the text because a right-hand column costs the
mod line its last eight characters at 268 px, and the mod line is the thing being identified
against the game's own tooltip. `CheckList` deliberately takes **no `limit`**, breaking the kit's
own truncate-and-report rule: a hidden row is a filter the player cannot see and cannot switch
off, and six affixes is the game's ceiling anyway. Both profiles run the same 146 tests.

**Done:** 1083 Python tests and 146×2 frontend tests, all offline. `poedex price <uid> --dry-run`
prints an item's mods with real per-base tiers and the word `unknown` where `moddb` will not
commit; without `--dry-run` it spends two trade requests on the query the player chose.

### Phase 9b — the three gaps Phase 9 flagged in its own report — **done**

Phase 9 wrote "I did not measure the coverage rate" about the trade-id bridge, offered the
open-affix filter without ever running one, and shipped the checkbox list without seeing it on an
identified item. All three are now measured.

**The bridge was not 94% covered, it was 87% correct, and the 7-point gap was invisible.**
Measured over all 9 353 mod lines in the artifact: 8 823 (94.3%) resolved to *some* trade stat id.
But 678 of those were the **global** stat where the mod is **local**, and 169 more had no id at
all for the same reason — `#% increased Energy Shield`, the four defence hybrids and a shield's
`+#% Chance to Block` exist only in the local reading. Correct coverage was **8 145 of 9 353,
87.1%**.

This is not a near miss. A rare body armour searched by the global `#% increased Armour` id
(`explicit.stat_2866361420`) matched **0** listings against the live API; the local id
(`explicit.stat_1062208444`) matched **10 000+**. A dropped filter at least shows up in the query
description; a filter with the wrong id returns an empty search that reads as "worthless".

Two upstream facts cause it and neither is fixable inside `stat_translations.json`:
`local_energy_shield_+%` carries no `trade_stats` block, and `local_energy_shield` carries one
pointing at the global id. GGG's own `/api/trade/data/stats` distinguishes the readings with a
`(Local)` suffix (plus `(Shields)`/`(Staves)`), so it is now a **fourth build-time source** —
a supplement, because GGG's document also carries 77 sentences under two ids with nothing to
choose between them, and only RePoE knows which game stat wrote the line. Nothing is downloaded at
runtime and the document is not committed; 22 sentences' worth of ids are.

Which reading a line wants is a property of the **mod**, not the sentence — `+# to maximum Energy
Shield` is local on a chest and global on a ring — so it is read off the mod's own `local_*` stat
ids at build time and carried as `ModMatch.local` → `ModOption.local` → `ModFocus.local` →
`StatIndex.stat_id(local=…)`. Deliberately computed from everything the base can spawn rather than
from the attribution survivors: which id to search does not depend on how well the mod rolled, and
attribution fails on about one line in five. **Correct coverage is now 96.9%** (582 KiB, schema 2).
What is left is mostly flask utility text GGG publishes no filter for at all — flasks bridge at
69%, gear at 98%.

**Twenty real rares now check `moddb` against GGG instead of against itself.**
`tests/fixtures/moddb/live_trade_rares.json` is scrubbed public listing data carrying, per line,
the stat id GGG's filter list uses and GGG's own tier label. Over 104 mod lines: **104 ids match,
0 mismatch, 0 missing**; prefix/suffix agrees 90/90 where `moddb` commits; tiers agree 85 of 86.
Every other test in the project compared the artifact to expectations written from the same file
it was built from, which is exactly why a *shared* mistake survived a green suite.

**The open-affix filter works, and its name and shape are right.** `# Empty Prefix Modifiers` →
`pseudo.pseudo_number_of_empty_prefix_mods`, `{"value": {"min": N}}`. Live: rare body armours with
`≥40% increased Armour` and **≥1** free prefix → 10 000+; the same query with **≥3** → **0**,
which is the right answer, since a 40% armour roll *is* a prefix. Five live searches total.

**The pre-ticking is usable and it does tick noise, and the noise is not a bug to fix here.**
Across the twenty items it ticks a median of 3 rows out of 5; the distribution is 1×4, 2×5, 3×5,
4×3, 5×2, 6×1. Of 53 ticks, **8 are on mods nobody prices** — `increased Stun and Block Recovery`
five times, plus light radius, global accuracy and physical reflect — every one of them a genuine
T1/T2 roll of a group nobody searches. That is `NEAR_TOP_TIER` working exactly as specified: it is
a claim about the *roll*, and §5b's own conclusion is that which mods matter is not derivable from
the item. A junk-mod list would be `MOD_GROUPS` coming back.

Two failure modes are worth naming. On the best item in the sample — 2-divine Soldier Gloves, six
T1/T2 rolls — it pre-ticks **6 of 6**, and a manual check never broadens, so the default button
press sends the six-filter conjunction that Phase 9 measured returning zero listings. And where
`moddb` mis-tiers, the pre-tick misses the mod that *makes* the item expensive: a 2-divine Platinum
Kris's `+1 to Level of all Lightning Spell Skill Gems` is GGG's P1 and `moddb`'s "T3 of 3", so it
starts unticked while two lesser suffixes start ticked.

**A row that cannot become a filter now says so before the button, not after the answer.**
The panel's annotation moved from `no offline trade id` to `no trade filter` — at 96.9% coverage
the remainder really is "GGG publishes none", which is a fact about the search rather than about
us — and ticking such a row raises a `not searchable` line naming the mods. `build_plan` already
reported this, but it reported it in the query description, which arrives once the requests have
been spent. Still annotated, never disabled.

**Still open, measured rather than guessed.**

- **77 sentences have two ids in GGG's own document** and the live `StatIndex` picks the first.
  Pre-existing, unchanged, and not fixable from the trade document alone.
- **Bench-craft tier ladders are numbered differently from GGG's `R<n>`.** Not compared here.
- **`scripts/build_moddb.py` is not deterministic.** Rebuilt from byte-identical sources (same
  four sha256s) it produced a different artifact: 16 mods with their local-reading flag flipped
  and 76 `game_stats` keys changed. Found while checking whether the bridge fix below could
  simply be regenerated; it cannot, because the diff would not be attributable. Almost certainly
  set iteration in `locality_index` / `line_locality`. Nothing downloads at runtime and the
  committed artifact is fine — what is broken is the ability to *verify* a rebuild.

**Done:** 1092 Python tests and 147×2 frontend tests, all offline. Nine live requests spent on
investigation — four searches, one fetch-shaped pair, and the build-time stats document.

### Phase 9c — the three measured defects Phase 9b left — **done**

**The pre-tick was worst exactly where the item was best, and the cap is 2 because 2 was
measured.** On the fixture's 2-divine Soldier Gloves the pre-tick ticked 6 of 6, and a manual
check never broadens, so the default press sent the six-filter conjunction Phase 9 had already
measured returning zero. The obvious cap was 3 — the median was 3 and §5b called it fine — so it
was run live against Allflame with the panel's own widened floors: **6 filters → 0 listings,
3 → 0, 2 → 3, 1 → 35.** Three is not a smaller version of the bug; the median was an observation
about what the pre-tick *did*, never a measurement that 3 finds anything. `MAX_PRETICKED` is 2,
which is also the number Phase 9 measured independently for the automatic path.

Order comes from `highlight.significance`, ranked on **facts `moddb` already has** and on no list
of mod names: influence pool first, then the **item level the game demands for the roll**
(`min(required_level)` over the survivors — GGG gates its deepest affixes behind item level, so
this is the game's own ordering), then proximity to the base's ceiling, then ladder depth, then
the item's own line order. On the gloves that keeps 96% increased Armour and Energy Shield
(ilvl 84) and 16% increased Attack Speed (76) and drops the T2 life whose tier unlocks at 44.
`ItemHighlight.note` says how many eligible rolls were held back, before the request rather than
after the answer; no row is disabled and `MAX_QUERY_FILTERS` is still 6.

Re-measuring the twenty rares: **ticks 57 → 33, maximum 6 → 2, median 3 → 2, and the 8 noise
ticks → 4.** Half the noise went without a single mod name being written down — the cap took
`+15% increased Light Radius` and `20% increased Global Accuracy Rating` off item 1 on
required-level alone, and the summed-line refusal below took two stun-and-block-recovery ticks.
The other four survive, and they should: they are genuine T1 rolls and §5b's conclusion still
holds.

**A negative roll needed a direction before it needed an id, and the reason it had neither was a
normalizer disagreement.** `-(4-9) to Total Mana Cost of Skills` is stored under the key
`-# to …` with a *positive* range, and a rolled `-9` normalized to `# to …` with a negative
value — so every mod in the family came back `unknown mod`, which reads exactly like caution.
`readings()` now offers both spellings and the vocabulary decides; both are real, since
`IncreaseFlatManaCost` writes `# to …` for its −4 and −5 tiers and `-# to …` for the rest, so the
first spelling that merely *exists* is the wrong answer. Ranges are flipped into displayed units
on load, `ModMatch.higher_is_better` reads the direction off the mod's own reachable range, and
`ModFocus` gained `maximum`: a ticked `-9` now sends `max: -7.2`, which contains the item it came
from. `ceiling` follows the same direction — taking the maximum of a negative ladder returned the
worst tier in the game and measured every roll against it.

The id half is two fixes. `StatIndex` tries `+#` when `-#` misses, which is what the *query* uses
and is live today; `build_moddb.py` does the same, which is what the offline `tradeable`
annotation uses and lands at the next league rebuild. Checked against GGG's document:
`explicit.stat_3736589033` and `explicit.stat_3441651621` exist under `+#` and under nothing
else. Four sentences in the current vocabulary are in this family and all four were unbridged.

**A tier that might be the sum of two affixes is no longer asserted as one.** The confidence was
the defect, not the miss: two readings fit `+161 to Evasion Rating` and nothing on the item
separates them, so the honest-unknown path is the answer and it was not firing.
`_summed_sides` asks whether two spawnable mods **of different groups**, both of whose other
sentences the item shows, could add up to the displayed number — and `report` then asks whether
the item has a **free affix slot** for the second one, counted from the same numbers
`open_prefixes` publishes. That second question is what makes it a refusal rather than a policy:
per line the sum is conceivable on 12 of 99 fixture lines, against the affix budget it is 5. The
Titanium Spirit Shield's `+159 to maximum Life` keeps its T1 because three prefixes are already
spent, which is the case where withholding would have cost the most.

**Done:** 1114 Python tests and 147×2 frontend tests, all offline. Four live searches spent, all
four on the same pair of query shapes.

### Phase 10 — stash tabs
Tab enumeration and per-tab fetch (one request per tab; no batch endpoint). Remove-only tabs
fetched once and cached forever — 86% of the measured Standard stash, taking a full refresh from
~34 min to ~45 s. Per-tab staleness. Quad tabs at 24×24. Special tabs have bespoke layouts and
`stackSize` legitimately exceeds `maxStackSize` there. Map tabs returned zero items in both
leagues and may need substash traversal — unresolved, and the failure mode that silently
under-reports value.

**Done:** the highlighting and manual check work over stash tabs, not only the bag.

---

## 6. Decisions made, flag if you disagree

| Decision | Rationale |
|---|---|
| `prices` its own module, siblings depend on it | Crafting wants a number, not a verdict |
| Runtime services are not modules | Every module needs them; declaring it everywhere is noise |
| Core modules may ship UI | `credentials` needs a pairing screen; "core" limits dependencies and decisions, not whether it has a face |
| Build-time profile selection | Neither bundle carries the other profile's implementation |
| `full` profile before `compact` | Reaches a usable surface sooner; contracts designed for both up front |
| No module versioning yet | Ids only. Add semver if third-party modules ever land |
| Standard league, 20c threshold, port 7331 | Configurable; 7331 avoids Decky's 1337 |
| `prices` requires `net` as well as `poeapi` | It fetches two non-account hosts; §4 forbids opening a socket outside `net`, and a passthrough on `PoeApi` would be a hole in the rule that protects the account |
| Third-party hosts get a per-hostname bucket in `net` | Structural rather than requested: a feature module cannot accidentally spend GGG's budget, and it needs no configuration call to avoid doing so |
| Chaos is the only unit inside `prices` | poe.ninja's item overviews are denominated in exchange chaos (measured); carrying two denominations makes every sum a conversion bug waiting to happen |
| Stash deferred | Prototype is bag-only; the digest reuses `prices` and the strict gate |
| `appraisal` requires `poeapi` as well as `prices` | Its API is defined over `NormalizedItem`, and without a bag accessor `appraise_bag` would have to be a method the frontend posts a whole bag to. Feature→core is the ordinary direction |
| A rare with no bulk price is *not* `unpriceable` | Bulk has never priced rares. Calling that a gap in poe.ninja's coverage would fill the panel with question marks and hide the gaps that are real |
| Which poe.ninja tables exist is discovered per league | A hardcoded list of 26 types omitted `Ducat` and made a whole item class unpriceable for a league. Being more careful with the list is not a fix; asking the league is |
| Discovery derives type names from sitemap slugs | It has to be able to find a type nobody typed in, or it is a slower way to have the same bug. There is no type-index endpoint (research-notes §9.6) |
| ~~Tier 3 is eager for a bag and never for a stash~~ | **Reversed in Phase 9.** It failed twice live in opposite directions, and no setting between them is right. Tier 3 is now never automatic at all; a rare is highlighted and the player asks |
| Tier 1b is numbered 1b, not 2 | Tiers 2 and 3 are referenced by number across four documents and every module docstring. The new source is a bulk answer that runs where tier 1 missed, so it belongs beside tier 1 |
| ~~Mod "tiers" are one regex and one threshold per group~~ | **Deleted in Phase 9**, as `gate.py`'s own docstring asked. `moddb` answers per base and per pool; `+95 to maximum Life` is T4 of 10 on a helmet and T7 of 13 on a body armour, and no single number says that |
| The four highlight criteria are the plan's, plus three stated retentions | §5b names valuable-base-at-high-ilvl, high-tier rolls, six-link and influence *mods*. `fractured` and `synthesised` are kept as hard signals and `unidentified`/`veiled` as soft ones — all four are facts about the item that cost nothing to read, and dropping them would be a regression nobody asked for. The deviation is stated in `gate.py` rather than buried |
| What survives subtracting GGG's tag is an opinion, and is labelled one | 7 of the 26 hand-typed bases carry `top_tier_base_item_type`. The other 19 are a claim about the *market* — a Stygian Vise is wanted for an abyssal socket and no game file says so — so they are a soft signal, off at strict, and extendable by setting rather than by editing code |
| A manual price check never broadens itself | Broadening answers a different question and reports the answer under the player's heading. "No listings matched what you ticked" is an answer, and the fix for it is a tick |
| `MAX_QUERY_FILTERS` is 6 for a selection and `MAX_STAT_FILTERS` stays 2 for the automatic path | Six is how many affixes a rare has. A player who ticks all six has asked for a near-exact-match search, and the honest response is to run it and report the one listing it found *as one listing* — not to silently drop half the query. A selection is not a heuristic |
| The stat index keeps every group per sentence and chooses at lookup | `setdefault` made the first group win and `pseudo` is first, so explicit mods were being searched as aggregates. Which id is right depends on where the line sits on the item, which is a lookup-time fact |
| `CheckList` is the one kit list with no `limit` | Every other list truncates and reports, which is honest. A checkbox list cannot: a hidden row is a filter the player can neither see nor switch off, and the item's own six-affix ceiling already bounds it |
| The wire schema is pydantic in `transports/wire.py` | `prices` and `appraisal` use plain classes with `to_json()` on purpose. §3 still wants pydantic as the single source of TS types, so the *wire* shape is declared in the transport, generated from, and validated against the real `to_json()` output. Both halves are tested; a schema nothing validates against is a wish |
| `check` is split in the UI, not in `Verdict` | Phase 4 asked for the split before the panel was drawn. Both lanes still mean *look before you vendor*, so the difference is layout: a fifth verdict would need a fifth colour and a change to the CLI, the event payload and every test |
| The compact profile is implemented, not stubbed | The phase's stated risk cannot be checked against signatures. Running the real test suite through the other profile found five content drops and one forked vocabulary; a stub would have found none of them |
| The HTTP transport refuses non-loopback `Host`/`Origin` | Binding to 127.0.0.1 stops another machine, not another tab. Any page can POST to localhost, and DNS rebinding gives it an origin. No CORS headers, ever |
| `moddb` is core with `requires: []` | Which affixes exist, what each rolls and where each spawns are facts. Judging whether a T1 roll is worth keeping is a feature opinion, and it stays in `appraisal` |
| The mod database is a committed artifact, not a download | A Decky plugin installs from a zip with no pip. 30 MB of upstream JSON trims to 566 KiB of what the consumers actually ask about, and a runtime fetch would put a startup dependency on a GitHub Pages site |
| Regeneration is documented and stamped rather than automated | It cannot be automated inside a plugin that must not fetch. So the artifact carries its game version and build date, four surfaces show them, and staleness is a warning rather than a surprise |
| Attribution has four states and two of them refuse to name a tier | "T2" when the truth is "probably T2, possibly T3" is a lie the player acts on and cannot check. The evidence stays on the return value; only the *claim* is withheld |
| A tier ladder is keyed by (group, base, pool) | The same sentence is T4 of 10 on a helmet and T7 of 13 on a body armour, and an essence or influence tier is counted from a different place entirely. A ladder keyed by group alone produces "T1–T5 of 9", which is two numbers off two rulers |
| Whole-item context narrows candidates but never eliminates them | A hybrid is ruled out by the line the item does not show — a deduction. But if *every* candidate wants a missing line, the likelier explanation is that the caller under-described the item, so the unfiltered answer comes back with a note |
| Mod texts are matched, not stat ids | RePoE's `text` is already rendered with every index handler applied, so the trim step never reimplements the translation renderer. `stat_translations.json` is then needed for exactly one thing: bridging that text to the trade API's opaque ids |

---

## 7. Open items

**Unresolved from SPEC §11**, none blocking: Cloudflare in Steam's CEF browser (OAuth only), map
stash substash traversal (stash only), suspend/resume (Phase 7), hideout re-entry log line
(Phase 6).

**OAuth scope list** for the application email — worth drafting once crafting and guides are
specced, since atlas passives are OAuth-only and adding scopes later means queueing with GGG twice.
