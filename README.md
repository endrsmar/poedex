# PoEDex

Path of Exile inventory price-checker for the Steam Deck — built for **gaming mode**, not just
desktop mode.

Finish a map, portal to your hideout, press the `…` button. Every inventory slot is marked
**keep / check / trash** with a value estimate and a bag total. No keyboard, no alt-tab, no
leaving gaming mode.

> **Status: specification (v0.2). No code yet.**
> See [`docs/SPEC.md`](docs/SPEC.md) for the design and [`docs/research-notes.md`](docs/research-notes.md)
> for the evidence behind it.

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

## Roadmap

| | Milestone | Deck needed |
|---|---|---|
| M1 | Data layer: log tailer, HTTPS client, rate limiter, item model | no |
| M2 | Bulk pricing and the keep/check/trash/unpriceable verdict engine | no |
| M3 | Decky plugin shell: bag grid, D-pad navigation, push updates | yes |
| M4 | LAN pairing — credential entry with zero characters typed on the Deck | yes |
| M5 | Stash digest: what's it worth, what should I sell | yes |
| M6 | Rare pricing: heuristic gate plus on-demand trade queries | yes |
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
