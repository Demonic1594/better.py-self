# Development

How to work on better.py-self itself. [`.github/CONTRIBUTING.md`](../.github/CONTRIBUTING.md)
has the contribution rules.

## Setup

```sh
git clone https://github.com/Demonic1594/better.py-self
cd better.py-self
python3 -m pip install -U -e ".[voice,speed]"
python3 -m pip install -U pytest pytest-asyncio "ruff==0.12.*" pyright sphinx
```

## Checks (these match CI)

```sh
ruff format --check && ruff check                        # CI pins ruff 0.12
PYRIGHT_PYTHON_FORCE_VERSION=1.1.394 pyright discord     # 0 errors required
python -m pytest -q                                      # ~644 tests, about 2.5-3 minutes
cd docs && sphinx-build -b html -n -T -W --keep-going . _build   # warnings are errors
```

CI ([`.github/workflows`](../.github/workflows)) runs pytest on Python **3.10
through 3.14**, with and without the `[speed]` extra. Without the extra,
orjson isn't installed, so every JSON path also runs on the stdlib `json`
module. A `dists-and-docs` job builds the wheel, the sdist and the Sphinx docs.

After editing a file in place without changing its size (for example `==` to
`is`), delete `__pycache__` before running the tests again, or Python may reuse
the stale `.pyc`.

### Running the tests with stdlib JSON

```sh
python -c "import json,sys,discord.utils as u; u._from_json=json.loads; u._to_json=lambda o: json.dumps(o,separators=(',',':')); import pytest; sys.exit(pytest.main(['-q']))"
```

## Test harnesses

Tests must stay **offline**: no network, and no real tokens, webhook URLs or
user IDs. Shared helpers, which test modules import from each other on purpose:

- **`tests/test_persistence.py`:** `drive_ready`, `ready_payload` and
  `guild_payload` drive a client through a synthetic READY.
- **`tests/test_rate_limit_behavior.py`:** `FakeResponse` and `make_http` for the
  HTTP client.
- **`tests/test_abuse.py`:** `make_env` builds a client, a state, a guild and an
  HTTP client with stubbed endpoints.

A bug fix's regression test must check the fixed behaviour, not just that no
exception is raised, and it must **fail on the old code**.

## Benchmarks

- **`python tests/memory_bench.py --members 50000 --top 15`** builds 100 guilds,
  6,000 channels and 50,000 members and reports retained memory. The baseline at
  v2.3.2 is **45.27 MiB**. Run it before and after any change to models, caches
  or parsers.
- **`python tools/stress_bench.py`** (moderate) or **`--heavy`** pushes
  pre-serialised gateway frames through the real receive, parse and dispatch path,
  offline. It has six phases: throughput, soak, backlog, multi (10 or 40
  accounts), persist and bomb (hostile frames). It exits non-zero when a gate
  fails. The baseline is in [`docs/cache_tuning.rst`](../docs/cache_tuning.rst).
  - Known: the **soak** phase's RSS-plateau gate fails by about +10.8 MiB. It fails
    the same way on code from before the audits, and the count of live objects stays
    flat, so this is allocator behaviour, not a leak. The
    [roadmap](../docs/roadmap.rst) lists it.
- **`tools/profile_live.py`** profiles a live account's real traffic.

## Rules for changes

The same rules are written in `CLAUDE.md` and the CodeRabbit configuration:

- **Public API.** Nothing public is removed or renamed, and new parameters are
  optional. A behaviour upstream removes stays here with a `DeprecationWarning`.
- **Changelog.** Every change users can observe goes into the topmost unreleased
  section of [`docs/whats_new.rst`](../docs/whats_new.rst), under New Features,
  Behaviour Changes or Fixes. New API gets `.. versionadded::` with the next version.
- **No silent failures.** New error paths log with context, or raise a clear exception.
- **Async safety.** No blocking work on the event loop. A cancellation must not
  leave a lock held, and every wait has a timeout. Fire-and-forget tasks go
  through `utils._spawn_background`.
- **Memory and CPU.**
  - Cached models use `__slots__`.
  - New work on a hot path (`received_message`, `parse_*`, `_update`,
    `dispatch`, `HTTPClient.request`) needs a stated reason. The `enable_raw_presences`
    option exists because a listener check cost 13% of presence parse time.
- **JSON** goes through `utils._from_json` / `utils._to_json`.
- **Credentials** never reach logs.
- **Upstream syncs and ports** are recorded in [`docs/upstream_syncing.rst`](../docs/upstream_syncing.rst),
  and the synced commit in `.github/LATEST_UPSTREAM_COMMIT.txt`.

## Live testing

Live tests use real user accounts and aren't part of CI. Rules from the
v2.3.2 test rounds:

- **Accounts and servers.** Use throwaway accounts, and a server you own, for
  anything that writes.
- **Pacing.** Sends at 2.6 s and reactions at 3.0 s or slower.
- **Cleanup.** Delete everything the test creates, in a `finally`.
- **Tokens.** Keep them in a file that isn't committed, scan the DEBUG logs for
  token fragments afterwards, then shred the file and rotate the tokens.

## Releasing

Follow the v2.3.0, v2.3.1 and v2.3.2 release commits:

1. **Bump the version.** Change `__version__` and `version_info` in
   [`discord/__init__.py`](../discord/__init__.py).
2. **Rename the changelog section.** Turn the "Unreleased" section of
   `docs/whats_new.rst` into `vX.Y.Z`, with the label `vpXpYpZ`.
3. **Update the README.** Point its release links and wheel name at the new version.
4. **Check `versionadded`.** Every note added since the last release must name
   the new version.
5. **Open the PR.** It goes through the normal review and CI, and is merged with a
   merge commit pinned to the tested head SHA.
6. **Publish.** Build with `python -m build --wheel --sdist`, create the GitHub
   release with the tag `vX.Y.Z` on the merge commit, and attach the wheel and the sdist.
