# Cortex Plugins

This directory holds plugins discovered at boot by `src/plugins_runtime.py`.

Each plugin is a self-contained subdirectory:

```
plugins/
└── <name>/
    ├── plugin.toml       # metadata + capabilities (required)
    ├── __init__.py       # def register(api) -> Plugin (required)
    ├── data/             # plugin-owned SQLite + state
    ├── assets/           # plugin-owned media
    └── tests/
```

See `../../notes/plugin-interface-sketch.md` for the full v0 design.

Discovery is filesystem-driven: a plugin is loaded if and only if its
directory contains a `plugin.toml` with `enabled = true`. Disable a
plugin by flipping that flag or removing the folder.

v0 status: discovery scans and logs found plugins. Actual loading and
API wiring land in the next extraction slice when the first plugin
(pet) arrives.
