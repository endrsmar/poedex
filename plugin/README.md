# The plugin directory

This is not the built plugin. `python scripts/build_plugin.py` assembles one in
`dist/poedex/` out of:

| from | to | why |
|---|---|---|
| `plugin/plugin.json` | `plugin.json` | `flags: ["debug"]` — hot reload refuses without it. **No `root`.** |
| `plugin/main.py` | `main.py` | the `Plugin` class Decky imports by path |
| `plugin/package.json` | `package.json` | Decky reads `name`/`version` from here |
| `runtime/ modules/ transports/` | same paths | the project, minus tests and `ui/` |
| pip wheels | `py_modules/` | httpx + pydantic, **built for CPython 3.11** |
| `surfaces/decky/dist/index.js` | `dist/index.js` | the compact-profile bundle |

`plugin.json` deliberately keeps `"debug"`: Decky's hot reload refuses to watch a
plugin without it, and this project cannot be developed against hardware any other
way. It deliberately does **not** carry `root`: plugins run as `deck`, and every path
this tool reads — `Client.txt`, the settings directory, the cache — is readable
without it (SPEC §8).

`main.py` will not run from here. It imports `decky`, which exists only inside the
plugin host, and it expects `py_modules/` beside it. Everything testable lives in
`transports/decky/`, which never imports `decky`.

## `publish` carries no `image`

That field is the Decky store's, and the store is a **non-goal**: it rejects AI-assisted plugins
outright and requires an attestation this project cannot honestly sign (research-notes §5). A URL
pointing at a screenshot nobody has taken would be a dead link in a manifest, so the key is
absent. `tags` and `description` stay because Decky shows them in the plugin list either way.

## Cutting a release

```bash
cd surfaces/decky && pnpm run build && cd ../..
python scripts/build_plugin.py --version 0.2.0
gh release create v0.2.0 dist/poedex.zip --title 'PoEDex 0.2.0' --notes-file - <<'EOF'
Install: Decky -> Settings -> Other -> Install plugin from URL, and paste this zip's URL.
Then run docs/deck-checklist.md.
EOF
```

The zip has one top-level directory named after the plugin, which is the shape Decky's
install-from-URL expects. `--version` sets `package.json` inside the archive; Decky reads it to
decide whether an installed plugin is out of date.
