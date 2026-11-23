# Consumers — smugmug-python

Who installs this library and what they build on top of it. The
no-host-dependencies rule in the root `AGENTS.md` is the flip side of this
section: py_amr may import smugmug; smugmug must never import py_amr.

- `~/SCRIPTS/py_amr` — git dependency `smugmug = { git = "https://github.com/amrutadotorg/smugmug-python.git" }`;
  `services/smugmug/` re-exports the client and adds the MySQL data layer
  (`data.py`) plus orchestration scripts (`scripts/smugmug_sync.py`).
