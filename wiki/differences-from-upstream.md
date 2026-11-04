# Differences from upstream

better.py-self tracks two upstream projects:

- **[dolfies/discord.py-self](https://github.com/dolfies/discord.py-self)** is
  the parent. This fork merges it periodically, and on conflicts this fork's
  code wins, but meaningful upstream fixes are ported in. The last synced commit
  is in `.github/LATEST_UPSTREAM_COMMIT.txt` (currently `2ba64a9`, which is also
  upstream's latest).
- **[Rapptz/discord.py](https://github.com/Rapptz/discord.py)** is the bot library both
  descend from. dolfies' fork last shared history with it in March 2022 and
  ports commits by hand. This fork ported three more rounds of Rapptz commits
  in v2.3.2.

Every divergence is recorded in [`docs/upstream_syncing.rst`](../docs/upstream_syncing.rst).
This page summarises them.

## Compared with discord.py-self

**Same public API.** Code written for discord.py-self runs unchanged: nothing
public is removed or renamed, and every new parameter is optional.

**What this fork adds:**

- **Bounded caches** with LRU eviction and `Client.cache_stats()`. See
  [Caching and memory](caching-and-memory.md). Pass `None` to any cap for
  upstream's unbounded behaviour.
- **Persistence** (`persistence_path`). See [Persistence](persistence.md).
- **Rate-limit visibility:** `on_http_throttle`, `http_throttle_events()`,
  `http_retry_events()` and `http_route_latencies()`. A shared-scope 429 also
  pauses every route. See [Rate limits](rate-limits-and-pacing.md).
- **Credential-safe DEBUG logs.** See [Logging](logging-and-credentials.md).
- **Faster hot paths:** O(1) message lookups, suppression of updates that change
  nothing, lazy parsing, `gc_freeze`, and `aiohttp` imported only when needed.
- **Opt-in options:** `eager_dispatch`, `enable_raw_presences` and
  `dispatch_backlog_warning`. An unknown `Client` option logs a warning.
- **Hardening.** The fork survived thousands of mutated gateway payloads, zlib
  bombs, ReDoS floods and hostile persistence snapshots. Each defect found has a
  regression test.
- **The Rapptz ports listed below.**

**What it doesn't carry:**

- **No PyPI package.** Install from the repository or a release wheel.
- **No `selfcord.py` renamed build** (upstream's `renamed` branch).

## Compared with discord.py (Rapptz)

discord.py is for **bot** accounts; this library is for **user** accounts. The
event and model API is largely the same. These Rapptz features aren't here,
because Discord only offers them to bots:

- **Commands and interactions:** `app_commands` / `CommandTree`, hybrid commands,
  `GroupCog`, and replying to interactions (`InteractionResponse`)
- **Sending components:** `discord.ui.View` / `Modal`. Receiving and clicking
  components works.
- **Bot-account features:** intents (user accounts receive what their
  subscriptions allow; see [Member lists](member-lists.md)), sharding and
  `AutoShardedClient`
- **App and store features:** entitlements, SKUs, application emojis and
  `Guild.self_role`
- **Voice-state REST endpoints** (`Member.fetch_voice` falls back to the cache)
  and `Guild.active_threads`

### Ported from Rapptz in v2.3.2

Rounds 1 and 2:

- **Gateway robustness:**
  - a 5xx on the resume URL falls back to the default gateway (`65232c3`)
  - a receive timeout closes the stalled socket (`1985498`)
  - a duplicate `GUILD_MEMBER_ADD` is ignored (`22ade98`)
- **Small fixes:** channel `clone()` fields (`354ae42`), the platform in
  activity reprs (`826aa22`), the deprecation-warning location (`aeab0d4`),
  poll durations rounded to hours (`38bbfed`) and `Paginator.pages` (`8f3224b`)
- **New API:** `Message.interaction_metadata`, `on_raw_presence_update` (behind
  `enable_raw_presences`) and `Member.fetch_voice`

Round 3 is soundboard and voice channel effects. Several bugs that Rapptz still
has are fixed here: unhashable sounds, `!=` raising on Python 3.14, emoji edits
that don't clear the previous emoji, audit-log entries that crash on a missing
target or lose a custom emoji's ID, and voice effects in private calls.

### Not ported, and why

- **`DummyCookieJar` (`643a7f4`):** webhooks bound to a client share its aiohttp
  session, and clients without cookies get 403s from Cloudflare.
- **Forcing IPv4 / `AF_INET` (`e6d2d82`):** this fork's aiohttp session only
  reaches the CDN and Google storage, which both serve IPv6.
- **`with_expiration` (`6ec2e53`):** the library already sends it, as the
  official client does.
- **The soundboard's intents gating (`14b2c1a`):** user accounts have no intents.
- **A cache-based `User.mutual_guilds`:** it would clash with
  `UserProfile.mutual_guilds`.

## Planned

`on_raw_typing`, `on_raw_thread_update`, `CallMessage.is_ended()`,
`MemberFlags.automod_quarantined_username` and possibly `ActivityType.unknown`
are planned for v2.3.4. See the [roadmap](../docs/roadmap.rst).
