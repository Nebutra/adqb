# Dub Tech Debt Audit — Baseline 001

**Date:** 2026-04-14  
**Scope:** `apps/web`, `packages/prisma`, `packages/utils`  
**Auditor:** Claude Sonnet 4.6 (automated)

---

## Executive Summary

Dub is a well-structured Next.js monorepo with solid auth, RBAC, and webhook infrastructure. The biggest categories of debt are:

1. **Security gaps** — dangerous auth flags and plaintext password comparison in middleware
2. **Counter drift** — billing counters that never decrement, causing users to hit limits incorrectly
3. **Lifecycle / cleanup gaps** — fire-and-forget work that silently disappears on function timeout, unnotified account lockouts, webhook fallback that silently drops events
4. **Policy as code gaps** — rate limit centralization half-done, malicious link check only on two domains, TODO-gated features
5. **Boundary violations** — business logic bled into middleware, dual write paths creating inconsistency, TypeScript suppression hiding real bugs

---

## Issue Rankings (Impact vs Effort)

| # | Title | Impact | Effort | Priority |
|---|-------|--------|--------|----------|
| 1 | `linksUsage` never decremented on delete | High | Low | P0 |
| 2 | Password compared plaintext in edge middleware | High | Low | P0 |
| 3 | `allowDangerousEmailAccountLinking: true` on all OAuth providers | High | Medium | P0 |
| 4 | Account lockout silently fires — no user notification | Medium | Low | P1 |
| 5 | Webhook fallback silently drops on cache miss | Medium | Low | P1 |
| 6 | Malicious link check only covers `dub.sh` / `dub.link` | High | Low | P1 |
| 7 | `ev.waitUntil` vs `waitUntil` inconsistency — work may be lost | Medium | Medium | P1 |
| 8 | `bulkCreateLinks` + `skipDuplicates: true` — silently drops links, skews `linksUsage` | Medium | Medium | P2 |
| 9 | Fragmented rate-limit policy — "TODO: Centralize" left open | Medium | Low | P2 |
| 10 | `console.log` in `signIn` callback — auth credentials leak to logs | High | Low | P0 |

---

## Detailed Findings

---

### Issue 1 — `linksUsage` counter never decremented on link deletion (Correctness Bug)

**Files:**
- `/apps/web/lib/api/links/delete-link.ts` (entire file — no `linksUsage` mutation)
- `/apps/web/lib/api/links/bulk-delete-links.ts` lines 28–35
- `/apps/web/lib/api/links/update-links-usage.ts` lines 17–20

**Why it's debt:**  
`linksUsage` is a billing-period counter that gates link creation (`throwIfLinksUsageExceeded`). It is incremented on creation via `updateLinksUsage()` and reset to 0 monthly by the billing cron. But it is **never decremented** when links are deleted — neither in `deleteLink()` nor `bulkDeleteLinks()`.

Users who create 25 links (the free-plan limit), delete 10, and try to create 10 more are incorrectly blocked mid-cycle. The monthly reset masks the bug, making it appear sporadic. `totalLinks` IS decremented correctly, but the billing counter is not.

Evidence from `bulk-delete-links.ts`:
```ts
// Update totalLinks for the workspace ← correct
prisma.project.update({
  data: { totalLinks: { decrement: links.length } },
  // linksUsage is NOT decremented here
})
```

And from `delete-link.ts` — no `linksUsage` write at all.

**Fix:**
```ts
// In deleteLink():
link.projectId &&
  prisma.project.update({
    where: { id: link.projectId },
    data: {
      totalLinks: { decrement: 1 },
      linksUsage: { decrement: 1 },  // ADD THIS
    },
  }),

// In bulkDeleteLinks():
prisma.project.update({
  data: {
    totalLinks: { decrement: links.length },
    linksUsage: { decrement: links.length },  // ADD THIS
  },
})
```

---

### Issue 2 — Password compared plaintext in edge middleware (Security Gap)

**File:** `/apps/web/lib/middleware/link.ts` lines 188–209

**Why it's debt:**  
Password-protected links store the password as plaintext in the database (confirmed in `packages/prisma/schema/link.prisma` line 11: `password String?`). The check in middleware compares the user-supplied `pw` query parameter directly against the stored value:

```ts
if (!pw || (await getLinkViaEdge({ domain, key }))?.password !== pw) {
```

This is a deliberate architectural choice (the password is also stored in the Redis cache as `password: true` as a boolean flag, not the actual value — see `format-redis-link.ts` line 37). However, the actual comparison happens against the raw DB value via `getLinkViaEdge`, which returns the full plaintext string. This means:

1. Passwords are stored in plaintext in MySQL
2. On every password-protected link visit, the full link is re-fetched from DB (bypassing cache), exposing the plaintext password in the response object traveling through the stack

An attacker with DB read access has all passwords. Additionally, this creates a **double DB query** on every visit to a password-protected link (once to check cache, once more at line 197 regardless of the cache result).

**Fix (minimal):** Hash passwords at rest using bcrypt or Argon2 on storage, store the hash in `password` field, compare using timing-safe comparison in middleware.

```ts
// At link creation
const hashedPassword = await hashPassword(password);
// store hashedPassword in DB

// In middleware
const hashedPw = await hashPassword(pw); // hash the submitted pw
if (!pw || cachedLink.passwordHash !== hashedPw) {
  // redirect to password page
}
```

This also removes the forced second DB query, which is a performance issue at scale.

---

### Issue 3 — `allowDangerousEmailAccountLinking: true` on ALL OAuth providers (Security Gap)

**File:** `/apps/web/lib/auth/options.ts` lines 71, 76, 131

**Why it's debt:**  
NextAuth's `allowDangerousEmailAccountLinking` disables the protection that prevents account takeover via OAuth providers. If an attacker controls a Google/GitHub account that shares an email with a Dub user, they can sign in as that user without verifying they own the Dub account.

This is explicitly named "dangerous" in the library. It is enabled on Google, GitHub, and the custom SAML provider:

```ts
GoogleProvider({
  ...
  allowDangerousEmailAccountLinking: true,  // line 71
}),
GithubProvider({
  ...
  allowDangerousEmailAccountLinking: true,  // line 76
}),
```

**Why it might be intentional but is still debt:** The codebase handles this partially in `signIn` callbacks (checking SAML enforcement), but the OAuth flow itself has no defense. A user who signed up with email+password can have their account accessed via Google OAuth if someone registers a Google account with the same email.

**Fix:** Remove `allowDangerousEmailAccountLinking: true` from Google and GitHub providers, or add an explicit email-verified check in the `signIn` callback that rejects sign-ins when a pre-existing non-OAuth account exists.

---

### Issue 4 — Account lockout has no user notification (Lifecycle Gap)

**File:** `/apps/web/lib/auth/lock-account.ts` lines 29–31

**Why it's debt:**  
When a user exceeds `MAX_LOGIN_ATTEMPTS` (10 failed logins), their account is locked at line 23–27, but the `// TODO: Send email to user that their account has been locked` comment at line 29 shows no notification is sent. Users have no way to know their account is locked, and no self-service unlock path exists.

```ts
if (!lockedAt && invalidLoginAttempts >= MAX_LOGIN_ATTEMPTS) {
  await prisma.user.update({ data: { lockedAt: new Date() } });
  // TODO:
  // Send email to user that their account has been locked  ← never fires
}
```

This creates a silent denial-of-service: an attacker who knows a user's email can lock their account with 10 login attempts, and the legitimate user gets no notification and no recovery path.

**Fix:**
```ts
await sendEmail({
  to: user.email,
  subject: "Your account has been locked",
  react: AccountLocked({ email: user.email, unlockUrl: ... }),
});
```

---

### Issue 5 — Webhook delivery silently drops when cache misses (Lifecycle Gap)

**File:** `/apps/web/lib/tinybird/record-click.ts` lines 306–312

**Why it's debt:**  
`sendLinkClickWebhooks` fetches webhook definitions from a Redis cache. If the cache is empty (cold start, cache eviction, Redis outage), it returns early with no fallback:

```ts
const webhooks = await webhookCache.mget(webhookIds);

// Couldn't find webhooks in the cache
// TODO: Should we look them up in the database?
if (!webhooks || webhooks.length === 0) {
  return;  // ← silently drops all webhook deliveries
}
```

The `TODO` comment acknowledges this is known debt. Every time Redis loses the webhook cache entry, all click webhooks for that link are silently dropped — no error, no retry, no fallback DB lookup. This is data loss in a paid feature.

**Fix (minimal):**
```ts
if (!webhooks || webhooks.length === 0) {
  // Fallback to database
  const dbWebhooks = await prisma.webhook.findMany({
    where: { id: { in: webhookIds } },
    select: { id: true, url: true, secret: true },
  });
  if (dbWebhooks.length === 0) return;
  return sendWebhooks({ trigger: "link.clicked", webhooks: dbWebhooks, data });
}
```

---

### Issue 6 — Malicious link check only applied to `dub.sh` and `dub.link` (Security Gap)

**File:** `/apps/web/lib/api/links/process-link.ts` lines 166–196

**Why it's debt:**  
The `maliciousLinkCheck()` function that checks destination URLs against a blacklist is called only when `domain === "dub.sh" || domain === "dub.link"`:

```ts
if (domain === "dub.sh" || domain === "dub.link") {
  const isMaliciousLink = await maliciousLinkCheck(url);
  // ...
} else if (isDubDomain(domain)) {
  // NO malicious link check here
} else {
  // NO malicious link check here for custom domains either
}
```

A user with a custom domain (e.g., `company.link`) or any other Dub-owned domain can create links to malicious URLs without triggering the blacklist check. This means phishing/malware links can be distributed through verified custom domains.

**Fix:**
```ts
// Move the malicious link check outside the domain-specific conditionals
const isMaliciousLink = await maliciousLinkCheck(url);
if (isMaliciousLink) {
  return { link: payload, error: "Malicious URL detected", code: "unprocessable_entity" };
}
```

---

### Issue 7 — `ev.waitUntil` vs `waitUntil` inconsistency — deferred work may be lost (Lifecycle Gap)

**Files:**
- `/apps/web/lib/middleware/link.ts` — uses `ev.waitUntil()` (NextFetchEvent, middleware context)
- `/apps/web/lib/tinybird/record-click.ts` line 179 — uses `waitUntil()` from `@vercel/functions`

**Why it's debt:**  
`record-click.ts` is called from within middleware via `ev.waitUntil(recordClick(...))`. Inside `recordClick`, `waitUntil` from `@vercel/functions` is used to schedule additional background work (the Tinybird ingestion, Redis writes, DB updates). This creates **nested deferred work**: the outer `ev.waitUntil` may complete before the inner `waitUntil` callbacks are guaranteed to run.

In Vercel's edge runtime, `waitUntil` from `@vercel/functions` and the middleware's `NextFetchEvent.waitUntil` operate in different lifecycle scopes. Background work scheduled inside an already-deferred function is not guaranteed to complete before the outer scope closes.

Concretely: click events, Redis cache writes, and DB increments may be silently dropped when the edge function terminates.

**Fix:** Remove the inner `waitUntil` from `record-click.ts` and return the work as a plain Promise. Let the caller (`LinkMiddleware`) schedule it via `ev.waitUntil`:

```ts
// record-click.ts: return the Promise instead of using waitUntil internally
export async function recordClick({ ... }) {
  // ... validation and setup ...
  const clickWorkPromise = Promise.allSettled([
    fetchWithRetry(...),
    recordClickCache.set(...),
    conn.execute("UPDATE Link SET clicks..."),
    // ...
  ]);
  return { clickData, workPromise: clickWorkPromise };
}

// In LinkMiddleware:
const { clickData, workPromise } = await recordClick({...});
ev.waitUntil(workPromise);
```

---

### Issue 8 — `bulkCreateLinks` with `skipDuplicates: true` silently drops links and skews `linksUsage` (Correctness Bug)

**File:** `/apps/web/lib/api/links/bulk-create-links.ts` lines 47–78

**Why it's debt:**  
`prisma.link.createMany({ skipDuplicates: true })` silently ignores links that conflict on the `domain+key` unique constraint. The subsequent `updateLinksUsage()` call at line 230 increments by `links.length` — the number requested — not the number actually created.

If a bulk create of 50 links has 10 duplicates, 40 are created but `linksUsage` is incremented by 50. This:
1. Causes users to hit their link limit earlier than they should
2. Inflates `linksUsage` for billing/quota purposes
3. Means the API response may include fewer links than requested, silently

**Fix:** Compare the count of actually-created links against `links.length` and only increment by the true count:

```ts
// After fetching createdLinksData:
const actuallyCreated = createdLinksData.length;

// Later in waitUntil:
updateLinksUsage({
  workspaceId: links[0].projectId!,
  increment: actuallyCreated,  // not links.length
})
```

---

### Issue 9 — Rate limit policy is fragmented and undocumented (Policy Gap)

**Files:**
- `/apps/web/lib/upstash/ratelimit-policy.ts` lines 8–10: `// TODO: Centralize rate limiting policies`
- `/apps/web/lib/auth/workspace.ts` lines 25–34 (inline `RATE_LIMIT_FOR_SESSIONS`)
- `/apps/web/lib/auth/options.ts` line 227: `ratelimit(5, "1 m")`
- `/apps/web/lib/api/utils.ts` line 33: `ratelimit()` (default 10/10s)
- `/apps/web/app/api/links/route.ts` line 60: `ratelimit(10, "1 d")`

**Why it's debt:**  
Rate limit configurations are scattered across at least 5 files with no single source of truth. Each call site invents its own window and limit. The `ratelimit-policy.ts` file exists explicitly to centralize this but has only one entry and a TODO comment. This means:

- No audit trail of what rate limits exist
- Inconsistent protection — some endpoints have limits, others don't
- Silent policy changes when one site is changed but not others
- No way to know if limits are appropriate without reading every call site

**Fix:** Finish the `RATE_LIMITS` constant in `ratelimit-policy.ts` and require all rate-limited code to reference it:

```ts
export const RATE_LIMITS = {
  api: { attempts: 600, window: "1 m" },
  analyticsApi: { attempts: 12, window: "1 s" },
  loginAttempts: { attempts: 5, window: "1 m" },
  anonymousLinkCreation: { attempts: 10, window: "1 d" },
  tokenLastUsed: { attempts: 1, window: "1 m" },
  // ...
} as const;
```

---

### Issue 10 — `console.log` of auth credentials in production (Security Gap)

**File:** `/apps/web/lib/auth/options.ts` lines 353, 563

**Why it's debt:**  
Two `console.log` calls exist in the authentication callbacks that log sensitive data to stdout in production:

```ts
// Line 353 — in signIn callback:
console.log({ user, account, profile });  // logs OAuth profile including tokens/emails

// Line 563 — in signIn event:
console.log("signIn", message);  // logs the full session message
```

On Vercel, `console.log` output goes to function logs, which are stored and potentially accessible via the Vercel dashboard or log drain integrations. OAuth `profile` objects may include email, name, avatar URL, and in some providers, OAuth access tokens. The `message` in the signIn event includes the user object with ID, email, and image.

This was likely left in during development. These are high-volume calls (every login triggers both).

**Fix:** Remove both lines entirely. If debugging is needed, use a proper logger with production-off guards:

```ts
// Remove:
console.log({ user, account, profile });
console.log("signIn", message);
```

---

## Secondary Observations (Not Full Issues)

### geolocation() called twice in link middleware
**File:** `/apps/web/lib/middleware/link.ts` lines 316–317  
`geolocation(req)` is called twice sequentially where it could be called once and stored. Minor performance issue at scale.

### `inFlightLinkLookups` Map is process-scoped (not shared)
**File:** `/apps/web/lib/planetscale/get-link-via-edge.ts` lines 50–76  
The in-flight deduplication Map works only within a single Node.js process/Fluid instance. Across multiple instances, duplicate DB queries still occur. Comment correctly notes this limitation but it's invisible to operators.

### Token cache expiry mismatch
**File:** `/apps/web/lib/auth/token-cache.ts` line 4  
Token cache has 24-hour TTL. If a token is revoked, it remains valid in cache for up to 24 hours. No cache invalidation is triggered on token deletion in the visible code paths.

### `@ts-ignore` count
37 TypeScript suppressions exist in `apps/web/lib`. Several are in auth-critical files (`options.ts` has 3). These hide type mismatches that could mask logic errors.

### `linksUsage` check uses `>=` but increment could overshoot
**File:** `/apps/web/lib/api/links/usage-checks.ts` line 23  
`throwIfLinksUsageExceeded` checks `>` (strictly over) but `throwIfLinksUsageExceeded` in the route checks only after the fact. The check is done before creation, but the actual increment is async (deferred via `waitUntil`). Under concurrent creation, two requests could both pass the limit check before either increments — a classic TOCTOU race condition.

---

## Prioritized Fix List

| Priority | Issue | Est. Effort |
|----------|-------|-------------|
| P0 | Remove `console.log` in signIn callback (auth credential leak) | 5 min |
| P0 | Decrement `linksUsage` on link delete (billing counter drift) | 30 min |
| P0 | Hash link passwords at rest and use timing-safe compare | 2 hours |
| P0 | Remove or gate `allowDangerousEmailAccountLinking` | 1 hour |
| P1 | Add DB fallback in `sendLinkClickWebhooks` on cache miss | 1 hour |
| P1 | Move malicious link check outside domain conditionals | 30 min |
| P1 | Send account-locked email to user | 1 hour |
| P1 | Fix nested `waitUntil` in `record-click.ts` | 2 hours |
| P2 | Fix `bulkCreateLinks` increment to use actual count | 1 hour |
| P2 | Centralize rate limit policy definitions | 2 hours |
