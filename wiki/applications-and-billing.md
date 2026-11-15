# Applications, billing and other account features

An overview of the less common areas the library covers: your developer
applications and teams, OAuth2 authorizations, billing and Nitro, boosts,
entitlements and gifts, the store, promotions, the game library, experiments,
and the tracking context the client sends. The full reference is
[`docs/api.rst`](../docs/api.rst); this page tells you what exists and where
the traps are.

> Many of these touch money or credentials. Read the docstring of anything
> you call here, and test with a throwaway account.

## Applications and teams

Manage the developer applications and teams your account owns, as the
Developer Portal does:

```python
for app in await client.applications():
    print(app.id, app.name, app.owner)

app = await client.fetch_application(app_id)
app = await app.edit(description='...', tags=['utility'])
bot = await app.fetch_bot()

public = await client.fetch_public_application(app_id)   # anyone's, public view
teams = await client.teams()
team = await client.create_team('My team')
```

| Area | Entry points |
|---|---|
| Applications | `client.applications()`, `fetch_application`, `fetch_public_application`, `create_application(name, team=None)` |
| Bots | `app.fetch_bot()`, `app.create_bot()`, `bot.token()` |
| Secrets | `app.secret()` |
| Testers and whitelist | `app.whitelisted()`, `app.whitelist(...)` |
| Intents | `app.request_intents(...)` (privileged intents for verified bots) |
| Store and monetisation | `app.skus()`, `app.create_sku(...)`, `app.entitlements(...)`, gift batches |
| Builds (games) | `app.branches()`, `create_branch`, `ApplicationBranch.create_build`, `ApplicationBuild.upload_files`, `publish()` |
| Teams | `client.teams()`, `fetch_team`, `create_team`, `team.fetch_members()`, `invite_member`, `payouts()`, `leave()`, `delete()` |
| Statistics | `client.activity_statistics()`, `global_activity_statistics(...)` |

Gotchas:

- **`bot.token()` and `app.secret()` reset the credential.** The previous
  token or secret stops working immediately.
- `app.create_bot()` returns a token only if the application had no bot yet.
- A team's owner can't `leave()` it; they must `delete()` it.

## OAuth2 authorizations

Authorize an app for your account, as the "Authorize" screen does:

```python
auth = await client.fetch_authorization(app_id, scopes=['identify', 'guilds'])
print(auth.application.name, auth.scopes)
redirect_url = await auth.authorize()            # or client.create_authorization(...)

for token in await client.oauth2_tokens():       # apps you've authorized
    print(token.application.name, token.scopes)
    # await token.revoke()
```

`on_oauth2_token_revoke(token, application_id)` fires when access is revoked.

## Billing, Nitro and payments

| Area | Entry points |
|---|---|
| Payment sources | `client.payment_sources()`, `fetch_payment_source`, `create_payment_source(...)` (low-level), `PaymentSource.edit/delete` |
| Payments | `client.payments(limit=100, ...)` (an async iterator), `fetch_payment`, `Payment.is_refundable()`, `Payment.void()` (pending payments only) |
| Subscriptions (Nitro) | `client.subscriptions()`, `fetch_subscription`, `preview_subscription`, `create_subscription`, `Subscription.edit/cancel/delete`, `invoices()` |
| Usage | `client.premium_usage()`, `checkout_recovery()` |

- DEBUG logs redact payment tokens and billing details
  (`payment_source_token`, `billing_address_token`, `billing_address`, `bank`).
- `create_payment_source` needs data from the payment gateway's own API; it's
  low-level by design.
- Events: `on_payment_sources_update`, `on_subscriptions_update`,
  `on_payment_update`, `on_payment_client_add`.

## Boosts

```python
for slot in await client.premium_guild_subscription_slots():
    if slot.is_available():
        await slot.apply(guild)
```

- `PremiumGuildSubscriptionSlot`: `is_available()`, `is_on_cooldown()`,
  `apply(guild)`, `cancel()`, `uncancel()`.
- `client.premium_guild_subscriptions()`, `premium_guild_subscription_cooldown()`;
  on the guild side `guild.premium_subscriptions()` and
  `guild.apply_premium_subscription_slots(*slots)`.

## Entitlements, gifts and the store

| Area | Entry points |
|---|---|
| Entitlements | `client.entitlements(...)`, `giftable_entitlements()`, `premium_entitlements(...)` (Nitro credits), `fetch_entitlements(app_id)`; `Entitlement.is_active()`, `consume()` (test purchases only, irreversible), `delete()` |
| Gifts | `client.fetch_gift(code)`, `Gift.redeem()`, `Gift.delete()` (gifts you created) |
| Store | `fetch_sku`, `fetch_store_listing`, `fetch_published_store_listing`, `fetch_primary_store_listing(s)`, `premium_subscription_plans()`, `fetch_sku_subscription_plans` |

`Entitlement.is_active()` reproduces the client's logic: a Nitro-perk
entitlement counts only while you have Nitro.

## Promotions and the game library

- `client.promotions(claimed=False)`, `Promotion.claim()` (returns the code),
  `client.user_offer(...)` (replaces the deprecated `trial_offer()`),
  `client.pricing_promotion()`.
- `client.library()` returns `LibraryApplication`s (games in your library),
  with `activity_statistics()`, `mark_installed()`, `edit(flags=...)`,
  `delete()`.

## Experiments

Discord rolls features out with **experiments**: each user and guild is
assigned a bucket deterministically.

```python
exp = client.get_experiment('2026-01_some_feature')     # by name or hash
if exp is not None:
    print(type(exp).__name__, getattr(exp, 'bucket', None))
    if isinstance(exp, discord.GuildExperiment):
        print(exp.bucket_for(guild))                       # -1 when the guild isn't in it

experiments = await client.fetch_experiments(with_guild_experiments=True)
```

- `UserExperiment` exposes only your assigned bucket (`bucket`, `result`).
- `GuildExperiment` has the whole rollout (populations, overrides,
  holdouts, A/A mode); `bucket_for(guild)` computes a guild's bucket:
  overrides first, then A/A mode, then holdouts, then the population ranges.
  `result_for(guild)` is `murmurhash32('<name>:<guild_id>') % 10000`, so the
  experiment's name must be known (`ValueError` otherwise; `get_experiment`
  fills it in when you look it up by name).
- `ApexExperiment` (newer experiments) has `assignment_for(unit)`.
- The hash is MurmurHash3 (`mmh3` from the `speed` extra, or a pure-Python
  copy without it).

## The tracking context: `X-Context-Properties`

The official client tells Discord **where in the UI** some actions come from
(the friends list, a DM, an invite link...) with an `X-Context-Properties`
header. Some actions (joining a guild, sending a friend request) are expected
to carry it. The library sends a matching `ContextProperties` automatically
for:

- opening DMs and groups, converting a DM to a group;
- sending messages (`chat_input`) and greetings;
- joining guilds (including "lurking"), accepting and creating invites;
- adding, removing and requesting friends;
- SKU purchase previews and purchases, and fetching experiments.

You don't need to do anything. `discord.tracking.ContextProperties` exists if
you call the HTTP layer yourself.

## Other areas

| Area | Entry points |
|---|---|
| Tutorial indicators | `client.tutorial`, `Tutorial.suppress()`, `confirm(*indicators)` (no gateway updates; tracked locally) |
| Connected accounts | `client.fetch_connections()`, `authorize_connection(type)` (returns the consent URL), `create_connection(...)` (low-level); see [Users and relationships](users-and-relationships.md#connected-accounts) |
| Guild integrations | `guild.integrations()`, `guild.create_integration(...)`, `Integration.delete()` |
| Student hub directories | `DirectoryChannel`, `DirectoryEntry.edit/delete` |
| Guild profiles | `guild.profile()`, `client.fetch_guild_profile(guild_id)`, `GuildProfile.edit` |
| Unverified game reports | `client.report_unverified_application(...)` |
| Active Developer badge | `client.join_active_developer_program(application=..., channel=...)` |
