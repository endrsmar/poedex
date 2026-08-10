# PoEDex — Specification

**Status:** draft v0.2 · **Last updated:** 2026-08-10
**Target game:** Path of Exile 1 only (see [§10](#10-assumptions) — PoE 2 is a hard constraint, not a caveat)

Revised after five parallel research streams. Findings and sources in
[`research-notes.md`](research-notes.md); this document carries only conclusions.

---

## 1. Goal

A background helper for Path of Exile on the Steam Deck that answers one question fast:

> *My bag is full. Is any of this worth a stash trip, or is it all vendor trash?*

It must work in **Steam gaming mode**, not just desktop mode.

### 1.1 Primary user story

Player finishes a map and portals to their hideout. The tool notices the zone change, syncs,
and prices the bag. Player presses the `…` button and sees each slot marked
**keep / check / trash / unpriceable** with a bag total — no keyboard, no alt-tab, no
leaving gaming mode.

---

## 2. Non-goals

Each rejected for a concrete reason. Do not re-propose without reading `research-notes.md`.

| Non-goal | Why |
|---|---|
| Any interaction with the game client | Clipboard scraping, keystroke injection, memory reading, OCR. Bannable or fragile, and unnecessary — the API and the log file provide everything. |
| Drawing an overlay on the game's inventory grid | Gamescope composites a single fullscreen surface in gaming mode. |
| Real-time drop highlighting | The API commits inventory at zone transitions (§4.3). Physically impossible. Loot filters own the drop moment. |
| Global controller chord to trigger sync | Valve removed `RegisterForControllerStateChanges` on 2025-10-03; hotkeys are broken across all Decky plugins. Achievable via raw `hidraw`, but obviated by the log trigger (§4.6). |
| Decky **store** distribution | The store rejects AI-assisted plugins outright, with a required attestation. This project is AI-assisted. Sideloading via GitHub Release is first-class and permanent (§6.4). |
| Path of Exile 2 support | GGG removed inventory items from the PoE2 character endpoint in 3.27.0. There is no data path. §10.1. |
| Executing trades | No PoE API can. The seller invites; the player takes the portal. |
| A shipped web UI | The UI is the Decky panel. A local renderer exists only as a **dev harness** (§6.5). |

---

## 3. Architecture

```
┌──────────────────────────────────────────────────────────────┐
│  UI — Decky Loader plugin (React, Quick Access Menu)          │
│       300 CSS px wide. Verdict surface, not a browser.        │
├──────────────────────────────────────────────────────────────┤
│  Pricing + verdict engine                                     │
│    tier 0  the item's own `note` field (free)                 │
│    tier 1  bulk price table (poe.ninja)                       │
│    tier 2  local heuristic gate, strictness-parameterized     │
│    tier 3  trade API query (rate-limited, queued, on demand)  │
├──────────────────────────────────────────────────────────────┤
│  Data layer                                                   │
│    Client.txt tailer  →  zone-change events (free, no API)    │
│    HTTPS client       →  inventory + stash, shared limiter    │
└──────────────────────────────────────────────────────────────┘
```

Two inputs, both passive: an HTTPS read of account data, and a read-only tail of a log file
the game writes to the Linux filesystem. Neither touches the game process.

---

## 4. Data layer

### 4.1 Authentication — the hardest UX problem

**v1 ships POESESSID entered via a LAN pairing page. OAuth is the target state, blocked on GGG.**

**Why not just type it:** POESESSID is a 32-char hex string, and gaming mode has only a
thumbstick-driven on-screen keyboard with no copy key. Estimated 60–120s with real typo risk.

**Why not OAuth yet:** GGG's registration is closed as of 2026-07 — *"We are currently unable
to process new applications."* Historically cyclical (it also closed for the PoE2 EA launch and
reopened), and approval takes 3 weeks to 2 months once open. OAuth is available to hobbyists —
no business entity required — but it cannot gate v1.

**The v1 mechanism — LAN pairing page:**

1. QAM shows a **Pair** button. Backend binds `0.0.0.0:<port>` and generates a 6-digit code.
2. Panel displays `http://<deck-ip>:<port>` and the code, both large.
3. User opens that on their PC — where the browser dev tools holding the cookie already are —
   enters the code, pastes POESESSID, submits.
4. Backend writes it `0600`, **closes the listener immediately**, pushes success via `decky.emit()`.

Zero characters typed on the Deck. Prior art: Decky Clipboard, DeckyFileServer, Decky LocalSend
all ship this pattern.

Security constraints on the pairing listener — it is a full-account credential intake:
bind only during an active pairing window, single-use code, short timeout, reject non-RFC1918
sources, never log the value.

Secondary escape hatch: a `TextField` with paste. The two-year gamescope clipboard bug closed
2025-08-01, so paste works; copy still does not.

**OAuth target state**, to build when a client ID lands:

- Public client, Authorization Code + **PKCE (`S256`)**, fixed `127.0.0.1:<port>` redirect.
  Register one port — the redirect URI must match exactly. Codes expire in **30 seconds**.
- Flow: backend `asyncio.start_server` → `Navigation.NavigateToExternalWeb(authorizeUrl)` opens
  Steam's browser in gaming mode → callback → exchange. Exact precedent: the **Moddy** plugin
  does Nexus Mods OAuth+PKCE this way from a non-root Python backend.
- Scopes to request, with justification: `account:characters`, `account:stashes`,
  `account:profile`, `account:leagues`. Nothing else.
- **Access token 10h, refresh token 7 days, and the 7 days cannot be extended** — a refreshed
  token inherits the original expiry. Silent refresh works inside the window; re-consent is
  **weekly**. OAuth reduces cost-per-event, not frequency. Do not oversell it.
- A working Auth-Code+PKCE-against-GGG reference already exists in this user's own `poe_mcp`
  project (`src/auth/oauth.ts`) — port it to Python rather than writing from scratch.
- **Blocking unknown:** whether pathofexile.com's login and Cloudflare work inside Steam's CEF
  browser. Untestable without hardware. Test before committing.

Three Decky-specific landmines, from Moddy's own source: persist the PKCE verifier and state
**to disk** (a backend reload between navigate and callback loses memory state); use
`asyncio.start_server`, **not `http.server`** (Decky's stripped Python crashes the backend on
some stdlib imports); no client secret.

Keep the credential path abstract so OAuth swaps in without touching callers.

### 4.2 Endpoints

Session-cookie path, header `Cookie: POESESSID=<value>`, host `https://www.pathofexile.com`:

| Purpose | Endpoint |
|---|---|
| List characters | `GET /character-window/get-characters` |
| Equipment + inventory | `GET /character-window/get-items?accountName={a}&character={c}` |
| Tab list + one tab | `GET /character-window/get-stash-items?accountName={a}&league={l}&tabs=1&tabIndex={n}` |

A `User-Agent` with contact details is required by GGG on all calls.

**Inventory is owner-only.** Unauthenticated requests across nine public profiles returned
equipped gear and flasks, zero `MainInventory` items. Auth is load-bearing, not a convenience.

### 4.3 Freshness — **ANSWERED**

**The character endpoint commits at zone/instance transitions, not live.** Also on login/logout
and level-up. Post-transition lag ~0–5s.

Measured: 20 samples at ~28.6s over 9.1 minutes against a live mapping character, tracking
summed socketed-gem XP. Nineteen identical readings, one discrete +3,622,397 jump, zero
intermediate values. A live endpoint would move at nearly every sample. Corroborated by GGG's
own "read-only snapshot" wording, explicit tool-author documentation, and the fact that every
PoE1 loot tracker fetches only inside an `entered`-zone branch. Confidence ~90%.

No HTTP or CDN caching (`cf-cache-status: DYNAMIC`, no `Cache-Control`/`ETag`/`Age`) — the
staleness is the sync model itself, not a TTL you can work around.

**The stash is different: near-live, seconds, not gated on zoning.** Chaos Recipe Enhancer polls
on a flat 15s timer and explicitly works while standing in a hideout. Occasionally laggy under
server load; an instance change forces it. This makes the stash the *fresher* data source, and
it updates exactly where the player is standing when the panel matters.

Confirm with a 60-second self-test during M1: stand in a map, poll every 5s, pick up currency,
keep polling ~60s (expect nothing), portal to hideout, poll again (expect it appears).

### 4.4 Sync policy — event-driven, no interval timer

**Measured limits**, authenticated: policy `backend-item-request-limit`, Account `30:60:60` and
`100:1800:600`. That is a sustained ceiling of **one request per 18 seconds, forever**.

- A 10s poll busts it at 16.7 minutes and earns a **600-second restriction**. 20s leaves zero
  headroom. Any interval poll is the wrong shape.
- **`get-items` and `get-stash-items` share this bucket.** Trade endpoints do **not** — separate
  policies, separate buckets. Inventory polling starves stash appraisal, never trade queries.
- `get-characters` is tighter still (`10:60`, `50:1800`). Cache hard; never poll it.
- Anonymous and authenticated requests get *different* IP policies on the same endpoint.
  **Never hardcode. Parse `X-Rate-Limit-*` and drive a token bucket from it.**

**Triggers, in priority order:**

| Trigger | Source | Debounce | Notes |
|---|---|---|---|
| Manual refresh | Button | 5s | Always preempts; blocked only by an active 429 |
| Zone entry → hideout/town | Client.txt | 20s | Primary auto-trigger |
| QAM opened | `useQuickAccessVisible()` | data > 30s | Covers log-watcher failure |
| Zone entry → map | Client.txt | — | **Skip.** Syncs, but the bag was just emptied — wasted budget |
| Interval timer | — | — | **None.** |

Budget allocation of 100 per 30 min: event sync 40, manual 20, stash 30, permanent headroom 10
(the same POESESSID is the user's live browser session).

Limiter: key buckets by `(policy, rule, period)`, learn policy names at runtime, trust the
server's `-State` count over your own, keep a margin of 2–3, pad periods 1–3s for clock skew,
seed pessimistically before the first response, exponential backoff with a `Retry-After` floor,
and **refuse rather than silently queue**.

Hash the **normalized item set**, not raw JSON, for change detection — and never back off a
user-initiated or event-driven sync on an unchanged hash.

### 4.5 Normalized item model

The boundary. Pricing consumes this, never raw API JSON.

```
Item
  uid, name, base_type, category, rarity, ilvl, stack_size
  grid          {x, y, w, h}          # enables slot-accurate rendering
  sockets       {count, links, colors}
  corrupted, fractured, synthesised, identified
  influences    [shaper, elder, crusader, hunter, redeemer, warlord]
  mods          {implicit[], explicit[], crafted[], enchant[], fractured[]}
  note          str | None            # the user's own ~price / ~b/o tag — tier 0
  location      {source: bag|stash, tab_id, tab_name}
```

Everything crossing to the frontend is `json.dumps`'d — plain JSON types only.

### 4.6 Client.txt trigger

PoE writes a plain UTF-8, append-only log the tool can tail without touching the game process.
GGG's policy explicitly carves out tools that *"read the client log files"*; the 2020 POE Overlay
ban wave was caused by **API abuse plus auto-pricing**, not log reading. Log tailing is the safe
part of this design, and it is safe *because* it lets us delete the risky polling.

**Path.** Under Proton the log is at `<library>/steamapps/common/Path of Exile/logs/Client.txt`
— a native Linux path, **not** inside the Wine prefix. Do not hardcode: parse
`~/.steam/steam/steamapps/libraryfolders.vdf` to enumerate library roots (the Deck's SD card is a
second library and its mount point has moved across SteamOS releases), then probe
`steamapps/common/Path of Exile/logs/Client.txt`, falling back to the two `compatdata/238960/pfx`
locations. Expose a manual override in settings.

**Parsing.** Lines look like:

```
2018/05/13 16:10:14 1801062 9b0 [INFO Client 1636] : You have entered The Twilight Strand.
2018/05/13 16:10:08 1795218 d8  [INFO Client 1636] Generating level 83 area "MapWorldsGrotto" with seed ...
```

- **Anchor on the system-message prefix `] : `.** A player in your instance can type
  "You have entered …" into local chat and it lands in the log verbatim. Substring matching is
  spoofable.
- **Strip `<<set:..>>` tokens** — non-English clients prepend gender/plurality markers, and the
  phrase itself is translated.
- **Classify on the area id** from the `Generating level N area "<id>"` line, not the display
  name. The id is language-independent; the display name is translated and user-themed
  (Canal Hideout, guild hideouts, …).

**Tailing.** Seek to EOF on start — never read from byte 0; the file never rotates and GGG's own
forum has a report of a 2 GB Client.txt. Poll `stat()` at ~1s (or `inotify`), read forward from
the offset. **Handle truncation** (`size < offset` → user deleted it → reopen at 0) and absence
(file does not exist until PoE has run once). Steady-state cost is one `stat()` per second.

Fallback: if the path cannot be resolved, degrade to button + QAM-open and **say so in the
panel** with a link to set the path. Never degrade silently.

---

## 5. Pricing + verdict engine

Measured composition of the real test account: **~98% of items resolve at tier 1.** The feared
"hundreds of rares" case does not exist, because players sort their stash and rares are
spatially segregated into one or two gear tabs.

### 5.0 Tier 0 — the item's own note

Items and tabs carry the user's own asking price (`~price 2 awakened-sextant`, `~b/o 25 chaos`).
Free with every fetch. Use as a value fallback *and* as a comparison signal (§6.3).

### 5.1 Tier 1 — bulk price table

poe.ninja. **The legacy `/api/data/currencyoverview` paths now 404.** Routes measured
2026-08-10 and cross-checked against poe.ninja's own API reference (`/docs/api`) — the league
and category are **query parameters**, and the path ends in `/overview`:

| Purpose | Route |
|---|---|
| Economy leagues | `GET /poe1/api/economy/leagues` |
| Exchange overview | `GET /poe1/api/economy/exchange/current/overview?league={l}&type={t}` |
| Stash item overview | `GET /poe1/api/economy/stash/current/item/overview?league={l}&type={t}` |

Covers currency, fragments, scarabs, fossils, essences, oils, delirium orbs, incubators, maps,
divination cards, uniques. Lookup by name (+ tier/corruption for maps, links/variant for uniques).
Divination cards are on the **exchange** overview, not the item one.

**Two shapes, one unit.** Exchange lines are keyed by an opaque `id` — which is also the trade id
a `~price N divine` note uses — with names in a sibling `items[]` array and values in
`core.primary`, i.e. chaos. Item lines carry `name`/`baseType`/`chaosValue` inline. A third
endpoint, the *stash currency* overview, quotes a materially different `chaosEquivalent`
(Divine Orb 618c against the exchange's 898c on the same day) and is **not used**: the item
overviews' `chaosValue/divineValue` ratio matches the exchange rate exactly, so the exchange is
the unit everything else is denominated in. See research-notes §9.2.

Cache 30 min with ETag conditional requests — poe.ninja serves `max-age=1800` (confirmed on the
wire; their docs page's "roughly 5 minutes" is wrong) and refreshes roughly every 15 min. The
ETag is weak and unquoted (`W/<hex>`) and must be echoed verbatim. Prefetch at plugin load, never
on a button press. Costs zero GGG budget — a different host gets its own rate-limit bucket.

**Sum by quantity, not by item.** `Jeweller's Orb ×2615` and `Divine Orb ×5` are both one row.

**Deduplicate before pricing.** Twenty identical flasks are one price lookup fanned out.

**One name is often many lines.** `Map (Tier 16)` is listed once per map series (13 lines, 1c to
898c) and `Pillar of the Caged God` once per base type × link count (6 lines, 0.96c to 718,160c).
Match on base type, links and corruption; break ties on **listing count**, because liquidity
picks the current series without the code needing to know what a series is.

⚠️ poe.ninja asks that desktop apps proxy these calls through their own backend. PoEDex has none
and cannot comply; the mitigations behind that request — caching, conditional requests, a
contactable User-Agent, controlled volume — are all in place. research-notes §9.4.

### 5.2 Tier 2 — local heuristic gate, strictness-parameterized

Same code, a `strictness` parameter, because the two contexts want opposite biases:

- **Bag: generous.** A false negative means telling the player to vendor something good.
- **Stash: strict.** The item already survived bag triage and is in storage, not about to be
  vendored. A generous gate here produces hundreds of false positives.

Strict gate hard requirements (must hit ≥1): influence / fractured / synthesised, 6-link,
ilvl-86 on a base where it matters, or a base on an explicit high-value allowlist. The
"desirable mod group present" signal is dropped at stash strictness — it is the false-positive
engine.

**Classify each tab by composition on first fetch** (`bulk` / `gear` / `mixed`). Bulk tabs never
enter tier 2 or 3 at all.

### 5.3 Tier 3 — trade API query

Only on demand: the user focuses an item, or explicitly prices a tab with a visible cost
(*"43 items, ~12 will escalate, ~4 min"*). **Never eager.**

`POST /api/trade/search/{league}` then `GET /api/trade/fetch/{ids}?query={id}` (max 10 ids).
Stat filters key off opaque ids from `/api/trade/data/stats` — never readable text.

Filter to online sellers; take the **median of the cheapest N**, not the minimum. Show a
per-item `pricing…` state that never gates the grid; display the bag total as `≥ N div` while
tier-3 items are outstanding.

Trade limits are separate from the item bucket: `trade-search-request-limit`
`5:10:60, 15:60:300, 30:300:1800, 600:21600:3600`. Both trade policies are **Ip-ruled only**,
with no Account rule — the endpoints need no credential, and sending one would tie a public
query to the account for nothing. Re-measured 2026-08-10, unchanged. `/api/trade/data/stats`
carries no rate-limit headers at all and a `max-age=1799`.

⚠️ Listings carry third-party PII: `account.name`, `lastCharacterName`, and a `whisper` string
containing the seller's character name. Never log a raw listing.

### 5.4 Verdict model

Four states, encoded in **both** color and shape so the grid survives greyscale.

| Verdict | Meaning |
|---|---|
| `keep` | At or above the keep threshold |
| `check` | Below threshold but non-trivial, or tier-3 pending |
| `trash` | Confidently below threshold |
| `unpriceable` | **Not in the price index** — do not conflate with trash |

`unpriceable` is not optional. The test account's Standard stash holds ~170 `Veiled Scarab`, a
removed item absent from poe.ninja's league index. Reporting those as `trash` would understate
stash value badly and destroy trust in the total.

**Open:** keep threshold default. ~20c gives a busy panel; divine-tier gives a quiet one.

---

## 6. UI — Decky Loader plugin

### 6.1 Verified constraints

The Deck's gamepad UI runs at **1.5× scale** (CSS viewport 853×533). The QAM column is
**300 CSS px**, **268** inside a `PanelSection`, with ~396 CSS px of scrollable height.
Rendering the grid *outside* a `PanelSection` recovers the full 300px and takes cells from
22px to 24px.

**2D grid navigation is free.** Steam's focus system resolves D-pad direction geometrically
against live DOM rects, not an ordered list. A CSS grid of `Focusable` cells navigates in 2D
with no extra work — verified against the shipping decky-steamgriddb plugin. Multi-slot items
place via `grid-column: span`; empty slots are simply absent, so the D-pad skips them.

Use `onGamepadFocus` to drive a detail line beneath the grid — that is how detail fits in 268px.
Use `noFocusRing` + a custom `focusClassName`; the default ring is thick enough to merge
adjacent 22px cells.

### 6.2 Backend lifecycle

Each plugin is a **separate OS process** with its own asyncio loop running `run_forever()`.
`_main()` is scheduled as a task and not awaited — an infinite loop there is the intended
pattern. It keeps running when the panel is closed, across game launch/exit, and across Steam UI
reloads. It stops only on Decky shutdown, plugin disable/uninstall, or a debug-flag hot reload.

**`decky.emit()` pushes events to the frontend**, so the panel opens *already populated* rather
than fetching on open. Register the listener at plugin definition, keep state in a module-level
store — panel content is unmounted whenever the QAM closes.

Frontend→backend methods must be `async def` and must not start with `_`.

**Unverified:** nothing in the loader handles suspend/resume, so `asyncio.sleep` likely overshoots
across sleep. Use `time.monotonic()` deltas; treat a large jump as "resumed — force refresh and
re-derive the rate budget conservatively." Test on hardware in M5.

### 6.3 Screens

**Bag (default).** Bag total, keep/check/trash/unpriceable tally, the 12×5 slot grid, ~5 item
rows. The grid earns its width here because it is a *map* — a green cell is the exact slot the
cursor must find.

**Stash digest.** Stash worth, value-by-class bar, "sell now" top items, **your-price vs market**
(from tier 0), near-complete splinter/card sets. Deliberately *not* a browser: when you are
looking at your stash you are standing at it, and the game already shows it full size. The
question is "what should I do", not "where is it".

**Tab list.** Per-tab value bar and item count, own staleness stamp, sorted by value. With the
remove-only rule (§6.6) the default list is 16 rows and never scrolls.

### 6.4 Distribution

**Sideload via GitHub Release.** Decky's install-from-URL is a first-class, permanent path — no
signing, no expiry, explicitly blessed in their docs. The store is a non-goal (§2).

### 6.5 Dev harness

A local renderer of the same JSON, for developing M1–M4 on Ubuntu without a Deck. It is a
**development tool, not a product surface** — no polish budget, not documented for users.

### 6.6 Stash scale

Measured on the real account: Standard has **117 tabs, 101 of them remove-only** (a dead
duplicate of the 16-tab set from every league ever played). Active tabs hold 818 items.

**Remove-only tabs are fetched exactly once, ever** — they cannot gain items. This single rule
takes steady state from 117 tabs to 16, and a full refresh from ~34 minutes to ~45 seconds. It
is the highest-leverage rule in the feature.

Never auto-crawl. A cold crawl is user-initiated, resumable, disk-backed, with an honest
*"~30 min, will pause inventory syncing"* warning. Lazy per-tab fetch on open (1 request, ~1s) is
the primary path.

Density is nothing like worst-case: the 24×24 quad tab holds 112 items (19% full).

Things that break a naive grid model: special tabs (currency/essence/fragment/div-card) have
bespoke layouts and **`stackSize` legitimately exceeds `maxStackSize`** (`Vaal Orb 163/20`);
quad tabs are 24×24, not 12×12; folder nesting exists but was not observed on this account.

---

## 7. Tech stack

| Layer | Choice | Notes |
|---|---|---|
| Backend | Python 3.11+ | Decky plugin backends are Python |
| HTTP | `httpx` | **Vendor into `py_modules/`** — no pip at install time. Pure-Python only; C extensions need the Docker `backend/` path |
| Frontend | React + TypeScript, Rollup via `@decky/rollup` | `react`/`@decky/ui` are external globals — you share Steam's React 19 instance |
| UI lib | `@decky/ui` + `@decky/api` | `decky-frontend-lib` and the old `serverAPI` are gone |
| Tests | `pytest` + recorded fixtures | Pricing must be testable offline |

`plugin.json` must carry `"debug"` (hot reload refuses without it) and **must not** enable
`root`. `package.json` needs `"type": "module"`.

`@decky/ui` locates Steam components by regex over minified code — a Steam update can make
`Focusable` come back `undefined` and white-screen the panel. Budget for it as maintenance.

---

## 8. Security and credentials

- **POESESSID is a full-account website credential**, not a scoped token. Store under
  `DECKY_PLUGIN_SETTINGS_DIR` at `0600`. Never commit, log, or let it reach error output —
  exceptions cross the process boundary into the CEF console.
- **`decky.logger` reconfigures the root logger with `force=True`**, so `httpx` debug logging
  would write request URLs and possibly headers into the plugin log. Explicitly set
  `logging.getLogger("httpx").setLevel(WARNING)`.
- **Do not enable the `root` flag.** Plugins run as `deck` by default; game files and the log are
  readable without it.
- The LAN pairing listener (§4.1) binds only during pairing, requires a single-use 6-digit code,
  times out, and rejects non-RFC1918 sources.
- Compliance risk in this project is **API volume, not the log file**. GGG revokes API access for
  sustained limit violations; the POE Overlay precedent was millions of hits/day plus auto-pricing.
- Sharing POESESSID with a *third party* violates ToS §16. Here it never leaves the user's
  hardware — grey zone, not over the line, but real. OAuth resolves it.

---

## 9. Milestones

| # | Milestone | Exit criteria | Deck? |
|---|---|---|---|
| M1 | Data layer | Client.txt tailer + HTTPS client + header-driven limiter + normalized model. Includes the 60s freshness self-test (§4.3) and the volatile-fields list that gates change-detection hashing. | no |
| M2 | Tier 0/1 pricing + verdicts | Bag total and four-state verdicts for the test bag, cached. CLI output is fine. | no |
| M3 | Decky plugin shell | Installs from a Release zip, panel renders the bag grid, D-pad navigates, `emit` push works. | **yes** |
| M4 | LAN pairing | Credential entered end-to-end with zero characters typed on the Deck. | yes |
| M5 | Stash digest | Tab enumeration, remove-only-once caching, per-tab staleness, tier-1 valuation, digest screen. | yes |
| M6 | Rare pricing | Tier 2 strictness gate + on-demand tier 3 with pending states. | yes |
| M7 | OAuth | When GGG registration reopens and a client ID lands. | yes |

M0 is **retired** — §4.3 answered it. Its residue is the self-test folded into M1.

Sequencing note: stash appraisal (M5) is insensitive to freshness risk and its data source is
*fresher* than the bag's. If anything about the bag path disappoints in practice, M5 is the
hedge, and it can move earlier.

---

## 10. Assumptions

1. **PoE 1 only, and PoE 2 is closed off.** GGG removed inventory items from the PoE2 character
   endpoint in 3.27.0. PoE2 trackers responded by reading game memory, which §2 rules out. This
   is a hard constraint: supporting PoE2 would be a re-architecture requiring either GGG to
   restore the field or an approach already rejected.
2. **League-parameterized throughout.** SSF characters have no meaningful trade prices — detect
   and fall back to tier 1, or say plainly that pricing is unavailable.
3. Development on Ubuntu 22.04; the Deck is a deploy target over SSH. M1–M2 need no hardware.
4. Decky Loader installed on the Deck, from M3 onward.

---

## 11. Open questions

| # | Question | Blocks | Resolution path |
|---|---|---|---|
| 1 | Keep threshold default (~20c vs divine-tier) | Verdict tuning | User decision. Phase 4 shipped 20c as `appraisal.keep_threshold_chaos`; `poedex appraise --threshold N` on a **real** bag settles it, and no real bag has been appraised yet |
| 1b | Should `check` be one state or two? §5.4 gives it two jobs — "cheap but non-trivial" and "unknown value, tier-3 pending" — and on a real bag the first will swamp the second | The Phase 5 bag screen | Decide before the panel is drawn, not after |
| 2 | ~~Which league is primary?~~ **Resolved: none is.** A bag is priced against the league of the character it came from (`ItemSet.league`, read off `get-characters`). `prices.league` became an override, empty by default; `--league` overrides it for one run; an unknown league raises `LeagueUnknownError` instead of defaulting. The old `"Standard"` default priced an Allflame bag against a 897.7c divine instead of 209.0c | — | Done |
| 3 | Does pathofexile.com login + Cloudflare work in Steam's CEF browser? | All of M7 | Hardware test |
| 4 | Do map stash tabs need substash traversal? Nine returned zero items across two leagues. | Stash value accuracy | M5, inspect raw JSON |
| 5 | Does GGG issue refresh tokens to public clients at all? Docs contradict themselves (7d vs 90d) and offer to disable them. | M7 UX | Ask in the application email |
| 6 | Suspend/resume behaviour of the plugin process | Sync correctness | M3 hardware test |
| 7 | Is `Generating level N area` emitted on re-entry to a persistent hideout? | Trigger classification | M1, log capture |
