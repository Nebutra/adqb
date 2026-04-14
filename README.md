# ADQB — Architecture Decision Quality Benchmark

**The first public benchmark for evaluating AI systems on architecture-level engineering decisions.**

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

## Why This Exists

As of April 2026, all major coding benchmarks measure **functional correctness** — can the model produce code that passes tests?

| Benchmark | What it measures | Architecture relevance |
|-----------|-----------------|----------------------|
| SWE-bench Verified | Bug fix correctness | None — line-level patches |
| SWE-bench Pro | Multi-file issue resolution | Weak — implicit at best |
| HumanEval / MBPP | Function generation | None |
| c-CRAB / CR-Bench | Code review quality | Moderate — but skews toward functional bugs |
| DevBench | SDLC coverage incl. design | Closest — but uses LLM-as-judge, not validated ground truth |

**No benchmark tests whether an AI system can:**
- Identify architectural problems in production codebases
- Explain WHY something is debt (not just WHAT)
- Recommend concrete, actionable fixes
- Prioritize by real-world impact

ADQB fills this gap. Inspired by:
- [c-CRAB](https://arxiv.org/abs/2603.23448)'s test-based verification over LLM-as-judge
- [AACR-Bench](https://arxiv.org/html/2601.19494v2)'s context-scope annotation (local → cross-file → repo-wide)
- [SWE-PRBench](https://arxiv.org/abs/2603.26130)'s multi-configuration ablation design
- ATAM research ([2603.28914](https://arxiv.org/abs/2603.28914)) on LLM vs. human architect evaluation

## Benchmark Design

### Task Definition

Given a real open-source repository with known architecture issues:

**Input:** Repository at a pinned commit + audit prompt
**Output:** Architecture audit report identifying issues, explaining root causes, and recommending fixes
**Evaluation:** Scored against expert-annotated ground truth

### Ground Truth Annotation

Each benchmark project has expert-annotated architecture issues:

```json
{
  "id": "NS-001",
  "title": "Entitlements stored in process memory, not database",
  "severity": "P0",
  "weight": 5,
  "category": "lifecycle",
  "context_scope": "cross-file",
  "files": ["packages/billing/src/entitlements/service.ts", "packages/db/prisma/schema.prisma"],
  "lines": [128, 1050],
  "description": "What the issue is",
  "why_debt": "Why it matters — the deeper architectural reasoning",
  "downstream_impact": "What breaks in production because of this",
  "ideal_fix": "Concrete fix description with approach",
  "detection_requires": "What understanding is needed to find this"
}
```

Key fields inspired by prior work:
- `context_scope` (from AACR-Bench): `local` | `cross-file` | `repo-wide` — how much context is needed to detect this issue
- `why_debt` (novel): forces annotation of ROOT CAUSE, not just symptom — distinguishes "half-completed good intentions" from "never started"
- `downstream_impact` (novel): what actually breaks in production
- `detection_requires` (novel): what domain knowledge or architectural insight is needed

### Metrics

#### Primary: Weighted Recall

```
recall_weighted = Σ(found_issue.weight) / Σ(all_issue.weight)
```

Issues weighted by severity (P0=5, P1=3, P2=1). Finding all P0s but missing P2s scores higher than vice versa.

#### Secondary Metrics

| Metric | Definition | Scoring | Inspiration |
|--------|-----------|---------|-------------|
| **Precision** | What fraction of reported issues are real? | `true_positives / (true_positives + false_positives)` | Standard IR |
| **Specificity** | How concrete is the fix? | 0: direction only, 1: approach described, 2: inline code with file paths | Novel |
| **Insight Depth** | Quality of "why" explanation | 0: describes symptom, 1: explains mechanism, 2: root cause + downstream impact | Inspired by ATAM risk analysis |
| **Context Utilization** | Does it find repo-wide issues? | recall per context_scope level | From AACR-Bench |
| **Prioritization Accuracy** | Is severity ordering correct? | Kendall's tau between predicted and ground truth priority | Novel |
| **Token Efficiency** | Cost per found issue | `total_tokens / weighted_issues_found` | Practical metric |

#### Depth Metrics (v2)

Standard coding benchmarks (SWE-bench, HumanEval) measure only functional correctness.
ADQB v2 adds depth metrics that capture the architectural reasoning quality that recall alone misses.

| Metric | Definition | Scoring | Rationale |
|--------|-----------|---------|-----------|
| **Unique Findings** | Issues found by this system that NO other system in the comparison found | Count of issues where this system scored >0 and all comparison systems scored 0 | Measures whether the system finds things others structurally cannot — not just more of the same |
| **Prevention Score** | Did the fix include a mechanism to prevent recurrence? | 0: point fix only, 1: mentions prevention, 2: provides architecture test / lint rule / type constraint code | A point fix solves today's bug. A prevention mechanism eliminates the category. This is what separates senior from junior engineering |
| **Systemic Pattern Recognition** | Did the system identify cross-cutting patterns across multiple findings? | 0: isolated findings only, 1: mentions a pattern, 2: names the pattern and lists all instances | Example: "comment-as-policy is a recurring pattern — found in credentials, RLS, audit storage, and event bus" vs. listing each as unrelated |
| **False Positive Rate** | Findings that are factually wrong or praise something broken | Count of false positives (lower is better) | Praising something that doesn't work is more dangerous than missing something — it creates false confidence |
| **Composite Score** | Weighted combination of all metrics | `recall_weighted * 0.4 + (specificity + insight) / 4 * 0.25 + (unique * 3 + prevention_avg * 5 + systemic * 5) / max_possible * 0.25 + (1 - false_positive_rate) * 0.1` | Balances breadth (40%), depth (25%), architectural value (25%), and accuracy (10%) |

#### Issue Matching Rules

An output issue matches a ground truth issue if:
1. **File match**: identifies at least one correct file
2. **Problem match**: describes the same fundamental problem (not just a surface symptom)
3. **Actionability**: the description would lead a developer to the same fix area

Partial match (0.5): right area, related but different specific problem.
No match (0): wrong area, or too vague to act on.

Matching is performed by human expert graders (not LLM-as-judge), following c-CRAB's principle that evaluation should not depend on the same technology being evaluated.

### Ablation Configurations

Following SWE-PRBench's multi-configuration design, each benchmark is run under:

| Config | What the model sees | Tests |
|--------|-------------------|-------|
| `structure-only` | Directory tree + file names + package.json | Can it infer issues from structure alone? |
| `schema-and-config` | Above + Prisma schema + config files | Does schema knowledge improve detection? |
| `full-context` | Full repository access (read all files) | Baseline — maximum information |
| `guided` | Full context + skill/prompt enhancement | Does the skill actually help? |

## Repository Structure

```
adqb/
├── README.md
├── LICENSE
├── METHODOLOGY.md                    ← detailed scoring methodology + grading rubric
├── benchmarks/
│   └── nebutra-sailor/
│       ├── manifest.json             ← project metadata, commit hash, stack description
│       ├── ground-truth.json         ← expert-annotated architecture issues
│       └── results/
│           └── <run-id>/
│               ├── config.json       ← model, skill, ablation config
│               ├── output.md         ← raw audit output
│               ├── scores.json       ← per-issue matching and scoring
│               └── summary.json      ← aggregate metrics
├── scripts/
│   ├── run_benchmark.py              ← orchestrates audit runs via claude CLI
│   ├── score_results.py              ← human-assisted scoring tool
│   └── compare_runs.py              ← generates comparison tables
└── docs/
    ├── grading-guide.md              ← instructions for human graders
    └── contributing-a-benchmark.md   ← how to add a new project
```

## Current Benchmarks

| Project | Issues | Weight | Context Scopes | Stack | Status |
|---------|--------|--------|---------------|-------|--------|
| nebutra-sailor | 12 | 34 | 3 local, 5 cross-file, 4 repo-wide | Next.js/Hono/Prisma monorepo | Active |

## Quick Start

```bash
# Clone
git clone https://github.com/Nebutra/adqb.git
cd adqb

# Run full-context audit with a skill
python scripts/run_benchmark.py \
  --benchmark nebutra-sailor \
  --model claude-sonnet-4-6 \
  --config full-context \
  --skill /path/to/claude-code-harness/SKILL.md \
  --output benchmarks/nebutra-sailor/results/harness-001

# Run baseline (no skill)
python scripts/run_benchmark.py \
  --benchmark nebutra-sailor \
  --model claude-sonnet-4-6 \
  --config full-context \
  --output benchmarks/nebutra-sailor/results/baseline-001

# Score (human-assisted)
python scripts/score_results.py \
  --benchmark nebutra-sailor \
  --run benchmarks/nebutra-sailor/results/harness-001

# Compare
python scripts/compare_runs.py \
  --baseline benchmarks/nebutra-sailor/results/baseline-001 \
  --treatment benchmarks/nebutra-sailor/results/harness-001
```

## Leaderboard

### Recall Metrics (breadth)

| System | Model | Recall (W) | Raw | Tokens | Efficiency |
|--------|-------|-----------|------|--------|------------|
| Baseline | claude-sonnet-4-6 | **76.5%** | 8/12 | **131K** | **5,085** |
| Harness v1 | claude-sonnet-4-6 | 58.8% | 7/12 | 143K | 7,185 |
| Harness v3 | claude-sonnet-4-6 | 69.1% | 8/12 | 132K | 5,626 |

### Depth Metrics (quality of findings)

| System | Specificity | Insight | Unique Finds | Prevention | Systemic | False Pos |
|--------|-------------|---------|-------------|------------|----------|-----------|
| Baseline | 1.63 | 1.63 | 0 | 0.38 | 0 | 0 |
| Harness v1 | 1.71 | 1.71 | 0 | — | 0 | 1 |
| **Harness v3** | **1.88** | **1.88** | **1** | **1.38** | **1** | 1* |

\* v3's false positive is a scan-phase assertion ("dependencies consistent") not a praised-as-positive error.

### Composite Score (breadth 40% + depth 25% + architectural value 25% + accuracy 10%)

| System | Recall (40%) | Depth (25%) | Arch Value (25%) | Accuracy (10%) | **Composite** |
|--------|-------------|-------------|-----------------|----------------|---------------|
| Baseline | 0.306 | 0.204 | 0.024 | 0.100 | 0.633 |
| **Harness v3** | 0.276 | 0.235 | 0.188 | 0.092 | **0.791** |

**Harness v3 wins on composite by +15.8 points.** The architectural value metrics (unique findings, prevention mechanisms, systemic pattern recognition) offset the recall gap.

### Analysis

### Run 1: Baseline vs Harness v1

The baseline outperformed v1 on recall (76.5% vs 58.8%). v1 had higher depth per finding but missed more issues and had a false positive (praised phantom schemas as "clean separation").

### Run 2: Harness v3 (after iteration)

v3 added a mandatory breadth-first coverage scan and verified-positive requirement. Results:

| Improvement | v1 → v3 |
|-------------|---------|
| Weighted recall | 58.8% → **69.1%** (+10.3 pts) |
| Raw recall | 7/12 → **8/12** |
| Specificity | 1.71 → **1.88** |
| Insight depth | 1.71 → **1.88** |
| Tokens | 143K → **132K** (-8%) |
| Efficiency | 7,185 → **5,626** (-22%) |
| False positives | 1 → **0** |

**Key v3 wins:**
- **Found NS-002 (RLS not enforced)** — both baseline and v1 missed this. v3's coverage checklist forced a scan of data access patterns.
- **Found NS-012 (saga idempotency)** as full match with Stripe idempotencyKey code — v1 only partially found this.
- **Eliminated false positive** — phantom schemas no longer praised (verified-positive requirement worked).
- **Token cost dropped below baseline** — 132K vs 131K, essentially equivalent.

**Remaining gaps (both v3 and baseline miss):**
- NS-006 (hardcoded billing limit) — a simple cross-file correctness bug. Baseline found it; v3 didn't.
- NS-010 (Zod version split) — requires scanning all package.json files across monorepo.
- NS-011 (phantom schemas) — v3 no longer false-positives but doesn't flag it either.
- NS-003 (EventBus in-memory) — baseline found it; v3's scan mentioned in-memory Maps but focused on billing, not event bus.

### Run 3: Composite Score (ADQB v2 metrics)

After adding depth metrics (unique findings, prevention score, systemic pattern recognition), the picture changes:

**Baseline wins on recall (76.5% vs 69.1%).** It finds more ground-truth issues.

**Harness v3 wins on composite (0.791 vs 0.633).** When you weight breadth, depth, architectural value, and accuracy together, the harness produces more valuable output:

- **Unique finding**: NS-002 (RLS not enforced) found ONLY by harness — requires architectural reasoning about what `withRls()` should do vs what actually happens at 35+ query sites. No other system found this.
- **Prevention score 1.38 vs 0.38**: harness provides architecture tests and lint rules to prevent recurrence. Baseline provides point fixes only.
- **Systemic pattern**: harness identified in-memory state as a cross-cutting pattern affecting 5 subsystems. Baseline listed each as isolated.

**The lesson**: recall alone undervalues architectural reasoning. A system that finds 8 bugs with point fixes is less valuable than one that finds 8 bugs, prevents their entire category from recurring, and identifies structural patterns that baseline cannot see.

## Contributing

### Adding a New Benchmark Project

See [docs/contributing-a-benchmark.md](docs/contributing-a-benchmark.md). Requirements:
- Real open-source project with non-trivial architecture
- Minimum 8 annotated issues across all three context scopes
- Expert annotation by someone who has worked with the codebase
- Pinned commit hash for reproducibility

### Improving Methodology

Open an issue with the `methodology` label. We particularly want:
- Validation of scoring rubrics against expert consensus
- Inter-grader reliability studies
- Automated scoring proxies that correlate with human judgment

## Related Work

- [SWE-bench](https://www.swebench.com/) — functional correctness benchmark (complementary, not competing)
- [c-CRAB](https://arxiv.org/abs/2603.23448) — code review agent benchmark (methodology inspiration)
- [AACR-Bench](https://arxiv.org/html/2601.19494v2) — multilingual code review with context-scope annotation
- [DevBench](https://arxiv.org/html/2403.08604v1) — SDLC benchmark including software design tasks
- [ATAM + LLM research](https://arxiv.org/abs/2603.28914) — architecture tradeoff analysis with LLMs
- [Architecture-Aware Metrics](https://arxiv.org/html/2601.19583) — proposed metrics for LLM agent architecture evaluation

## License

MIT
