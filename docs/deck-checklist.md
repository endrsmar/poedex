# The ten-minute Deck checklist

**Nobody who built this has a Steam Deck.** Everything below is a claim the code
makes that no test on a laptop can check. Each item says what to do, what a pass
looks like, and — more usefully — **what the failure looks like**, because most of
these fail by looking slightly wrong rather than by throwing.

Work down the list. Items 1–5 need only the plugin installed; 6–8 need a PC on the
same network; 9–10 need the game and the lid.

**Nothing here is optional maintenance.** These are the ten things Phase 7 shipped
untested, and until they are ticked the phase is "written" rather than "working".

---

## Before you start

```bash
# on the dev machine
cd surfaces/decky && pnpm run build && cd ../..
python3.12 scripts/build_plugin.py           # dist/poedex.zip
```

Install it either way:

- **From a Release:** Decky → the plug icon → *Settings* → *Other* → *Install plugin
  from URL*, paste the `.zip` URL from the GitHub Release.
- **Over SSH, for iteration:** `scripts/build_plugin.py --no-zip --no-vendor`, then
  `rsync -a --delete dist/poedex/ deck@<ip>:~/homebrew/plugins/PoEDex/`. The manifest
  keeps the `debug` flag, so Decky watches the directory and reloads.

Watch the log the whole way through: `journalctl -u plugin_loader -f`, or
`tail -f ~/homebrew/logs/PoEDex/plugin.log`.

---

## 1 · The backend starts, and stays started

**Do:** open the QAM, find PoEDex, open the panel. Then close the QAM, launch a game,
quit it, and open the panel again.

**Pass:** the panel opens already populated the second time — no spinner. The log
shows `poedex backend up: N method(s)` exactly **once**.

**Fail:** a second `backend up` line means the process is being restarted (Decky is
not meant to do that, and if it does, every cache and the rate-limit budget are being
thrown away with it). A panel that spins on every open means `decky.emit` is not
reaching the frontend — check item 4 before anything else.

**If it does not start at all:** the panel says so, with the reason, because
`_main()` emits `backend.broken` rather than raising. The likeliest reason by a wide
margin is **item 2**.

## 2 · `py_modules/` matches the loader's Python

**This is the single most likely thing to be wrong**, and it is wrong silently.

**Do:** read the panel. If it says
`py_modules/ is built for CPython 3.11` — or the log has
`ModuleNotFoundError: pydantic_core` — this is it.

**Why:** `pydantic-core` is a compiled Rust extension and publishes **no `abi3`
wheel**, so its `.so` is pinned to one CPython minor version. The plugin process runs
the **frozen Decky Loader's own CPython 3.11**, *not* SteamOS's `python3` (which is
3.13 from SteamOS 3.7). `scripts/build_plugin.py` targets 3.11 and refuses to build
anything else, so the only way to get here is a Decky Loader that has moved.

**Check it directly:** in the panel's log, or over SSH:

```bash
python3 -c "print('system python:', __import__('sys').version)"      # probably 3.13
grep -o 'cpython-3[0-9]*' ~/homebrew/plugins/PoEDex/py_modules/pydantic_core/*.so
```

The second number is what we shipped. If Decky's is no longer 3.11, change
`TARGET_PYTHON` in `scripts/build_plugin.py` and rebuild — **that is the whole fix**,
but it is not automatic and nothing will tell you it is needed except this.

## 3 · Real geometry: 300 px, and the grid actually fits

**Do:** open the bag screen with something in the backpack. Look at the grid.

**Pass:** twelve columns across, five rows, cells roughly square, no horizontal
scrollbar anywhere on the panel.

**Fail, and what it means:**

- **The grid sticks out past the panel, or is clipped on one side.** `ItemGrid`
  applies `margin: 0 -16px` to escape the `PanelSection` inset, derived from SPEC
  §6.1's two measurements (300 px column, 268 px inside a section → 16 px a side).
  If the real inset is not 16, change `PANEL_INSET` in
  `ui-kit/src/profiles/compact/index.tsx`. That constant is the whole fix.
- **The cells are noticeably smaller than 24 px.** Same constant, other direction.
- **Anything scrolls sideways.** Report which screen; that is a layout bug, not a
  constant.

## 4 · D-pad navigation, against real `@decky/ui`

**Do:** with the panel open, press the D-pad **left, right, up, down** across the bag
grid. Then down into the item rows. Then onto a checkbox list (open a highlighted
rare and press **Check price**).

**Pass:** focus moves geometrically — right goes right, down goes down, empty slots
are skipped. **The detail line under the grid changes as focus moves**, with no press.
The focus ring is a thin outline that does not visually merge two adjacent cells.
**A** ticks a checkbox row.

**Fail, and what it means:**

- **Focus does not move at all, or jumps to the wrong place.** This is the claim
  research-notes §5 called "free", verified against a shipping plugin but not by us.
- **The detail line does not follow focus.** `onGamepadFocus` is not firing;
  everything else still works, but the panel now needs a press per item, which is the
  design failing rather than a bug.
- **Adjacent cells look like one blob when focused.** `noFocusRing` is not being
  honoured. The inline outline is drawn from focus state as well, so you may be
  seeing both.
- **The panel is blank / white.** Almost certainly `@decky/ui` handed back
  `undefined` for a component after a Steam update. The panel is built to say so —
  look for **"Steam components missing"** at the top, which names them. If the panel
  is blank with *no* message, the guard itself is broken and that is a real bug worth
  reporting.

## 5 · **B** goes back, and does not trap you

**Do:** from the bag, press the **Pair** control to push that screen. Press **B**.
Then press **B** again at the root.

**Pass:** the first **B** returns to the bag. The second **B** closes the QAM, the way
it does in every other plugin.

**Fail:** if **B** closes the QAM from the pushed screen, `onCancel` is not reaching
the panel's root `Focusable`. If **B** never closes the QAM, the shell is swallowing
it at the root — worse, because there is no other way out.

## 6 · Pairing: the address is right

**Do:** open **Pair**, press **Start pairing**.

**Pass:** the panel shows `http://192.168.x.x:7332` (your Deck's LAN address) and six
digits, both large, with a countdown.

**Fail:** `no network address` means `local_addresses()` found nothing private — the
Deck is on no network, or its hostname does not resolve to its LAN address and the
UDP-route fallback also failed. If the address shown is `127.0.0.1`, that is a bug:
loopback is filtered out deliberately, because typing it into a PC browser reaches
the PC.

## 7 · Pairing: the credential actually lands

**Do:** on the PC, open that URL. Type the six digits, paste the POESESSID from
`pathofexile.com`'s cookies, submit.

**Pass:** the page says **"Paired."**; the panel switches off the pairing screen
within a second; the bag screen starts working.

```bash
ls -l ~/.local/share/decky/settings/PoEDex/session.json   # or $DECKY_PLUGIN_SETTINGS_DIR
# -rw------- , i.e. 0600
grep -c 'POESESSID\|<the value>' ~/homebrew/logs/PoEDex/plugin.log    # must be 0
```

**Fail:** a page that does not load at all means the port is firewalled or the
address is wrong (item 6). A page that loads and then says *"This pairing window has
closed"* means the three-minute timeout ran out — start another one.

## 8 · Pairing: it refuses what it should

Three quick negatives, all from the PC:

1. **Wrong code.** Type six wrong digits. Expect *"That code is not right"* and a
   count of attempts left. Do it three times: the window must close and the panel
   must say *"Too many wrong codes"*.
2. **The socket really is gone.** After a successful pair, reload the page. Expect a
   connection error, not a form.
3. **Nothing is left listening.** `ss -ltn | grep 7332` on the Deck, between pairing
   windows — must print nothing.

**Fail on any of these is a security bug**, not a polish item: this is a
full-account credential intake on a network.

## 9 · Suspend and resume

**Do:** with the panel having synced at least once, close the lid. Wait **five
minutes at least** — the threshold is 90 seconds, but a short suspend may not sleep
deeply. Open the lid, open the panel.

**Pass:** the log has `resumed after about Ns away`, and the bag re-syncs rather than
showing a price from before the nap.

**Fail:** nothing in Decky Loader handles suspend/resume, and the mechanism here —
comparing `time.monotonic()` against `time.time()`, because `CLOCK_MONOTONIC` stops
during suspend and the wall clock does not — is **entirely unverified on hardware**.
If no `resumed` line appears, that assumption is wrong for SteamOS and the panel is
quietly serving stale prices after every nap. That is the most consequential unknown
in this list after item 2.

## 10 · Unload finishes before the kill

**Do:** disable the plugin in Decky's list. Watch the log.

**Pass:** the process goes away quietly. If a module was slow, you see
`shutdown gave up after 3s` — which is the intended outcome, not a fault: Decky
SIGKILLs at 5 s and every write this project makes is atomic.

**Fail:** a SIGKILL with no `shutdown` line at all means `_unload` never ran. Check
that `ss -ltn | grep 7332` is empty afterwards — an orphaned pairing socket is the
one leak that matters.

---

## Also worth doing once, while you have the hardware

These are open from earlier phases and the Deck is where they get closed:

- `poedex selftest freshness` — needs someone in the game (research-notes §2.1).
- `poedex gamelog status` and `poedex gamelog watch` through one portal-to-hideout
  and one map entry (CLAUDE.md).
- `poedex appraise` **against a real backpack after a real map**. Phase 4's
  validation question — *does this tell the player something they did not already
  know?* — has still never been answered against real loot.

On the Deck the CLI runs out of the installed plugin:

```bash
cd ~/homebrew/plugins/PoEDex && PYTHONPATH=.:py_modules python3 -m cli.main appraise
```

...if the system `python3` is 3.11+. On SteamOS 3.7 it is 3.13, which runs the pure
Python fine but **cannot load the vendored `pydantic_core`** — see item 2. Use a
`pip install -e .` checkout in desktop mode instead.
