# Repo map — smugmug-python

Standalone repo following the same pattern as `~/SCRIPTS/soundcloud-python`
(standalone repo, installed into `~/SCRIPTS/py_amr` as a git dependency).

## Structure

```
├── smugmug/
│   ├── __init__.py     # Re-exports SmugMugClient, SmugMugError, RateLimitError, __version__
│   ├── client.py       # OAuth1 API v2 client (upload, dedup, albums, nodes)
│   └── tests/
│       ├── test_paths.py       # Unit tests — path splitting/sanitization (no API)
│       ├── test_rate_limits.py # Unit tests — retry, rate limits, async jobs, upload (no API)
│       └── test_integration.py # Live API tests (marker: integration)
├── pyproject.toml      # hatchling, name "smugmug", requires-python >= 3.14
└── README.rst
```

## CI

GitHub Actions runs lint (`ruff check .`) + pyright + unit tests on push and
pull requests (`.github/workflows/tests.yml`).
