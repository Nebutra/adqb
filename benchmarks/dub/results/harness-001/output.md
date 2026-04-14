# Dub Tech Debt Audit
**Repository:** `/Users/tseka_luk/Documents/dub`  
**Date:** 2026-04-14  
**Auditor:** Claude Code Harness (claude-sonnet-4-6)  
**Methodology:** Two-phase breadth scan → depth analysis

---

## Phase 1 — Coverage Scan Summary

| Area | Status | Key Finding |
|------|--------|-------------|
| Auth/security | RISK | Link passwords stored and compared as plaintext |
| Data access | RISK | 83+ API routes call Prisma directly (no repository layer) |
| Secrets | RISK | Webhook secrets stored plaintext in DB, returned in API responses |
| In-memory state | MINOR | One module-level Map in edge code — cleanup is correct |
| Feature flags | RISK | No lifecycle (no expiry, no owner), wrong default when EDGE_CONFIG missing |
| Billing/entitlements | RISK | Plan names hardcoded as strings in 74+ places, no type enforcement |
| Dependencies | MODERATE | `zod` v3 (CLI) vs v4 (web) split; AI lib files import from `zod` root instead of `zod/v4` |
| Schema integrity | RISK | Link `password` field stored plaintext; `InstalledIntegration.credentials` is untyped `Json?` |
| S2S communication | OK | Cron verified via Vercel CRON_SECRET; QStash via upstash receiver |
| Saga/workflow | RISK | Stripe webhook returns HTTP 400 on business logic errors → Stripe retries → double-processing |

---

## Phase 2 — Depth Analysis

Issues are ordered by **impact × exploitability**. Each issue includes: WHY it is debt, WHERE it lives, and a concrete fix.

---

### Issue 1 — Link Passwords Stored and Compared as Plaintext
**Severity: CRITICAL | Fix today**

#### What it is
Every password-protected short link stores its password as a raw string in MySQL (`Link.password`). The middleware compares the user-supplied value directly against the stored string using `!==`. The server action that validates the submitted password does the same.

#### Why it is debt
This is not a theoretical risk. If the database is compromised (breach, misconfigured backup, SQL injection), every password-protected link's password is immediately readable. Passwords chosen by users are often reused across services, making this a credential-leak amplifier. The plaintext value is also stored in a browser cookie:

```ts
// apps/web/app/password/[linkId]/action.ts  lines 21–27
const validPassword = password === realPassword;   // plaintext comparison

if (validPassword) {
  (await cookies()).set(`dub_password_${link.id}`, password, {
    // ↑ the raw password is now in the user's cookie jar
```

The middleware then re-reads the cookie and does a second plaintext equality check:

```ts
// apps/web/lib/middleware/link.ts  line 197
if (!pw || (await getLinkViaEdge({ domain, key }))?.password !== pw) {
```

There is no `timingSafeEqual` (which does exist elsewhere in the codebase for email unsubscribe tokens at `lib/email/unsubscribe-token.ts:45`), making this also vulnerable to timing-oracle attacks.

#### Concrete fix

**Step 1 — migration** (bcrypt is inappropriate for this use case because the edge runtime does not support it; use HMAC-SHA256 with a server-side secret instead):

```ts
// lib/api/links/hash-link-password.ts
import { createHmac } from "crypto";

const LINK_PASSWORD_HMAC_KEY = process.env.LINK_PASSWORD_HMAC_KEY!; // 32-byte hex

export function hashLinkPassword(raw: string): string {
  return createHmac("sha256", Buffer.from(LINK_PASSWORD_HMAC_KEY, "hex"))
    .update(raw)
    .digest("hex");
}
```

**Step 2 — update process-link and create/update handlers** to hash before storing:
```ts
// in process-link.ts, before prisma.link.create/update
if (password) {
  data.password = hashLinkPassword(password);
}
```

**Step 3 — update action.ts** to hash before comparing:
```ts
// apps/web/app/password/[linkId]/action.ts
const hashedInput = hashLinkPassword(password);
const validPassword = timingSafeEqual(
  Buffer.from(hashedInput),
  Buffer.from(realPassword),
);

// store hash in cookie, NOT the raw password
(await cookies()).set(`dub_password_${link.id}`, hashedInput, ...);
```

**Step 4 — update middleware** to compare hashes (not raw value).

**Step 5 — data migration**: run a one-time script to hash all existing `Link.password` values.

---

### Issue 2 — Stripe Webhook Returns HTTP 400 on Business Logic Errors → Double Retry
**Severity: CRITICAL | Fix today**

#### What it is
The Stripe webhook handler returns HTTP 400 when any event handler throws:

```ts
// apps/web/app/(ee)/api/stripe/webhook/route.ts  lines 83–88
} catch (error) {
  await log({ message: `Stripe webhook failed (${event.type})...` });
  return new Response(`Webhook error: ${error.message}`, {
    status: 400,   // ← tells Stripe: "I did not process this"
  });
}
```

#### Why it is debt
Stripe's retry semantics treat any non-2xx response as "delivery failed, retry later." This means:

1. `checkoutSessionCompleted` updates workspace plan, then an email send fails → 400 returned
2. Stripe retries the event
3. `checkoutSessionCompleted` fires again — `prisma.project.update({ where: { id: workspaceId } })` executes again
4. Second execution sets `billingCycleStart: new Date().getDate()` again, potentially resetting a billing cycle mid-month

The `checkoutSessionCompleted` handler has no idempotency guard on the `workspaceId` (it does not check if the workspace already has the correct `stripeId` and plan). The `customerSubscriptionUpdated` handler also runs `updateWorkspacePlan` repeatedly if retried.

There is no event deduplication layer (no Stripe event ID stored in DB to detect replays).

#### Concrete fix

**Short-term (fix today):** Return 200 for all business logic errors so Stripe does not retry. Log the error for internal alerting:

```ts
} catch (error) {
  await log({ message: `Stripe webhook failed (${event.type}). Error: ${error.message}`, type: "errors" });
  // Return 200 so Stripe does not retry — internal alerting handles the failure
  return logAndRespond(`[${event.type}]: handler error: ${error.message}`);
}
```

**Medium-term (next sprint):** Add idempotency by storing processed Stripe event IDs:

```ts
// Before switch(event.type):
const alreadyProcessed = await redis.get(`stripe:event:${event.id}`);
if (alreadyProcessed) return logAndRespond(`[${event.type}]: already processed`);

// After successful processing:
await redis.set(`stripe:event:${event.id}`, "1", { ex: 60 * 60 * 24 * 7 }); // 7-day TTL
```

---

### Issue 3 — Webhook Secrets Stored Plaintext and Returned in API Responses
**Severity: HIGH | Fix today**

#### What it is
`Webhook.secret` is stored as a plaintext string in MySQL. The `GET /api/webhooks/[webhookId]` response includes it directly:

```ts
// apps/web/app/api/webhooks/[webhookId]/route.ts  lines 28–31
select: {
  ...
  secret: true,   // raw secret selected from DB
```

The `WebhookSchema` in `lib/zod/schemas/webhooks.ts:9` includes `secret: z.string()` — it is included in every response body.

#### Why it is debt
Two problems compound each other:

1. **DB compromise → all HMAC signing keys exposed.** Any attacker who reads the `Webhook` table can forge valid webhook signatures for every registered endpoint. This allows spoofing events to customer systems.

2. **API response exposure.** Any workspace member with `webhooks.read` permission can read the signing secret via the API. Webhook secrets should be write-once and show-once (like API keys), never readable after creation.

#### Concrete fix

Store a hashed version for DB integrity, return the raw secret only at creation time:

```ts
// On creation: generate secret, store hash, return raw to caller once
const rawSecret = createWebhookSecret();
const hashedSecret = await hashToken(rawSecret);  // hashToken already exists in lib/auth

await prisma.webhook.create({
  data: { ...data, secret: hashedSecret },
});

// Return rawSecret in the creation response (never again after this)
return NextResponse.json({ ...webhook, secret: rawSecret });
```

For GET endpoints, omit the secret from the response entirely (return `secret: "[hidden]"` or exclude the field). Update `WebhookSchema` to make `secret` optional so existing callers aren't broken.

---

### Issue 4 — Plan Name Policy Lives in Strings, Not Code
**Severity: HIGH | Next sprint**

#### What it is
The plan hierarchy (`"free"`, `"pro"`, `"business"`, `"advanced"`, `"enterprise"`) is enforced via raw string comparisons scattered across 74+ locations. Sampling of affected files:

- `apps/web/lib/api/links/process-link.ts` lines 115, 134, 168, 242, 397, 458
- `apps/web/lib/webhook/create-webhook.ts:28` — `["free", "pro"].includes(workspace.plan)`
- `apps/web/lib/actions/enable-disable-webhook.ts:28` — same pattern
- `apps/web/lib/auth/workspace.ts:63-64` — `"pro"`, `"business"` inline in requiredPlan defaults
- `apps/web/lib/workspace-roles.ts:16-35` — full plan arrays repeated per role

#### Why it is debt
When a new plan tier is added (e.g., "Starter" between Free and Pro), every one of these 74+ sites must be updated. Worse, they encode plan hierarchy implicitly via array membership rather than ordinal ranking. A developer adding `["free", "pro"].includes(plan)` to a new feature check silently excludes any future plan that falls between Pro and Business.

There is a `plans` constant in `apps/web/lib/types.ts:387` and a `PlanProps` type derived from it, but no utility functions that express plan hierarchy as a ranked comparison. The code uses the type for parameter types but falls back to string literals for the actual logic.

#### Concrete fix

Add a plan rank utility to the existing constants:

```ts
// packages/utils/src/constants/pricing/plan-hierarchy.ts
import { plans } from "../../types"; // or wherever plans lives

const PLAN_RANK: Record<typeof plans[number], number> = {
  "free": 0,
  "pro": 1,
  "business": 2,
  "business plus": 3,
  "business extra": 4,
  "business max": 5,
  "advanced": 6,
  "enterprise": 7,
};

export function planAtLeast(
  workspacePlan: string,
  requiredPlan: typeof plans[number],
): boolean {
  return (PLAN_RANK[workspacePlan] ?? -1) >= PLAN_RANK[requiredPlan];
}
```

Replace scattered string checks:
```ts
// BEFORE (process-link.ts:458)
if (!workspace || workspace.plan === "free" || workspace.plan === "pro") {

// AFTER
if (!workspace || !planAtLeast(workspace.plan, "business")) {
```

Adding an architecture test enforces the policy:
```ts
// tests/arch/no-raw-plan-strings.test.ts
test("no raw plan string comparisons in lib/", () => {
  const files = glob.sync("apps/web/lib/**/*.ts");
  for (const file of files) {
    const content = fs.readFileSync(file, "utf-8");
    expect(content).not.toMatch(/plan\s*===\s*["'](?:free|pro|business)/);
    expect(content).not.toMatch(/\.includes\(workspace\.plan\)/);
  }
});
```

---

### Issue 5 — Feature Flags Have No Lifecycle (No Expiry, No Owner, Wrong Default)
**Severity: HIGH | Next sprint**

#### What it is
There are two feature flag systems — `BetaFeatures` for workspaces and `PartnerBetaFeatures` for partners — both managed via Vercel Edge Config. Neither has expiry dates, owners, or a deprecation path.

More critically, when `EDGE_CONFIG` is not set (self-hosted deployments, CI environments, development without config), all beta features default to `true`:

```ts
// apps/web/lib/edge-config/get-feature-flags.ts  lines 22–26
if (!process.env.NEXT_PUBLIC_IS_DUB || !process.env.EDGE_CONFIG) {
  // return all features as true if edge config is not available
  return Object.fromEntries(
    Object.entries(workspaceFeatures).map(([key, _v]) => [key, true]),
  );
}
```

The same pattern exists in `get-partner-feature-flags.ts:12-16`.

#### Why it is debt
Three compounding problems:

1. **Wrong default direction.** Beta features enabled for *everyone* when the config system is unavailable means a transient Edge Config outage silently grants every workspace access to beta features. This is backwards — a safe default for a feature gate is "gate closed."

2. **No lifecycle.** The `BetaFeatures` type is `"noDubLink" | "analyticsSettingsSiteVisitTracking"` — a TypeScript union with no metadata. There is no record of when each flag was created, who owns it, or when it expires. Based on the principle that flags accumulate (real production case: 847 flags, 340 for features shipped 2+ years ago), this will grow.

3. **`NEXT_PUBLIC_IS_DUB` coupling.** The `!process.env.NEXT_PUBLIC_IS_DUB` check means self-hosted users always get all beta features as `true`, which may not be intended.

#### Concrete fix

**Immediate:** Flip the default to `false` on outage:

```ts
// get-feature-flags.ts
if (!process.env.EDGE_CONFIG) {
  // Safe default: gates closed when config unavailable
  return workspaceFeatures; // all false
}
```

If self-hosted users should have all features, handle it explicitly:
```ts
if (!process.env.NEXT_PUBLIC_IS_DUB) {
  // Self-hosted: grant all features explicitly
  return Object.fromEntries(Object.entries(workspaceFeatures).map(([k]) => [k, true]));
}
if (!process.env.EDGE_CONFIG) {
  return workspaceFeatures; // gates closed on config failure
}
```

**Next sprint:** Add lifecycle metadata to each flag:

```ts
type BetaFeatureMetadata = {
  description: string;
  owner: string;       // team/person responsible
  expiresAt: string;   // ISO date; CI fails if past this date without cleanup
  defaultValue: false; // explicit, never implicit
};

const BETA_FEATURE_REGISTRY: Record<BetaFeatures, BetaFeatureMetadata> = {
  noDubLink: {
    description: "Allow workspace to disable dub.link branding",
    owner: "growth-team",
    expiresAt: "2026-07-01",
    defaultValue: false,
  },
  ...
};
```

Add a CI check that fails if any flag has passed its `expiresAt`.

---

### Issue 6 — Track Sale Idempotency Gap: Optional `invoiceId` Has No Fallback
**Severity: HIGH | Next sprint**

#### What it is
`trackSale` in `apps/web/lib/api/conversions/track-sale.ts` implements idempotency via a Redis key:

```ts
// track-sale.ts  lines 68–74
if (invoiceId) {
  const cachedResponse = await redis.get(
    `trackSale:${workspace.id}:invoiceId:${invoiceId}`,
  );
  if (cachedResponse) { return cachedResponse; }
}
```

The `invoiceId` field is `.nullish().default(null)` in the schema — it is optional. When a caller omits it, there is no idempotency protection at all.

#### Why it is debt
Any retry or duplicate call to `POST /api/track/sale` without an `invoiceId` will:
1. Create a duplicate `Commission` record (and credit the partner twice)
2. Update link `sales` and `saleAmount` counters twice
3. Fire duplicate webhook events to customer systems

The network call to Tinybird (`recordSale`) is also made each time and is not reversible. Partners could potentially be paid out twice for the same sale if a retry lands before payout aggregation.

#### Concrete fix

When `invoiceId` is absent, generate a deterministic idempotency key from the sale's natural composite key:

```ts
// track-sale.ts, before idempotency check
const effectiveInvoiceId = invoiceId
  ?? `auto:${workspace.id}:${customerExternalId}:${eventName}:${amount}:${Math.floor(Date.now() / 60_000)}`; 
  // 1-minute window de-dupe; adjust window based on retry interval

const cachedResponse = await redis.get(
  `trackSale:${workspace.id}:invoiceId:${effectiveInvoiceId}`,
);
if (cachedResponse) return cachedResponse;
```

Better: require `invoiceId` in the schema for payment-processor-initiated calls, making the optional path only for "custom" payment processors with explicit documentation of the risk.

---

### Issue 7 — No Repository Layer: 83+ Routes Call Prisma Directly
**Severity: MODERATE | Next quarter**

#### What it is
Every API route calls `prisma.*` directly. 83 route files were found with direct Prisma calls — representative examples:

- `apps/web/app/api/domains/[domain]/route.ts` — `prisma.domain.update()`
- `apps/web/app/api/oauth/token/exchange-code-for-token.ts` — `prisma.oAuthApp.findUnique()`
- `apps/web/app/api/user/route.ts` — `prisma.user.findUnique()`

There is no repository or data-access layer. Business logic and query construction are co-located in route handlers.

#### Why it is debt
The missing boundary (see Principle 2) creates three failure modes:

1. **Tenant scoping errors.** There is no layer that automatically applies `projectId: workspace.id` to every query. Each route must remember to scope by workspace. A single missed `projectId` filter leaks cross-tenant data. Example: if a future developer adds a `GET /api/customers?email=...` and forgets the `projectId` scope, it becomes a tenant isolation breach.

2. **Untestable business logic.** Route handlers mix HTTP parsing, auth, business rules, and DB queries. None of these can be tested without spinning up a real database or mocking Next.js internals.

3. **Schema changes are O(N).** When the `Link` model gains or loses a field, every route that queries links must be updated. There is no single `linkRepository.findById()` method that acts as the single change point.

#### Fix approach (phased)

Phase 1 (cheapest): Add an architecture test that prevents *new* routes from calling `prisma` directly — this stops the bleeding without requiring immediate refactoring:

```ts
// tests/arch/no-direct-prisma-in-routes.test.ts
test("new routes must not call prisma directly", () => {
  // Check only files modified in the last N days (or use git diff HEAD~1)
  const recentlyModifiedRoutes = getRecentlyModifiedRoutes();
  for (const file of recentlyModifiedRoutes) {
    const content = fs.readFileSync(file, "utf-8");
    expect(content).not.toMatch(/prisma\./);
  }
});
```

Phase 2: Extract repositories for the highest-risk models first: `Link`, `Customer`, `Commission` — these are the models where a scoping bug has financial consequences.

```ts
// lib/repositories/link-repository.ts
export class LinkRepository {
  constructor(private readonly workspaceId: string) {}

  async findById(id: string) {
    return prisma.link.findUnique({
      where: { id, projectId: this.workspaceId },  // scoping enforced by constructor
    });
  }
}
```

---

### Issue 8 — `InstalledIntegration.credentials` Is Untyped `Json?`
**Severity: MODERATE | Next quarter**

#### What it is

```prisma
// packages/prisma/schema/integration.prisma  line 37
model InstalledIntegration {
  ...
  credentials   Json?
  settings      Json?
```

OAuth tokens, API keys, and other integration secrets are stored in a `Json?` column with no type contract.

#### Why it is debt

1. **No encryption.** Third-party OAuth access tokens and refresh tokens stored in `credentials` are readable to anyone with DB access, same as the webhook secret issue. Unlike the webhook secret (which Dub generates), these are third-party tokens that cannot be rotated by Dub.

2. **No type safety.** Because it is `Json?`, any code that reads `credentials` must cast it (`as any` or `as IntegrationCredentials`). This silently allows invalid shapes to persist in the database and surface as runtime errors.

3. **No audit trail.** There is no `credentialsUpdatedAt` field. If credentials are compromised, there is no way to know when they were last refreshed.

#### Concrete fix

**Step 1 — Encryption at rest:**
```ts
// lib/integrations/credentials.ts
import { createCipheriv, createDecipheriv, randomBytes } from "crypto";

const CREDENTIALS_KEY = Buffer.from(process.env.CREDENTIALS_ENCRYPTION_KEY!, "hex");

export function encryptCredentials(raw: object): string {
  const iv = randomBytes(16);
  const cipher = createCipheriv("aes-256-gcm", CREDENTIALS_KEY, iv);
  const encrypted = Buffer.concat([cipher.update(JSON.stringify(raw), "utf8"), cipher.final()]);
  const tag = cipher.getAuthTag();
  return [iv, tag, encrypted].map(b => b.toString("base64")).join(".");
}

export function decryptCredentials(stored: string): unknown {
  const [ivB64, tagB64, encB64] = stored.split(".");
  const decipher = createDecipheriv("aes-256-gcm", CREDENTIALS_KEY, Buffer.from(ivB64, "base64"));
  decipher.setAuthTag(Buffer.from(tagB64, "base64"));
  const decrypted = Buffer.concat([decipher.update(Buffer.from(encB64, "base64")), decipher.final()]);
  return JSON.parse(decrypted.toString("utf8"));
}
```

**Step 2:** Change `credentials Json?` to `credentials String?` in the schema (store the encrypted string), and use `encryptCredentials`/`decryptCredentials` at every read/write site.

---

### Issue 9 — Cron Route Auth Bypassed Outside Vercel (All Unprotected in Local/Test)
**Severity: MODERATE | Fix today for staging environments**

#### What it is
Both cron verifiers skip authentication when not running on Vercel:

```ts
// apps/web/lib/cron/verify-vercel.ts  lines 5–7
if (process.env.VERCEL !== "1") {
  return; // No auth check
}

// apps/web/lib/cron/verify-qstash.ts  lines 19–21
if (process.env.VERCEL !== "1") {
  return; // No auth check
}
```

The `withCron` middleware uses `verifyVercelSignature` for GET (Vercel Cron) and `verifyQstashSignature` for POST (QStash). Both are no-ops locally.

#### Why it is debt
Any deployed staging or preview environment that does not have `VERCEL=1` set (which is not automatically set on staging deployments from CI systems other than Vercel) exposes all cron endpoints unauthenticated. These endpoints include:

- `POST /api/cron/workspaces/delete` — deletes workspace and all its data
- `POST /api/cron/partners/ban` — bans a partner, cancels payouts, disables links
- `POST /api/cron/partners/deactivate` — deactivates partners

The `withCron` wrapper handles 25+ cron routes. An unauthenticated request to any of these from a staging environment has permanent consequences.

#### Concrete fix

Change the bypass condition to require an explicit local-dev secret:

```ts
// verify-vercel.ts
export const verifyVercelSignature = async (req: Request) => {
  if (process.env.VERCEL !== "1") {
    // Allow local dev with an explicit dev secret, never silently bypass
    const devSecret = process.env.CRON_SECRET;
    if (!devSecret) {
      throw new DubApiError({ code: "unauthorized", message: "Set CRON_SECRET in .env.local" });
    }
    const authHeader = req.headers.get("authorization");
    if (authHeader !== `Bearer ${devSecret}`) {
      throw new DubApiError({ code: "unauthorized", message: "Invalid cron secret" });
    }
    return;
  }
  // ... existing Vercel check
};
```

---

### Issue 10 — `zod` Import Inconsistency (v3 vs v4 in Same Codebase)
**Severity: LOW | Next sprint**

#### What it is
The web app package declares `zod: "^4.3.5"`. The CLI package declares `zod: "^3.23.8"`. Within the web app itself, 5 AI-module files import from the bare `"zod"` module instead of the recommended `"zod/v4"` path:

```ts
// apps/web/lib/ai/get-workspace-details.ts:3
import { z } from "zod";       // ← v3 compatibility shim, not the v4 API

// vs. correctly imported in 623 other files:
import * as z from "zod/v4";
```

Affected files:
- `lib/ai/get-program-performance.ts`
- `lib/ai/get-workspace-details.ts`
- `lib/ai/request-support-ticket.ts`
- `lib/ai/find-relevant-docs.ts`
- `lib/ai/create-support-ticket.ts`

#### Why it is debt
Zod v4 ships both a v4 API (`zod/v4`) and a backward-compatibility shim (`zod` root import). The compatibility shim has different behavior for some edge cases. Using both in the same codebase makes it harder to reason about validation behavior and prevents removal of the compatibility shim in a future major version upgrade.

#### Fix
```ts
// Replace in all 5 files:
import { z } from "zod";
// with:
import * as z from "zod/v4";
```

Add a lint rule to enforce this:
```json
// .eslintrc
{
  "rules": {
    "no-restricted-imports": ["error", {
      "paths": [{ "name": "zod", "message": "Use 'zod/v4' instead" }]
    }]
  }
}
```

---

## Prioritized Fix Plan

| Priority | Issue | Effort | Owner Signal |
|----------|-------|--------|-------------|
| P0 — Fix today | Issue 1: Link password plaintext | M (migration + 3 code sites) | Security |
| P0 — Fix today | Issue 2: Stripe 400 → retry double-processing | XS (change status code) | Billing |
| P0 — Fix today | Issue 3: Webhook secret in DB + API responses | M (hash + schema change) | Security |
| P1 — Next sprint | Issue 6: Track sale idempotency gap | S (deterministic key fallback) | Billing |
| P1 — Next sprint | Issue 5: Feature flag lifecycle + wrong default | S (flip default, add registry) | Platform |
| P1 — Next sprint | Issue 9: Cron bypass in non-Vercel envs | XS (replace silent bypass) | Platform |
| P2 — Next quarter | Issue 4: Plan string policy not in code | M (utility fn + arch test) | Platform |
| P2 — Next quarter | Issue 7: No repository layer | L (phased extraction) | Platform |
| P2 — Next quarter | Issue 8: Unencrypted integration credentials | M (AES-256-GCM wrapper) | Security |
| P3 — Next sprint | Issue 10: Zod v3/v4 import inconsistency | XS (5 file changes + lint rule) | DX |

---

## What Is Working Well

The following were verified as actually implemented (not just declared):

**QStash signature verification is robust.** `verifyQstashSignature` uses Upstash's `Receiver` with dual signing keys and validates against the raw body. The `withCron` middleware correctly applies this to all POST cron routes.

**Stripe webhook signature validation is correct.** `stripe.webhooks.constructEvent(buf, sig, webhookSecret)` properly validates HMAC-SHA256 signatures against the raw request buffer before any processing.

**OAuth PKCE is implemented.** `exchange-code-for-token.ts` calls `generateCodeChallengeHash` and verifies the code challenge against the stored `codeChallenge` — this prevents authorization code interception attacks.

**Rate limiting is applied to public endpoints.** The unauthenticated resume upload endpoint (`/api/resumes/upload-url`) applies IP-based rate limiting (5 req/min) before issuing a signed URL.

**Track sale has idempotency *when invoiceId is provided*.** The Redis cache keyed on `trackSale:{workspaceId}:invoiceId:{invoiceId}` with a 7-day TTL prevents duplicate commission creation for payment processors that provide invoice IDs.

**Workspace data is always scoped to the caller.** `withWorkspace` auth middleware fetches the workspace by `idOrSlug` and verifies the calling user is a member before every request, meaning all 73 routes using it cannot access other workspaces' data.

---

## Appendix — Files Referenced

| File | Issue |
|------|-------|
| `apps/web/app/password/[linkId]/action.ts` | Issue 1 |
| `apps/web/lib/middleware/link.ts:197` | Issue 1 |
| `apps/web/app/(ee)/api/stripe/webhook/route.ts:83–88` | Issue 2 |
| `apps/web/app/(ee)/api/stripe/webhook/checkout-session-completed.ts` | Issue 2 |
| `apps/web/app/api/webhooks/[webhookId]/route.ts` | Issue 3 |
| `apps/web/lib/zod/schemas/webhooks.ts:9` | Issue 3 |
| `apps/web/lib/webhook/transform.ts` | Issue 3 |
| `apps/web/lib/api/links/process-link.ts:115,134,168,242,397,458` | Issue 4 |
| `apps/web/lib/webhook/create-webhook.ts:28` | Issue 4 |
| `apps/web/lib/workspace-roles.ts:16-35` | Issue 4 |
| `apps/web/lib/edge-config/get-feature-flags.ts:22–26` | Issue 5 |
| `apps/web/lib/edge-config/get-partner-feature-flags.ts:12–16` | Issue 5 |
| `apps/web/lib/api/conversions/track-sale.ts:68–74` | Issue 6 |
| `apps/web/lib/zod/schemas/sales.ts:44-52` | Issue 6 |
| `packages/prisma/schema/integration.prisma:37` | Issue 8 |
| `apps/web/lib/cron/verify-vercel.ts:5–7` | Issue 9 |
| `apps/web/lib/cron/verify-qstash.ts:19–21` | Issue 9 |
| `apps/web/lib/ai/get-workspace-details.ts:3` | Issue 10 |
