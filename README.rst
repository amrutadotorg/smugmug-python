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
