# Security & Privacy Policy

Cortex is a personal AI memory system. It is built so the **public repository
stays generic** and **all personal/private data lives outside version control**.

## The boundary rule (non-negotiable)

**No real personal data in tracked files.** That includes code, comments, docs,
prompts, example files, READMEs, commit messages, and this file. The 2026-06
incident that motivated this policy happened because real client names and
personal facts were baked into tracked code and docs; the gitignored files never
leaked. The fix is to keep every personal value in the gitignored zone below.

Personal / instance-specific values live ONLY in:

- `~/.cortex/cortex.local.toml` (gitignored): your sensitivity + category rules,
  instance settings, and ingest lists. Loaded at runtime by `config_loader`.
- `~/.cortex/secrets.toml` (gitignored): API keys.
- `memory/core/USER.md`, `OVERSEER.md`, `APP.md`, and `dossier/` (gitignored):
  your identity and context.
- The local databases under `plugins/overseer/data/` (gitignored).

The repo ships `*.example` templates for these. Copy them, fill in your own
values locally, and they will never be committed.

## Fail-closed by default

With no sensitivity config, imports **gist-and-drop**: capture structural signal,
never store raw content. You opt IN to keeping raw by adding your own rules. The
default off-box ceiling is `internal`, so confidential/restricted data never
leaves the machine unless you explicitly raise it.

## If you find exposed data

- As a user: open a private GitHub security advisory; do not file a public issue.
- As a maintainer of your own fork: scrub it from history with `git filter-repo`
  and **rotate any exposed credential**. A history rewrite does NOT un-compromise
  a key that was ever committed; treat it as compromised and rotate at the
  provider.

## Reporting

Report security or privacy issues via a private GitHub security advisory on this
repository, not a public issue.
