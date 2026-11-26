# Installation

Everything about installing better.py-self: the supported Python versions,
the ways to install it, every dependency and extra and what each one is for,
what happens when an optional one is missing, and the platform and version
quirks that trip people up.

## Requirements at a glance

| | |
|---|---|
| **Python** | 3.10, 3.11, 3.12, 3.13 or 3.14 (`requires-python = ">=3.10"`). CI tests all five. |
| **Operating systems** | Linux, macOS and Windows. Nothing in the library is platform-specific except voice (libopus, ffmpeg) and a few small details listed [below](#platform-notes). |
| **Distribution name** | `discord.py-self` (the wheel file is `discord_py_self-2.3.5-py3-none-any.whl`). |
| **Import name** | `discord`, as in upstream. |
| **Where to get it** | This repository or its [GitHub releases](https://github.com/Demonic1594/better.py-self/releases). It is **not on PyPI**. |

> **Gotcha: `pip install discord.py-self` installs upstream, not this fork.**
> This fork keeps upstream's distribution name so it is a true drop-in, but it
> is not published to PyPI. A plain `pip install discord.py-self` (or a
> dependency that requires `discord.py-self`) pulls dolfies' package from PyPI
> and replaces this one. Always install from the repository URL or the release
> wheel, and pin it the same way in your requirements file.

## Installing

### From a release wheel (recommended for production)

Each [release](https://github.com/Demonic1594/better.py-self/releases) has a
wheel. Installing it needs no git and no compiler:

```sh
python3 -m pip install -U https://github.com/Demonic1594/better.py-self/releases/download/v2.3.5/discord_py_self-2.3.5-py3-none-any.whl
```

The wheel is pure Python (`py3-none-any`), so the same file works on every
platform and every supported Python version. Its dependencies (curl_cffi and
the others) come from PyPI as usual.

### From the repository

```sh
# The latest master
python3 -m pip install -U git+https://github.com/Demonic1594/better.py-self.git

# A specific release tag or commit
python3 -m pip install -U "git+https://github.com/Demonic1594/better.py-self.git@v2.3.5"
python3 -m pip install -U "git+https://github.com/Demonic1594/better.py-self.git@565d068f"
```

On Windows, use the launcher: `py -3 -m pip install -U git+https://github.com/Demonic1594/better.py-self.git`.

`master` is what CI last tested. It may contain changes that aren't in a
release yet; the "Unreleased" section of the
[changelog](../docs/whats_new.rst) lists them.

### With extras

Extras go in square brackets after the distribution name, with a
`name @ url` requirement:

```sh
# Voice
python3 -m pip install -U "discord.py-self[voice] @ git+https://github.com/Demonic1594/better.py-self.git"

# Speed (recommended)
python3 -m pip install -U "discord.py-self[speed] @ git+https://github.com/Demonic1594/better.py-self.git"

# Both, from the release wheel
python3 -m pip install -U "discord.py-self[voice,speed] @ https://github.com/Demonic1594/better.py-self/releases/download/v2.3.5/discord_py_self-2.3.5-py3-none-any.whl"
```

Quote the requirement: most shells treat `[` and `]` as wildcards.

### In a requirements file or pyproject.toml

```text
# requirements.txt
discord.py-self[speed] @ https://github.com/Demonic1594/better.py-self/releases/download/v2.3.5/discord_py_self-2.3.5-py3-none-any.whl
```

```toml
# pyproject.toml
[project]
dependencies = [
    "discord.py-self[speed] @ git+https://github.com/Demonic1594/better.py-self.git@v2.3.5",
]
```

Pin a tag or a wheel URL, not bare `master`, so a deploy doesn't change
version under you. Tools that resolve from PyPI only (some lockfile
generators, `pip install --index-url ... --no-deps` setups) need the direct
URL spelled out like this.

### In a virtual environment

A virtual environment keeps the library away from the system Python and from
other projects:

```sh
python3 -m venv .venv
. .venv/bin/activate              # Windows: .venv\Scripts\activate
python -m pip install -U pip
python -m pip install -U "discord.py-self[speed] @ git+https://github.com/Demonic1594/better.py-self.git"
```

### Replacing another discord library

These packages all install a top-level `discord` package, so only one of
them can be installed in an environment at a time:

- `discord.py-self` from PyPI (dolfies' upstream)
- `discord.py` (Rapptz's bot library)
- `py-cord`

Installing one over another leaves a mix of files from both, which fails in
confusing ways (missing attributes, wrong signatures). Uninstall the others
first:

```sh
python3 -m pip uninstall -y discord.py discord.py-self py-cord
python3 -m pip install -U "discord.py-self[speed] @ git+https://github.com/Demonic1594/better.py-self.git"
```

To use a bot library and this one side by side, give each its own virtual
environment.

### Checking the installation

```sh
python3 -m discord -v
```

prints the versions that matter for bug reports: Python, better.py-self (with
its release level), `discord-protos`, curl_cffi (with its libcurl build and the
browser it impersonates), aiohttp, davey, and the operating system:

```text
- Python v3.13.18-final
- discord.py-self v2.3.5-final
    - discord-protos v1.2.419
- curl_cffi v0.17.0 (curl vlibcurl/8.23.0-IMPERSONATE ... impersonating chrome152)
- aiohttp v3.14.5
- davey not found
- system info: Linux 6.18.44 ...
```

From Python:

```python
import discord
print(discord.__version__)        # '2.3.5'
print(discord.version_info)       # _VersionInfo(major=2, minor=3, micro=5, releaselevel='final', serial=0)
```

`python -m discord` also has `newbot` and `newcog` commands that generate a
project skeleton (`python -m discord newbot mybot --prefix '!'`). Note that this
entry point imports aiohttp eagerly, unlike `import discord`.

## Dependencies

### Required

These are installed automatically. `import discord` fails without curl_cffi,
yarl or discord_protos (and, on Python 3.13+, audioop-lts). tzlocal and
aiohttp are imported only when they're needed.

| Package | Constraint | What it's for |
|---|---|---|
| **curl_cffi** | `>=0.14.0,<1` | All REST requests and the gateway websocket. It drives a build of libcurl that reproduces a real browser's TLS and HTTP/2 fingerprint, so requests look like they come from the Discord web client. |
| **yarl** | `>=1.6.3,<2` | URL handling (gateway URLs, assets, files). |
| **discord_protos** | `>=1.2.117,<2.0.0` | The protobuf schemas of Discord's user settings (`UserSettings`, frecency settings). |
| **tzlocal** | `>=4.0.0,<6` | Your local time zone, sent in the `X-Discord-Timezone` header like the official client does. Imported lazily; if it fails, the header is left out silently. |
| **aiohttp** | `>=3.7.4,<4` | Only for CDN downloads (`Asset.read`, `Attachment.read`), webhooks and `discord.ext.tasks`. It is **not** imported by `import discord` (see below). |
| **audioop-lts** | Python 3.13+ only | `audioop` was removed from the standard library in 3.13; the player imports it for volume control. |

### The `speed` extra (recommended)

```text
orjson>=3.5.4
aiodns>=1.1        (not on Windows)
Brotli
mmh3>=2.5
zstandard>=0.23.0  (Python 3.13 and older)
```

| Package | What it changes | Without it |
|---|---|---|
| **orjson** | Every JSON payload is parsed and serialised with orjson. Gateway events are handled about 20% faster overall (up to 40% for small events such as typing and reactions), and READY and member chunks parse faster. | The stdlib `json` module is used. Same results, slower. |
| **zstandard** | The gateway uses `zstd-stream` compression, which is smaller and faster to decompress than zlib. | On Python 3.14, the stdlib `compression.zstd` module is used instead. Before 3.14, the gateway falls back to `zlib-stream`. |
| **mmh3** | A C implementation of MurmurHash3, used to compute experiment buckets. | A pure-Python MurmurHash3 is used. Only experiment lookups are affected. |
| **Brotli**, **aiodns** | Not used by better.py-self's own code. They speed up Brotli decoding and DNS for aiohttp, which only handles CDN downloads and webhooks here. | Nothing changes. |

The extra is optional because some of its packages have no prebuilt wheels on
a few platforms (Termux/Android, 32-bit ARM, PyPy) and would need a compiler
there. The library behaves the same without it.

> **Gotcha: orjson and json produce different bytes.** orjson writes
> non-ASCII characters as raw UTF-8, while the stdlib (as the library calls
> it) escapes them as `\uXXXX`. Discord accepts both. It only matters if you
> compare serialised payloads, or measure their size, across machines with
> and without the extra.

> **Gotcha: a broken orjson.** The library catches `ModuleNotFoundError` only.
> If orjson is installed but broken (for example built for another Python
> version), `import discord` raises its `ImportError` instead of falling back.
> Reinstall or uninstall orjson.

### The `voice` extra

```text
PyNaCl>=1.6.0,<1.7
davey>=0.1.0
```

| Package | What it's for | Without it |
|---|---|---|
| **PyNaCl** | Encrypts and decrypts voice packets. | `Client()` logs `PyNaCl is not installed, voice will NOT be supported.` once per process, and connecting to voice raises `RuntimeError: PyNaCl library needed in order to use voice`. |
| **davey** | DAVE, Discord's end-to-end encryption for voice (built on MLS). The library requires it for any voice connection, encrypted or not. | `Client()` logs `davey is not installed, voice will NOT be supported.`, and connecting raises `RuntimeError: davey library needed in order to use voice`. |

Voice also needs two things pip doesn't install:

- **libopus** (the Opus codec), to encode audio. On **Windows** the library
  ships `libopus-0.x64.dll` and `libopus-0.x86.dll` in `discord/bin` and loads
  the right one automatically. On **Linux and macOS** it looks for a system
  copy with `ctypes.util.find_library('opus')`: install `libopus0`
  (Debian/Ubuntu), `opus` (Fedora, Arch, Homebrew). If it isn't found
  automatically, load it yourself before playing audio:

  ```python
  import discord.opus
  if not discord.opus.is_loaded():
      discord.opus.load_opus('libopus.so.0')   # the full file name, or a full path
  ```

  Loading is lazy and errors are swallowed: nothing fails at import, and a
  missing libopus only shows up as `discord.opus.OpusNotLoaded` when audio is
  encoded.
- **ffmpeg**, to decode files and streams for `FFmpegPCMAudio` and
  `FFmpegOpusAudio`. The library runs the `ffmpeg` executable (or the one you
  pass as `executable=`). If it can't be started you get
  `ClientException: ffmpeg was not found.` Install it from your package
  manager, or pass a full path.

Building PyNaCl from source (only needed when there's no wheel for your
platform) needs `libffi-dev` and the Python headers (`python3-dev`, or
`python3.12-dev` for a specific version) on Linux:

```sh
sudo apt install libffi-dev python3-dev      # Debian/Ubuntu
sudo dnf install libffi-devel python3-devel  # Fedora
```

See [Soundboard and voice](soundboard-and-voice.md) for using voice.

### Optional packages outside the extras

| Package | Needed for |
|---|---|
| `requests` | `SyncWebhook` (the blocking webhook client). Without it, `SyncWebhook.from_url` raises `ModuleNotFoundError`. |
| `PyQt6` | Only [`examples/captcha.py`](../examples/captcha.py), which shows captchas in a webview. |

### Other extras

| Extra | For |
|---|---|
| `test` | pytest, pytest-asyncio, pytest-cov, pytest-mock and coverage, to run the test suite (plus `tzdata` on Windows). |
| `docs` | Sphinx 4.4.0 and its pinned extensions, to build `docs/`. Use this extra rather than a newer Sphinx. On 3.13+ it adds `imghdr-lts`, because Sphinx 4.4 imports `imghdr`, which 3.13 removed. |
| `dev` | ruff 0.12, the version CI uses. |

See [Development](development.md) for working on the library.

## Why aiohttp isn't imported

`import discord` doesn't import aiohttp. The library uses curl_cffi for every
request that matters (the REST API and the gateway), and aiohttp only for CDN
downloads and webhooks, so it is imported the first time one of those runs.
Importing it costs about 10 MB of memory and noticeable startup time per
process, which adds up when you run many accounts in separate processes.

Consequences:

- **`aiohttp` must still be installed** (it's a required dependency), because
  those paths need it.
- **`discord.ext.tasks` imports aiohttp** at the top, so a process that uses
  `tasks.loop` pays for it anyway.
- **The first CDN download or webhook call** is slightly slower, because it
  imports aiohttp then.
- **A global patch.** When aiohttp is first loaded, the library sets
  `aiohttp.hdrs.WEBSOCKET = 'websocket'` (lowercase), which the voice
  websocket needs. This affects any other aiohttp code in the same process.

## Python version notes

| Version | What differs |
|---|---|
| **3.10, 3.11** | `eager_dispatch` is not available: passing it logs `eager_dispatch requires Python 3.12 or newer; event handlers will be scheduled normally.` `asyncio.wait_for` runs the awaited coroutine in a separate task on these versions, which only matters if you cancel tasks from inside a handler that is itself awaited with `wait_for`. |
| **3.12+** | `Client(eager_dispatch=True)` starts event handlers eagerly (see [Client options](client-options.md)). |
| **3.13** | `audioop` is gone from the stdlib; the `audioop-lts` dependency replaces it. If you install with `--no-deps`, install it yourself or `import discord` fails. |
| **3.14** | The stdlib has `compression.zstd`, so gateway zstd compression works without the `zstandard` package (the `speed` extra doesn't install it on 3.14). Annotations are evaluated lazily (PEP 649); `ext.commands` handles that, including flag converters. |

## Platform notes

- **Windows:** libopus is bundled (above). ffmpeg is started without opening a
  console window. `aiodns` is not installed by the `speed` extra. Use
  `py -3 -m pip` to be sure you install into the Python you run.
- **Linux:** install libopus and ffmpeg from your package manager for voice.
  Headless servers need nothing else.
- **macOS:** `brew install opus ffmpeg` for voice. Homebrew on Apple Silicon
  installs under `/opt/homebrew`, which `find_library` may not search; if
  `discord.opus.is_loaded()` stays `False`, call
  `discord.opus.load_opus('/opt/homebrew/lib/libopus.dylib')`.
- **Docker:** a slim Python image plus `pip install` is enough without voice.
  For voice, add `libopus0 ffmpeg` to the image. Colour in logs is detected
  from the terminal, and inside Docker it is on by default.
- **Termux / Android, 32-bit ARM, PyPy:** install without the `speed` extra if
  its packages fail to build. curl_cffi needs a prebuilt wheel or a build of
  curl-impersonate for the platform.

## Upgrading

- **Read the [changelog](../docs/whats_new.rst)** for the versions you skip.
  Behaviour changes are listed separately from fixes.
- **Reinstall rather than mix.** `pip install -U` with the new URL replaces the
  old version cleanly.
- **Persistence snapshots** made by an older version are checked when they are
  loaded; see [Persistence](persistence.md) for what happens when the format
  changes.
- **discord_protos** follows Discord's settings schema, and new versions
  sometimes drop fields. The library reads the schema at runtime and copes
  with both old and new versions (for example the client theme fields that
  disappeared in recent versions); see [Settings](settings.md).
- **curl_cffi** updates change the browser fingerprint the library
  impersonates, and the Chrome version it advertises follows it (see
  [Tokens and login](tokens-and-login.md#the-tls-fingerprint)). Keep curl_cffi
  up to date: an old one makes the library claim an old Chrome.
  `python -m discord -v` shows the profile.

## Troubleshooting installation

| Symptom | Cause and fix |
|---|---|
| `ModuleNotFoundError: No module named 'curl_cffi'` (or `yarl`, `discord_protos`) | Installed with `--no-deps`, or into another Python. Reinstall without `--no-deps`, with `python -m pip` for the Python you run. |
| `ModuleNotFoundError: No module named 'audioop'` on 3.13+ | `audioop-lts` is missing: `pip install audioop-lts`. |
| `AttributeError` on something the docs say exists | Another `discord` package is installed over this one. Uninstall all of them and reinstall (see [above](#replacing-another-discord-library)). |
| `python -m discord -v` shows `discord.py-self v2.0.x` or another old version | pip resolved upstream from PyPI. Reinstall from this repository's URL. |
| `RuntimeError: PyNaCl library needed in order to use voice` | Install the `voice` extra. |
| `discord.opus.OpusNotLoaded` | libopus isn't installed or wasn't found; install it or call `discord.opus.load_opus()`. |
| `ClientException: ffmpeg was not found.` | Install ffmpeg or pass `executable='/path/to/ffmpeg'`. |
| pip fails building `orjson`, `zstandard` or `mmh3` | No wheel for your platform. Install without the `speed` extra. |
| `ImportError` from orjson at `import discord` | A broken orjson build; reinstall or uninstall it. |
