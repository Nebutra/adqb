#!/usr/bin/env python3
"""Human-assisted scoring tool for ADQB benchmark results."""

import argparse
import json
from pathlib import Path


def load_ground_truth(benchmark_dir: Path) -> list[dict]:
    with open(benchmark_dir / "ground-truth.json") as f:
        return json.load(f)


def load_output(run_dir: Path) -> str:
    with open(run_dir / "output.md") as f:
        return f.read()


def score_issue(issue: dict, output: str) -> dict:
    """Interactive scoring for a single issue."""
    print(f"\n{'='*70}")
    print(f"Issue: {issue['id']} — {issue['title']}")
    print(f"Severity: {issue['severity']} (weight: {issue['weight']})")
    print(f"Category: {issue['category']} | Context scope: {issue['context_scope']}")
    print(f"Files: {', '.join(issue['files'])}")
    print(f"\nGround truth: {issue['description']}")
    print(f"Why debt: {issue['why_debt']}")
    print(f"{'='*70}")

    # Check if output mentions the issue
    print("\nDoes the audit output identify this issue?")
    print("  2 = Full match (correct file + correct problem)")
    print("  1 = Partial match (right area, related but different problem)")
    print("  0 = Not found")
    while True:
        try:
            match_score = int(input("Match score [0/1/2]: "))
            if match_score in (0, 1, 2):
                break
        except ValueError:
            pass
        print("Please enter 0, 1, or 2")

    if match_score == 0:
        return {
            "issue_id": issue["id"],
            "match": 0.0,
            "specificity": 0,
            "insight_depth": 0,
            "weighted_score": 0.0,
            "notes": "",
        }

    match_value = 1.0 if match_score == 2 else 0.5

    # Specificity
    print("\nHow concrete is the recommended fix?")
    print("  0 = Direction only ('should use database')")
    print("  1 = Approach described ('replace Map with Prisma query, cache in Redis')")
    print("  2 = Inline code fix with file paths")
    while True:
        try:
            specificity = int(input("Specificity [0/1/2]: "))
            if specificity in (0, 1, 2):
                break
        except ValueError:
            pass

    # Insight depth
    print("\nHow deep is the 'why' explanation?")
    print("  0 = Describes symptom only ('this uses in-memory storage')")
    print("  1 = Explains mechanism ('process restart wipes state')")
    print("  2 = Root cause + downstream impact ('creates 3 diverging counters...')")
    while True:
        try:
            insight = int(input("Insight depth [0/1/2]: "))
            if insight in (0, 1, 2):
                break
        except ValueError:
            pass

    notes = input("Notes (optional): ").strip()

    return {
        "issue_id": issue["id"],
        "match": match_value,
        "specificity": specificity,
        "insight_depth": insight,
        "weighted_score": match_value * issue["weight"],
        "notes": notes,
    }


def compute_summary(scores: list[dict], ground_truth: list[dict], run_dir: Path) -> dict:
    """Compute aggregate metrics."""
    total_weight = sum(i["weight"] for i in ground_truth)
    weighted_recall = sum(s["weighted_score"] for s in scores) / total_weight

    found = [s for s in scores if s["match"] > 0]
    found_count = len(found)
    total_count = len(ground_truth)

    avg_specificity = sum(s["specificity"] for s in found) / max(len(found), 1)
    avg_insight = sum(s["insight_depth"] for s in found) / max(len(found), 1)

    # Load run metadata for efficiency
    run_meta_path = run_dir / "run_meta.json"
    total_tokens = 0
    if run_meta_path.exists():
        with open(run_meta_path) as f:
            meta = json.load(f)
            total_tokens = meta.get("total_tokens", 0)

    efficiency = total_tokens / max(sum(s["weighted_score"] for s in scores), 0.01)

    # Context scope breakdown
    scope_recall = {}
    for scope in ("local", "cross-file", "repo-wide"):
        scope_issues = [i for i in ground_truth if i["context_scope"] == scope]
        scope_scores = [s for s in scores if any(
            i["id"] == s["issue_id"] and i["context_scope"] == scope
            for i in ground_truth
        )]
        scope_weight = sum(i["weight"] for i in scope_issues)
        scope_found = sum(s["weighted_score"] for s in scope_scores)
        scope_recall[scope] = scope_found / max(scope_weight, 1)

    return {
        "weighted_recall": round(weighted_recall, 3),
        "raw_recall": f"{found_count}/{total_count}",
        "avg_specificity": round(avg_specificity, 2),
        "avg_insight_depth": round(avg_insight, 2),
        "token_efficiency": round(efficiency, 1),
        "total_tokens": total_tokens,
        "context_scope_recall": scope_recall,
    }


def main():
    parser = argparse.ArgumentParser(description="Score ADQB benchmark results")
    parser.add_argument("--benchmark", required=True)
    parser.add_argument("--run", required=True, help="Path to run directory")
    args = parser.parse_args()

    benchmark_dir = Path(f"benchmarks/{args.benchmark}")
    run_dir = Path(args.run)

    ground_truth = load_ground_truth(benchmark_dir)
    output = load_output(run_dir)

    print(f"Scoring run: {run_dir}")
    print(f"Ground truth: {len(ground_truth)} issues")
    print(f"\nOutput preview (first 500 chars):")
    print(output[:500])
    print("...\n")

    input("Press Enter to begin scoring each issue...")

    scores = []
    for issue in ground_truth:
        score = score_issue(issue, output)
        scores.append(score)
        print(f"  → {score['issue_id']}: match={score['match']}, "
              f"specificity={score['specificity']}, insight={score['insight_depth']}")

    # Save scores
    with open(run_dir / "scores.json", "w") as f:
        json.dump(scores, f, indent=2)

    # Compute and save summary
    summary = compute_summary(scores, ground_truth, run_dir)
    with open(run_dir / "summary.json", "w") as f:
        json.dump(summary, f, indent=2)

    print(f"\n{'='*70}")
    print("RESULTS")
    print(f"{'='*70}")
    print(f"Weighted Recall:    {summary['weighted_recall']:.1%}")
    print(f"Raw Recall:         {summary['raw_recall']}")
    print(f"Avg Specificity:    {summary['avg_specificity']:.2f}/2.00")
    print(f"Avg Insight Depth:  {summary['avg_insight_depth']:.2f}/2.00")
    print(f"Token Efficiency:   {summary['token_efficiency']:.0f} tokens/weighted-issue-point")
    print(f"\nContext scope recall:")
    for scope, recall in summary["context_scope_recall"].items():
        print(f"  {scope:12s}: {recall:.1%}")

    print(f"\nScores saved to {run_dir / 'scores.json'}")
    print(f"Summary saved to {run_dir / 'summary.json'}")


if __name__ == "__main__":
    main()
