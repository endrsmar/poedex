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
│  CORE         credentials · net · poeapi · gamelog            │
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
| Tier 3 is eager for a bag and never for a stash | "Never eager" was a stash rule applied to a bag. `Strictness` already encodes exactly that distinction, so it is reused rather than duplicated |
| Tier 1b is numbered 1b, not 2 | Tiers 2 and 3 are referenced by number across four documents and every module docstring. The new source is a bulk answer that runs where tier 1 missed, so it belongs beside tier 1 |
| Mod "tiers" are one regex and one threshold per group | A real mod database is tens of thousands of rows that go stale every league. Stated as an approximation in `gate.py`, used only by the generous gate, and off entirely at strict |
| The wire schema is pydantic in `transports/wire.py` | `prices` and `appraisal` use plain classes with `to_json()` on purpose. §3 still wants pydantic as the single source of TS types, so the *wire* shape is declared in the transport, generated from, and validated against the real `to_json()` output. Both halves are tested; a schema nothing validates against is a wish |
| `check` is split in the UI, not in `Verdict` | Phase 4 asked for the split before the panel was drawn. Both lanes still mean *look before you vendor*, so the difference is layout: a fifth verdict would need a fifth colour and a change to the CLI, the event payload and every test |
| The compact profile is implemented, not stubbed | The phase's stated risk cannot be checked against signatures. Running the real test suite through the other profile found five content drops and one forked vocabulary; a stub would have found none of them |
| The HTTP transport refuses non-loopback `Host`/`Origin` | Binding to 127.0.0.1 stops another machine, not another tab. Any page can POST to localhost, and DNS rebinding gives it an origin. No CORS headers, ever |

---

## 7. Open items

**Unresolved from SPEC §11**, none blocking: Cloudflare in Steam's CEF browser (OAuth only), map
stash substash traversal (stash only), suspend/resume (Phase 7), hideout re-entry log line
(Phase 6).

**OAuth scope list** for the application email — worth drafting once crafting and guides are
specced, since atlas passives are OAuth-only and adding scopes later means queueing with GGG twice.
