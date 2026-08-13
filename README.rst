================
smugmug-python
================

A friendly Python client for the **SmugMug API v2** (OAuth 1.0a).

Fork of the SmugMug automation client originally developed for
`amruta.org <https://www.amruta.org>`_ — extracted into a standalone
library, mirroring the `soundcloud-python <https://github.com/amrutadotorg/soundcloud-python>`_
setup used by the amruta automation stack.

This project is **not affiliated with SmugMug Inc.**

Overview
--------

The client handles authentication (OAuth 1.0a), node/album traversal,
image upload (with MD5 dedup), album organization (move/collect), and
idempotent album creation. It is deliberately dependency-light:
``requests``, ``requests-oauthlib`` and ``tenacity``.

Requirements
------------

- Python 3.14+
- requests, requests-oauthlib, tenacity
- pytest (for running tests)

Installation
------------

Until an official PyPI release is published, install directly from GitHub: ::

    pip install git+https://github.com/amrutadotorg/smugmug-python.git

Basic Usage
-----------

::

    from smugmug import SmugMugClient

    with SmugMugClient.from_config(
        api_key="...",
        api_secret="...",
        access_token="...",
        token_secret="...",
    ) as client:
        # Idempotent: folder "2007" + album "2007-05-04 Evening Program ..." under a parent node
        album_uri = client.get_album_else_create(
            "/api/v2/node/BHtXzC",
            "2007/2007-05-04 Evening Program, Cabella Ligure (Italy) #80376",
        )
        client.upload_folder(album_uri, Path("/tmp/photos"))

Running tests
-------------

Unit tests (no API access): ::

    uv run pytest smugmug/tests -m "not integration" -q

Integration tests (live API, requires ``SMUGMUG_API_KEY``, ``SMUGMUG_API_SECRET``,
``SMUGMUG_ACCESS_TOKEN``, ``SMUGMUG_TOKEN_SECRET`` env vars): ::

    SMUGMUG_API_KEY=... uv run pytest smugmug/tests -m integration -q

Rate limits
-----------

The client is proactive about the SmugMug rate limits (windowed,
``X-RateLimit-Remaining`` / ``X-RateLimit-Reset`` headers):

- Every response is inspected; a warning is logged when fewer than
  ``RATE_LIMIT_WARNING_THRESHOLD`` (10) requests remain in the current window.
- HTTP 429 raises :class:`RateLimitError` (a ``SmugMugError`` subclass) and is
  retried up to 5 times, sleeping exactly the ``Retry-After`` value (capped at
  ``RATE_LIMIT_MAX_SLEEP`` = 60 s) between attempts.
- The same handling applies to image uploads (up to 4 attempts) and to
  ``_post``/``_patch``/``_delete`` (previously these had no retry at all).
- After retries are exhausted the ``RateLimitError`` propagates so callers
  can decide how to proceed.
