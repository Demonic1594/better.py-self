# Live Anti-Detection Validation Report — better.py-self v2.2.0

**Account:** dxddys_girl (1221362476063395852) — throwaway, user token
**Date:** 2026-10-10/11, from this device (no proxy)
**Exclusions honored:** guild 1219341038146162779 never touched; writes only to own DMs

## Outcome summary

**2 real defects found and fixed** (both merged, CI green):

1. **CRITICAL — Fingerprint regression: `client_build_number=9999` on every request**
   My earlier lazy-aiohttp change left the CDN session as the MISSING sentinel;
   `Client.headers_context()` passed it to the fingerprint fetcher, so all three
   fetch paths (info API → /login scrape → chrome version) silently failed and
   every request since that commit carried the hardcoded 9999 fallback build +
   a stale Chrome/150 UA. No real client ever sends 9999 — a glaring automation
   signal. **Fixed** (`headers_context` now ensures the session exists).
   Live-verified: build=**634304**, browser=**156.0.0.0**, UA Chrome/**156** —
   coherent with cordapi's current properties.

2. **Persistence replay crash on pre-connect session parsing**
   `parse_ready` → `parse_sessions_replace` dereferenced `self.ws.status`, but
   no websocket exists during replay. Every real READY carries sessions, so
   warm restarts always fell back to cold start (gracefully, thanks to the
   replay hardening — but the feature never actually worked live).
   **Fixed** (the internal-state discovery is skipped when `ws is None`).
   Live-verified: 8 guilds restored from a 161 KiB snapshot in 20–59 ms.

## Phase results

### A. Fingerprint (after fix)
| field | value | verdict |
|---|---|---|
| client_build_number | 634304 | live from cordapi ✅ |
| browser_version | 156.0.0.0 | matches UA ✅ |
| UA | Chrome/156.0.0.0 Windows | coherent ✅ |
| sec-ch-ua coherence | present, consistent | ✅ |
| required headers | all present (UA, X-Super-Properties, Origin, Referer, Accept…) | ✅ |

### B. Session & cookies
- Login 0.40–1.00 s; **all three Cloudflare cookies held** (`__dcfduid`, `__sdcfduid`, `_cfuvid`) — matching findings.md's mitigation
- Zero 403 challenges across all sessions

### C. Gateway fidelity
- READY in 2.0–2.5 s; heartbeat latency 181–342 ms, **stable across the 5-min soak** (no drift)
- IDENTIFY payload carries token/capabilities(0x57fd)/properties/presence/compress/client_state + guild_versions ✅
- **RESUME verified live**: hard socket close → reconnect resumed the *same session id* (`11cdf886…` → `11cdf886…`), no re-IDENTIFY
- Session state learning works: account's `dnd` status discovered from SESSIONS_REPLACE
- No invalid-session, no reconnect storms, no heartbeat ratelimits

### D. Traffic rhythm
- DM send: 0.41–0.44 s (single, well under the 1.1 s/channel guidance) — **latency EWMA captured it at 0.40–0.44 s on the send route**
- Reaction add/remove on own message: 0.32–0.37 s, honored the 2.5 s account-global lane
- No 429s (expected at this volume), no queuing latency observed

### E. Payload integrity
- Nonces are snowflake-shaped with real timestamps ✅; consecutive nonces may
  share a millisecond (monotonic=False occasionally) — matches how real clients
  can generate nonces within the same ms; harmless
- Interaction payload keys verified (session_id, message_flags present — code audit)

### F. Soak (5 min)
- 28 gateway events (GUILD_MEMBER_LIST_UPDATE ×10, THREAD_LIST_SYNC ×8,
  GUILD_MEMBER_UPDATE ×5, READY/READY_SUPPLEMENTAL/SESSIONS_REPLACE/
  GUILD_MEMBERS_CHUNK/GUILD_CREATE) — all parsed, zero handler errors
- Members crept 1938→1943 (live joins), backlog 0 throughout,
  loop lag 0.1–0.2 ms
- **Account alive at the end** (fetch_user succeeded; no flag, no invalidation)

## Remaining observations (not fixed, noted)

- `_has_sent_presence=False` at soak end: the client relies on IDENTIFY's
  presence field + session learning (per code comments, the gateway usually
  tells us); a real web client may also send an explicit OP3 early. Low risk,
  upstream behavior.
- Event volume was low (quiet account) — the 20k-item SYNC / presence-flood
  paths remain covered by the offline abuse suites rather than live traffic.
- The 9999-fallback class of failure is now *silent* only if cordapi AND the
  /login scrape AND the chrome-version fetch all fail; consider a startup
  warning when the fallback is used (not implemented; would need care not to
  warn when users intentionally pin contexts).

## Verdict

The stack now presents a **coherent, current fingerprint** (build 634304 /
Chrome 156), holds cookies, resumes sessions correctly, and survives a soak
without a single anomalous signal. The two defects fixed here were the most
detection-relevant bugs in the fork's history — the 9999 build number being
flagged on every request since the lazification commit is exactly the kind of
static, never-in-the-wild value that anti-abuse systems fingerprint.
