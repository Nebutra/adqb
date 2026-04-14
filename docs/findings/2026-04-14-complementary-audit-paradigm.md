# Finding: Complementary Audit Paradigm

**Date:** 2026-04-14
**Validated across:** Nebutra Sailor (known codebase) + Dub.co (held-out)
**Models tested:** claude-sonnet-4-6

## Core Finding

SKILL-augmented and baseline (no-skill) audits find **fundamentally different categories of issues** with near-zero overlap. They are not competing approaches — they are complementary lenses.

## Evidence

### Nebutra Sailor (12 ground-truth issues)

| Issue | Baseline | Harness |
|-------|----------|---------|
| NS-001 Entitlements in memory | Found | Found |
| NS-002 RLS not enforced | **Missed** | **Found (unique)** |
| NS-003 EventBus in memory | Found | Missed |
| NS-004 Plaintext credentials | Found | Found |
| NS-005 Auth guards missing | Found | Partial |
| NS-006 Hardcoded billing limit | Found | **Missed** |
| NS-007 Mock payment path | Found | Found |
| NS-008 S2S HMAC mismatch | Found | Found |
| NS-009 Dual feature flags | Missed | Found |
| NS-010 Zod version split | Missed | Missed |
| NS-011 Phantom schemas | Missed | Missed |
| NS-012 Saga no idempotency | Partial | Found |

### Dub.co (14 ground-truth issues)

| Issue | Baseline | Harness |
|-------|----------|---------|
| DUB-001 Plaintext passwords | Found | Found |
| DUB-002 Dangerous email linking | **Found** | Missed |
| DUB-003 linksUsage not decremented | **Found** | Missed |
| DUB-004 console.log auth creds | **Found** | Missed |
| DUB-005 Account lockout no notify | **Found** | Missed |
| DUB-006 Webhook cache miss drops | Found | Missed |
| DUB-007 Malicious check skips domains | Found | Missed |
| DUB-008 Stripe webhook 4xx retry | Missed | **Found (unique)** |
| DUB-009 Webhook secret in GET API | Missed | **Found (unique)** |
| DUB-010 Feature flag default true | Missed | **Found (unique)** |
| DUB-011 Cron unauth outside Vercel | Missed | **Found (unique)** |
| DUB-012 bulkCreate usage drift | Found | Missed |
| DUB-013 No repository layer | Missed | **Found (unique)** |
| DUB-014 74+ hardcoded plan strings | Missed | **Found (unique)** |

## Pattern

| Dimension | Baseline | Harness |
|-----------|----------|---------|
| **Detection method** | Surface pattern matching (grep-able) | Cross-file architectural reasoning |
| **Typical finds** | Dangerous flag names, TODO comments, console.log, counter bugs | Stripe retry semantics, secret exposure in API design, failsafe defaults, missing abstraction layers |
| **Detection difficulty** | Moderate — experienced reviewer finds in 10 min | High — requires understanding interaction semantics across subsystems |
| **Fix quality** | Point fixes (change this line) | Prevention mechanisms (architecture tests, idempotency patterns, lint rules) |
| **Prevention score** | 0.0–0.38 | 1.14–1.38 |

## Implication for Practitioners

**Do not choose between SKILL and no-SKILL. Run both.**

The optimal audit workflow:
1. Run baseline (no skill) — catches surface bugs, TODOs, pattern-match issues
2. Run harness (with skill) — catches architectural debt, semantic mismatches, systemic patterns
3. Merge and deduplicate findings
4. Prioritize using the harness's prevention scores to identify which fixes prevent entire categories

## Implication for Benchmark Design

Recall alone is an insufficient metric for architecture audit quality. A system that finds 8 surface bugs with point fixes is less valuable than one that finds 7 architectural issues with prevention mechanisms — but recall ranks the first higher.

The ADQB composite score (recall 40% + depth 25% + architectural value 25% + accuracy 10%) better reflects real-world audit value.

## Limitations

- Validated on only 2 projects (1 known, 1 held-out)
- Single model (claude-sonnet-4-6) — pattern may differ on other models
- Ground truth established from union of both audits, not independent expert annotation
- Need more held-out projects to confirm generalizability
