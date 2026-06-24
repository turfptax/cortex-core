# Contributing

Thanks for your interest in Cortex.

## The one hard rule: no personal data in tracked files

Cortex is built so the public repo stays generic and all personal/instance data
lives in gitignored config. Before you commit, make sure you are NOT adding:

- real names, client/employer names, or private life details (in code, comments,
  docs, prompts, example files, or commit messages),
- secrets / API keys / tokens,
- `cortex.local.toml`, `secrets.toml`, `*.db`, or anything under the gitignored
  `memory/core/` and `dossier/`.

See `SECURITY.md` for the full policy. A pre-commit hook + a CI guardrail enforce
this mechanically. Install them:

```
pip install pre-commit && pre-commit install
```

## Instance-specific values

If you need a configurable value (a path, a rule, a name list), put it in the
`cortex.local.toml` schema plus the `data/cortex.example.toml` template, and load
it via `config_loader`. Never hardcode a real value in tracked source.

## Workflow

- Keep changes focused; match the surrounding code style.
- Run the guardrail before committing: `python scripts/check_no_personal_data.py`.
- Open a PR against `master`.
