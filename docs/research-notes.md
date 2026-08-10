# Research notes

Evidence behind [`SPEC.md`](SPEC.md), kept so the reasoning does not have to be rediscovered and
so rejected approaches stay rejected for stated reasons.

Sessions: feasibility 2026-08-09; five parallel research streams 2026-08-09/10.

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
