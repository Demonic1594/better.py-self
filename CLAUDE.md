# Notes for Claude

## Project

A discord.py-self fork (Discord user-account API wrapper). Goal: stay a drop-in
library with the same public API, while being more efficient (CPU and memory)
than upstream. Changes should be backend-only: never remove or rename public
functionality unless explicitly asked.

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
