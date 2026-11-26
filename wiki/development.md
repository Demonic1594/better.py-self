# Development

This page is for people working on better.py-self itself: how the repository
is laid out, how to set up an environment, every check CI runs and how to run
it locally, how the tests are organised, the benchmarks, the rules a change
must follow, live testing, the wiki, upstream syncs and the release process.

[`.github/CONTRIBUTING.md`](../.github/CONTRIBUTING.md) has the contribution
rules (bug reports, pull requests, commit messages, AI-assisted contributions).
This page explains how to meet them.

## Repository layout

| Path | What lives there |
|---|---|
| [`discord/`](../discord) | The library. `client.py`, `state.py` (gateway event parsers and caches), `gateway.py`, `http.py` and the model modules (`guild.py`, `member.py`, `message.py` and so on). |
| [`discord/types/`](../discord/types) | `TypedDict`s describing Discord's JSON payloads. They are for type checking only and are never used at runtime. |
| [`discord/ext/commands/`](../discord/ext/commands), [`discord/ext/tasks/`](../discord/ext/tasks) | The command framework and the background-loop helper. |
| [`discord/webhook/`](../discord/webhook) | `Webhook` (async) and `SyncWebhook` (blocking). |
| [`discord/bin/`](../discord/bin) | Bundled libopus DLLs for Windows. |
| [`tests/`](../tests) | The pytest suite, the memory benchmark and the traffic harness. Everything is offline. |
| [`tools/`](../tools) | `stress_bench.py`, `profile_live.py` and `sync_wiki.py`. Not part of the package. |
| [`docs/`](../docs) | The Sphinx documentation: API reference, changelog, FAQ, guides, the audit report and the upstream divergence map. |
| [`wiki/`](../wiki) | This wiki. Published to the GitHub Wiki by a workflow. |
| [`examples/`](../examples) | Runnable examples. They are not tested by CI, so keep them simple. |
| [`.github/workflows/`](../.github/workflows) | CI: `test.yml`, `lint.yml`, `build.yml` and `wiki.yml`. |

The [architecture page](architecture.md) explains how the modules fit together
at runtime.

## Setting up

```sh
git clone https://github.com/Demonic1594/better.py-self
cd better.py-self
python3 -m venv .venv && . .venv/bin/activate

# The library in editable mode, with every extra CI uses
python -m pip install -U pip
python -m pip install -U -e ".[test,docs,voice,speed]"

# Tools CI pins
python -m pip install "ruff==0.12.*" pyright build
```

Notes on the extras:

- **`docs`** pins an old Sphinx (4.4.0) and the matching `sphinxcontrib-*`
  packages. A newer Sphinx can fail on the theme or the extensions, so install
  it from this extra rather than by hand. On Python 3.13+ it also installs
  `imghdr-lts`, because Sphinx 4.4 imports `imghdr`, which 3.13 removed.
- **`speed`** installs orjson, zstandard and friends. Develop with it **and**
  test without it: CI runs most of the matrix without it, so the stdlib `json`
  paths must keep working (see [below](#running-the-tests-without-orjson)).
- **`voice`** installs PyNaCl and davey. The voice tests use fakes, but the
  imports must succeed.

> **Gotcha: ruff versions.** CI pins ruff **0.12**. Newer ruff versions
> format some constructs differently and also format Markdown code blocks,
> so `ruff format --check` can pass locally and fail in CI, or the other way
> round. If you have a newer ruff installed globally, use a separate venv for
> 0.12.

## The checks (they match CI)

```sh
ruff format --check && ruff check                              # ruff 0.12
PYRIGHT_PYTHON_FORCE_VERSION=1.1.394 pyright discord           # 0 errors required
python -m pytest -q                                            # about 2.5 minutes
cd docs && sphinx-build -b html -n -T -W --keep-going . _build  # warnings are errors
```

What each one guards:

- **ruff** formats with single quotes and a 125-character line limit. The
  lint rules are deliberately small (`E4`, `E7`, `E9`, `F`); the reasons for the
  ignored ones are written next to them in `pyproject.toml`. `tests/` is linted
  but not reformatted.
- **pyright** runs in `basic` mode against Python 3.10, pinned to **1.1.394**.
  Other versions report different errors. There are 3 known warnings (two
  `# type: ignore` comments in `utils.py` that are only needed when orjson is
  missing, and one in `ext/commands/bot.py`); errors must stay at 0.
- **pytest** runs with `asyncio_mode = "strict"`, so every async test needs
  `@pytest.mark.asyncio`. Tests marked `perf` (allocation-count smoke tests)
  only run in the CI legs with the `speed` extra.
- **Sphinx** builds with `-n -W`: every cross-reference (`:class:`,
  `:meth:`, `:attr:`) must resolve, and every warning fails the build. A new
  public class needs an `autoclass` (and usually an `attributetable`) entry in
  `docs/api.rst`.

> **Gotcha: stale bytecode.** After editing a file in place without changing
> its size (for example `==` to `is`), delete `__pycache__` before running the
> tests again. Python checks the size and modification time of the source, and
> an edit within the same second keeps both, so the old `.pyc` can be reused:
> `find . -name __pycache__ -prune -exec rm -rf {} +`

### Running a subset

```sh
python -m pytest -q tests/test_reconnect.py              # one file
python -m pytest -q -k "chunk and not slow"              # by name
python -m pytest -q -x --lf                              # stop at the first failure, rerun the last failures
python -m pytest -q -p no:cacheprovider                  # don't write .pytest_cache
```

### Running the tests without orjson

CI's non-speed legs have no orjson, so every JSON path must also work with the
stdlib `json` module. To check that without making a second venv:

```sh
python -c "import json,sys,discord.utils as u; u._from_json=json.loads; u._to_json=lambda o: json.dumps(o,separators=(',',':')); import pytest; sys.exit(pytest.main(['-q']))"
```

The library always goes through `utils._from_json` and `utils._to_json`, so
replacing those two functions is enough. Never call `json.loads` or
`orjson.loads` directly in library code.

### Other Python versions

CI runs Python **3.10, 3.11, 3.12, 3.13 and 3.14**. Several behaviours
differ between them, and tests for those behaviours must run on the real
interpreter, not be reasoned about:

- **3.10 and 3.11:** `asyncio.wait_for` runs the awaited coroutine in a
  child task, so cancelling from inside it behaves differently from 3.12+.
  Voice and handshake teardown code is sensitive to this.
- **3.12+:** `eager_dispatch` is available (eager task factory). Before 3.12
  the option is ignored with a warning.
- **3.14:** annotations are evaluated lazily (PEP 649). Command signatures in
  `ext.commands` resolve exactly as on 3.13, which `tests/test_audit3_round3.py`
  pins.

Interpreters from [python-build-standalone](https://github.com/astral-sh/python-build-standalone)
or `uv python install 3.10 3.14` are a quick way to get them locally.

## How CI is organised

| Workflow | Runs on | What it does |
|---|---|---|
| `Test` (`test.yml`) | every push and pull request | pytest on 3.10 to 3.14 with the `test` extra, plus 3.12 and 3.14 with `test,speed`. Coverage is printed. `perf` tests only run in the speed legs. |
| `Lint` (`lint.yml`) | every push and pull request | pyright 1.1.394 and ruff 0.12. |
| `Build` (`build.yml`) | every push and pull request | `python -m build` (wheel and sdist) and the Sphinx build with `-W`. |
| `Wiki` (`wiki.yml`) | pushes to `master` that touch `wiki/` | Renders `wiki/` and pushes it to the GitHub Wiki. |
| `Docs` (`docs.yml`) | pushes to `master` that touch `docs/` or `discord/` | Builds the Sphinx docs and publishes them to GitHub Pages. Needs Settings > Pages > Source: "GitHub Actions". |

A pull request from a fork runs the same jobs. The `Test` workflow skips the
duplicate `pull_request` run for branches in this repository, since the push
already ran.

## The tests

Tests must stay **offline**: no network, and no real tokens, webhook URLs or
user IDs (use small fake IDs such as `100`, `501`).

### How the suite is organised

- **Feature and behaviour tests** (`test_files.py`, `test_embed.py`,
  `test_ext_tasks.py`, `test_permissions_all.py`, ...) test one area each.
- **Regression rounds** (`test_audit*_round*.py`, `test_round5_fixes.py`,
  `test_release_233.py`, ...) hold the tests written for each audit or fix
  round. Each test's docstring says what used to go wrong.
- **Ports** (`test_rapptz_ports*.py`, `test_rapptz_soundboard.py`,
  `test_ported_features.py`) test features ported from Rapptz/discord.py.
- **Hardening** (`test_abuse*.py`, `test_hardening*.py`) feeds hostile or
  malformed payloads through the parsers.
- **Optimisations** (`test_optimizations_*.py`, `test_perf_smoke.py`,
  `test_memory.py`) pin the memory and allocation properties of the hot paths.
- **Traffic regimes** (`test_traffic_regimes*.py`, `test_fixture_replay.py`,
  `test_gateway_regimes.py`) drive the real `HTTPClient` against a mock Discord
  server, and replay recorded sessions from `tests/fixtures/`.

### Shared helpers

Test modules import helpers from each other on purpose:

| Helper | In | What it gives you |
|---|---|---|
| `_client()` | `tests/test_user_paths.py` | A `Client` driven through a synthetic READY with a guild (`id=1`), a text channel (`id=501`) and a cached member (`id=100`). |
| `drive_ready`, `ready_payload`, `guild_payload` | `tests/test_persistence.py` | Build and dispatch a synthetic READY of any size. |
| `FakeResponse`, `make_http` | `tests/test_rate_limit_behavior.py` | An `HTTPClient` with a fake transport, to test rate limits and retries. |
| `make_env` | `tests/test_abuse.py` | A client, state, guild and HTTP client with stubbed endpoints. |
| mock server, recorder, replayer | `tests/traffic_harness.py` | A stateful mock of Discord's HTTP semantics measured live, a JSONL session recorder and a replayer. Not collected by pytest. |

A typical parser test:

```python
import asyncio
import pytest
from test_user_paths import _client


@pytest.mark.asyncio
async def test_raw_typing_fires_for_an_uncached_channel():
    client, state = await _client()
    state.raw_typing_flag = True  # what Client(enable_raw_typing=True) sets
    seen = []

    @client.event
    async def on_raw_typing(payload):
        seen.append(payload)

    state.parse_typing_start({'channel_id': '9999', 'guild_id': '1', 'user_id': '424242', 'timestamp': 1_700_000_000})
    await asyncio.sleep(0)  # let the dispatched handler task run

    assert [(p.channel_id, p.user) for p in seen] == [(9999, None)]
    await client.close()
```

Parsers take the payload as Discord sends it, so give them a complete one: a
`THREAD_UPDATE` for a thread that isn't cached builds a `Thread` from it and
needs `owner_id`, `thread_metadata` and the other fields Discord always sends.

### What a regression test must do

- **Check the fixed behaviour,** not just that no exception is raised.
- **Fail on the old code.** Before opening the PR, run the new test against
  the code from `master`:

  ```sh
  git stash push -- discord        # take out the library change, keep the test
  python -m pytest -q tests/test_new_thing.py   # must fail
  git stash pop
  ```

- **Say why** in its docstring: what used to happen.
- **Guards** (tests that pin behaviour that must not change) are welcome too;
  say in the docstring that they are guards, since they pass on the old code.

## Benchmarks

### Memory benchmark

```sh
python tests/memory_bench.py --members 50000 --top 15
```

It builds 100 guilds, 6,000 channels and 50,000 members (`--members`), and
reports the memory they retain, with the 15 largest allocation sites
(`--top`). `--experiments N` adds N user and N guild experiments (a real
READY has about 350). The baseline is **45.27 MiB**, unchanged from v2.3.2 to
v2.3.3. Run it before and after any change to models, caches or parsers, and
put both numbers in the pull request.

### Stress benchmark

```sh
python tools/stress_bench.py           # moderate, a few minutes
python tools/stress_bench.py --heavy   # several million events, 40 accounts, 500k members
python tools/stress_bench.py --phase soak --events 2000000 --json soak.json
```

It pushes pre-serialised gateway frames through the real receive, parse and
dispatch path, offline, in six phases:

| Phase | What it checks |
|---|---|
| `throughput` | Events per second through the full path. |
| `soak` | One million events (`--events`): caches stay within their caps, no warnings, and live objects plateau. |
| `backlog` | A burst of slow handlers (`--burst`): every handler completes, `on_dispatch_backlog` fires, memory recovers. |
| `multi` | 10 accounts (`--accounts`; 40 with `--heavy`) in one process: READY cost per account. |
| `persist` | Writes and replays a snapshot of 200,000 members (`--persist-members`). |
| `bomb` | Hostile frames (`--bomb-events`): corrupt, oversized and malformed. Memory stays bounded and nothing escapes. |

It exits non-zero when a gate fails. The **soak** plateau gate counts live
allocated blocks (`sys.getallocatedblocks()`, after a `gc.collect()`), which
must grow by at most 2% (`--plateau-pct`) over the second half of the run. RSS
growth over the same window is printed as `[INFO]` only: it grows by about
10 MiB because the allocator keeps freed arenas, while the live objects stay
flat. The baseline numbers are in [`docs/cache_tuning.rst`](../docs/cache_tuning.rst).

Run it for changes to hot paths (`gateway.py`, `state.py` parsers,
`HTTPClient.request`) or persistence.

### Live profiling

```sh
DISCORD_TOKEN=... python tools/profile_live.py --minutes 10
DISCORD_TOKEN=... python tools/profile_live.py --minutes 10 --cprofile
```

It connects a real account, **only listens** (it never sends anything), and
writes `profile_report.txt` (`--output`) with aggregate numbers: event counts,
parse times, JSON decode time, memory and cache sizes. The report has no
message content, tokens, IDs or names, so it is safe to share.
`--cprofile` profiles every call and slows the client down 1.5 to 2 times, so
run once without it for realistic timings. `--eager-dispatch` runs with
`Client(eager_dispatch=True)`.

## Rules for changes

The same rules are written in `CLAUDE.md` and the CodeRabbit configuration.

- **The public API stays the same.** Nothing public is removed or renamed,
  signatures keep their meaning, and new parameters are optional. When
  upstream removes something, it stays here with a `DeprecationWarning`.
- **Changelog.** Every change users can observe goes into the topmost
  unreleased section of [`docs/whats_new.rst`](../docs/whats_new.rst), under
  New Features, Behaviour Changes or Fixes. New API gets
  `.. versionadded::` with the next version; a changed behaviour gets
  `.. versionchanged::`.
- **No silent failures.** A new error path logs with enough context to
  diagnose it, or raises a clear exception. For a value from Discord or the
  caller that the library can't use, the pattern since v2.3.3 is: check it,
  log a warning once (`utils._warn_once(logger, key, message)`, with a bounded
  set of keys), skip the value and keep working.
- **Async safety.**
  - No blocking work on the event loop.
  - A cancellation must not leave a lock held or a future unresolved.
  - Every wait has a timeout.
  - Fire-and-forget tasks go through `utils._spawn_background`, which keeps a
    reference so the task isn't garbage-collected mid-run.
- **Memory and CPU.**
  - Cached models use `__slots__`.
  - New work on a hot path (`received_message`, `parse_*`, `_update`,
    `dispatch`, `HTTPClient.request`) needs a stated reason. For example
    `enable_raw_presences` and `enable_raw_typing` are opt-in because even
    checking for a listener cost 13% of a presence update's parse time.
- **JSON** goes through `utils._from_json` and `utils._to_json`.
- **Credentials never reach logs.** New request fields that carry secrets go
  into the redaction list (see [Logging and credentials](logging-and-credentials.md)).
- **Upstream syncs and ports** are recorded in
  [`docs/upstream_syncing.rst`](../docs/upstream_syncing.rst), and the synced
  commit in `.github/LATEST_UPSTREAM_COMMIT.txt`.

## Live testing

Live tests use real user accounts and are not part of CI. The results of past
rounds are in [`docs/live_validation_2026-10.md`](../docs/live_validation_2026-10.md).
The rules:

- **Accounts and servers.** Use an account and a server you control for
  anything that writes, never ones other people rely on.
- **Pacing.** Sends at 2.6 s and reactions at 3.0 s or slower. See
  [Rate limits and pacing](rate-limits-and-pacing.md).
- **Cleanup.** Delete everything the test creates, in a `finally`.
- **Tokens.** Keep them in a file that isn't committed (or an environment
  variable), scan the DEBUG logs for token fragments afterwards, then delete
  the file and log the accounts out of every session to rotate the tokens.

A minimal live smoke test:

```python
import asyncio, logging, os
import discord

discord.utils.setup_logging(level=logging.INFO)


class Smoke(discord.Client):
    async def on_ready(self):
        print('ready as', self.user, 'in', len(self.guilds), 'guilds')
        channel = self.get_channel(int(os.environ['TEST_CHANNEL']))
        msg = await channel.send('smoke test')
        await asyncio.sleep(2.6)
        await msg.delete()
        # Force a reconnect: the client should resume, not identify again
        await self.ws.close(code=4000)

    async def on_resumed(self):
        print('resumed')
        await self.close()


Smoke().run(os.environ['DISCORD_TOKEN'], log_handler=None)
```

## Syncing with upstream

The parent project is [dolfies/discord.py-self](https://github.com/dolfies/discord.py-self).

```sh
git remote add upstream https://github.com/dolfies/discord.py-self   # once
git fetch upstream
git log --oneline $(cat .github/LATEST_UPSTREAM_COMMIT.txt)..upstream/master   # what's new
git merge upstream/master
```

- On conflicts **this fork's code wins**, but meaningful upstream fixes and
  features are ported in by hand.
- [`docs/upstream_syncing.rst`](../docs/upstream_syncing.rst) is the
  divergence map: for every area this fork changed, it says what changed and
  where conflicts will appear. Read the section for a file before resolving a
  conflict in it, and keep the map current.
- Record the merged commit in `.github/LATEST_UPSTREAM_COMMIT.txt`.
- Ports from Rapptz/discord.py are done commit by commit and recorded in the
  same file, with the Rapptz commit hash. Some Rapptz changes don't apply to
  user accounts or conflict with this fork's design; those are recorded as
  "not ported" with the reason (see [Differences from upstream](differences-from-upstream.md)).

## The wiki

The pages in `wiki/` are the source of truth. On every push to `master` that
touches `wiki/`, the `Wiki` workflow runs [`tools/sync_wiki.py`](../tools/sync_wiki.py)
and pushes the result to the GitHub Wiki. The script:

- renames `README.md` to `Home.md` (the wiki's front page);
- drops `.md` from links between pages (`[x](events.md)` becomes `[x](events)`);
- points `../` links at the files on GitHub (`../docs/api.rst` becomes
  `https://github.com/<repo>/blob/master/docs/api.rst`);
- leaves code blocks and inline code alone;
- writes `_Sidebar.md` (every page, sorted by its title) and `_Footer.md`.

Edits made on the Wiki tab are overwritten by the next publish, so edit the
files here, in a pull request.

To preview the rendered pages and check links:

```sh
python tools/sync_wiki.py wiki /tmp/wiki-preview --repo Demonic1594/better.py-self
python -m pytest -q tests/test_sync_wiki.py
```

`tests/test_sync_wiki.py` renders the real wiki and checks that every link
resolves to a page, or to a file or folder that exists in the repository. Run
it after adding or renaming a page. The workflow only publishes from `master`
(a manual run from another branch does nothing).

Style for wiki pages:

- Every statement is checked against the code at the version the home page
  names. Facts from elsewhere (Discord's behaviour, a live test, an upstream
  project) say where they come from.
- Examples are complete enough to run, use `discord.Client` or
  `commands.Bot` explicitly, and never contain a real token or ID.
- Gotchas go in a `> **Gotcha:**` block next to the feature they affect.

## Releasing

Follow the v2.3.0 to v2.3.3 release commits:

1. **Bump the version.** Change `__version__` and `version_info` in
   [`discord/__init__.py`](../discord/__init__.py).
2. **Rename the changelog section.** Turn the "Unreleased" section of
   `docs/whats_new.rst` into `vX.Y.Z`, with the label `vpXpYpZ`, and give it
   a short introduction.
3. **Update the README.** Point its release links and wheel name at the new
   version, and add a "New in" block. Do the same for the wheel URL in
   [Installation](installation.md) and [Getting started](getting-started.md),
   and the version on the wiki's [home page](README.md).
4. **Check the version tags.** Every `versionadded` and `versionchanged`
   added since the last release must name the new version.
5. **Open the PR.** It goes through the normal review and CI, and is merged
   with a merge commit pinned to the tested head SHA.
6. **Publish.**
   - Build the files: `python -m build --wheel --sdist`.
   - Check that the wheel installs in a clean venv and that `discord.__version__`
     is the new version.
   - Create the GitHub release with the tag `vX.Y.Z` on the merge commit, and
     attach the wheel and the sdist. The README's install command points at
     the wheel's release URL, so it works from the moment the release exists.
