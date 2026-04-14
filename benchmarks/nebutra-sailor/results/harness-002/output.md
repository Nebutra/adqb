# Nebutra Sailor — Tech Debt Audit

**Scope:** Full monorepo (`/Users/tseka_luk/Documents/Nebutra-SaaS-Lab/Nebutra-Sailor`)  
**Stack:** Next.js / React 19 / Hono / Prisma 7 / Python FastAPI / pnpm Turbo  
**Audit date:** 2026-04-14  
**Method:** Two-phase (breadth scan → depth analysis)

---

## Phase 1 — Coverage Scan Summary

| Area | Status | Severity |
|------|--------|----------|
| Auth/security: route coverage | **CRITICAL GAP** — 4 route groups missing `requireAuth` | CRITICAL |
| Data access: RLS/scoping | Defined but not called anywhere outside its own package | HIGH |
| Secrets: credential encryption | Comment says "Encrypt in app layer" — no code does it | CRITICAL |
| In-memory state | 8+ module-level Maps holding billing-critical data | HIGH |
| Feature flags | No lifecycle fields (owner, expiresAt, deprecation) | MEDIUM |
| Billing/entitlements | In-memory store; credit deduction is fire-and-forget | HIGH |
| Dependencies | Catalog-pinned, consistent | OK |
| Schema integrity | Schemas match usage; `Integration.credentials` is plain JSON | HIGH |
| S2S communication | Saga `gatewayFetch` sends raw `SERVICE_SECRET` string (not HMAC) | HIGH |
| Saga/workflow | Missing idempotency; mock payment path in production code | HIGH |

---

## Phase 2 — Depth Analysis

---

### ISSUE 1 — Missing `requireAuth` on Four Route Groups (CRITICAL)

**Affected files:**
- `apps/api-gateway/src/routes/ai/index.ts` — no auth middleware
- `apps/api-gateway/src/routes/billing/index.ts` — no auth middleware
- `apps/api-gateway/src/routes/search/index.ts` — no auth middleware
- `apps/api-gateway/src/routes/integrations/index.ts` — no auth middleware

**Why this is debt, not just a gap:**

The middleware comment at the top of each file says "All routes require authentication (tenantContextMiddleware applied upstream)." That statement is false. `tenantContextMiddleware` _extracts_ identity but does **not** reject unauthenticated requests — that is the explicit job of `requireAuth`. The comment creates false confidence in code review.

Evidence from `apps/api-gateway/src/routes/ai/index.ts` line 76:
```typescript
const tenant = c.get("tenant");
// tenant?.organizationId is "anonymous" for unauthenticated callers
// but no rejection happens
await proxyToAiService("/v1/chat/completions", "POST", body, tenant?.organizationId ?? "anonymous")
```

An unauthenticated request reaches the upstream AI service with `organizationId = "anonymous"`. The AI service bills nothing (no tenant), but the proxy call goes through. This is a direct cost vector: anyone can drain your AI provider budget with unauthenticated POST requests.

The `agentRoutes` file (`apps/api-gateway/src/routes/agents/index.ts`, line 23) correctly uses `agentRoutes.use("*", requireAuth)` — that pattern was applied once and not replicated across the other route groups.

**Fix (fix today — 30 minutes per file):**

```typescript
// Add to the top of each affected route file, after imports:

// apps/api-gateway/src/routes/ai/index.ts
import { requireAuth } from "../../middlewares/tenantContext.js";
export const aiRoutes = new OpenAPIHono();
aiRoutes.use("*", requireAuth);

// apps/api-gateway/src/routes/billing/index.ts
import { requireAuth } from "../../middlewares/tenantContext.js";
export const billingRoutes = new OpenAPIHono();
billingRoutes.use("*", requireAuth);

// apps/api-gateway/src/routes/search/index.ts
import { requireAuth } from "../../middlewares/tenantContext.js";
export const searchRoutes = new OpenAPIHono();
searchRoutes.use("*", requireAuth);

// apps/api-gateway/src/routes/integrations/index.ts
import { requireAuth } from "../../middlewares/tenantContext.js";
export const integrationRoutes = new OpenAPIHono();
integrationRoutes.use("*", requireAuth);
```

**Encode as policy (next sprint):** Add an architecture test that fails CI if any route file under `apps/api-gateway/src/routes/` exports an OpenAPIHono instance without importing `requireAuth`:

```typescript
// tests/architecture/route-auth.test.ts
import { glob } from 'glob'
import { readFileSync } from 'fs'
import { describe, it, expect } from 'vitest'

const EXEMPT = new Set(['misc/health.ts', 'system/status.ts', 'webhooks'])

describe('Route auth coverage', () => {
  it('every route file applies requireAuth', () => {
    const files = glob.sync('apps/api-gateway/src/routes/**/*.ts')
    for (const file of files) {
      if (EXEMPT.some(e => file.includes(e))) continue
      const content = readFileSync(file, 'utf-8')
      if (content.includes('new OpenAPIHono')) {
        expect(content, `${file} defines routes without requireAuth`).toMatch(/requireAuth/)
      }
    }
  })
})
```

---

### ISSUE 2 — Integration Credentials Stored in Plaintext JSON (CRITICAL)

**Affected files:**
- `packages/db/prisma/schema.prisma` line 307: `credentials Json @default("{}") // Encrypt in app layer`
- `apps/api-gateway/src/routes/integrations/index.ts` line 144: `credentials: (body.credentials ?? {}) as any`

**Why this is debt:**

The schema comment `// Encrypt in app layer` is a wiki policy — it documents an intent without enforcing it. The vault package (`packages/vault/`) provides AES + AWS KMS envelope encryption. It is fully built. It is imported by zero production paths outside of the CLI's mock implementation.

When a tenant stores Shopify credentials (API keys, webhooks secrets), those credentials land in the `integrations.credentials` column as plaintext JSON. A single PostgreSQL injection, a DB backup leak, or a compromised read replica exposes every integration secret for every tenant.

Evidence — `apps/api-gateway/src/routes/integrations/index.ts` lines 142-145:
```typescript
const integration = await prisma.integration.create({
  data: {
    credentials: (body.credentials ?? {}) as any,  // plaintext, no encryption
```

The `@nebutra/vault` package is production-ready (`packages/vault/src/index.ts` exports `encrypt`, `decrypt`, `getVault`, `AWSKMSProvider`). The fix is a call-site wrapper, not new infrastructure.

**Fix (next sprint — 1 day):**

```typescript
// apps/api-gateway/src/routes/integrations/index.ts

import { getVault } from "@nebutra/vault";

// In create handler:
const vault = await getVault();
const encryptedCredentials = body.credentials
  ? await vault.encrypt(JSON.stringify(body.credentials))
  : "{}";

await prisma.integration.create({
  data: {
    credentials: encryptedCredentials as any,  // now an EncryptedSecret object
```

```typescript
// In read handler (get single integration):
const vault = await getVault();
const raw = integration.credentials;
const decrypted = raw && typeof raw === "object" && "ciphertext" in raw
  ? JSON.parse(await vault.decrypt(raw as EncryptedSecret))
  : raw;
```

Note: the `as any` casts at lines 144, 145, 208, 209 in the integrations route also mask a type boundary violation — credentials should be a typed `EncryptedSecret | EmptyCredentials`, not `any`. Clean that up in the same pass.

---

### ISSUE 3 — Billing-Critical State Is In-Memory (HIGH)

**Affected files:**
- `packages/billing/src/credits/service.ts` lines 49-50: `const creditBalances: Map<...>` and `const creditTransactions: Map<...>`
- `packages/billing/src/entitlements/service.ts` line 128: `const entitlements: Map<...>`
- `packages/billing/src/usage/service.ts` line 48: `const usageBuffer: Map<...>`
- `packages/rate-limit/src/tokenBucket.ts` line 160: `const rateLimiters: Map<...>`
- `apps/api-gateway/src/routes/admin/index.ts` line 51: `const flagOverrides = new Map<...>()`

**Why this is debt:**

These are not caches of database data — they ARE the data. When a Node.js process restarts (deploy, crash, OOM), every tenant's credit balance is reset to zero. Every entitlement check returns "no entitlement found." Every active rate limit bucket is reset to full. Every admin feature flag override is lost.

More acutely: `packages/agents/src/agent.ts` line 104 calls `deductCredits(...)` — a synchronous call that mutates the in-memory Map — without `await`. The in-memory store never persists. So the entire credit deduction system is: check an in-memory balance (that starts at zero for every new process), deduct from an in-memory balance (that restarts at zero on redeploy), and never persist any of it.

The comments acknowledge this ("production would use database", "In production, this would write to the database") but the same code is imported by production routes right now.

The `checkAgentQuota` function in `packages/agents/src/tenant.ts` line 44 has a silent failure mode:
```typescript
} catch (err) {
  // Failsafe open if billing is unconfigured
  return { allowed: true, remaining: -1 };
}
```
When billing fails (which it always will in-memory after restart), the system grants unlimited quota. This is the opposite of safe-fail.

**Fix priority:**

1. **Fix today — credits and entitlements:** Route `addCredits`, `deductCredits`, `getCreditBalance`, `checkEntitlement` through the existing Prisma models (`CreditLedger`, `Entitlement`) that are already defined in `packages/db/prisma/schema.prisma`. The schema is ready; the service implementations are not.

2. **Fix today — usage buffer:** The `setInterval` flush at `packages/billing/src/usage/service.ts` lines 199-202 calls `flushUsageBuffer()` which has a commented-out `// await prisma.usageRecord.createMany(...)`. Uncomment and implement it. The buffer is fine as a performance optimization; the missing persistence is the bug.

3. **Next sprint — rate limiter:** The `TokenBucket` class already has a `RedisTokenBucket` sibling in the same file. Switch `getRateLimiter` to use `RedisTokenBucket` backed by `@nebutra/cache`. Until then, every pod has an independent rate limit counter — a multi-pod deployment has N× the effective rate limit.

4. **Next sprint — admin flag overrides:** The `flagOverrides` map in `adminRoutes` is explicitly labeled "replace with Redis/DB in prod." Do it. The feature flag override system being invisible across restarts means ops cannot reliably use it for incident mitigation.

---

### ISSUE 4 — Saga Payment Path Contains Mock Code (HIGH)

**Affected file:**
- `packages/saga/src/workflows/orderSaga.ts` lines 29-36, 81-83

**Why this is debt:**

The `gatewayFetch` helper in the order saga has this comment at line 30-31:
```typescript
// In a real execution, we'd sign this request with an HMAC token.
// We'll mock the S2S headers for now.
```

This is in `packages/saga/` — a package exported and callable by production services. The function sends `SERVICE_SECRET` as a raw header value instead of an HMAC signature:

```typescript
headers: {
  "x-service-token": SERVICE_SECRET,  // raw secret, not HMAC
```

But `tenantContextMiddleware` at line 67 in `tenantContext.ts` verifies the token by computing `createHmac("sha256", secret).update(canonical)` and comparing. This means the saga's `x-service-token` header contains the raw secret string, not the HMAC of `userId:orgId:role:plan`. The verification in `verifyServiceToken` will fail because `SERVICE_SECRET` as a hex string won't match the HMAC output.

There is also a mock payment fallback at line 81-83:
```typescript
if (!ctx.customerId) {
  // Mock fallback if customerId isn't provided but we require payment
  return { ...ctx, paymentId: `mock_pay_${Date.now()}` };
}
```

A `mock_pay_*` payment ID looks like a real payment but is fictional. If this path is reached in production (customer has no Stripe customer ID), an order record gets created with `paymentId: "mock_pay_1234567890"`, the compensation check at line 98 (`if (ctx.paymentId && ctx.paymentId.startsWith("pi_"))`) correctly skips Stripe refund — but the order is still marked as "confirmed" with no actual payment.

**Fix (next sprint — 2 hours):**

```typescript
// Replace gatewayFetch with proper HMAC signing:
import { createHmac } from "node:crypto";

async function gatewayFetch(
  path: string,
  ctx: { userId?: string; orgId?: string; role?: string; plan?: string },
  options: RequestInit = {}
) {
  const secret = process.env.SERVICE_SECRET;
  if (!secret) throw new Error("SERVICE_SECRET is not configured");

  const canonical = `${ctx.userId ?? ""}:${ctx.orgId ?? ""}:${ctx.role ?? ""}:${ctx.plan ?? ""}`;
  const hmac = createHmac("sha256", secret).update(canonical).digest("hex");

  return fetch(`${API_GW_URL}${path}`, {
    ...options,
    headers: {
      "Content-Type": "application/json",
      "x-service-token": hmac,
      "x-user-id": ctx.userId ?? "",
      "x-organization-id": ctx.orgId ?? "",
      ...options.headers,
    },
  });
}
```

For the mock payment path, remove the fallback entirely and require `customerId` as a precondition:
```typescript
execute(ctx) {
  if (!ctx.customerId) {
    throw new Error("Cannot charge payment: customerId is required");
  }
  // ... real Stripe call
}
```

---

### ISSUE 5 — Saga Steps Lack Idempotency Keys (HIGH)

**Affected files:**
- `packages/saga/src/orchestrator.ts` — `execute()` method has no idempotency keys
- `packages/saga/src/workflows/orderSaga.ts` — all four steps

**Why this is debt:**

Inngest retries failed functions automatically (configured to 5 retries in `gdprDeletion.ts`). When a saga is triggered by an Inngest event and the saga crashes mid-execution (e.g. after `chargePayment` succeeds but before `createOrderRecord` completes), Inngest will re-trigger the saga from scratch. Without idempotency keys, `chargePayment` will charge the customer's card a second time.

The `reserveInventory` compensation check is correct — it checks `ctx.inventoryReserved` before releasing. But the execution side has no guard: if `reserveInventory` succeeds and then the process crashes before the context is returned, retrying will double-reserve.

The `SagaOrchestrator.execute()` receives `initialContext` but never persists `completedSteps` to any durable store. There is no way to resume from where it stopped.

**Fix (next sprint — 1 day):**

Add an idempotency token to each saga execution and persist step completion to Redis or a DB table before starting each step:

```typescript
// In SagaOrchestrator:
async execute(initialContext: TContext, idempotencyKey: string): Promise<SagaResult<TContext>> {
  // Load any previously completed steps for this execution
  const resumeState = await this.loadResumeState(idempotencyKey);
  const alreadyCompleted = new Set(resumeState?.completedSteps ?? []);

  for (const step of this.steps) {
    if (alreadyCompleted.has(step.name)) {
      // Step already succeeded in a prior attempt — skip
      context = resumeState!.contextAfter[step.name] as TContext;
      completedSteps.push(step.name);
      continue;
    }
    context = await step.execute(context);
    await this.persistStepCompletion(idempotencyKey, step.name, context);
    completedSteps.push(step.name);
  }
}
```

For payment specifically, pass `idempotencyKey` to the Stripe call:
```typescript
const paymentIntent = await stripe.paymentIntents.create({
  ...params,
  metadata: { orderId: ctx.orderId },
}, { idempotencyKey: `order:${ctx.orderId}:payment` });
```

---

### ISSUE 6 — RLS Tenant Isolation Is Defined but Not Enforced (HIGH)

**Affected files:**
- `packages/tenant/src/isolation.ts` — `withRls`, `createTenantPrismaProxy` are fully implemented
- `apps/api-gateway/src/routes/integrations/index.ts` line 49: `await prisma.integration.findMany({ where: { organizationId: orgId } })`
- (Same pattern in admin routes, consent routes)

**Why this is debt:**

The `withRls()` function in `packages/tenant/src/isolation.ts` correctly wraps Prisma with a `$before` hook that calls `SET LOCAL app.current_tenant_id = ?` before each query. This would enforce PostgreSQL row-level security policies. But the function is imported nowhere outside its own package's index.

Every route handler uses `prisma.someModel.findMany({ where: { organizationId: orgId } })` — an application-level filter. Application-level filters are correct in isolation but they have a known failure mode: if a developer forgets the `where` clause (as happens during rapid feature development), there is no database-level safety net. One missing `where` clause leaks all tenants' data.

This is the multi-tenant trust model question: the system chose to support both shared-schema RLS and application-level filters, but only implemented one and documented the intent of the other.

**The distinction matters for compliance.** SOC 2, ISO 27001, and GDPR audit questions specifically ask how tenant isolation is enforced. "We check organizationId in every WHERE clause" is a weaker answer than "row-level security enforced by the database engine."

**Fix (next sprint — 2 days):**

The `withRls` implementation is complete. The work is:

1. Create PostgreSQL RLS policies for every multi-tenant table (a migration file):
```sql
-- migrations/XXXX_add_rls_policies.sql
ALTER TABLE integrations ENABLE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON integrations
  USING (organization_id = current_setting('app.current_tenant_id'));
```

2. Use `withRls(prisma, tenant.organizationId)` in route handlers instead of raw `prisma`:
```typescript
// apps/api-gateway/src/routes/integrations/index.ts
import { withRls } from "@nebutra/tenant";

integrationRoutes.openapi(listRoute, async (c) => {
  const tenant = c.get("tenant");
  const db = withRls(prisma, tenant.organizationId ?? "");  // RLS-scoped client
  const integrations = await db.integration.findMany();     // WHERE clause optional with RLS
```

The application-level `where: { organizationId }` filters can remain as defense-in-depth — the database policy is the safety net, not the primary enforcement.

---

### ISSUE 7 — Agent Loop Has No Token Budget, No Consecutive Failure Circuit Breaker (HIGH)

**Affected files:**
- `packages/agents/src/providers/vercel-ai.ts` — `stopWhen: stepCountIs(this.config.maxSteps ?? 20)`
- `packages/agents/src/orchestrator.ts` — `chat()`, `pipeline()`, `broadcast()` methods
- `packages/agents/src/types.ts` — `AgentConfig.maxSteps` only

**Why this is debt:**

The only termination condition is `maxSteps` (default 20), implemented via the Vercel AI SDK's `stepCountIs` stopper. There is no:
- Token budget (a 20-step agent could consume 200K tokens per request)
- Consecutive tool failure circuit breaker
- Wall-clock timeout
- User abort signal propagation

The `broadcast()` method fans out to ALL registered agents simultaneously with `Promise.all()`. If there are 10 registered agents and each runs 20 steps, one `broadcast` call can generate 200 LLM API calls with no budget ceiling.

The `checkAgentQuota` function checks in-memory credit balance (which starts at zero after each restart, so the `catch` branch always fires, returning `{ allowed: true, remaining: -1 }` — unlimited quota for everyone). The quota check is structurally present but functionally disabled.

**Fix (next sprint — 1 day):**

1. Add `tokenBudget` and `timeoutMs` to `AgentConfig`:
```typescript
export interface AgentConfig {
  // ...existing...
  readonly maxSteps?: number;      // default 20
  readonly tokenBudget?: number;   // max total tokens per execution (e.g. 50000)
  readonly timeoutMs?: number;     // wall-clock limit (e.g. 30000)
}
```

2. In `BaseAgent.run()`, enforce the budget:
```typescript
async run(messages, context): Promise<AgentResponse> {
  const budget = this.config.tokenBudget ?? 50_000;
  const timeout = this.config.timeoutMs ?? 30_000;

  const timer = new Promise<never>((_, reject) =>
    setTimeout(() => reject(new Error("Agent execution timeout")), timeout)
  );

  const execution = this._doExecute(messages, context, budget);

  return Promise.race([execution, timer]);
}
```

3. Fix `checkAgentQuota` to fail-safe closed, not open:
```typescript
} catch (err) {
  // Billing unavailable — deny to prevent unmetered usage
  logger.error("Billing quota check failed — denying agent execution", { tenantId, err });
  return { allowed: false, remaining: 0 };
}
```

---

### ISSUE 8 — Feature Flags Have No Lifecycle (MEDIUM)

**Affected file:**
- `packages/feature-flags/src/index.ts` — `FLAGS` constant, `FeatureFlagContext` interface

**Why this is debt:**

The `FLAGS` object has 25+ entries. None of them carry metadata about owner, creation date, expiry, or deprecation state. There is no mechanism to track which flags are for shipped features vs. active experiments vs. dead code.

The flag system uses environment variables as the store: `FEATURE_FLAG_${FLAG}_true/false`. This means:
- No audit trail of flag changes
- No way to answer "which flags have been on for more than 6 months?"
- No flag expiry — flags accumulate forever
- The admin `flagOverrides` Map (in-memory, lost on restart) is the only runtime override mechanism

The `FeatureFlagContext.plan` field is typed as `"free" | "pro" | "enterprise"` (lowercase) while the billing system uses `"FREE" | "PRO" | "ENTERPRISE"` (uppercase). Every plan-based flag check will silently fail because `"pro" !== "PRO"`.

**Fix:**

1. **Fix today — type mismatch (15 minutes):**
```typescript
// packages/feature-flags/src/index.ts line 14
plan?: "FREE" | "PRO" | "ENTERPRISE";  // match billing Plan type
```

2. **Next sprint — add lifecycle fields to FLAGS:**
```typescript
export const FLAGS: Record<string, FlagDefinition> = {
  AI_VISION: {
    key: "ai-vision",
    owner: "platform",
    createdAt: "2025-01-15",
    expiresAt: "2026-01-15",  // requires review or removal
    description: "AI vision capabilities (camera input)",
  },
  // ...
}
```

3. Add a CI check that flags older than their `expiresAt` fail the build, forcing a deliberate renewal or removal.

---

### ISSUE 9 — S2S Auth Has a Dev-Mode Bypass That Ships to Production (MEDIUM)

**Affected file:**
- `apps/api-gateway/src/middlewares/tenantContext.ts` lines 53-64

**Why this is debt:**

When `SERVICE_SECRET` is not set, `verifyServiceToken` returns `true`:

```typescript
const secret = process.env.SERVICE_SECRET;
if (!secret) {
  // Dev mode: allow headers without verification
  return true;
}
```

This means any request that includes `x-service-token: <any-value>` with arbitrary `x-user-id`, `x-organization-id`, `x-role` headers will be trusted as an authenticated service call — impersonating any user at any plan level — if `SERVICE_SECRET` is not set in the deployment environment.

If a production deployment is missing `SERVICE_SECRET` (environment variable not set, secret misconfigured, new pod starts before secret is injected), the entire S2S auth layer silently disables. The warning is logged once (`serviceSecretWarningLogged` prevents repetition), but the system continues accepting spoofed service calls.

**Fix (fix today — 10 minutes):**

```typescript
if (!secret) {
  if (process.env.NODE_ENV === "production") {
    // Hard failure in production — no service calls without a valid secret
    logger.error("SERVICE_SECRET is not set in production — rejecting S2S call");
    return false;
  }
  // Dev/test: allow without verification, log loudly
  logger.warn("SERVICE_SECRET not set — S2S verification bypassed (dev mode)");
  return true;
}
```

Longer term: add `SERVICE_SECRET` to the startup env validation (`infra/scripts/check-env.ts`) so the process refuses to start rather than silently degrading.

---

### ISSUE 10 — Rate Limiter Is Per-Process, Not Per-Deployment (MEDIUM)

**Affected files:**
- `packages/rate-limit/src/tokenBucket.ts` line 160: `const rateLimiters: Map<...>`
- `apps/api-gateway/src/middlewares/rateLimit.ts` — uses `getRateLimiter(plan)`

**Why this is debt:**

The `getRateLimiter` function returns an in-memory `TokenBucket`. In a multi-pod deployment (3 pods behind a load balancer), each pod has its own independent bucket. A client making 300 requests per minute to a plan with `maxTokens: 100` will succeed if their requests are distributed evenly — they experience effectively 300 tokens per minute, 3× the intended limit.

The code already ships a `RedisTokenBucket` in the same file and `@nebutra/cache` provides an Upstash Redis client. The infrastructure is present; the wiring is absent.

Additionally, `createEndpointRateLimit` in `rateLimit.ts` creates a new `Map<string, {count, resetAt}>` closure per call — meaning if this factory is called inside a route definition (per-request), the rate limit resets on every request. This is only safe if the factory is called once at module initialization.

**Fix (next sprint — 4 hours):**

```typescript
// packages/rate-limit/src/tokenBucket.ts

import { getRedis } from "@nebutra/cache";

const redisLimiters: Map<string, RedisTokenBucket> = new Map();

export function getRateLimiter(plan: string): RedisTokenBucket {
  if (!redisLimiters.has(plan)) {
    const config = PLAN_LIMITS[plan as keyof typeof PLAN_LIMITS] ?? PLAN_LIMITS.FREE;
    const redis = getRedis();
    redisLimiters.set(plan, new RedisTokenBucket(config, redis));
  }
  return redisLimiters.get(plan)!;
}
```

The in-memory `TokenBucket` can remain as a development fallback when Redis is unavailable.

---

## What Is Working Well

The following were verified as actually wired end-to-end (not just declared):

**Circuit breakers on external services.** `apps/api-gateway/src/services/circuitBreaker.ts` exports `billingServiceBreaker` and `aiServiceBreaker`, both used in their respective route handlers. This prevents cascading failures when Stripe or the AI service degrades.

**HMAC implementation in `tenantContextMiddleware`.** The HMAC verification logic at `tenantContext.ts` lines 66-80 is correct — it uses `timingSafeEqual` (preventing timing attacks), the canonical string format is consistent, and hex decoding handles malformed input with a `try/catch`. The _saga_ doesn't use it correctly (Issue 4), but the verification side is sound.

**Inngest GDPR deletion is genuinely idempotent.** The `processGdprDeletion` function (`apps/api-gateway/src/inngest/functions/gdprDeletion.ts`) uses `step.run()` wrappers for each phase. Inngest's `step.run()` persists step results — retrying the function after a partial failure replays only incomplete steps. Each step is also intrinsically idempotent (`updateMany` where data is already anonymized is a no-op, `deleteMany` where rows are already gone is a no-op).

**Architecture dependency tests exist.** `tests/architecture/dependency-flow.test.ts` uses `fast-check` property testing to verify the UI package dependency hierarchy. This is genuine policy-in-code for the frontend layer. The gap is that no equivalent test exists for the backend layer (no test preventing route files from importing Prisma directly, no test enforcing `requireAuth` coverage).

---

## Prioritized Fix List

| Priority | Issue | Effort | Impact |
|----------|-------|--------|--------|
| P0 — Fix today | Issue 1: Missing `requireAuth` on AI/billing/search/integrations | 2h | CRITICAL — unauth API access |
| P0 — Fix today | Issue 9: S2S bypass returns `true` when `SERVICE_SECRET` missing | 30m | HIGH — full impersonation |
| P0 — Fix today | Issue 8 (partial): Feature flag plan type mismatch `"pro"` vs `"PRO"` | 15m | HIGH — silent billing bypass |
| P1 — This week | Issue 2: Encrypt integration credentials via `@nebutra/vault` | 1d | CRITICAL — credential exposure |
| P1 — This week | Issue 3 (partial): Persist credit deductions and usage to DB | 1d | HIGH — billing data loss |
| P1 — This week | Issue 4: Fix saga S2S HMAC + remove mock payment path | 2h | HIGH — fake payments |
| P2 — Next sprint | Issue 5: Add idempotency keys to saga steps | 1d | HIGH — double-charges |
| P2 — Next sprint | Issue 6: Wire `withRls()` + PostgreSQL RLS policies | 2d | HIGH — tenant isolation |
| P2 — Next sprint | Issue 7: Add token budget + fail-safe-closed quota | 1d | HIGH — cost blowout |
| P3 — Backlog | Issue 10: Redis-backed rate limiter | 4h | MEDIUM — rate limit bypass |
| P3 — Backlog | Issue 8 (full): Feature flag lifecycle fields + CI expiry check | 1d | MEDIUM — flag accumulation |
| P3 — Backlog | Architecture test for route auth coverage | 2h | MEDIUM — prevents regression |

---

## Debt Root Cause Pattern

Seven of the ten issues share a single root cause: **policy expressed only as a comment or a "TODO in production" stub, with no code enforcement.**

- `// Encrypt in app layer` — no vault call
- `// In production, batch insert to database here` — commented out
- `// We'll mock the S2S headers for now` — ships in production
- `// Failsafe open if billing is unconfigured` — returns unlimited quota
- `// Dev mode: allow headers without verification` — no production guard

Each of these is a comment that a new engineer will read, trust, and leave alone. The fix pattern is the same in every case: move the intent from the comment into code that either enforces the invariant or fails loudly when the prerequisite is missing. The vault package, the Redis token bucket, and the HMAC signing utility are all already built — they just need to be called.
