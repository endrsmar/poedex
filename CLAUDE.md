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

**Phase 1 done.** `runtime/` (registry, context, events, storage, settings, methods, redacting
log), `modules/credentials/backend/`, a `poedex` CLI, and the boundary tests. Next action is
**Phase 2** (`net` and `poeapi`).

```bash
python3.11 -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/pytest        # everything offline
.venv/bin/ruff check .
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
  and anonymous on the same endpoint.
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

SPEC §11. The two worth raising early: the **keep threshold default** (~20c gives a busy panel,
divine-tier a quiet one) and which **league** is primary.
