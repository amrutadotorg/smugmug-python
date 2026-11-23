# AGENTS.md — smugmug-python

Standalone Python 3.14+ client library for the SmugMug API v2 (OAuth1) — upload with MD5 dedup, idempotent album creation, async image moves — fork of the SmugMug automation client used by amruta.org, installed into `~/SCRIPTS/py_amr` as a git dependency; **uv**, ruff, pyright.

## Commands used on nearly every task

| Task | Command |
|---|---|
| Install | `uv sync --dev` |
| Unit tests (no API; what CI runs) | `uv run pytest smugmug/tests -m "not integration" -q` |
| Lint · format | `uv run ruff check .` · `uv run ruff format .` |
| Type check | `uv run pyright smugmug` |

## Rules that apply to every task

- **No host dependencies**: the client must not import from py_amr
  (`core.config`, `core.logging`). Credentials are passed to
  `SmugMugClient.from_config(...)` explicitly; logging uses the stdlib
  `logging` module (logger name `smugmug`).

## Read on demand

| Document | Reach for it when |
|---|---|
| [repo-map](docs/agents/repo-map.md) | Orienting: where the client and each test file live, package metadata, what CI runs on push |
| [api-behavior](docs/agents/api-behavior.md) | Touching albums, nodes, uploads or polling: split-before-sanitize rule, TOCTOU accepted risk, dedup primitives, async job semantics |
| [consumers](docs/agents/consumers.md) | Changing the public surface: how `~/SCRIPTS/py_amr` installs and wraps this library |
