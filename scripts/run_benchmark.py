#!/usr/bin/env python3
"""Run an ADQB benchmark using the Claude CLI."""

import argparse
import json
import os
import subprocess
import time
from pathlib import Path

AUDIT_PROMPT = """Audit this repository for tech debt and engineering quality.

Explore the codebase structure, read key files (package.json, prisma schema,
key source files, config files), and produce a tech debt report that:

1. Identifies the top architecture-level issues (not code style issues)
2. For each issue, explains WHY it's debt (not just what it is)
3. Includes specific file paths and line numbers
4. Recommends concrete fixes (inline code when possible)
5. Prioritizes by impact vs effort

Focus on: security gaps, lifecycle issues (missing termination/cleanup),
boundary violations (wrong abstractions), policy gaps (rules that exist
only as comments), and correctness bugs in business logic."""


def load_manifest(benchmark_dir: Path) -> dict:
    with open(benchmark_dir / "manifest.json") as f:
        return json.load(f)


def build_prompt(manifest: dict, config: str, skill_path: str | None) -> str:
    prompt_parts = []

    if skill_path:
        prompt_parts.append(
            f"First, read the skill file at {skill_path} and ALL reference files "
            f"in the same directory tree. Follow the skill's guidance."
        )

    prompt_parts.append(AUDIT_PROMPT)
    prompt_parts.append(f"\nRepository: {manifest['repo']}")
    prompt_parts.append(f"Stack: {manifest['stack']}")
    prompt_parts.append(f"Description: {manifest['description']}")

    if config == "structure-only":
        prompt_parts.append(
            "\nConstraint: Only examine directory structure, file names, "
            "and package.json files. Do NOT read source code."
        )
    elif config == "schema-and-config":
        prompt_parts.append(
            "\nConstraint: Only examine directory structure, package.json, "
            "Prisma schema, and config files. Do NOT read route handlers or "
            "business logic source code."
        )

    return "\n\n".join(prompt_parts)


def run_claude(prompt: str, model: str, repo_path: str, output_file: str) -> dict:
    """Run claude CLI with the given prompt and capture output."""
    cmd = [
        "claude",
        "-p", prompt,
        "--model", model,
        "--output-format", "json",
        "--max-turns", "50",
    ]

    start = time.time()
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        cwd=repo_path,
        timeout=600,
    )
    duration_ms = int((time.time() - start) * 1000)

    # Parse JSON output
    try:
        output = json.loads(result.stdout)
    except json.JSONDecodeError:
        output = {"raw_stdout": result.stdout, "raw_stderr": result.stderr}

    # Extract text content and usage
    text_content = ""
    total_tokens = 0
    if isinstance(output, dict):
        if "result" in output:
            text_content = output["result"]
        if "usage" in output:
            total_tokens = output["usage"].get("total_tokens", 0)

    return {
        "text": text_content,
        "total_tokens": total_tokens,
        "duration_ms": duration_ms,
        "exit_code": result.returncode,
    }


def main():
    parser = argparse.ArgumentParser(description="Run ADQB benchmark")
    parser.add_argument("--benchmark", required=True, help="Benchmark name")
    parser.add_argument("--model", default="claude-sonnet-4-6")
    parser.add_argument("--config", default="full-context",
                       choices=["structure-only", "schema-and-config", "full-context", "guided"])
    parser.add_argument("--skill", default=None, help="Path to SKILL.md")
    parser.add_argument("--repo", default=None, help="Path to repo (overrides manifest)")
    parser.add_argument("--output", required=True, help="Output directory for results")
    args = parser.parse_args()

    benchmark_dir = Path(f"benchmarks/{args.benchmark}")
    manifest = load_manifest(benchmark_dir)
    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)

    repo_path = args.repo or os.path.expanduser(f"~/Documents/Nebutra-SaaS-Lab/Nebutra-Sailor")

    # Determine effective skill path
    skill_path = args.skill if args.config == "guided" or args.skill else None

    prompt = build_prompt(manifest, args.config, skill_path)

    # Save config
    config = {
        "benchmark": args.benchmark,
        "model": args.model,
        "config": args.config,
        "skill": args.skill,
        "repo_path": repo_path,
        "commit": manifest["commit"],
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    with open(output_dir / "config.json", "w") as f:
        json.dump(config, f, indent=2)

    print(f"Running ADQB benchmark: {args.benchmark}")
    print(f"  Model: {args.model}")
    print(f"  Config: {args.config}")
    print(f"  Skill: {args.skill or 'none'}")
    print(f"  Output: {output_dir}")
    print()

    result = run_claude(prompt, args.model, repo_path, str(output_dir / "output.md"))

    # Save raw output
    with open(output_dir / "output.md", "w") as f:
        f.write(result["text"])

    # Save run metadata
    run_meta = {
        "total_tokens": result["total_tokens"],
        "duration_ms": result["duration_ms"],
        "exit_code": result["exit_code"],
    }
    with open(output_dir / "run_meta.json", "w") as f:
        json.dump(run_meta, f, indent=2)

    print(f"Done. Tokens: {result['total_tokens']}, Duration: {result['duration_ms']}ms")
    print(f"Output saved to {output_dir / 'output.md'}")
    print(f"\nNext step: python scripts/score_results.py --benchmark {args.benchmark} --run {output_dir}")


if __name__ == "__main__":
    main()
