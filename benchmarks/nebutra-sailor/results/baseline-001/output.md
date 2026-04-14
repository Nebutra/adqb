# Nebutra Sailor — Tech Debt Audit Report
**Baseline**: `baseline-001`  
**Audited by**: Claude Sonnet 4.6 (autonomous sub-agent)  
**Date**: 2026-04-14  
**Scope**: Architecture-level issues — security gaps, lifecycle problems, boundary violations, policy gaps, correctness bugs

---

## Priority Matrix

| # | Issue | Impact | Effort | Priority |
|---|-------|--------|--------|----------|
| 1 | Entitlements live in process memory — gate has no teeth in multi-instance deployments | CRITICAL | Medium | P0 |
| 2 | Billing routes and AI routes have no `requireAuth` guard | CRITICAL | Low | P0 |
| 3 | Integration credentials stored in plaintext despite `// Encrypt in app layer` comment | CRITICAL | Medium | P0 |
| 4 | Saga S2S auth sends the raw secret as the HMAC token | CRITICAL | Low | P0 |
| 5 | Audit package `AuditAction` string type is incompatible with the Prisma `AuditAction` enum | HIGH | Medium | P1 |
| 6 | `@nebutra/audit` defaults to in-memory storage; `setAuditStorage` is never called | HIGH | Low | P1 |
| 7 | Stripe client instantiated per-request in two hot paths | MEDIUM | Low | P2 |
| 8 | `createEndpointRateLimit` in-memory `Map` has no cleanup — unbounded memory leak | MEDIUM | Low | P2 |
| 9 | Rate limiter uses in-memory `TokenBucket` — state is per-process, not shared | MEDIUM | Medium | P2 |
| 10 | `checkoutSession` billing route uses a hardcoded limit of 10,000 for all plans | HIGH | Low | P1 |
| 11 | Subscription status `incomplete` mapped to `PAST_DUE` in Python but `INCOMPLETE` in TypeScript | HIGH | Low | P1 |
| 12 | Saga compensation swallows payment-refund errors silently | HIGH | Low | P1 |
| 13 | `chargePayment` saga step has a mock-payment fallback that can ship to production | HIGH | Low | P1 |
| 14 | Event bus and DLQ are in-memory — data is lost on restart | MEDIUM | High | P2 |
| 15 | Usage buffer flush is a fire-and-forget `setInterval` with no shutdown drain | MEDIUM | Low | P2 |
| 16 | `usageMeteringMiddleware` INCR + EXPIRE are not atomic | LOW | Low | P3 |

---

## Issue Details

---

### 1 — Entitlements Live in Process Memory (P0 — CRITICAL)

**File**: `packages/billing/src/entitlements/service.ts`, lines 128–322  

```typescript
// In-memory store (production would use database)
const entitlements: Map<string, Entitlement[]> = new Map();
```

**Why it is debt**: The entire entitlement enforcement layer — feature gates and quota checks — is stored in a module-level `Map`. There is no database read at check time. In any multi-instance deployment (two API Gateway pods, a serverless deployment, or even a restart), the map is empty. Every new request hits an empty map and `checkEntitlement` returns `{ allowed: false, feature, reason: "Feature not available in your plan" }` — which `requireEntitlement` converts to a thrown `EntitlementError` that the middleware turns into a 402. Paradoxically, if no one calls `initializePlanEntitlements` on a fresh process, the correct response is "denied" for every feature, meaning the system defaults to locking everyone out of paid features rather than accidentally granting them. This is more likely to cause a production outage than a billing bypass, but both are possible depending on call paths.

Additionally, the `entitlements.ts` middleware leaves a code comment acknowledging the problem:

```typescript
// In a real application, ensure initializePlanEntitlements has been
// called prior to this, usually on tenant creation or session resolution.
requireEntitlement(tenant.organizationId, feature, quantity);
```
(`apps/api-gateway/src/middlewares/entitlements.ts`, line 24)

`initializePlanEntitlements` is never called anywhere in the gateway startup or request pipeline — only its declaration exists in the billing package.

**Fix**: Replace the module-level `Map` with a database-backed check against `Entitlement` rows (already in the Prisma schema at line 1050–1069). At minimum, load from DB on cache miss with a short TTL:

```typescript
export async function checkEntitlement(
  organizationId: string,
  feature: string,
  quantity?: number,
): Promise<EntitlementCheckResult> {
  const entitlement = await prisma.entitlement.findUnique({
    where: { organizationId_feature: { organizationId, feature } },
  });
  // ... rest of logic unchanged
}
```

---

### 2 — Billing and AI Routes Have No Auth Guard (P0 — CRITICAL)

**Files**:  
- `apps/api-gateway/src/routes/billing/index.ts` — 0 occurrences of `requireAuth` or `requireOrganization`  
- `apps/api-gateway/src/routes/ai/index.ts` — 0 occurrences  
- `apps/api-gateway/src/routes/search/index.ts` — 0 occurrences  

**Why it is debt**: `tenantContextMiddleware` runs on all routes and _populates_ the tenant context, but it does **not** reject unauthenticated requests — it explicitly says "Downstream `requireAuth` guards are responsible for rejecting requests that need authentication." (line 93 of `tenantContext.ts`). Contrast with `agentRoutes` which correctly applies `requireAuth`:

```typescript
agentRoutes.use("*", requireAuth); // apps/api-gateway/src/routes/agents/index.ts:23
```

The billing routes serve `/api/v1/billing/checkout`, `/api/v1/billing/portal`, `/api/v1/billing/subscription`, and `/api/v1/billing/usage`. An unauthenticated request will have `tenant.organizationId = undefined`, and both `createCheckoutSession` and `createBillingPortalSession` are called with an empty string (`tenant?.organizationId ?? ""`). The AI routes proxy requests to the internal AI service with `X-Tenant-ID: anonymous`. These are not auth errors that fail fast — they silently use empty strings as tenant identifiers.

**Fix**: Add middleware at the route group level for all protected routes:

```typescript
// apps/api-gateway/src/routes/billing/index.ts
billingRoutes.use("*", requireAuth, requireOrganization);

// apps/api-gateway/src/routes/ai/index.ts  
aiRoutes.use("*", requireAuth, requireOrganization);
```

---

### 3 — Integration Credentials Stored Unencrypted (P0 — CRITICAL)

**File**: `packages/db/prisma/schema.prisma`, line 307

```prisma
credentials    Json  @default("{}") // Encrypt in app layer
```

**Files**: `apps/api-gateway/src/routes/integrations/index.ts`, lines 144 and 208

```typescript
credentials: (body.credentials ?? {}) as any,  // line 144 (create)
...(body.credentials !== undefined ? { credentials: body.credentials as any } : {}),  // line 208 (update)
```

**Why it is debt**: The comment "Encrypt in app layer" is a policy promise that is never fulfilled. The route handler writes credentials (Shopify API keys, Stripe keys for custom integrations, etc.) directly to the `credentials` JSON column without any encryption. The `@nebutra/vault` package exists and provides envelope encryption (`getVault().encrypt()`), but it is not used here. A SQL injection attack, a database backup exposure, or a compromised DB read replica would expose all third-party API credentials in plaintext for every tenant.

**Fix**: Encrypt before write, decrypt on read:

```typescript
import { getVault } from "@nebutra/vault";

// On create/update:
const vault = await getVault();
const encryptedCredentials = body.credentials
  ? await vault.encrypt(JSON.stringify(body.credentials))
  : {};
await prisma.integration.create({ data: { credentials: encryptedCredentials } });

// On read (only when credentials are needed, never in list views):
const decrypted = JSON.parse(await vault.decrypt(integration.credentials));
```

---

### 4 — Saga S2S Auth Sends Raw Secret Instead of HMAC Token (P0 — CRITICAL)

**File**: `packages/saga/src/workflows/orderSaga.ts`, lines 23–38

```typescript
const SERVICE_SECRET = process.env.SERVICE_SECRET || "";

async function gatewayFetch(path: string, options: RequestInit = {}) {
  // In a real execution, we'd sign this request with an HMAC token.
  // We'll mock the S2S headers for now.
  return fetch(`${API_GW_URL}${path}`, {
    ...options,
    headers: {
      "Content-Type": "application/json",
      "x-service-token": SERVICE_SECRET,  // <-- wrong: raw secret, not HMAC
      ...options.headers,
    },
  });
}
```

**Why it is debt**: The `tenantContextMiddleware` in the gateway (`apps/api-gateway/src/middlewares/tenantContext.ts`, lines 66–80) expects `x-service-token` to be a hex-encoded HMAC-SHA256 of the canonical headers using `SERVICE_SECRET` as the key:

```typescript
const canonical = `${headerUserId}:${headerOrganizationId}:${headerRole}:${headerPlan}`;
const expected = createHmac("sha256", secret).update(canonical).digest();
const tokenBuffer = Buffer.from(serviceToken, "hex");
return timingSafeEqual(expected, tokenBuffer);
```

The saga sends the raw `SERVICE_SECRET` value as the token. This will always fail HMAC verification when `SERVICE_SECRET` is set, causing all saga S2S calls to be treated as unauthenticated. Additionally, the saga sends **no** `x-user-id` or `x-organization-id` headers, so `verifyServiceToken` is computing an HMAC of `":::""` against the raw secret string — structurally impossible to match. In development without `SERVICE_SECRET`, the gateway logs a warning and falls back to trusting everything, masking the broken auth.

**Fix**:

```typescript
import { createHmac } from "node:crypto";

async function gatewayFetch(
  path: string,
  options: RequestInit = {},
  context: { userId?: string; organizationId?: string; role?: string; plan?: string } = {},
) {
  const secret = process.env.SERVICE_SECRET;
  if (!secret) throw new Error("SERVICE_SECRET must be set for S2S calls");

  const canonical = `${context.userId ?? ""}:${context.organizationId ?? ""}:${context.role ?? ""}:${context.plan ?? ""}`;
  const hmac = createHmac("sha256", secret).update(canonical).digest("hex");

  return fetch(`${API_GW_URL}${path}`, {
    ...options,
    headers: {
      "Content-Type": "application/json",
      "x-service-token": hmac,
      ...(context.userId ? { "x-user-id": context.userId } : {}),
      ...(context.organizationId ? { "x-organization-id": context.organizationId } : {}),
      ...(context.role ? { "x-role": context.role } : {}),
      ...(context.plan ? { "x-plan": context.plan } : {}),
      ...options.headers,
    },
  });
}
```

---

### 5 — Audit Package `AuditAction` Type Is Incompatible with Prisma Enum (P1 — HIGH)

**Files**:  
- `packages/audit/src/index.ts`, line 13: TypeScript union type defines strings like `"user.login"`, `"api.key_create"`, `"billing.payment_success"`
- `packages/db/prisma/schema.prisma`, lines 1111–1121: Prisma enum defines `CREATE | READ | UPDATE | DELETE | LOGIN | LOGOUT | EXPORT | IMPORT`
- `packages/audit/src/index.ts`, line 150: `action: event.action` is written directly to the Prisma `auditLog.create` call

**Why it is debt**: `createPrismaStorage` writes `event.action` (e.g., `"user.login"`) to the Prisma `auditLog.create` data object. The `AuditLog.action` column has type `AuditAction` (a PostgreSQL enum with values `CREATE`, `READ`, `UPDATE`, `DELETE`, `LOGIN`, `LOGOUT`, `EXPORT`, `IMPORT`). Writing `"user.login"` to this column will fail with a Prisma/PostgreSQL enum constraint violation at runtime. The TypeScript compiler does not catch this because `createPrismaStorage` accepts the Prisma client with a weakly-typed interface (`data: unknown`). The two `AuditAction` types — one in the audit package (rich, dotted strings) and one in the Prisma schema (coarse CRUD verbs) — were designed independently and diverged.

**Fix**: Either (a) change the `AuditLog.action` column from a Prisma enum to a `String` to match the richer TS type, or (b) add a mapping function in `createPrismaStorage`:

```typescript
function toDbAction(action: AuditAction): DbAuditAction {
  if (action.includes("login")) return "LOGIN";
  if (action.includes("logout")) return "LOGOUT";
  if (action.includes("export") || action.includes("delete")) return action.includes("delete") ? "DELETE" : "EXPORT";
  if (action.startsWith("org.") || action.startsWith("api.")) return "CREATE";
  return "UPDATE";
}
```

Option (a) is lower risk. The Prisma enum was likely added for DB-level constraint but is not worth the mapping complexity when the TS layer is already the authority.

---

### 6 — Audit Storage Defaults to In-Memory; Never Wired to Database (P1 — HIGH)

**File**: `packages/audit/src/index.ts`, lines 196–199

```typescript
let storage: AuditStorage = inMemoryStorage;  // module-level default

export function setAuditStorage(newStorage: AuditStorage): void {
  storage = newStorage;
}
```

**Why it is debt**: `setAuditStorage` is never called in any app entrypoint, Inngest function, or middleware. Searching the full monorepo confirms zero call sites outside the audit package itself. Every `audit()` call in the middleware, webhook handlers, and convenience functions writes to the process-local `memoryStorage: AuditEvent[]` array. On process restart, all audit history is lost. In a compliance context (SOC 2, GDPR access logs), losing audit trails silently is a regulatory risk.

**Fix**: Wire Prisma storage at gateway startup in `apps/api-gateway/src/index.ts`:

```typescript
import { createPrismaStorage, setAuditStorage } from "@nebutra/audit";
import { prisma } from "@nebutra/db";

setAuditStorage(createPrismaStorage(prisma));
```

This requires fixing issue #5 first (enum mismatch).

---

### 7 — Stripe Client Instantiated Per Request (P2 — MEDIUM)

**Files**:  
- `apps/api-gateway/src/routes/webhooks/stripe.ts`, line 78: `const stripe = new Stripe(secretKey);`  
- `apps/api-gateway/src/inngest/functions/tenantProvisioning.ts`, line 145: `const stripe = new Stripe(secretKey);`

**Why it is debt**: The Stripe Node.js client sets up TLS connections, reads configuration, and allocates internal state on construction. Instantiating it on every webhook delivery and every new tenant provisioning event means paying that overhead repeatedly. The Stripe library documentation recommends a module-level singleton. For the webhook route, this is called on every webhook Stripe delivers (potentially thousands per day for active billing).

**Fix**: Create a singleton in `packages/billing/src/stripe/client.ts`:

```typescript
let _stripe: Stripe | null = null;
export function getStripe(): Stripe {
  if (!_stripe) {
    const key = process.env.STRIPE_SECRET_KEY;
    if (!key) throw new Error("STRIPE_SECRET_KEY not set");
    _stripe = new Stripe(key);
  }
  return _stripe;
}
```

The `packages/billing/src/stripe/index.ts` already exports `getStripe()` — the webhook file just does not use it (it constructs inline instead).

---

### 8 — `createEndpointRateLimit` In-Memory Map Has No Cleanup (P2 — MEDIUM)

**File**: `apps/api-gateway/src/middlewares/rateLimit.ts`, lines 54–79

```typescript
export function createEndpointRateLimit(maxPerMinute: number) {
  const requests = new Map<string, { count: number; resetAt: number }>();

  return async (c: Context, next: Next) => {
    const key = tenant?.userId || tenant?.ip || "anonymous";
    let record = requests.get(key);
    if (!record || now > record.resetAt) {
      record = { count: 0, resetAt: now + 60000 };
      requests.set(key, record);  // never deleted
    }
    // ...
  };
}
```

**Why it is debt**: The `Map` is never pruned. Every unique `userId` or IP that hits an endpoint protected by this limiter adds an entry. In a long-running process receiving requests from a large tenant base or undergoing a DDoS (many unique IPs), this grows without bound. The global `TokenBucket` in `tokenBucket.ts` has an explicit `cleanup()` method called by a `setInterval` — but `createEndpointRateLimit` does not use `TokenBucket` and has no equivalent.

**Fix**: After resetting an expired record, consider a simple periodic sweep, or use the existing `TokenBucket` class which already handles cleanup:

```typescript
export function createEndpointRateLimit(maxPerMinute: number) {
  const limiter = new TokenBucket({
    maxTokens: maxPerMinute,
    refillRate: maxPerMinute,
    refillInterval: 60_000,
  });
  return async (c: Context, next: Next) => {
    const key = c.get("tenant")?.userId ?? c.get("tenant")?.ip ?? "anonymous";
    const result = await limiter.consume(key, 1);
    if (!result.allowed) { /* ... 429 */ }
    await next();
  };
}
```

---

### 9 — Rate Limiter Is In-Memory Per Process (P2 — MEDIUM)

**File**: `packages/rate-limit/src/tokenBucket.ts`, lines 160–167

```typescript
const rateLimiters: Map<string, TokenBucket> = new Map();

export function getRateLimiter(plan: string): TokenBucket {
  if (!rateLimiters.has(plan)) {
    rateLimiters.set(plan, createRateLimiter(plan));
  }
  return rateLimiters.get(plan) as TokenBucket;
}
```

**Why it is debt**: `getRateLimiter` returns an in-memory `TokenBucket`. Each process has its own bucket state. With two API Gateway instances, a tenant can make 2× the allowed requests per second by splitting traffic across instances. A `RedisTokenBucket` class exists in the same file (lines 175–239) and is explicitly documented as the production-safe option. The production rate limiter silently uses the wrong implementation.

The file even has this comment: _"In production, use Redis for distributed rate limiting."_

**Fix**: Replace the `getRateLimiter` export with a Redis-backed factory, injecting it from the gateway entrypoint where the Redis client is available:

```typescript
// apps/api-gateway/src/middlewares/rateLimit.ts
import { getRedis } from "@nebutra/cache";
import { createRedisRateLimiter, PLAN_LIMITS } from "@nebutra/rate-limit";

const limiters = new Map<string, ReturnType<typeof createRedisRateLimiter>>();

function getDistributedLimiter(plan: string) {
  if (!limiters.has(plan)) {
    const config = PLAN_LIMITS[plan as keyof typeof PLAN_LIMITS] ?? PLAN_LIMITS.FREE;
    limiters.set(plan, createRedisRateLimiter(config, getRedis()));
  }
  return limiters.get(plan)!;
}
```

---

### 10 — Billing Usage Route Has a Hardcoded Limit of 10,000 for All Plans (P1 — HIGH)

**File**: `apps/api-gateway/src/routes/billing/index.ts`, lines 163–171

```typescript
// Use the synchronous checkUsageLimit with a default limit of 10000
const limitResult = checkUsageLimit(BigInt(snapshot.apiCalls), BigInt(10000), BigInt(0));

return c.json({
  period: snapshot.period,
  apiCalls: {
    used: snapshot.apiCalls,
    limit: Number(limitResult.limit),  // always 10000
    percentUsed: limitResult.percentUsed,
  },
```

**Why it is debt**: This is the quota display endpoint used by tenants to see their usage. ENTERPRISE tenants with a different (higher) limit will see 10,000 as their limit regardless of their actual entitlement. PRO tenants with a lower limit won't see an accurate overage warning. This misinforms tenants about their billing position and could result in unexpected charges or blocked API calls with no warning. The Prisma schema has `PlanUsageLimit` and `CustomerUsageLimit` tables designed precisely to store per-plan, per-tenant limits — they are not consulted here.

**Fix**: Resolve the actual limit from the plan/customer override hierarchy:

```typescript
const plan = tenant?.plan ?? "FREE";
const limit = await getPlanUsageLimit(plan, "API_CALL");  // from @nebutra/billing
const limitResult = checkUsageLimit(BigInt(snapshot.apiCalls), limit, BigInt(0));
```

---

### 11 — Subscription Status `incomplete` Maps Differently in Python vs TypeScript (P1 — HIGH)

**Files**:  
- `services/billing/services/subscription_service.py`, line 40: `"incomplete": "PAST_DUE"`  
- `apps/api-gateway/src/routes/webhooks/stripe.ts`, line 175: `incomplete: "INCOMPLETE"`

**Why it is debt**: The two services write to the same `Subscription.status` column in the same database. When Stripe fires a subscription event that results in `incomplete` status, one code path writes `PAST_DUE` (Python billing service) while the other writes `INCOMPLETE` (TypeScript webhook handler). Which value ends up in the database depends on race conditions in event delivery ordering. Application logic that checks `status === "PAST_DUE"` to trigger dunning flows would behave inconsistently depending on which service last touched the row.

Additionally, the Python service maps `incomplete_expired` to `CANCELED`, matching the TypeScript mapping, but maps `incomplete` to `PAST_DUE` — conflating two distinct Stripe states.

**Fix**: Create a canonical status mapping in a shared package (e.g., `packages/contracts/src/stripe.ts`) and import it in both services. Python can read it from a generated JSON or have a matching constant file maintained by contract tests.

---

### 12 — Saga Compensation Swallows Payment Refund Errors (P1 — HIGH)

**File**: `packages/saga/src/workflows/orderSaga.ts`, lines 101–109

```typescript
compensate: async (ctx) => {
  if (ctx.paymentId && ctx.paymentId.startsWith("pi_")) {
    const stripe = getStripe();
    await stripe.refunds
      .create({ payment_intent: ctx.paymentId })
      .catch((err) => logger.error("Payment refund compensation failed", { error: err }));
  }
},
```

**Why it is debt**: If the Stripe refund call fails (e.g., the payment was already refunded, Stripe is down, or the payment intent ID is invalid), the error is caught and logged, but the saga continues treating the compensation as complete. The customer has been charged and the refund was not issued. The `SagaOrchestrator.compensate()` method also silently catches errors from all compensation steps (lines 139–146 in `orchestrator.ts`):

```typescript
} catch (error) {
  await this.eventBus.publish(
    this.eventBus.createEvent("saga.compensation.failed", { ... }),
  );
}
```

Both layers swallow refund failures. There is no mechanism to alert, retry the refund, or record the failed-compensation state for manual intervention.

**Fix**: At minimum, do not catch the refund error in the compensation function — let it propagate to the orchestrator. The orchestrator should then write the failed-compensation state to a `SagaCompensationLog` table or DLQ for manual review, rather than silently continuing. Consider an idempotency key on refund creation to allow safe retries.

---

### 13 — Saga `chargePayment` Step Has a Mock-Payment Fallback That Can Ship to Production (P1 — HIGH)

**File**: `packages/saga/src/workflows/orderSaga.ts`, lines 80–84

```typescript
if (!ctx.customerId) {
  // Mock fallback if customerId isn't provided but we require payment
  // In real life, require customerId or fail.
  return { ...ctx, paymentId: `mock_pay_${Date.now()}` };
}
```

**Why it is debt**: If `customerId` is absent (e.g., a new customer whose Stripe customer hasn't been provisioned yet, or a bug upstream), the saga silently succeeds with a fake payment ID (`mock_pay_1714000000000`). The order is fulfilled — inventory is reserved, the order record is created, the confirmation email is sent — but no payment has been collected. The comment acknowledges this is wrong ("In real life, require customerId or fail") but the code is in the production path without any guard to ensure this cannot run with real data. The `compensate` function also explicitly skips refund logic for IDs not starting with `pi_` (line 102: `if (ctx.paymentId && ctx.paymentId.startsWith("pi_"))`), meaning orders with mock payment IDs cannot be compensated.

**Fix**: Remove the fallback and make missing `customerId` a hard failure:

```typescript
if (!ctx.customerId) {
  throw new Error(
    `Cannot charge order ${ctx.orderId}: no Stripe customerId for organization ${ctx.tenantId}`,
  );
}
```

---

### 14 — Event Bus and DLQ Are In-Memory — Data Lost on Restart (P2 — MEDIUM)

**Files**:  
- `packages/event-bus/src/bus.ts`, line 28: `private eventLog: BaseEvent[] = []`  
- `packages/event-bus/src/dlq.ts`, line 29: `const queue: DeadLetterEntry[] = []`

**Why it is debt**: The `EventBus` comment says "In production, replace with Redis Streams or NATS." The DLQ comment says "In a real deployment backed by Redis Streams or NATS this module should delegate to the broker's native DLQ." No production-ready implementation has been provided, and the comment-as-policy means nothing enforces the replacement. On process restart: all in-flight events are lost, all DLQ entries are lost, and any system using `eventBus.subscribe()` (rather than Inngest) has no guarantee of delivery.

Note that the gateway also exposes `/api/v1/admin/dlq` which surfaces the in-memory DLQ to operators — creating false confidence that DLQ entries are durable.

**Fix**: For short-term, implement a Postgres-backed event store using the existing `WebhookEvent` table pattern, or switch all saga/workflow events to Inngest (which already uses a durable store). For the DLQ specifically, write failed events to a `SagaFailedEvent` Prisma model rather than a module-level array.

---

### 15 — Usage Buffer Has No Shutdown Drain (P2 — MEDIUM)

**File**: `packages/billing/src/usage/service.ts`, lines 48–49, 199–202

```typescript
const BUFFER_FLUSH_INTERVAL = 5000; // 5 seconds
// ...
if (typeof setInterval !== "undefined") {
  setInterval(() => {
    flushUsageBuffer().catch(...)
  }, BUFFER_FLUSH_INTERVAL);
}
```

**Why it is debt**: The `flushUsageBuffer()` function comment says "In production, this would write to the database" — but the flush is a no-op (it returns the array but the database write is commented out: `// await prisma.usageRecord.createMany({ data: flushed })`). Additionally, the graceful shutdown in `apps/api-gateway/src/index.ts` closes the HTTP server and the Prisma connection, but does not call `flushUsageBuffer()`. Any usage records buffered in the last 5 seconds before a SIGTERM are silently dropped.

**Fix**: First, uncomment the DB write in `flushUsageBuffer`. Second, add a drain call to the shutdown handler:

```typescript
const shutdown = async (signal: string) => {
  logger.info(`Received ${signal}, starting graceful shutdown...`);
  server.close(async () => {
    await flushUsageBuffer(); // drain remaining usage records
    await prisma.$disconnect();
    process.exit(0);
  });
};
```

---

### 16 — Usage Metering INCR + EXPIRE Are Not Atomic (P3 — LOW)

**File**: `apps/api-gateway/src/middlewares/usageMetering.ts`, lines 98–100

```typescript
await r.incr(apiCallKey);
// Refresh TTL every call (cheap, idempotent)
await r.expire(apiCallKey, TTL_SECONDS);
```

**Why it is debt**: Between the `INCR` and the `EXPIRE`, another process could execute its own `INCR` + `EXPIRE` — not harmful. The more relevant concern is: if the process crashes after `INCR` but before `EXPIRE`, the key will never expire and will persist until Redis evicts it or the node is flushed. In a 35-day TTL scenario, that means stale billing counters accumulate indefinitely in Redis. The correct pattern is a Lua script or `SET key (value+1) EX ttl` via a pipeline.

**Fix**: Use a pipeline or a Lua script:

```typescript
await r.pipeline()
  .incr(apiCallKey)
  .expire(apiCallKey, TTL_SECONDS)
  .exec();
```

---

## Cross-Cutting Observations

### Schema Comment-as-Contract Anti-Pattern

The Prisma schema contains `// Encrypt in app layer` at `Integration.credentials` (line 307). This comment is the only enforcement mechanism — there is no code that reads this comment, no test that asserts encryption is applied, and no type system constraint. Comments-as-contracts are a class of policy gap: the intent is documented, but implementation is delegated to whoever writes the next route. Two of the three integration CRUD routes (create and update) both skip encryption.

### Dual-Entry Billing Architecture Without Synchronization

Usage is tracked in two independent systems simultaneously:
1. Redis counters via `usageMeteringMiddleware` (gateway)
2. `UsageRecord` table writes via `@nebutra/billing`'s `recordUsage()` (in-memory buffer, never flushed to DB)
3. `TenantUsage` table (Prisma model exists but nothing writes to it)
4. `UsageLedgerEntry` table (immutable ledger, exists in schema but no observed writes in audited code)

The billing quota check in `GET /billing/usage` reads only from Redis. The ledger tables exist but are not populated. This creates a risk that billing overhaul work (moving to ledger-based billing) would read $0 from the ledger tables because nothing has been writing to them.

### AuditMutation Middleware Action Inference Is Path-Fragile

`apps/api-gateway/src/middlewares/auditMutation.ts`, lines 25–43: The `inferAction` function uses `path.includes("/api-keys")` and `path.includes("/billing")` string matching to determine the audit action. This will produce incorrect actions for routes that don't match any pattern (falls back to `"custom"`) and could produce misleading actions if new routes use similar path segments. A structured approach — passing the action explicitly from each route handler — would be more reliable.

---

## Summary

The highest-risk cluster is issues #1–4: the entitlement system has no enforcement in multi-process deployments, billing and AI routes are accessible without authentication, third-party credentials are stored in plaintext, and the primary S2S authentication mechanism is broken at implementation (raw secret vs HMAC). These four issues together mean that in a production deployment, the security boundary between tenants and the billing/AI/credential data is significantly weaker than the code's intent suggests.

Issues #5–6 mean the audit log — a compliance requirement — is silently discarding all records. Issues #10–13 represent correctness bugs in billing and payment flows where tenants may be over/under-charged or where payment failures are silently swallowed.

The remaining issues are lifecycle and scalability concerns that will surface under load or multi-instance deployment, all with moderate to low effort to fix.
