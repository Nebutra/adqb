# Nebutra Sailor — Tech Debt Audit Report

**Date:** 2026-04-14  
**Auditor:** Claude Code Harness (claude-sonnet-4-6)  
**Scope:** Full architecture audit — security, lifecycle, boundary violations, policy gaps, correctness bugs  
**Stack:** Next.js 16 / React 19 / Hono / Prisma 7 / Python FastAPI / pnpm Turbo monorepo  
**Schema size:** 59 Prisma models, 11 API route groups, 7 middlewares, 55+ packages

---

## Executive Summary

The codebase is well-structured at the macro level: graceful shutdown is wired, webhook idempotency is implemented, circuit breakers exist on outbound calls, and architecture tests enforce versioned routes. The tech debt that does exist is concentrated in three areas:

1. **Security gaps** — credential plaintext storage, unprotected internal services, and a broken S2S guard
2. **Lifecycle gaps** — feature flags with no expiry path, billing entitlements backed by process-local memory that disappears on restart, and a Stripe billing sync pipeline that is wired up but permanently unreachable
3. **Correctness bugs** — the rate limiter silently uses per-process memory in multi-instance deploys, the order saga accepts mock payments without charging anything, and entitlement enforcement is never actually invoked on any route

The section below is organized by impact. Items marked **FIX TODAY** will cause data loss, revenue loss, or security breaches in production. Items marked **NEXT SPRINT** are architectural risk that compounds over time.

---

## What Is Working Well

Before the debt: several patterns here are unusually solid for a pre-launch SaaS.

- **Graceful shutdown** (`apps/api-gateway/src/index.ts` lines 225–240): SIGTERM/SIGINT handlers close the HTTP server, wait, then disconnect Prisma. Forces exit after 10 s. This is rare to see done correctly.
- **Webhook idempotency**: The `idempotencyMiddleware` uses Redis SET NX locking, 24-hour TTL caching, and only caches 2xx/4xx (never 5xx). Follows the Stripe convention correctly.
- **S2S HMAC design**: `verifyServiceToken` in `tenantContextMiddleware.ts` uses `timingSafeEqual` from Node crypto — correct resistance to timing attacks.
- **Circuit breakers on outbound calls**: `aiServiceBreaker` and `billingServiceBreaker` wrap all outbound fetches. Implemented as CLOSED → OPEN → HALF_OPEN with configurable thresholds.
- **Architecture tests**: `tests/architecture/` enforces API route versioning, dependency boundaries, and OpenAPI spec existence via code — not a wiki.
- **GDPR deletion workflow**: Inngest `processGdprDeletion` covers PII anonymization, API key revocation, and downstream notification. Audit log pseudonymization is semantically correct (SHA-256 hash preserves attributability without storing PII).
- **Prisma multi-schema**: `ecommerce`, `recsys`, `web3` schemas map to distinct domains. Clean separation.

---

## Critical Issues (FIX TODAY)

### 1. Integration Credentials Stored as Plaintext in PostgreSQL

**File:** `apps/api-gateway/src/routes/integrations/index.ts`, lines 133–154  
**Schema:** `packages/db/prisma/schema.prisma`, line 307

**The debt:**
```prisma
// schema.prisma line 307
credentials    Json            @default("{}") // Encrypt in app layer
```

The comment "Encrypt in app layer" exists. The encryption does not. The `createRoute_` handler writes credentials directly to Postgres with no transformation:

```typescript
// integrations/index.ts line 139-145
const integration = await prisma.integration.create({
  data: {
    organizationId: orgId,
    type: body.type,
    name: body.name,
    credentials: (body.credentials ?? {}) as any,  // plaintext Shopify API key, Stripe key, etc.
    settings: (body.settings ?? {}) as any,
  },
```

The `updateRoute` handler has the same pattern at line 210: `credentials: body.credentials as any`.

A `@nebutra/vault` package exists (`packages/vault/src/`) with full AES envelope encryption over AWS KMS or HKDF local key. It is imported nowhere in the API gateway.

**Why it's debt:** Shopify, Stripe, and custom API keys stored as JSON columns are readable by anyone with database access. A single leaked `pg_dump` or compromised read replica exposes all tenant third-party credentials. The vault exists; it is just not connected.

**Fix:**

```typescript
// integrations/index.ts — create handler
import { getVault } from "@nebutra/vault";

const vault = await getVault();
const encryptedCredentials = credentials
  ? await vault.encrypt(JSON.stringify(credentials))
  : null;

await prisma.integration.create({
  data: {
    ...
    credentials: encryptedCredentials
      ? JSON.stringify(encryptedCredentials)
      : "{}",
  },
});
```

Decrypt on read (only for the owning tenant):
```typescript
// GET /:id — only when the credential is needed for an operation, never in list responses
const vault = await getVault();
const decryptedCredentials = integration.credentials
  ? JSON.parse(await vault.decrypt(JSON.parse(integration.credentials as string)))
  : {};
```

This also applies to `updateRoute` — any credential update should re-encrypt.

---

### 2. Rate Limiter Uses Per-Process In-Memory Storage in a Multi-Instance Deployment

**File:** `packages/rate-limit/src/tokenBucket.ts`, line `export function getRateLimiter`

**The debt:**
```typescript
// tokenBucket.ts
const rateLimiters: Map<string, TokenBucket> = new Map();

export function getRateLimiter(plan: string): TokenBucket {
  if (!rateLimiters.has(plan)) {
    rateLimiters.set(plan, createRateLimiter(plan));
  }
  return rateLimiters.get(plan) as TokenBucket;
}
```

`TokenBucket` stores state in a process-local `Map`. `getRateLimiter` is what `rateLimitMiddleware` in `apps/api-gateway/src/middlewares/rateLimit.ts` calls. With two instances of `api-gateway` running (any Kubernetes or serverless deployment), each instance grants the full quota. A FREE tenant with a 100-token limit effectively gets `100 × N` tokens where N is replica count.

The `RedisTokenBucket` class exists in the same file (line 175) with a compatible interface. It is wired correctly with `buildKey()` namespacing. It is simply not what `getRateLimiter()` returns.

**Why it's debt:** The rate limit is the primary financial and abuse protection for a metered SaaS. Silent bypass makes the metering and billing meaningless once you scale beyond one process. The fix is a one-function swap.

**Fix:**
```typescript
// tokenBucket.ts — replace getRateLimiter
import { getRedis } from "@nebutra/cache";

const rateLimiters: Map<string, RedisTokenBucket> = new Map();

export function getRateLimiter(plan: string): RedisTokenBucket {
  if (!rateLimiters.has(plan)) {
    const config = plan === "FREE"
      ? PLAN_LIMITS.FREE
      : plan === "PRO"
      ? PLAN_LIMITS.PRO
      : PLAN_LIMITS.ENTERPRISE;
    rateLimiters.set(plan, createRedisRateLimiter(config, getRedis()));
  }
  return rateLimiters.get(plan)!;
}
```

`createRedisRateLimiter` already exists and works. This is a 5-line change.

---

### 3. Entitlement Enforcement Is Never Called on Any Route

**File:** `apps/api-gateway/src/middlewares/entitlements.ts`  
**File:** `apps/api-gateway/src/routes/ai/index.ts`

**The debt:**

The comment in `entitlements.ts` (line 10) shows the intended usage:
```typescript
// app.post("/api/v1/ai/generate", requireFeature("ai.images", 1), ...)
```

Searching the entire codebase for actual `requireFeature(` calls finds exactly one result: the comment in `entitlements.ts` itself. The middleware is exported but never applied to any route.

Additionally, `requireEntitlement` calls `checkEntitlement` which reads from an in-memory `Map<string, Entitlement[]>`. That map is populated only by `initializePlanEntitlements`. That function is never called from any non-test path either. Search result:

```
packages/billing/src/entitlements/index.ts:11 (export)
packages/billing/src/entitlements/service.ts:283 (definition)
packages/billing/src/index.ts:88 (re-export)
apps/api-gateway/src/middlewares/entitlements.ts:24 (comment)
```

**Why it's debt:** Every tenant, regardless of plan, has unrestricted access to every feature including AI image generation, Web3 NFT minting, audit logs, and SSO. This means the billing system cannot enforce the feature gates that the pricing tiers define. Revenue is miscounted and plan upgrades have no effect.

**Fix — in two parts:**

Part 1: Initialize entitlements on session/request resolution:
```typescript
// tenantContextMiddleware.ts — after plan is resolved from JWT
import { initializePlanEntitlements } from "@nebutra/billing";

if (tenant.organizationId && tenant.plan) {
  // Only initialize once per org per process restart; check memory cache first
  const existing = getEntitlements(tenant.organizationId);
  if (existing.length === 0) {
    initializePlanEntitlements(tenant.organizationId, tenant.plan as Plan);
  }
}
```

Part 2: Apply `requireFeature` to guarded routes:
```typescript
// routes/ai/index.ts
import { requireFeature } from "../../middlewares/entitlements.js";

// Chat
aiRoutes.use("/chat", requireFeature("ai.chat"));
// Embeddings (PRO+)
aiRoutes.use("/embeddings", requireFeature("ai.embeddings"));
```

Note: for production, the in-memory entitlements store must be replaced with a Redis or DB-backed read (see Issue 5).

---

### 4. S2S HMAC Guard Silently Disables Itself in Dev Mode with an Empty String

**File:** `apps/api-gateway/src/middlewares/tenantContext.ts`, lines 53–66

**The debt:**
```typescript
function verifyServiceToken(...): boolean {
  const secret = process.env.SERVICE_SECRET;

  if (!secret) {
    if (!serviceSecretWarningLogged) {
      serviceSecretWarningLogged = true;
      logger.warn("SERVICE_SECRET is not set — S2S header verification is disabled (dev mode fallback)");
    }
    // Dev mode: allow headers without verification
    return true;   // <-- unconditional trust when secret is missing
  }
  ...
}
```

And in `packages/saga/src/workflows/orderSaga.ts` (line 24):
```typescript
const SERVICE_SECRET = process.env.SERVICE_SECRET || "";
```

If `SERVICE_SECRET` is set to an empty string `""` — which is the default in many `.env.example` files — `process.env.SERVICE_SECRET` returns `""`, which is truthy for the `if (!secret)` check? No: `""` is falsy. This means an empty string disables HMAC. But the gateway's `verifyServiceToken` returns `true` in that case: **any caller who sends the headers `x-user-id`, `x-organization-id`, `x-role`, `x-plan` with any `x-service-token` value will have their headers trusted when `SERVICE_SECRET=""` in production.**

The saga's `gatewayFetch` sends `"x-service-token": SERVICE_SECRET` — when `SERVICE_SECRET=""`, this sends an empty string as the service token. The gateway receives `serviceToken = ""` (truthy for `if (serviceToken)` at line 116 of `tenantContext.ts`), then calls `verifyServiceToken("", ...)` which returns `true` because `!secret` is true for `""`. The empty token passes HMAC verification.

**Why it's debt:** Any caller who knows the API gateway URL can forge arbitrary tenant identity by setting `SERVICE_SECRET=` (empty) and sending `x-service-token: ` (empty) alongside any `x-user-id` they choose. This bypasses authentication entirely.

**Fix:** Treat empty string as absent:
```typescript
// tenantContext.ts
const secret = process.env.SERVICE_SECRET?.trim() || null;

if (!secret) {
  if (process.env.NODE_ENV === "production") {
    // Hard fail in production — missing SERVICE_SECRET is misconfiguration
    logger.error("SERVICE_SECRET is not set in production — S2S auth is broken");
    return false;  // reject, do not trust
  }
  // Dev mode only
  logger.warn("SERVICE_SECRET not set — S2S verification disabled (dev only)");
  return true;
}
```

---

## High Priority Issues (NEXT SPRINT)

### 5. Entitlements and Credits Are Process-Local Memory — Reset on Every Restart

**Files:**  
- `packages/billing/src/entitlements/service.ts` — `const entitlements: Map<string, Entitlement[]>`  
- `packages/billing/src/credits/service.ts` — `const creditBalances: Map<string, CreditBalance>`

Both comment "production would use database" on their in-memory stores. Both are called in production code paths:

- `checkAgentQuota` (agents/tenant.ts line 22) calls `getCreditBalance` to gate agent execution
- `emitUsage` in BaseAgent (agent.ts line 68) calls `deductCredits`
- `requireEntitlement` is called from `entitlements.ts` middleware (when used)

**Why it's debt:** Every deployment restart zeros all credit balances. A tenant that purchased 10,000 agent credits gets 0 credits after the next deployment. The `checkAgentQuota` failsafe (`return { allowed: true, remaining: -1 }` when billing is unconfigured) means agents always run when credits are absent — making the quota system permanently silent. This is the "zombie state" failure mode: the system appears to work but is not enforcing any economic constraints.

**Fix:** Replace the in-memory maps with a Redis-backed read-through cache backed by the existing `CreditLedger` / `UsageLedger` Prisma models that already exist in the schema.

```typescript
// credits/service.ts — getCreditBalance
export async function getCreditBalance(organizationId: string): Promise<CreditBalance> {
  const cached = await redis.get(`credits:balance:${organizationId}`);
  if (cached) return cached as CreditBalance;
  
  const ledger = await prisma.creditLedger.aggregate({
    where: { organizationId },
    _sum: { amount: true },
  });
  
  const balance = { organizationId, balance: Number(ledger._sum.amount ?? 0), currency: "USD" };
  await redis.set(`credits:balance:${organizationId}`, balance, { ex: 300 }); // 5-min TTL
  return balance;
}
```

---

### 6. Stripe Billing Sync Inngest Function Is Dead Code — Organization.plan Never Updates

**Files:**  
- `apps/api-gateway/src/inngest/functions/billingSync.ts`  
- `apps/api-gateway/src/routes/webhooks/stripe.ts`

**The debt:** There are two code paths that claim to handle Stripe subscription events:

**Path A** — `stripe.ts` webhook handler. Receives Stripe events inline, updates `Subscription` and `Invoice` Prisma models, handles `handleSubscriptionUpdated`, `handleSubscriptionDeleted`, `handleInvoicePaid`, `handleInvoicePaymentFailed`. Does **not** fire any Inngest events. Does **not** update `Organization.plan`.

**Path B** — `billingSync.ts` Inngest function. Registers triggers for `"stripe/subscription.updated"` and `"stripe/subscription.deleted"`. When triggered, calls `orgRepo.updateById(organizationId, { plan: targetPlan })` to update `Organization.plan`. Is **never triggered** because nobody fires these Inngest events.

The result: the `Organization.plan` field is set to `FREE` at creation and never updated when a tenant upgrades or cancels their subscription. The billing system stores the subscription status correctly in the `Subscription` model, but the `plan` field that gates rate limits and feature entitlements is never changed.

**Why it's debt:** Tenant upgrades to PRO have no effect on their API rate limits (`PLAN_LIMITS.PRO` vs `PLAN_LIMITS.FREE`) or feature access. Cancellations don't downgrade the tenant. The subscription system is storing state that is permanently disconnected from the authorization system.

**Fix — choose one path and commit:**

Option A (simpler): Update `Organization.plan` directly inside `handleSubscriptionUpdated` in `stripe.ts`:
```typescript
// stripe.ts — handleSubscriptionUpdated
async function handleSubscriptionUpdated(sub: Stripe.Subscription, db: PrismaClient) {
  const status = mapStripeStatus(sub.status);
  await db.subscription.updateMany({ where: { stripeId: sub.id }, data: { status, ... } });

  // Also sync Organization.plan
  const stripeCustomer = await db.stripeCustomer.findUnique({
    where: { stripeId: sub.customer as string },
  });
  if (stripeCustomer) {
    const plan = resolvePlanFromStatus(sub.status);  // reuse billingSync logic
    await db.organization.update({
      where: { id: stripeCustomer.organizationId },
      data: { plan },
    });
  }
}
```

Option B (maintains the Inngest architecture): Add `inngest.send` calls at the end of each inline handler in `stripe.ts` to trigger `billingSync`:
```typescript
// After processing subscription.updated inline:
await inngest.send({
  name: "stripe/subscription.updated",
  data: { organizationId, status: sub.status, subscriptionId: sub.id },
});
```

Either way, delete the duplicate implementation once unified.

---

### 7. Order Saga Has a Silent Mock Payment Fallback That Silently Charges Nothing

**File:** `packages/saga/src/workflows/orderSaga.ts`, lines 79–100

**The debt:**
```typescript
const chargePayment: SagaStep<OrderContext> = {
  name: "charge_payment",
  async execute(ctx) {
    if (!ctx.customerId) {
      // Mock fallback if customerId isn't provided but we require payment
      // In real life, require customerId or fail.
      return { ...ctx, paymentId: `mock_pay_${Date.now()}` };  // ← silent fake payment
    }
    // Real Stripe charge only if customerId is present
    ...
  },
  async compensate(ctx) {
    if (ctx.paymentId && ctx.paymentId.startsWith("pi_")) {
      // Only refunds if payment ID starts with "pi_" (real Stripe intent)
      // mock_pay_* IDs are silently skipped during compensation
    }
  },
};
```

The comment says "In real life, require customerId or fail." The code does the opposite: it proceeds without failing, marks the order as paid, creates the order record, sends a confirmation email — all for a payment that was never collected.

The compensation function compounds this: it only calls `stripe.refunds.create` for IDs starting with `"pi_"`. A `mock_pay_*` ID during compensation silently skips the refund step. This means if the order record creation fails after a "mock" payment, there is nothing to compensate because the payment was never real.

**Why it's debt:** Any order placed by a user without a stored Stripe customer ID (new user, guest checkout, misconfigured account) results in inventory being reserved, an order confirmation email being sent, and an order record created — all with zero payment collected. This is a revenue loss bug that is entirely silent.

**Fix:** Remove the fallback entirely. Make missing `customerId` a hard failure:
```typescript
async execute(ctx) {
  if (!ctx.customerId) {
    throw new Error(
      `Cannot charge payment: no Stripe customer ID for order ${ctx.orderId}. ` +
      `Ensure the user has a Stripe customer before initiating checkout.`
    );
  }
  // ... real Stripe charge
}
```

---

### 8. GDPR Deletion Workflow Misses Redis-Stored PII

**File:** `apps/api-gateway/src/inngest/functions/gdprDeletion.ts`

**The debt:** The deletion workflow correctly anonymizes Postgres PII (user table, audit logs). It explicitly acknowledges the analytics purge as a stub:
```typescript
// Step 3
// Implementation depends on your analytics table schema.
// Example for a hypothetical AnalyticsEvent model:
// await prisma.analyticsEvent.deleteMany({ where: { userId } });
logger.info("Analytics PII purge step complete", { userId }); // <-- does nothing
```

But more critically, it does not clean up Redis-stored PII that the platform actually does populate:

- Agent conversation memory: `agent:memory:{tenantId}:{conversationId}` — stores full chat history including user messages (PII)
- Usage metrics: `usage:{orgId}:{period}:api_calls` — these are aggregate (safe) but could contain user-linked data
- Idempotency cache: `idempotency:{tenantId}:{key}` — cached response bodies may contain PII

**Why it's debt:** GDPR Article 17 (Right to Erasure) requires deleting personal data from all processing systems, including caches. Agent conversation histories stored in Redis under the user's tenant ID contain message content. These are not cleaned up by the current workflow.

**Fix:** Add a Redis cleanup step:
```typescript
// Step 3.5: Purge Redis PII
await step.run("purge-redis-pii", async () => {
  const { getRedis } = await import("@nebutra/cache");
  const redis = getRedis();
  
  for (const orgId of orgIds) {
    // Purge agent memory for all conversations in this tenant
    let cursor = "0";
    do {
      const [nextCursor, keys] = await redis.scan(cursor, {
        match: `agent:memory:${orgId}:*`,
        count: 100,
      });
      cursor = String(nextCursor);
      if (keys.length > 0) {
        await redis.del(...keys);
      }
    } while (cursor !== "0");
  }
  
  logger.info("Redis PII purged", { userId, organizationIds: orgIds });
});
```

---

## Medium Priority Issues (ARCHITECTURAL RISK)

### 9. Feature Flags Have No Lifecycle — They Accumulate Forever

**File:** `packages/db/prisma/schema.prisma`, lines 512–527

**The debt:**
```prisma
model FeatureFlag {
  id          String          @id @default(cuid())
  key         String          @unique @db.VarChar(100)
  name        String
  description String?
  type        FeatureFlagType @default(BOOLEAN)
  value       Json            @default("false")
  isEnabled   Boolean         @default(false) @map("is_enabled")
  createdAt   DateTime        @default(now()) @map("created_at")
  updatedAt   DateTime        @updatedAt @map("updated_at")
  // No: owner, expiresAt, deprecatedAt, linkedFeatureId, rolloutPercentage
}
```

The flag schema has no `expiresAt`, no `owner`, no `deprecatedAt`. The admin route stores flag overrides in a process-local `Map` (`apps/api-gateway/src/routes/admin/index.ts` — `const flagOverrides = new Map<string, Record<string, boolean>>()`), which also resets on restart.

**Why it's debt:** Without expiry paths, flags accumulate. A team that ships one feature per week using feature flags will accumulate 52 flags per year. Each flag is a code branch that every developer must reason about. Production systems that have operated for 2+ years routinely find hundreds of permanent flags — the referenced principle calls this out explicitly as the "feature graveyard" pattern.

**Fix — minimum viable lifecycle:**
```prisma
model FeatureFlag {
  // ... existing fields
  owner       String?   @db.VarChar(100)  // team/person responsible
  expiresAt   DateTime? @map("expires_at") // auto-archive date
  linkedIssue String?   @map("linked_issue") @db.VarChar(255) // PR/issue for tracking
}
```

Also: move `flagOverrides` from in-memory `Map` to the existing `FeatureFlagOverride` Prisma model.

---

### 10. Python AI Service Has No Service-to-Service Authentication

**Files:**  
- `services/ai/app/api/v1/routes_generate.py`  
- `services/ai/app/api/v1/routes_embed.py`  
- `services/ai/app/api/v1/routes_translate.py`  
- `services/ai/app/main.py`

**The debt:** The Python AI service accepts any HTTP request without authentication. The Hono gateway's `proxyToAiService` function sends `X-Tenant-ID` but the Python routes do not read or validate it:

```python
# routes_generate.py — no auth check anywhere
@router.post("/", response_model=GenerateResponse)
async def generate(request: GenerateRequest):
    result = await generate_text(...)
    return result
```

The AI service is described in `main.py` as "internal and should not be exposed directly to browsers." The `CORS` comment says gateway handles CORS. But if the service port is reachable on the same network, any process can call it without credentials and consume LLM API quota directly.

**Why it's debt:** The entire rate limiting, entitlement, and metering stack in the Node.js gateway is bypassed. Any internal service (or a container that escapes the network boundary) can call `POST /api/v1/generate` with arbitrary prompts and incur OpenAI costs attributed to the platform's API key without any quota tracking.

**Fix:** Add a shared-secret middleware to all Python services:

```python
# _shared/auth.py
import os
import hmac
from fastapi import Request, HTTPException

INTERNAL_API_KEY = os.getenv("INTERNAL_API_KEY")

async def require_internal_auth(request: Request):
    if not INTERNAL_API_KEY:
        if os.getenv("ENV") == "production":
            raise HTTPException(status_code=500, detail="INTERNAL_API_KEY not configured")
        return  # dev fallback
    
    key = request.headers.get("X-Internal-Key")
    if not key or not hmac.compare_digest(key, INTERNAL_API_KEY):
        raise HTTPException(status_code=401, detail="Unauthorized")
```

Apply as a router dependency:
```python
router = APIRouter(dependencies=[Depends(require_internal_auth)])
```

---

### 11. Billing Has Two Competing Feature Entitlement Systems with No Binding

**Files:**  
- `packages/billing/src/entitlements/service.ts` — hardcoded `PLAN_FEATURES` map + in-memory `entitlements` store  
- `packages/db/prisma/schema.prisma` — `FeatureDefinition`, `PlanFeature`, `PlanUsageLimit` relational tables  
- `packages/billing/src/types.ts` — `DEFAULT_PLAN_LIMITS` hardcoded object

**The debt:** The schema has a sophisticated dynamic plan configuration system (`FeatureDefinition`, `PlanFeature`, `PlanUsageLimit`, `CustomerUsageLimit`, `CustomerPlanVersion`) designed to support per-customer pricing, grandfathering, and runtime feature configuration without code deploys. The `billing` package has a parallel hardcoded system (`PLAN_FEATURES`, `PLAN_LIMITS`) that reads from static TypeScript objects. These two systems do not communicate.

The PricingPlan model even has migration comments acknowledging this:
```prisma
// Legacy JSON fields (for migration, prefer relations)
features     Json  @default("[]") // Deprecated: use planFeatures
limits       Json  @default("{}") // Deprecated: use planLimits
```

But the TypeScript billing package still reads `DEFAULT_PLAN_LIMITS` which mirrors those deprecated fields.

**Why it's debt:** When the team changes a plan limit in the database (e.g., increasing the PRO token limit to 2M), the `rateLimitMiddleware` still reads `PLAN_LIMITS.PRO.maxTokens = 1000` from the TypeScript constant. The Prisma plan definition and the enforcement code are disconnected. This makes database-driven plan configuration — the entire design goal of the relational schema — impossible without also updating TypeScript constants.

**Fix:** Replace the `DEFAULT_PLAN_LIMITS` lookup with a DB-driven read at startup:
```typescript
// billing/src/planCache.ts
let planCache: Map<string, PricingPlanConfig> | null = null;

export async function loadPlanCache(): Promise<void> {
  const plans = await prisma.pricingPlan.findMany({
    where: { isActive: true },
    include: { planFeatures: { include: { feature: true } }, planLimits: true },
  });
  planCache = new Map(plans.map(p => [p.plan, p]));
}
```

Call `loadPlanCache()` in the API gateway startup sequence. Fall back to the hardcoded constants only if the DB is unreachable at startup.

---

### 12. OrderItem Schema Has No Timestamps or Audit Trail

**File:** `packages/db/prisma/schema.prisma`, lines 288–299

**The debt:**
```prisma
model OrderItem {
  id        String  @id @default(cuid())
  orderId   String  @map("order_id")
  productId String  @map("product_id")
  quantity  Int
  unitPrice Decimal @db.Decimal(10, 2)
  // No: createdAt, updatedAt, addedById, metadata
}
```

Every other model in the schema has `createdAt`/`updatedAt`. `OrderItem` has neither. This means there is no audit trail for when items were added to an order, no way to answer "was this item added before or after the promotion expired?", and no `updatedAt` trigger if quantity is modified.

**Why it's debt:** Order disputes, refund calculations, and fraud detection all require knowing when line items were recorded. The missing timestamp is forensically blind in the event of a dispute.

**Fix:**
```prisma
model OrderItem {
  // ... existing fields
  createdAt DateTime @default(now()) @map("created_at")
  updatedAt DateTime @updatedAt @map("updated_at")
}
```

Requires a migration. Non-breaking.

---

## Triage Summary

| # | Issue | Category | Impact | Effort | Priority |
|---|-------|----------|--------|--------|----------|
| 1 | Integration credentials stored plaintext | Security | Critical (data breach) | Low (vault exists, 20 lines) | **FIX TODAY** |
| 2 | Rate limiter is per-process, not distributed | Security/Billing | High (quota bypass) | Low (swap one function) | **FIX TODAY** |
| 3 | Entitlement enforcement never invoked | Billing | High (revenue loss) | Medium (wire initialization) | **FIX TODAY** |
| 4 | S2S HMAC guard bypassed with empty secret | Security | High (auth bypass) | Low (2-line fix) | **FIX TODAY** |
| 5 | Credits/entitlements reset on restart | Lifecycle | High (credit loss) | Medium (Redis/DB-backed store) | NEXT SPRINT |
| 6 | Organization.plan never updates from Stripe | Correctness | High (billing disconnect) | Low (add inngest.send or direct update) | NEXT SPRINT |
| 7 | Order saga silent mock payment fallback | Correctness | High (revenue loss) | Low (remove fallback, throw) | NEXT SPRINT |
| 8 | GDPR deletion misses Redis PII | Compliance | High (legal) | Medium (add Redis scan step) | NEXT SPRINT |
| 9 | Feature flags have no lifecycle | Lifecycle | Medium (code debt accumulation) | Low (add schema fields) | BACKLOG |
| 10 | Python AI service has no S2S auth | Security | Medium (quota abuse) | Low (add dependency) | NEXT SPRINT |
| 11 | Two competing entitlement systems | Architecture | Medium (config drift) | High (DB-driven cache) | BACKLOG |
| 12 | OrderItem has no timestamps | Correctness | Low (audit gap) | Trivial (migration) | BACKLOG |

---

## Key File Paths for Reference

- API Gateway entry: `apps/api-gateway/src/index.ts`
- Tenant context middleware: `apps/api-gateway/src/middlewares/tenantContext.ts`
- Rate limit middleware: `apps/api-gateway/src/middlewares/rateLimit.ts`
- Entitlements middleware: `apps/api-gateway/src/middlewares/entitlements.ts`
- Integration routes (credential bug): `apps/api-gateway/src/routes/integrations/index.ts`
- Stripe webhook (billing disconnect): `apps/api-gateway/src/routes/webhooks/stripe.ts`
- Billing sync (dead Inngest function): `apps/api-gateway/src/inngest/functions/billingSync.ts`
- Order saga (mock payment): `packages/saga/src/workflows/orderSaga.ts`
- GDPR deletion (incomplete): `apps/api-gateway/src/inngest/functions/gdprDeletion.ts`
- Token bucket (in-memory): `packages/rate-limit/src/tokenBucket.ts`
- Entitlements service (in-memory): `packages/billing/src/entitlements/service.ts`
- Credits service (in-memory): `packages/billing/src/credits/service.ts`
- Vault package (unused): `packages/vault/src/index.ts`
- Prisma schema: `packages/db/prisma/schema.prisma`
- Python AI service: `services/ai/app/api/v1/routes_generate.py`
- Architecture tests: `tests/architecture/`
