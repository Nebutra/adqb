#!/usr/bin/env python3
"""Compare two ADQB benchmark runs."""

import argparse
import json
from pathlib import Path


def load_run(run_dir: Path) -> tuple[dict, dict, list]:
    config = json.loads((run_dir / "config.json").read_text())
    summary = json.loads((run_dir / "summary.json").read_text())
    scores = json.loads((run_dir / "scores.json").read_text())
    return config, summary, scores


def main():
    parser = argparse.ArgumentParser(description="Compare two ADQB runs")
    parser.add_argument("--baseline", required=True)
    parser.add_argument("--treatment", required=True)
    parser.add_argument("--output", default=None, help="Save comparison as markdown")
    args = parser.parse_args()

    b_config, b_summary, b_scores = load_run(Path(args.baseline))
    t_config, t_summary, t_scores = load_run(Path(args.treatment))

    lines = []
    lines.append("# ADQB Run Comparison\n")
    lines.append(f"| | Baseline | Treatment | Delta |")
    lines.append(f"|--|----------|-----------|-------|")
    lines.append(f"| **Model** | {b_config['model']} | {t_config['model']} | |")
    lines.append(f"| **Config** | {b_config['config']} | {t_config['config']} | |")
    lines.append(f"| **Skill** | {b_config.get('skill') or 'none'} | {t_config.get('skill') or 'none'} | |")

    # Metrics comparison
    metrics = [
        ("Weighted Recall", "weighted_recall", "{:.1%}", True),
        ("Raw Recall", "raw_recall", "{}", None),
        ("Avg Specificity", "avg_specificity", "{:.2f}", True),
        ("Avg Insight Depth", "avg_insight_depth", "{:.2f}", True),
        ("Token Efficiency", "token_efficiency", "{:.0f}", False),  # lower is better
        ("Total Tokens", "total_tokens", "{:,}", False),
    ]

    lines.append(f"| | | | |")
    for label, key, fmt, higher_better in metrics:
        bv = b_summary[key]
        tv = t_summary[key]
        if isinstance(bv, str):
            delta = ""
        else:
            diff = tv - bv
            if higher_better is True:
                arrow = "+" if diff > 0 else ""
            elif higher_better is False:
                arrow = "" if diff > 0 else ""
            else:
                arrow = ""
            if isinstance(bv, float):
                delta = f"{arrow}{diff:+.3f}"
            else:
                delta = f"{arrow}{diff:+,}"
        lines.append(f"| **{label}** | {fmt.format(bv)} | {fmt.format(tv)} | {delta} |")

    # Context scope comparison
    lines.append(f"\n## Context Scope Recall\n")
    lines.append(f"| Scope | Baseline | Treatment | Delta |")
    lines.append(f"|-------|----------|-----------|-------|")
    for scope in ("local", "cross-file", "repo-wide"):
        bv = b_summary["context_scope_recall"].get(scope, 0)
        tv = t_summary["context_scope_recall"].get(scope, 0)
        diff = tv - bv
        lines.append(f"| {scope} | {bv:.1%} | {tv:.1%} | {diff:+.1%} |")

    # Per-issue comparison
    lines.append(f"\n## Per-Issue Comparison\n")
    lines.append(f"| Issue | Baseline Match | Treatment Match | Delta |")
    lines.append(f"|-------|---------------|-----------------|-------|")
    b_scores_map = {s["issue_id"]: s for s in b_scores}
    t_scores_map = {s["issue_id"]: s for s in t_scores}
    for issue_id in sorted(set(list(b_scores_map.keys()) + list(t_scores_map.keys()))):
        bm = b_scores_map.get(issue_id, {}).get("match", 0)
        tm = t_scores_map.get(issue_id, {}).get("match", 0)
        diff = tm - bm
        marker = " **NEW**" if bm == 0 and tm > 0 else (" LOST" if bm > 0 and tm == 0 else "")
        lines.append(f"| {issue_id} | {bm:.1f} | {tm:.1f} | {diff:+.1f}{marker} |")

    output_text = "\n".join(lines)
    print(output_text)

    if args.output:
        Path(args.output).write_text(output_text)
        print(f"\nSaved to {args.output}")


if __name__ == "__main__":
    main()
