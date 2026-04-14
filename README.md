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

| System | Model | Config | Recall (W) | Raw | Specificity | Insight | Tokens | Efficiency |
|--------|-------|--------|-----------|------|-------------|---------|--------|------------|
| Baseline (no skill) | claude-sonnet-4-6 | full-context | **76.5%** | 8/12 | 1.63 | 1.63 | 131K | 5,085 |
| Claude Code Harness | claude-sonnet-4-6 | guided | 58.8% | 7/12 | **1.71** | **1.71** | 143K | 7,185 |

### Analysis

The baseline **outperformed** the harness on weighted recall (76.5% vs 58.8%) in this first run. Key observations:

- **Baseline found more ground-truth issues** (8 vs 7), particularly NS-005 (missing auth guards) and NS-006 (hardcoded billing limit) which the harness missed
- **Harness had higher specificity and insight depth** per found issue (1.71 vs 1.63) — when it found something, the analysis was deeper
- **Harness found unique bonus issues** not in ground truth: dead Stripe billing sync, GDPR Redis gap, Python AI service no auth, OrderItem missing timestamps
- **Harness had a false positive**: praised phantom PostgreSQL schemas as "clean separation" when they are actually empty — a dangerous miss
- **Baseline found 9 bonus issues** (audit type mismatch, subscription status inconsistency, saga compensation swallowing, etc.)
- **Both missed**: NS-002 (RLS not enforced), NS-010 (Zod version split)

**Verdict**: The harness needs iteration. It improved depth-per-finding but reduced breadth. The SKILL's routing toward "architectural thinking" may have caused it to spend more tokens on fewer, deeper analyses rather than casting a wider net. The false positive (praising phantom schemas) is a specific failure mode where the SKILL's "what is working well" output standard led to over-generous assessment.

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
