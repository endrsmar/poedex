# PoEDex

Path of Exile inventory price-checker for the Steam Deck — built for **gaming mode**, not just
desktop mode.

Finish a map, portal to your hideout, press the `…` button. Every inventory slot is marked
**keep / check / trash** with a value estimate and a bag total. No keyboard, no alt-tab, no
leaving gaming mode.

> **Status: backend working, web surface working, Decky plugin built and never run on a Deck.**
> The module runtime, the rate-limited API client, the log tailer, the pricing engine and the
> verdict engine are in place; `poedex serve` puts the priced bag on `http://127.0.0.1:7331`; and
> `scripts/build_plugin.py` now produces an installable Decky zip in which the same
> `BagScreen.tsx` renders at 300 px through `@decky/ui`. **Nobody on this project has a Steam
> Deck**, so the panel's geometry, its D-pad navigation, suspend/resume and the LAN pairing flow
> are written and unverified — [`docs/deck-checklist.md`](docs/deck-checklist.md) is the
> ten-minute list that closes them, and it is the first thing to run with hardware in hand. See
> [`docs/SPEC.md`](docs/SPEC.md) for the design,
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

### Working on the Deck panel without a Deck

```bash
pnpm run deck        # http://127.0.0.1:5174 — arrows are the D-pad, Enter is A, Esc is B
```

The **Deck preview** renders the real QAM panel — the same `Panel.tsx` the plugin mounts, the
same `compact` profile, the same module screens — at the Deck's real geometry: a 1280 × 800
device-pixel frame, a 300 CSS px column, gaming mode's 1.5× scale. Focus is resolved
**geometrically against live element rects**, the way Steam's is, rather than in DOM order —
which matters because the bag grid is drawn in verdict order and laid out in slot order, so
those two disagree about nearly every cell. It talks to `poedex serve` when one is running and
falls back to the committed fixtures when it is not, saying which on screen.

It is a *simulation of* `@decky/ui`, not `@decky/ui`: Steam's components are the same five-line
stand-ins the test suite uses, and the focus system is a model of Steam's. So it **retires
nothing** from [`docs/deck-checklist.md`](docs/deck-checklist.md) — it makes four of the eleven
items cheap to get mostly right before a build-copy-install-restart cycle, and that document
now says per item which half it covers. It is a dev tool and never ships: the plugin zip is
four Python packages and one JavaScript file, and a test asserts this is neither.

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
| [`docs/deck-checklist.md`](docs/deck-checklist.md) | The ten things that need a Steam Deck, what a pass looks like, and what each failure means |
| [`CLAUDE.md`](CLAUDE.md) | Orientation for contributors and coding agents |

## Repository layout

```
runtime/            registry, context, events, storage, settings, methods
modules/<id>/       one directory per module: backend/ (Python) + ui/ (TypeScript) + tests/
ui-kit/             @poedex/ui — primitives, one implementation per surface profile
frontend/core/      @poedex/core — transports and stores. Framework-agnostic, no React
transports/http/    FastAPI on 127.0.0.1, SSE, and the built SPA
transports/decky/   the plugin backend: the registry, decky.emit, a shutdown on a deadline
surfaces/web/       the browser shell; discovers module UIs and mounts them
surfaces/decky/     the QAM panel: a screen stack, B to go back
surfaces/deck-preview/  a dev tool: that panel, at Deck geometry, in a browser. Never shipped
plugin/             plugin.json + the Plugin class Decky loads. Assembled by scripts/build_plugin.py
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
| M3 | Decky plugin shell: bag grid, D-pad navigation, push updates — **built, unverified on hardware** | yes |
| M4 | LAN pairing — credential entry with zero characters typed on the Deck — **built, unverified end to end** | yes |
| M5 | Stash digest: what's it worth, what should I sell | yes |
| M6 | Rare pricing: highlight the interesting ones, then an on-demand trade query the player composes | yes |
| M7 | OAuth, once GGG reopens developer registration | yes |

## Installing the Decky plugin

**Sideload from a GitHub Release using Decky's install-from-URL.** This project is deliberately
not distributed through the Decky plugin store, which does not accept AI-assisted plugins — it
requires an attestation that generative AI was not used to write a majority of the submission,
and that is not true here. Install-from-URL is a first-class, permanent path, so this costs
nothing but the store listing.

On the Deck: **Decky → the plug icon → Settings → Other → Install plugin from URL**, and paste
the `.zip` URL from the [Releases](../../releases) page. Then work through
[`docs/deck-checklist.md`](docs/deck-checklist.md).

### Building the zip

```bash
cd surfaces/decky && pnpm install && pnpm run build && cd ../..
python3 scripts/build_plugin.py                  # dist/poedex/ and dist/poedex.zip (~3.4 MB)
```

Two things about that build are worth knowing before it surprises you:

- **It needs network access, once.** `py_modules/` is filled by `pip download`, because a Decky
  plugin installs from a zip with no pip at the far end.
- **It targets CPython 3.11, and that is not the Deck's Python.** A plugin backend is a fork of
  the Decky Loader process, which is frozen against 3.11.7; SteamOS itself ships 3.13 from 3.7
  onward. `pydantic-core` is a compiled Rust extension and publishes **no `abi3` wheel**, so its
  `.so` is pinned to one CPython minor and the build refuses to ship the wrong one. If Decky
  Loader ever reships against another version, change `TARGET_PYTHON` in
  `scripts/build_plugin.py` — that is the whole fix, and item 2 of the checklist is how you find
  out you need it.

### Developing against hardware

```bash
python3 scripts/build_plugin.py --no-zip --no-vendor
rsync -a --delete dist/poedex/ deck@<ip>:~/homebrew/plugins/PoEDex/
```

`plugin.json` keeps Decky's `debug` flag, so the plugin reloads when the directory changes. It
never carries `root`: plugins run as `deck`, and every path this tool reads is readable that way.

## Path of Exile 2

Not supported, and not a matter of effort. GGG removed unequipped inventory items from the PoE2
character endpoint in 3.27.0, so the data this tool depends on does not exist there. PoE2 tools
have responded by reading game memory, which this project rules out.

## License

Apache 2.0 — see [LICENSE](LICENSE).
