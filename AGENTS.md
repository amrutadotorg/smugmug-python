# Project Context & Rules for AI Agents

## Overview

Standalone Python client library for the SmugMug API v2 — fork of the
SmugMug automation client used by amruta.org, following the same pattern
as `~/SCRIPTS/soundcloud-python` (standalone repo, installed into
`~/SCRIPTS/py_amr` as a git dependency).

## Structure

```
├── smugmug/
│   ├── __init__.py     # Re-exports SmugMugClient, SmugMugError, __version__
│   ├── client.py       # OAuth1 API v2 client (upload, dedup, albums, nodes)
│   └── tests/
│       ├── test_paths.py       # Unit tests — path splitting/sanitization (no API)
│       ├── test_rate_limits.py # Unit tests — retry, rate limits, async jobs, upload (no API)
│       └── test_integration.py # Live API tests (marker: integration)
├── pyproject.toml      # hatchling, name "smugmug"
└── README.rst
```

## Rules

- **No host dependencies**: the client must not import from py_amr
  (`core.config`, `core.logging`). Credentials are passed to
  `SmugMugClient.from_config(...)` explicitly; logging uses the stdlib
  `logging` module (logger name `smugmug`).
- **Idempotent album creation**: `get_album_else_create` splits the
  `"parent/album"` path BEFORE sanitizing each segment — never sanitize
  the whole path first (that collapses the hierarchy). Accepted risk:
  `get_or_create_node` is list-then-create (TOCTOU); a 409 race is
  resolved by re-listing, but a duplicate name that returns 200 instead
  of 409 would create a second node.
- **Dedup**: `upload(..., dedup=True)` and `get_album_image_hashes`
  (ArchivedMD5) are the dedup primitives; `upload_new_only` is a thin
  wrapper; `move_image_uris` / `collect_image_uris` move or collect specific
  images (idempotent flows).
- Async job endpoints (`moveimages`/`collectimages`) may take > 2 minutes
  server-side; `_poll_async_job` polls up to 120 s and returns `bool` —
  `move_images`/`collect_images` return `False` when a job fails or times
  out (they do not silently report success).

## Commands

```bash
uv sync --dev              # install
uv run pytest smugmug/tests -m "not integration" -q   # unit tests (CI)
uv run ruff check . && uv run ruff format .           # lint/format
uv run pyright smugmug                                 # type check
```

## CI

GitHub Actions runs lint + pyright + unit tests on push (`.github/workflows/tests.yml`).

## Consumers

- `~/SCRIPTS/py_amr` — git dependency `smugmug = { git = "https://github.com/amrutadotorg/smugmug-python.git" }`;
  `services/smugmug/` re-exports the client and adds the MySQL data layer
  (`data.py`) plus orchestration scripts (`scripts/smugmug_sync.py`).
