# Setup

Cortex Core is the device-side memory + interpretive layer. The public repo
ships generic; you supply your own data through gitignored local config, so
nothing personal is ever committed. See `SECURITY.md` for the boundary policy.

## 1. Clone

```
git clone https://github.com/turfptax/cortex-core
cd cortex-core
```

Requires Python 3.11+ (for `tomllib`). See the README for the full install and
run steps for your platform.

## 2. Configure your instance (private, never committed)

All personalization lives in `~/.cortex/`, outside the repo tree:

```
mkdir -p ~/.cortex
cp plugins/overseer/data/cortex.example.toml ~/.cortex/cortex.local.toml
```

Edit `~/.cortex/cortex.local.toml` and fill in YOUR values:

- `[instance]`: `owner_name`, host, paths, optional LAN service URLs.
- `[[sensitivity]]`: cwd-patterns to promote to a tier, so confidential work is
  gist-and-dropped (or never imported), never stored raw.
- `[[category]]`: your own project names, classified `work` / `cortex` / `personal`.
- `[tiers]`: `off_box_max` (default `internal`): the highest tier allowed to
  sync/export off the machine.

API keys go in a separate gitignored file:

```
# ~/.cortex/secrets.toml  (see the secrets template / README for the schema)
```

## 3. Identity (optional)

```
cp memory/core/example.USER.md memory/core/USER.md   # fill in; gitignored
```

## 4. Redaction / privacy posture

With no `[[sensitivity]]` rules you are **fail-closed**: every import
gist-and-drops (structural signal only, never raw content). Add rules to opt
specific non-sensitive paths into keeping raw. This is intentional: an
unconfigured install never stores raw confidential data by accident.
