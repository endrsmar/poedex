# Research notes

Evidence behind [`SPEC.md`](SPEC.md), kept so the reasoning does not have to be rediscovered and
so rejected approaches stay rejected for stated reasons.

Sessions: feasibility 2026-08-09; five parallel research streams 2026-08-09/10;
poe.ninja and trade endpoints measured 2026-08-10 (§9, §10).

---

## 1. Why the Awakened PoE Trade model does not port

APT draws an Electron window over the game, triggered by injecting `Ctrl+C` over a hovered item
and reading the clipboard. All three legs break on the Deck:

1. **Overlay.** Gamescope composites a single fullscreen surface in gaming mode. APT users
   confirm the overlay "never appears" when fullscreen; the only workaround is `--no-overlay`
   ([discussion #896](https://github.com/SnosMe/awakened-poe-trade/discussions/896)).
2. **Clipboard.** Proton clipboard bridging has been fixed and re-broken repeatedly
   ([Proton changelog](https://github.com/ValveSoftware/Proton/wiki/Changelog)).
3. **Trigger.** Hover-plus-hotkey assumes a mouse and keyboard.

Do not attempt to port or wrap it. The whole approach is obsolete once the API path exists.

---

## 2. Freshness — measured

**Question:** does the character endpoint reflect items picked up during play?

**Answer: no. It commits at zone/instance transitions**, plus login/logout and level-up. Lag
~0–5s. Confidence ~90%.

**Method.** Needed a continuously-increasing in-zone counter visible through the endpoint.
Socketed **gem XP** is exactly that. Found an actively-mapping character via the trade API's
online-not-AFK listings and polled `get-items` every ~28.6s for 9.1 minutes:

```
samples  1–10  (0–257s)   234,677,561   identical
sample  11     (286s)     238,299,958   +3,622,397   ← single discrete commit
samples 12–20  (314–543s) 238,299,958   identical
```

Nineteen flat readings, one step, zero intermediate values. A live view of a killing character
would change at nearly every sample. Piecewise-constant with one commit is the signature of a
snapshot flushed at an event, and ~3.6M gem XP is roughly a whole map arriving at once.

*Caveat:* the subject's zone transitions were not observable, so the jump cannot be *proven* to
coincide with one. The alternative — idle 4.3 min, earn a map's XP in one 28s window, idle again
— is possible but implausible.

**Corroboration.** GGG's docs call it a *"read-only snapshot."* Tool authors state it outright:
*"The PoE API caches inventory data until you change zones"* (dillapoe2stat);
*"Checks are sent on zone change, not the moment you pick something up"* (Archipelago PoE
apworld). Every PoE1 tracker — exile-diary, Exilence, poe-heistress — fetches only inside an
`entered`-zone branch, with no in-zone polling path at all.

**No HTTP caching.** Live 200 carried no `Cache-Control`, `ETag`, `Last-Modified`, or `Age`;
`cf-cache-status: DYNAMIC`. The staleness is the sync model, not a TTL.

**Stash is the exception — near-live.** Chaos Recipe Enhancer polls a flat 15s timer and states
it works *"no matter if you are in a map or in your hideout"*; the entire chaos-recipe workflow
depends on it. PoETiS notes it can lag under server load and that changing instance forces a
refresh. So the stash is the *fresher* data source, and it is fresh exactly where the player
stands when the panel matters.

**Inventory is owner-only.** Unauthenticated `get-items` across nine public profiles returned
equipped gear and flasks, zero `MainInventory` items.

### 2.1 Closing the last 10% — `poedex selftest freshness`

The measurement above is observational: someone else's character, whose zone transitions we could
not see. The remaining doubt is exactly the part that cannot be automated, because it needs a
hand on the controller. Phase 2 ships the experiment as a command.

```bash
poedex selftest freshness [--character NAME] [--interval 5] [--seconds 240]
```

It polls `get-items`, hashes the **normalized** item set (raw JSON churns on fields unrelated to
the inventory), and prints one timestamped row per poll, marking every hash change. Refusals from
the rate limiter are printed as rows too rather than slept through — a run that is never refused
was not polling hard enough to prove anything.

**Procedure.** Read all of it before starting; the timing is the experiment.

1. Be in a **map**, mid-run, with at least one free inventory slot.
2. Start the command. Let it print two or three baseline rows with an unchanged hash.
3. **Pick up an item** off the floor. Note the wall-clock time.
4. Keep watching for ~60 s. **Expected: the hash does not move.**
5. Take a **portal to your hideout**.
6. Within ~0–5 s of loading in, the hash should change and the item count go up by one.

**Reading the result.**

| Outcome | Meaning |
|---|---|
| Hash moves only after the portal | SPEC §4.3 confirmed. Sync on zone entry; no timer. |
| Hash moves while still in the map | The endpoint is live. That invalidates the event-driven sync model of SPEC §4.4 — raise it before Phase 6 rather than building the log watcher on a false premise. |
| Hash never moves | Wrong character, or the pickup did not land in `MainInventory`. Check the item-count column. |

**Cost.** A 5 s interval is 12 requests a minute against a `30:60` Account bucket and a
`100:1800` one. A 240 s run can spend nearly half the 30-minute budget, and the same POESESSID is
the user's live browser session. Run it once, deliberately.

**Status: not yet run.** It needs a human in the game and was not executed during Phase 2.
Record the outcome here when it is — the wall-clock time of the pickup, of the portal and of the
first changed row, plus which fields moved.

---

## 3. Rate limits — measured

Authenticated, 2026-08-10:

| Policy | Endpoints | Account | Ip |
|---|---|---|---|
| `backend-item-request-limit` | `get-items` **and** `get-stash-items` | `30:60:60`, `100:1800:600` | `45:60`, `180:1800` |
| `backend-character-request-limit` | `get-characters` | `10:60`, `50:1800` | `30:60`, `120:1800` |

Trade (IP): `trade-search-request-limit` `5:10:60, 15:60:300, 30:300:1800, 600:21600:3600`;
`trade-fetch-request-limit` `12:4:10, 16:12:300, 50:300:300, 1000:21600:1800`.

Three structural facts:

1. **Account is the binding rule.** `100:1800` = one request per 18s sustained, forever. A 10s
   poll busts at 16.7 min → 600s restriction. **The v0.1 spec's 10s default was ~2× over.**
2. **Inventory and stash share a bucket; trade does not.** An earlier claim that polling starves
   trade queries was wrong — it starves *stash appraisal*.
3. **Anonymous and authenticated get different IP policies on the same endpoint.** Strongest
   possible argument for parsing headers rather than hardcoding.

Header format: `X-Rate-Limit-<Rule>` as `max_hits:period:restriction`, with
`X-Rate-Limit-<Rule>-State` positionally aligned. Mature limiters (Path of Building, Exilence,
PoeStack, APT) all keep a margin of 1–3 below the max, pad periods for clock skew, seed
pessimistically, and refuse rather than queue. Copy them.

---

## 4. Client.txt

**Works, and it is what lets us delete polling.**

**Path.** Under Proton: `<library>/steamapps/common/Path of Exile/logs/Client.txt` — native
Linux, **not** in the Wine prefix. Verified against APT's Linux build, which hardcodes exactly
that as one of two candidates
([GameLogWatcher.ts](https://github.com/SnosMe/awakened-poe-trade/blob/master/main/src/host-files/GameLogWatcher.ts)),
corroborated by ExileCompanion and SinsGuide. On the Deck, resolve via `libraryfolders.vdf` —
the SD card is a second library and its mount point has moved repeatedly across SteamOS releases.

**Parsing gotchas**, all from mapwatch's regression tests:

- Local chat can spoof `You have entered …` verbatim into the log. Anchor on the `] : `
  system-message prefix.
- Non-English clients prepend `<<set:MS>>` markers and translate the phrase.
- The `Generating level N area "MapWorldsGrotto"` line carries a language-independent area id —
  a far better classification key than a translated, user-themed hideout name.

**Tailing.** No rotation, unbounded growth (GGG's forum has a 2 GB report). Seek to EOF; never
read from 0. Handle truncation and absence.

**ToS.** GGG's policy is *"you may not run programs that interact with the game client,"* with an
explicit carve-out for things entirely external *such as ones that read the client log files.*
The 2020 POE Overlay ban wave was caused by **API abuse (reportedly millions of hits/day) plus
auto-pricing** — not log reading, not the overlay. So log tailing is the safe part of this
design, and the compliance risk lives in request volume.

⚠️ **The carve-out quote is second-hand** (devtrackers.gg mirror; pathofexile.com/forum 429s to
automated fetches). Consistently reported and matches an ecosystem of log-tailing tools running
publicly for years, but **worth a manual browser check before it goes in the README.**

---

## 5. Decky plugin

Read from source rather than the wiki, which is stale (it still documents the removed `serverAPI`).

**Backend is fully independent of the UI.** Separate OS process, own asyncio loop running
`run_forever()`, `_main()` scheduled as an unawaited task. Survives panel close, game
launch/exit, and Steam UI reload. Stops only on Decky shutdown, disable/uninstall, or debug hot
reload. `decky.emit()` pushes to the frontend, so the panel opens already populated.

**2D grid navigation is free.** Steam's focus system is geometric over live DOM rects, not an
ordered list. A CSS grid of `Focusable` cells navigates in 2D with no work — verified against
decky-steamgriddb's asset browser, which does exactly this. This was the design's biggest
perceived risk and it evaporated.

**Geometry, measured** from five gaming-mode screenshots plus a known-CSS-size Decky element:
the Deck's gamepad UI runs at **1.5×** (CSS viewport 853×533). QAM column **300 CSS px**, **268**
inside a `PanelSection`, ~396 px scrollable height. The v0.1 estimate of 310px and a 22px cell
was essentially right.

**⚠️ The Decky store rejects AI-assisted plugins.** Verbatim: *"We do not accept any plugin that
uses any LLM based code… there will be no appeals,"* with a required PR checkbox attesting
*"Generative AI was NOT used to write a majority of the code I am submitting."* Given how this
project is built, the store is not a viable path. Sideloading via install-from-URL is
first-class and permanent, so this costs nothing but the store listing.

**Security notes.** `decky.logger` reconfigures the *root* logger with `force=True` — httpx debug
logging would land request URLs in the plugin log file. Do not enable the `root` flag; plugins
run as `deck` and that is sufficient. Vendor pure-Python deps into `py_modules/`; there is no pip
at install time.

**Unverified:** nothing in the loader handles suspend/resume, so `asyncio.sleep` probably
overshoots across sleep. Needs monotonic-clock deltas and a hardware test.

---

## 6. Auth

**OAuth is open to hobbyists.** No business entity, website, or privacy policy required.
`acquisition` is a solo open-source dev with an approved public client and a bare `127.0.0.1`
callback. Path of Building shipped OAuth 2026-07-25, stating the motive verbatim: *"this
primarily fixes annoying issues with POESESSID expiring all the time."* No statement anywhere —
from GGG or otherwise — that OAuth is business-only.

**But registration is closed.** `/developer/docs` today: *"We are currently unable to process new
applications."* Closed since somewhere between 2026-07-02 and 2026-07-20. Cyclical — it also
closed 2024-11 → 2025-01 for the PoE2 EA launch and reopened. Approval takes **3 weeks to 2
months** (reported figures; the "1–4 weeks" in various READMEs is boilerplate propagating between
repos). Apply by email to `oauth@grindinggear.com` with per-scope justification.

⚠️ GGG: *"we will immediately reject any low-effort or LLM-generated requests."* Write that email
personally.

**No device grant.** Grepped both docs pages for `device`, `urn:ietf`, `8628` — zero hits. Only
Authorization Code + PKCE, Client Credentials, Refresh Token. The phone-plus-short-code UX is off
the table.

**Public client constraints:** PKCE `S256` mandatory, fixed `127.0.0.1` redirect matching exactly,
codes expire in **30 seconds**, access token **10h**, refresh token **7 days that cannot be
extended** — a refreshed token inherits the original expiry. So OAuth means *weekly re-consent*.
It reduces cost-per-event (two button presses vs typing 32 hex chars), not frequency.

**The Deck can do the flow.** `Navigation.NavigateToExternalWeb(url)` opens Steam's browser in
gaming mode and accepts plain `http://` loopback. Exact precedent: **Moddy** does Nexus Mods
OAuth+PKCE from a non-root Python backend in Game Mode. Its landmines are worth stealing:
persist PKCE verifier/state **to disk** (backend reload loses memory), use
`asyncio.start_server` **not `http.server`** (Decky's stripped Python crashes on some stdlib
imports).

**⚠️ Blocking unknown:** whether pathofexile.com's login and Cloudflare work inside Steam's CEF
browser. Untestable without hardware. Precedent is worrying — Decky Cloud Save's own UI warns
*"may not work if Google does not trust the Steam Browser."*

**Text entry reality.** Paste works — the two-year gamescope clipboard bug
([#916](https://github.com/ValveSoftware/gamescope/issues/916)) closed 2025-08-01. Copy does not;
there is no copy key and no Ctrl on the OSK. The Steam mobile app has **no** remote keyboard
(checked against Valve's feature list). No camera, so QR only ever runs Deck→phone. Desktop↔gaming
mode clipboard does not survive the session restart.

**So: the answer is to get bytes onto the Deck's clipboard, or bypass it entirely.**

**LAN paste page — the v1 mechanism.** Backend serves a page on the LAN; user pastes on their PC
from the dev tools already open. Zero characters typed on the Deck. Shipping prior art in the
Decky store: Decky Clipboard, DeckyFileServer (36.8k downloads), Decky LocalSend. Guard with a
6-digit code (MoonDeck uses the same PIN pattern), bind only during pairing, RFC1918 only.

Dead ends eliminated: QR (no camera), KDE Connect (broken on SteamOS), Steam Notes (PC→Deck sync
broken since Jul 2024), Steam chat self-DM (not possible), phone-side OAuth (redirect must be the
Deck's own loopback, and codes expire in 30s).

**Prior art direction is unambiguous:** everything actively maintained has moved to OAuth, and
the stated reason is POESESSID expiry pain. APT uses neither, deliberately, having rejected OAuth
in 2021 before PKCE was supported.

**Useful:** a working Auth-Code+PKCE-against-GGG implementation already exists in this user's own
`poe_mcp` project (`src/auth/oauth.ts`, ~199 lines — loopback listener, PKCE, refresh, token
store). Port to Python rather than writing fresh.

---

## 7. Stash — measured on the live account

**Standard: 117 tabs, 101 remove-only (86%)** — a dead duplicate of the 16-tab set from every
league ever played. Allflame: 16 tabs, all active.

**818 items across the 16 active Standard tabs.** Largest: div cards 214, fragments 133, premium
125, quad 112, normal 99.

Two observations that reframe the problem:

1. **Density is nothing like worst case.** The 24×24 quad tab holds 112 items — 19% full.
2. **Value lives in stack sizes.** The currency tab's 50 "items" include `Jeweller's Orb ×2615`
   and `Divine Orb ×5`. Any per-item cost model measures the wrong thing.

**~98% of the active stash resolves at tier 1.** Rares are *spatially segregated* into one or two
gear tabs (43 in one, 24 in another). The feared "hundreds of stash rares" case does not exist,
because players sort their stash.

**Remove-only tabs can never gain items** → fetch once, cache forever → steady state 117 tabs
becomes 16, and full refresh ~34 min becomes ~45s. Highest-leverage rule in the feature.

**Legacy items break tier 1.** Standard holds ~170 `Veiled Scarab`, a removed item, absent from
poe.ninja's league index — and zero in Allflame, whose fragment tab resolves to correct current
names. Hence the `unpriceable` verdict; calling these `trash` would understate stash value badly.

**`note` fields are populated** with the user's own asking prices (`~price 2 awakened-sextant`,
`~b/o 25 chaos`), free with every fetch. Enables a price-vs-market diff no Deck-side tool offers.

**Special tabs break the grid model.** Currency/essence/fragment/div-card tabs have bespoke
layouts, and `stackSize` legitimately exceeds `maxStackSize` (`Vaal Orb 163/20`,
`Deafening Essence of Greed 57/10`).

⚠️ **Map tabs returned zero items in both leagues** (three sampled). The MCP layer exposes a
`substash_id` parameter documented for map/folder stashes, which suggests traversal is required
rather than the tabs being empty. This is exactly the failure mode that silently under-reports
value. Unresolved.

### 7.1 Map tabs — as far as this can be taken without OAuth (Phase 10)

**Still zero, and now with a named cause.** Two more map tabs were sampled through
`get-stash-items?tabIndex=N`, including an **active** one (Standard tab 90, `5 [MapStash]`) rather
than only remove-only leftovers. Both returned `items: []`. That is **five map tabs across two
leagues, zero items every time** — no longer plausibly a coincidence of empty tabs.

Three pieces of outside evidence say the zero means *not traversed*:

1. **GGG's documented stash API models a map tab as a parent.** `GET /stash/<league>/<stash_id>
   [/<substash_id>]`; `StashTab` carries `children: StashTab[]`; the list endpoint says it
   "includes sub-tabs and stash tabs in folders", and the substash parameter exists so "the inner
   tab will be wrapped by the parent". A map stash is one of those parents.
2. **A player asking GGG how to read a map stash was told it "needs 1 call per map type"**
   (forum thread 3415304) — one request per child, not one per tab. Same shape.
3. **The legacy endpoint has no equivalent parameter.** `tabIndex` addresses the top-level list
   only, and the tab list gives the map stash exactly one index. There is nothing to traverse
   *with*.

**So: not a mystery, a missing capability.** The traversal path is the OAuth stash API, and OAuth
is blocked on Cloudflare in Steam's CEF browser (SPEC §11). Until that lands, a map tab is
reported **`not supported yet`** — never as `0c`, never counted in a total, and never fetched at
all (there is nothing to fetch). What would settle it definitively is one OAuth `list stashes`
call showing `children` on a map tab; that is a ten-minute check for whoever gets OAuth working,
and it is the only thing left to do here.

*Not* proven: that a map tab's children hold anything on this account. The five samples say the
API will not tell us, not that there are maps in there.

### 7.2 What a full refresh actually costs, against the real tab list (Phase 10)

Computed from the account's own tab list (117 tabs, 10 of them map tabs) and its own published
policy (`backend-item-request-limit`, Account `30:60:60` and `100:1800:600` — §3):

| | requests | wall clock |
|---|---|---|
| Cold crawl, every readable tab | 107 | **~30 min** (the `100:1800` bucket binds) |
| Steady state, remove-only cached | **15** | **~15 s** |
| Opening one tab | 1 | ~1 s |

The ~45 s in the line above is the right order of magnitude and slightly pessimistic; 15 s is what
the buckets give. **Do not compute this as `requests × 18 s`.** The 18 s figure is the *sustained*
rate and it is the wrong tool for a small refresh: thirty requests fit in the first minute, so
multiplying gives 4.5 minutes for a 15-tab refresh that takes fifteen seconds. `estimate_seconds`
models the windows instead.

---

## 8. What the mockup exercise revealed

Drawing both options at true 1280×800 (`ui-mockups.html`) produced findings reasoning alone did
not:

- **300px is tighter than it sounds.** Bag total, tally, 12×5 grid at 22px, ~5 rows. No mod list.
  It has to be a verdict surface.
- **The grid beats a list at narrow width — for the bag.** A green cell sits where the cursor
  must go. It is a map.
- **That justification does not transfer to the stash.** When you are looking at your stash you
  are standing at it, and the game shows it full size. The question becomes "what should I do",
  which is why the stash surface is a digest, not a browser.
- **The deciding factor was the way in, not the size.** One button with the game unobstructed
  beats a multi-step overlay summon.

Caveats on the mockup: prices are illustrative placeholders, and the Yoke of Suffering mod list
is representative rather than fetched. Bag contents, verdict model, and grid positions are real.

Note: the mockup's Option B assumed the web app is summoned via the Steam overlay browser.
`SteamClient.Overlay` turns out to be observe-only, so that entry path was never real. Moot —
the UI is the Decky panel, and the local renderer is now only a dev harness.

---

## 9. poe.ninja — measured, and the spec was half right

**Measured 2026-08-10** by request, against the live site, and cross-checked against
poe.ninja's own published API reference at <https://poe.ninja/docs/api> — which exists,
is linked from the site footer, and nobody in this project had found before.

### 9.1 The routes

SPEC §5.1 said the legacy paths 404 and that current routes look like
`/{poe1|poe2}/api/economy/{exchange|stash}/current/...`. The 404 is confirmed
(`/api/data/currencyoverview` and `/api/data/itemoverview` both return `404` with a
nine-byte `text/plain` body). The shape of the replacement was close but not usable as
written: the real paths end in `/overview`, and the league and category are **query
parameters**, not path segments.

| Purpose | Route |
|---|---|
| Economy leagues | `GET /poe1/api/economy/leagues` |
| Exchange overview | `GET /poe1/api/economy/exchange/current/overview?league={l}&type={t}` |
| Stash item overview | `GET /poe1/api/economy/stash/current/item/overview?league={l}&type={t}` |
| Stash currency overview | `GET /poe1/api/economy/stash/current/currency/overview?league={l}&type={t}` |

`/poe1/api/economy/exchange/current/Standard/Currency` — the spec's implied form —
returns `404` with an empty body. SPEC §5.1 has been corrected.

The league list is `[{id, name}]`, current challenge league first:
`Allflame`, `Hardcore Allflame`, `Standard`, `Hardcore`.

### 9.2 Two shapes, and only one of them agrees with the other

**Exchange overview** — currency-exchange pricing. `lines[]` carry an opaque `id`, a
`primaryValue`, and a volume; names live in a sibling top-level `items[]` array, and
`core.primary` names the unit (`chaos` for PoE 1). Types accepted (all verified 200):
`Currency Fragment Runegraft AllflameEmber Tattoo Omen DjinnCoin Ducat
EnshroudingCrystal DivinationCard Artifact Oil DeliriumOrb Scarab Astrolabe Fossil
Resonator Essence`.

**Stash item overview** — `lines[]` with `name`, `baseType`, `chaosValue`,
`divineValue`, and, where relevant, `links`, `variant`, `corrupted`. Types accepted:
`Wombgift Incubator UniqueWeapon UniqueArmour UniqueAccessory UniqueFlask UniqueJewel
ForbiddenJewel ShrineBelt UniqueTincture UniqueRelic SkillGem ImbuedGem ClusterJewel
Map BlightedMap BlightRavagedMap UniqueMap ValdoMap Invitation Memory IncursionTemple
BaseType Flask Beast Vial`. Note that `DivinationCard` is **not** one of them — cards
moved to the exchange overview, and asking the item overview for them is a 404.

**Stash currency overview** — the legacy shape, `currencyTypeName` +
`chaosEquivalent`. `Currency` and `Fragment` are documented; `Scarab` also answers 200
undocumented.

**They disagree, and the disagreement matters.** Standard, same minute:

| | exchange `primaryValue` | stash currency `chaosEquivalent` |
|---|---|---|
| Divine Orb | 897.7 | 618.2 |
| Exalted Orb | 14.23 | 29.15 |
| Mirror of Kalandra | 1,387,737 | 919,819 |
| Jeweller's Orb | 0.01025 | 0.11 |

Which is canonical is decidable rather than a matter of taste: **every** stash item
line's `chaosValue / divineValue` equals the *exchange* Divine Orb rate. Standard
897.7 (n=531 lines, median ratio 897.7); Allflame 209.0 against an exchange rate of
209.0 and a stash-currency rate of 196.6. So the item overviews are denominated in
exchange chaos, and mixing in the stash currency overview would put two different
chaoses in one total. `prices` uses the exchange overview for everything the exchange
covers and ignores the stash currency overview entirely.

### 9.3 Caching — the spec's number is right, the docs' is not

Live response headers on every overview:

```
cache-control: public, max-age=1800, stale-while-revalidate=300, stale-if-error=86400
etag: W/59c2bf736b708b3d773c5985cdb3375e
```

SPEC §5.1's `max-age=1800` is confirmed. poe.ninja's own docs page says "roughly 5
minutes", which the wire contradicts; the docs also say PoE 1 overviews refresh about
every 15 minutes, which is consistent with a 30-minute TTL being generous rather than
stale. Conditional requests work: `If-None-Match` with the weak ETag returns `304` and
zero bytes. Note the ETag is `W/<hex>` — weak, and **without quotes**, which is not
what RFC 9110 specifies. It has to be echoed back verbatim rather than re-quoted.

### 9.4 What poe.ninja asks for, and where we do not comply

From the docs page, verbatim-ish:

- "Responses are HTTP-cached (roughly 5 minutes, ETag-based). Use conditional requests
  and respect the cache headers; do not bypass caching." — done.
- "Polling faster than a few minutes wastes bandwidth for no fresher data." — done;
  30-minute TTL, prefetch at start, never on a user action.
- "Send a descriptive User-Agent that identifies your app and a contact." — done; the
  same `net` User-Agent GGG requires.
- "Be reasonable with concurrency and volume." — one connection, serialised through
  the limiter, 16 requests per refresh.
- ⚠️ **"Desktop apps and other clients should proxy these requests through their own
  backend rather than calling the endpoints directly from end-user machines."** — we
  do **not** comply, and cannot: PoEDex has no backend, by design. This is a stated
  preference rather than a prohibition ("should"), and the mitigations they give the
  reason for — caching, a proper User-Agent, controlled volume — are all in place. But
  it is a real divergence from what the operator asked for, and if this project ever
  grows a hosted component the tables belong behind it.
- The **builds / profiles API is explicitly closed** to third parties, with AI-assisted
  development named as a reason the request volume has risen. We use none of it. Do not
  add a build-import feature on the back of these endpoints.

There is no stability guarantee: "This API exists to run the poe.ninja website, not as
a product… breaking changes can happen without notice." The 404 that opened this
section is that promise being kept. Expect to re-measure.

### 9.5 Consequences for the pricing model

- **Category is not a lookup key.** Scarabs, fossils, essences, oils, delirium orbs and
  incubators are all `frameType: 5` and all sit under `2DItems/Currency`, so
  `normalize.py` calls all of them `currency`. Routing has to be an *ordered
  preference* with a fall-through to every table, not a dictionary.
- **Maps are indexed by tier, not by name.** poe.ninja lists ordinary maps as
  `Map (Tier 16)` and names only special ones (`Drox Map (Tier 16)`). The item's
  `Map Tier` property is therefore load-bearing, which is why `NormalizedItem` gained a
  `map_tier` field in this phase.
- **`mapTier` and `corrupted` are documented but mostly absent.** No map line in either
  league carries `mapTier`; no unique-weapon line carries `corrupted`. Skill gems do
  carry `corrupted` (4,663 of 7,509 Allflame lines). Both are read when present.
- **One name, many lines.** `Map (Tier 16)` appears 13 times in Standard, once per map
  series, spanning 1c to 898c; `Pillar of the Caged God` appears 6 times (two base
  types × three link counts) spanning 0.96c to 718,160c. Picking wrong is a
  750,000-fold error. `prices` scores on base type, links and corruption, then breaks
  ties on **listing count** — liquidity selects the current map series without anything
  in the code knowing what a map series is.
- **Skill gems are not prefetched.** One 360 kB table, 7,509 lines, and correct pricing
  needs level/quality/corruption matching this phase does not do. An unpriced gem is
  honest; a gem priced as the wrong variant is not.

---

## 10. Official trade API — measured

**Measured 2026-08-10**, anonymous, no credential needed.

`GET /api/trade/data/stats` → `{result: [14 groups]}`
(`pseudo explicit implicit imbued fractured enchant scourge crafted mercenary veiled
delve ultimatum sanctum crucible`), 409 kB, `cache-control: public, max-age=1799`, and
**no `X-Rate-Limit-*` headers at all**. Entries are `{id, text, type}` with ids like
`pseudo.pseudo_total_cold_resistance`. Item mod text has to be normalized to the
document's form (numbers → `#`) before it will match.

`POST /api/trade/search/{league}` → `{id, complexity, result: [hash…], total}`.
Headers, verbatim:

```
x-rate-limit-policy: trade-search-request-limit
x-rate-limit-rules:  Ip
x-rate-limit-ip:     5:10:60,15:60:300,30:300:1800,600:21600:3600
```

`GET /api/trade/fetch/{ids}?query={id}` → `{result: [{id, listing, item}]}`, max 10 ids.

```
x-rate-limit-policy: trade-fetch-request-limit
x-rate-limit-ip:     12:4:10,16:12:300,50:300:300,1000:21600:1800
```

Both match SPEC §5.3 and research-notes §3 exactly, and both are **Ip-ruled only** —
no Account rule, consistent with them needing no credential. Confirms the structural
claim that trade and the item endpoints cannot starve each other.

`listing.account.online` is present only for online sellers, and the search's own
`status: online` filter is not sufficient — sellers go offline between the index and
the fetch, so the filter is applied again after fetching.

⚠️ **Listings carry third-party PII**: `account.name`, `lastCharacterName`, `language`,
and a `whisper` string containing the seller's character name. The recorded fixture is
scrubbed; anything that logs a raw listing would not be.

### 9.6 There is no per-league type index — measured

**Measured 2026-08-10.** Asked directly, because Phase 4b needed to stop hardcoding the type list
and the honest first question is whether poe.ninja will simply tell us.

It will not. Every plausible sibling of `/poe1/api/economy/leagues` returns `404` with an empty
body:

```
/poe1/api/economy/types                    404
/poe1/api/economy/exchange/current/types   404
/poe1/api/economy/exchange/current         404
/poe1/api/economy/exchange/current/index   404
/poe1/api/economy/categories               404
/poe1/api/economy/overview                 404
```

The economy pages are client-rendered Astro islands with the category list inside serialised
props, not in a fetchable document. The one machine-readable index that exists is
**`https://poe.ninja/sitemap.xml`**: 1,139 URLs, of which the `/poe1/economy/{league}/{slug}`
entries give **44 distinct category slugs, identical for every league** (`allflame`,
`allflamehc`, `standard`, `hardcore`). It is an index of *categories*, not of what a league
serves — so it tells you the names and you still have to probe.

Slug → API `type` is derivable: de-pluralise the last word (with `ies → y`) and PascalCase.
That resolves **43 of 44**. The single exception is `temples → IncursionTemple`. This is the part
that matters: a discovery that only validated names already in the code would have confirmed all
twenty-six of the old catalogue and still missed `Ducat`.

**`HEAD` works** on the overview routes and returns `content-length` and `etag` — a cheap way to
size a table before fetching it. Not used: an empty exchange overview is 545 B against 972 B for
a one-line one, and a byte-size threshold is a fragile way to ask a yes/no question when the
`GET` you would make anyway answers it exactly.

**Which types serve data, both leagues, all 44** (lines / bytes):

| | Allflame | Standard |
|---|---|---|
| Empty (0 lines) | `DjinnCoin` `Incubator` `Memory` `ShrineBelt` | `DjinnCoin` `Ducat` `Memory` |
| Largest | `BaseType` 20,165 / 9.4 MB · `SkillGem` 7,508 / 4.0 MB · `ValdoMap` 1,509 / 1.6 MB | `BaseType` 10,879 / 5.0 MB · `SkillGem` 6,951 / 3.6 MB · `ImbuedGem` 5,412 / 2.4 MB |
| Candidate set (38, excluding the six never-fetched) | 36 served, 3.4 MB | 37 served, 4.0 MB |

`Ducat` is 11 lines in Allflame and 0 in Standard, which is exactly the league-specific pattern
`AllflameEmber` and `Runegraft` show — and exactly why a hardcoded list is wrong in both
directions at once.

---

## 11. Bulk exchange — measured

**Measured 2026-08-10**, anonymous, no credential needed.

`POST /api/trade/exchange/{league}`, body
`{"query":{"status":{"option":"online"},"have":["chaos"],"want":[…]},"sort":{"have":"asc"},"engine":"new"}`.

```
x-rate-limit-policy: trade-exchange-request-limit
x-rate-limit-rules:  Ip
x-rate-limit-ip:     5:15:60,10:90:300,30:300:1800
```

Tighter than search, `Ip`-ruled like the other two, and its own policy — so a bag valuation can
use it without touching the account's item budget.

Response: `{id, complexity, result: {hash: {id, item: null, listing}}, total}`. `result` is a
**dict keyed by hash**, not an array, and it carries the full listings inline — there is no
separate fetch step. Each `listing.offers[]` entry is `{exchange: {currency, amount}, item:
{currency, amount, stock, id}}`; the unit price is `exchange.amount / item.amount`, because the
wire says "N chaos for M ducats" and never a unit price.

**Two hard numbers.** The `want` array takes at most **10** ids — eleven is a `400` with
`Too many items \`want\` items selected.` And the response is capped at **100 rows**, sorted by
price ascending **across the whole batch**.

That second cap is the trap, and it was measured rather than reasoned about:

| `want` size | total | returned | Merrick's Ducat offers seen | median of its cheapest 10 |
|---|---|---|---|---|
| 1 | 39 | 39 | 39 (1c–5c+) | **3.0c** |
| 2 | 164 | 100 | 39 | 3.0c |
| 5 | 1,854 | 100 | 2 | 1.0c |
| 10 | 3,917 | 100 | 2 | 1.0c |

A naive ten-id batch prices Merrick's Ducat at a third of its real rate and looks exactly as
confident doing it. The fix is not "do not batch": *a want that received at least N offers from a
globally cheapest-first result set holds precisely its own cheapest N*, so a batch is trusted per
want and only the starved wants are re-queried alone.

The eleven ducats and their live Allflame prices, for reference: Ukatoa's 17.57c, Telesia's 12.4c,
Tzamoto's 7.67c, Brinehook's 6.28c, The Genteel's 4.95c, Katakohi's 4.25c, Cyaxan's 3.93c,
Kishara's 2.83c, Merrick's 1.11c, Rotmother's 0.6966c, The Changeling's 0.1269c — *from
poe.ninja's `Ducat` exchange overview*, which had them all along.

`GET /api/trade/data/static` → `{result: [23 groups]}` (`Currency` 103, `Fragments` 245,
`Ducats` 11, `Cards` 468, …), 195 kB, `cache-control: public, max-age=1800`, **no rate-limit
headers**. Entries are `{id, text, image}`. This is the only place an item *name* maps to a bulk
exchange id, and it is why tier 1b needs it.

⚠️ The response sets a `POESESSID` cookie on an anonymous request. `net` strips `set-cookie` from
every response it returns, so nothing downstream can mistake it for the account's session.

⚠️ Same third-party PII as trade listings: `account.name`, `lastCharacterName`, `whisper`. The
recorded fixture is scrubbed; only numbers survive parsing.
