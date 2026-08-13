#!/usr/bin/env python3
import argparse
import json
from pathlib import Path


def load_json(path, default):
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def latest_with(predicate, runs):
    for run in reversed(runs):
        if predicate(run):
            return run
    return None


def attempt_summary(attempt):
    return {
        "attempt": attempt["attempt"],
        "validation_passed": attempt["validation"]["passed"],
        "restored": attempt["restored"],
        "sql": attempt["sql"],
    }


def write_markdown(path, lines):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description="Summarize Step 10 failure-analysis cases from experiment logs.")
    parser.add_argument("--batch", default="results/batch_summary.json", help="Batch summary JSON.")
    parser.add_argument("--explore-history", default="logs/explore_runs_pretty.json", help="Exploration pretty JSON history.")
    parser.add_argument("--out", default="results/case_analysis.md", help="Markdown output.")
    parser.add_argument("--json-out", default="results/case_analysis.json", help="JSON output.")
    args = parser.parse_args()

    batch = load_json(Path(args.batch), [])
    explore_runs = load_json(Path(args.explore_history), [])

    helped = latest_with(
        lambda run: run.get("success")
        and len(run.get("attempts", [])) > 1
        and any(attempt.get("restored") for attempt in run["attempts"][:-1]),
        explore_runs,
    )
    did_not_help = latest_with(
        lambda run: not run.get("success") and any(attempt.get("restored") for attempt in run.get("attempts", [])),
        explore_runs,
    )

    overhead_cases = []
    for item in batch:
        linear = item["linear"]
        checkpoint = item["checkpoint"]
        if linear["success"] and checkpoint["success"] and checkpoint["num_restores"] == 0:
            overhead_cases.append(
                {
                    "task_id": item["task_id"],
                    "type": item["type"],
                    "table": item["schema_summary"]["table"],
                    "linear_runtime": linear["runtime_seconds"],
                    "checkpoint_runtime": checkpoint["runtime_seconds"],
                    "overhead_seconds": checkpoint["runtime_seconds"] - linear["runtime_seconds"],
                }
            )

    payload = {
        "checkpoint_helped": None
        if helped is None
        else {
            "task_id": helped["task_id"],
            "question": helped["question"],
            "accepted_attempt": helped["accepted_attempt"],
            "attempts": [attempt_summary(attempt) for attempt in helped["attempts"]],
        },
        "checkpoint_did_not_help": None
        if did_not_help is None
        else {
            "task_id": did_not_help["task_id"],
            "question": did_not_help["question"],
            "attempts": [attempt_summary(attempt) for attempt in did_not_help["attempts"]],
        },
        "overhead_not_worth_it_candidates": overhead_cases,
    }

    lines = [
        "# Step 10 Case Analysis",
        "",
        "## Case 1: Checkpointing Helped",
        "",
    ]
    if helped is None:
        lines.append("No successful retry-after-restore case found yet.")
    else:
        lines.extend(
            [
                f"- Task: `{helped['task_id']}`",
                f"- Question: {helped['question']}",
                f"- Accepted attempt: `{helped['accepted_attempt']}`",
                "",
                "Evidence:",
                "",
            ]
        )
        for attempt in helped["attempts"]:
            lines.append(
                f"- Attempt {attempt['attempt']}: validation={attempt['validation']['passed']}, restored={attempt['restored']}, SQL=`{attempt['sql']}`"
            )
        lines.extend(
            [
                "",
                "Interpretation to write yourself:",
                "",
                "> Checkpointing helped because the first mutation executed but failed semantic validation. The system restored the pre-mutation state and accepted a later candidate.",
            ]
        )

    lines.extend(["", "## Case 2: Checkpointing Did Not Help", ""])
    if did_not_help is None:
        lines.append("No all-retries-failed case found yet.")
    else:
        lines.extend(
            [
                f"- Task: `{did_not_help['task_id']}`",
                f"- Question: {did_not_help['question']}",
                "",
                "Evidence:",
                "",
            ]
        )
        for attempt in did_not_help["attempts"]:
            lines.append(
                f"- Attempt {attempt['attempt']}: validation={attempt['validation']['passed']}, restored={attempt['restored']}, SQL=`{attempt['sql']}`"
            )
        lines.extend(
            [
                "",
                "Interpretation to write yourself:",
                "",
                "> Checkpointing preserved the database, but it did not solve the task because every proposed retry still violated the post-condition.",
            ]
        )

    lines.extend(["", "## Case 3: Overhead Not Worth It", ""])
    if not overhead_cases:
        lines.append("No clean overhead cases found yet.")
    else:
        lines.append("Reference-SQL batch cases where both modes succeeded and checkpoint mode did not restore:")
        lines.append("")
        lines.append("| task_id | type | table | linear_s | checkpoint_s | overhead_s |")
        lines.append("| --- | --- | --- | ---: | ---: | ---: |")
        for case in overhead_cases:
            lines.append(
                f"| {case['task_id']} | {case['type']} | {case['table']} | {case['linear_runtime']:.4f} | {case['checkpoint_runtime']:.4f} | {case['overhead_seconds']:.4f} |"
            )
        lines.extend(
            [
                "",
                "Interpretation to write yourself:",
                "",
                "> When the first SQL action is already correct, checkpointing mainly adds dump overhead. Its value appears when actions are uncertain or validation can reject unsafe mutations.",
            ]
        )

    write_markdown(Path(args.out), lines)
    Path(args.json_out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.json_out).write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    print(f"Wrote Markdown analysis: {args.out}")
    print(f"Wrote JSON analysis: {args.json_out}")


if __name__ == "__main__":
    main()
