#!/usr/bin/env python3
import argparse
import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from auto_verify import infer_verifier
from build_agent_prompt import build_prompt
from load_task import select_task
from run_linear import append_jsonl, append_pretty_json_array, write_json


def clean_sql(text):
    stripped = text.strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        stripped = "\n".join(lines).strip()
    return stripped


def generate_sql(prompt, model):
    try:
        from openai import OpenAI
    except ImportError as exc:
        raise SystemExit(
            "The OpenAI Python SDK is not installed. Install it with: python3 -m pip install openai"
        ) from exc

    client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))
    response = client.responses.create(
        model=model,
        instructions=(
            "You generate safe MySQL mutation statements for DBBench tasks. "
            "Return exactly one SQL statement and no explanation."
        ),
        input=prompt,
    )
    return clean_sql(response.output_text)


def run_checkpoint_attempt(args, sql):
    command = [
        "python3",
        "scripts/run_checkpoint.py",
        "--jsonl",
        args.jsonl,
        "--mutation-index",
        str(args.mutation_index),
        "--action-sql",
        sql,
        "--restore-on-fail",
    ]
    if args.expected_affected_rows is not None:
        command.extend(["--expected-affected-rows", str(args.expected_affected_rows)])
    if args.expected_row_delta is not None:
        command.extend(["--expected-row-delta", str(args.expected_row_delta)])
    if args.verify_sql:
        command.extend(["--verify-sql", args.verify_sql])

    result = subprocess.run(
        command,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    checkpoint_log = json.loads(Path("tmp/checkpoint_run.json").read_text(encoding="utf-8"))
    return command, result, checkpoint_log


def retry_prompt(original_prompt, sql, checkpoint_log):
    checks = checkpoint_log.get("validation", {}).get("checks", [])
    check_lines = []
    for check in checks:
        check_lines.append(
            f"- {check['name']}: expected={check['expected']} actual={check['actual']} passed={check['passed']}"
        )
    return f"""{original_prompt}

The previous SQL candidate failed validation.

Previous SQL:
{sql}

Validation feedback:
{chr(10).join(check_lines)}

Generate a safer revised MySQL state-changing SQL statement. Return exactly one SQL statement and no explanation.
"""


def main():
    parser = argparse.ArgumentParser(description="Autonomous OpenAI SQL agent for one DBBench mutation task.")
    parser.add_argument("--jsonl", required=True, help="Path to DBBench JSONL file.")
    parser.add_argument("--mutation-index", type=int, default=0, help="Nth INSERT/UPDATE/DELETE task.")
    parser.add_argument("--model", default="gpt-5-mini", help="OpenAI model for SQL generation.")
    parser.add_argument("--max-attempts", type=int, default=2, help="Maximum generate/validate attempts.")
    parser.add_argument("--expected-affected-rows", type=int)
    parser.add_argument("--expected-row-delta", type=int)
    parser.add_argument("--verify-sql")
    parser.add_argument("--auto-verify", action="store_true", help="Infer validation rules from DBBench reference SQL.")
    parser.add_argument("--dry-run", action="store_true", help="Only build and print the first prompt.")
    parser.add_argument("--log", default="logs/autonomous_agent_pretty.json", help="Pretty run history.")
    parser.add_argument("--jsonl-log", default="logs/autonomous_agent.jsonl", help="JSONL run history.")
    args = parser.parse_args()

    line_no, task = select_task(Path(args.jsonl), args.mutation_index)
    prompt = build_prompt(task, sample_rows=5)
    inferred = None
    if args.auto_verify:
        reference_sql = task.get("label", [""])[0]
        inferred = infer_verifier(reference_sql)
        if inferred is None:
            raise SystemExit("Could not infer an automatic verifier for this task.")
        if args.verify_sql is None:
            args.verify_sql = inferred["verify_sql"]
        if args.expected_affected_rows is None:
            args.expected_affected_rows = inferred["expected_affected_rows"]
        if args.expected_row_delta is None:
            args.expected_row_delta = inferred["expected_row_delta"]

    if args.dry_run:
        print(prompt)
        return

    if not os.environ.get("OPENAI_API_KEY"):
        raise SystemExit("OPENAI_API_KEY is not set. Run: export OPENAI_API_KEY='your_key_here'")

    attempts = []
    current_prompt = prompt
    accepted_attempt = None

    for attempt_no in range(1, args.max_attempts + 1):
        sql = generate_sql(current_prompt, args.model)
        command, result, checkpoint_log = run_checkpoint_attempt(args, sql)
        validation_passed = checkpoint_log.get("validation", {}).get("passed", False)

        attempts.append(
            {
                "attempt": attempt_no,
                "prompt": current_prompt,
                "sql": sql,
                "command": command,
                "runner_exit_code": result.returncode,
                "runner_stdout": result.stdout,
                "runner_stderr": result.stderr,
                "validation": checkpoint_log.get("validation"),
                "restored": checkpoint_log.get("restored"),
            }
        )

        if validation_passed:
            accepted_attempt = attempt_no
            break
        current_prompt = retry_prompt(prompt, sql, checkpoint_log)

    payload = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "jsonl_line": line_no,
        "mutation_index": args.mutation_index,
        "model": args.model,
        "question": task.get("description", "").strip(),
        "mode": "autonomous_openai_checkpoint_exploration",
        "auto_verify": inferred,
        "max_attempts": args.max_attempts,
        "attempts": attempts,
        "accepted_attempt": accepted_attempt,
        "success": accepted_attempt is not None,
    }

    write_json(Path("tmp/autonomous_agent_run.json"), payload)
    append_jsonl(Path(args.jsonl_log), payload)
    append_pretty_json_array(Path(args.log), payload)

    print(f"Question: {payload['question']}")
    print(f"Model: {args.model}")
    print(f"Attempts: {len(attempts)}")
    print(f"Accepted attempt: {accepted_attempt}")
    print(f"Success: {payload['success']}")
    for attempt in attempts:
        print(f"Attempt {attempt['attempt']}: validation={attempt['validation']['passed']} restored={attempt['restored']}")
        print(attempt["sql"])
    print("Wrote run log: tmp/autonomous_agent_run.json")
    print(f"Appended pretty history: {args.log}")
    print(f"Appended JSONL history: {args.jsonl_log}")


if __name__ == "__main__":
    main()
