# Notes for Claude

## Project

A discord.py-self fork (Discord user-account API wrapper). Goal: stay a drop-in
library with the same public API, while being more efficient (CPU and memory)
than upstream. Changes should be backend-only: never remove or rename public
functionality unless explicitly asked.

## Upstream and refactor policy

- Upstream is dolfies/discord.py-self (`master`). Staying fully in sync is
  not required: periodically fetch it, review what changed, and merge. On
  conflicts **our code wins**, but port in meaningful upstream fixes and
  features. Record the synced commit in `.github/LATEST_UPSTREAM_COMMIT.txt`
  and keep `docs/upstream_syncing.rst` (the divergence map) current.
- Backend refactors are welcome anywhere, including code still identical
  to upstream, when they make it faster, leaner or cleaner. The public API
  (what users call, its signatures and its behaviour) must stay the same.
  New optional parameters are fine; invalid input or unexpected states
  should be logged clearly so issues are diagnosable.

## Working preferences

- When reading through the whole codebase, or delegating code reading/mapping
  to subagents, explicitly run the subagents on the **haiku** model (any
  effort from low to high). Compensate with thorough, specific prompts:
  files to read, exact questions, `file:line` citations, an output format and
  a word limit. This is to keep token usage down.
- Before writing an unfamiliar feature, look at how comparable open-source
  projects solve it and outline the trade-offs before implementing.

## Checks (match CI)

- `ruff format --check && ruff check`
- `pyright discord` with pyright **1.1.394** (CI pin):
  `PYRIGHT_PYTHON_FORCE_VERSION=1.1.394 pyright discord`
- `python -m pytest -q` (about 2.5 minutes)
- Memory benchmark: `python tests/memory_bench.py --members 50000 --top 15`
- After same-size in-place edits (e.g. `==` to `is`), clear `__pycache__`
  before re-running tests: the stale `.pyc` can be reused.
