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

### Phase 4 — `appraisal` backend
`requires: ["prices"]`. Tier-2 gate with the strictness parameter. Four-state verdicts including
`unpriceable`.

**Done:** `poedex appraise` prints keep/check/trash/unpriceable with a total.

**Validation checkpoint.** If the output doesn't tell you something you didn't know, rethink
before building UI on it. It also gives the real value distribution of your own loot, which
settles the keep-threshold question with data.

### Phase 5 — UI kit, web surface, appraisal UI
Primitive **contracts** designed against both profiles up front — informed by the existing
mockups, which already show what each surface needs. `full` profile implemented; `compact`
deferred to Phase 7. HTTP transport (FastAPI on `127.0.0.1:7331`, SSE, static). Web shell.
`modules/appraisal/ui/` with the bag screen. Honest sync states: fresh / stale / syncing /
unchanged / error / restricted.

*Risk, stated:* designing primitives while implementing only one profile can produce contracts
that don't fit `compact`. Mitigated by writing both profile signatures before either
implementation, and by Phase 7 being allowed to revise contracts rather than pile on overrides.

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

---

## 7. Open items

**Unresolved from SPEC §11**, none blocking: Cloudflare in Steam's CEF browser (OAuth only), map
stash substash traversal (stash only), suspend/resume (Phase 7), hideout re-entry log line
(Phase 6).

**OAuth scope list** for the application email — worth drafting once crafting and guides are
specced, since atlas passives are OAuth-only and adding scopes later means queueing with GGG twice.
