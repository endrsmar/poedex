# PoEDex

Path of Exile inventory price-checker for the Steam Deck — built for **gaming mode**, not just
desktop mode.

Finish a map, portal to your hideout, press the `…` button. Every inventory slot is marked
**keep / check / trash** with a value estimate and a bag total. No keyboard, no alt-tab, no
leaving gaming mode.

> **Status: backend working, web surface working, no Deck panel yet.** The module runtime, the
> rate-limited API client, the log tailer, the pricing engine and the verdict engine are in
> place, and `poedex serve` puts the priced bag on `http://127.0.0.1:7331` with verdicts, price
> provenance, an honest total and a working refresh. The Decky panel is next: the UI kit already
> has two surface profiles, and the same `BagScreen.tsx` is what will render at 300 px. Nothing
> has run on a Deck, or against the live API. See [`docs/SPEC.md`](docs/SPEC.md) for the design,
> [`docs/IMPLEMENTATION-PLAN.md`](docs/IMPLEMENTATION-PLAN.md) for the phases, and
> [`docs/research-notes.md`](docs/research-notes.md) for the evidence behind them.

```bash
python3 -m venv .venv && .venv/bin/pip install -e '.[dev,web]'   # 3.11+
pnpm install && pnpm build                                       # the web surface
poedex auth set                                                  # a POESESSID, from a hidden prompt
poedex config set net.contact you@example.com                    # GGG asks for one in the User-Agent
poedex serve                                                     # http://127.0.0.1:7331
```

`poedex config list` prints every setting, its value and whether that value is stored or the
default; `config get|set|unset <module>.<key>` is the rest of it. The POESESSID is not a setting
and is not reachable from any of them.

`serve` binds to the loopback interface and refuses anything else — it reads your account's
inventory, and `0.0.0.0` would put that on whatever network you are attached to.

## How it works

Unlike desktop tools such as Awakened PoE Trade, this never interacts with the game client — no
clipboard scraping, no keystroke injection, no overlay fighting the compositor. It has two
passive inputs:

- **The official Path of Exile API** over HTTPS, for character inventory and stash contents.
- **`Client.txt`**, the log the game writes to the Linux filesystem, tailed read-only to detect
  zone changes.

That is what makes it work under gamescope in gaming mode, and it keeps the tool within GGG's
terms — GGG's policy explicitly permits tools that run entirely outside the game and read its log
files.

The UI is a **Decky Loader plugin** living in the Quick Access Menu: one button, D-pad
navigation, game keeps running. See [`docs/ui-mockups.html`](docs/ui-mockups.html) for mockups at
true Deck proportions.

## Design constraints worth knowing

Three measured facts shape everything:

- **Inventory updates on zone transition, not live.** Confirmed by polling a live mapping
  character's gem XP — nineteen flat samples, one discrete commit. So the tool is event-driven
  off zone changes rather than polling, and it cannot do real-time drop highlighting. Loot
  filters still own that.
- **The API allows one item request per 18 seconds, sustained.** Syncing on zone entry uses ~12%
  of that budget while being *more* current than aggressive polling.
- **The Quick Access panel is 300 CSS px wide.** It is a verdict surface, not a browser. Detail
  lives behind focus, not in a side pane.

## Documentation

| Document | Contents |
|---|---|
| [`docs/SPEC.md`](docs/SPEC.md) | Architecture, auth, pricing tiers, data model, milestones |
| [`docs/research-notes.md`](docs/research-notes.md) | Measurements, sources, and why alternatives were rejected |
| [`docs/ui-mockups.html`](docs/ui-mockups.html) | Visual mockups |
| [`CLAUDE.md`](CLAUDE.md) | Orientation for contributors and coding agents |

## Repository layout

```
runtime/            registry, context, events, storage, settings, methods
modules/<id>/       one directory per module: backend/ (Python) + ui/ (TypeScript) + tests/
ui-kit/             @poedex/ui — primitives, one implementation per surface profile
frontend/core/      @poedex/core — transports and stores. Framework-agnostic, no React
transports/http/    FastAPI on 127.0.0.1, SSE, and the built SPA
surfaces/web/       the browser shell; discovers module UIs and mounts them
```

A module is a vertical slice: its backend logic **and its own screens** live in one directory.
Module UI is written once against `@poedex/ui` and reshaped by surface profiles — `compact`
(300 px, gamepad) and `full` (browser) — so one `BagScreen.tsx` renders on both. Both boundaries
are enforced by tests rather than by discipline: `tests/test_boundaries.py` walks the Python AST,
and `eslint-plugin-poedex` checks what a module's UI may import.

## Roadmap

| | Milestone | Deck needed |
|---|---|---|
| M1 | Data layer: log tailer, HTTPS client, rate limiter, item model | no |
| M2 | Bulk pricing and the keep/check/trash/unpriceable verdict engine | no |
| M2b | Web surface on localhost: the bag screen, the UI kit, the HTTP transport | no |
| M3 | Decky plugin shell: bag grid, D-pad navigation, push updates | yes |
| M4 | LAN pairing — credential entry with zero characters typed on the Deck | yes |
| M5 | Stash digest: what's it worth, what should I sell | yes |
| M6 | Rare pricing: highlight the interesting ones, then an on-demand trade query the player composes | yes |
| M7 | OAuth, once GGG reopens developer registration | yes |

## Installation

Sideload from a GitHub Release using Decky's install-from-URL. This project is not distributed
through the Decky plugin store, which does not accept AI-assisted plugins.

## Path of Exile 2

Not supported, and not a matter of effort. GGG removed unequipped inventory items from the PoE2
character endpoint in 3.27.0, so the data this tool depends on does not exist there. PoE2 tools
have responded by reading game memory, which this project rules out.

## License

Apache 2.0 — see [LICENSE](LICENSE).
