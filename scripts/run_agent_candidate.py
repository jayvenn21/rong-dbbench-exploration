#!/usr/bin/env python3
import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

from auto_verify import infer_verifier
from build_agent_prompt import build_prompt
from load_task import select_task
from run_linear import append_jsonl, append_pretty_json_array, write_json


def run_command(command):
    import subprocess

    return subprocess.run(command, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)


def main():
    parser = argparse.ArgumentParser(description="Run an externally generated SQL-agent candidate through an existing mode.")
    parser.add_argument("--jsonl", required=True, help="Path to DBBench JSONL file.")
    parser.add_argument("--mutation-index", type=int, default=0, help="Nth INSERT/UPDATE/DELETE task.")
    parser.add_argument("--mode", choices=["linear", "checkpoint", "explore"], default="checkpoint")
    parser.add_argument("--candidate-sql", required=True, help="SQL produced by the agent.")
    parser.add_argument("--second-candidate-sql", help="Optional second candidate for explore mode.")
    parser.add_argument("--expected-affected-rows", type=int)
    parser.add_argument("--expected-row-delta", type=int)
    parser.add_argument("--verify-sql")
    parser.add_argument("--auto-verify", action="store_true", help="Infer validation rules from DBBench reference SQL.")
    parser.add_argument("--log", default="logs/agent_candidates_pretty.json", help="Pretty agent candidate history.")
    parser.add_argument("--jsonl-log", default="logs/agent_candidates.jsonl", help="JSONL agent candidate history.")
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

    if args.mode == "linear":
        command = [
            "python3",
            "scripts/run_linear.py",
            "--jsonl",
            args.jsonl,
            "--mutation-index",
            str(args.mutation_index),
            "--action-sql",
            args.candidate_sql,
        ]
    elif args.mode == "checkpoint":
        command = [
            "python3",
            "scripts/run_checkpoint.py",
            "--jsonl",
            args.jsonl,
            "--mutation-index",
            str(args.mutation_index),
            "--action-sql",
            args.candidate_sql,
            "--restore-on-fail",
        ]
        if args.expected_affected_rows is not None:
            command.extend(["--expected-affected-rows", str(args.expected_affected_rows)])
        if args.expected_row_delta is not None:
            command.extend(["--expected-row-delta", str(args.expected_row_delta)])
        if args.verify_sql:
            command.extend(["--verify-sql", args.verify_sql])
    else:
        command = [
            "python3",
            "scripts/run_explore.py",
            "--jsonl",
            args.jsonl,
            "--mutation-index",
            str(args.mutation_index),
            "--action-sql",
            args.candidate_sql,
        ]
        if args.second_candidate_sql:
            command.extend(["--action-sql", args.second_candidate_sql])
        if args.expected_affected_rows is not None:
            command.extend(["--expected-affected-rows", str(args.expected_affected_rows)])
        if args.expected_row_delta is not None:
            command.extend(["--expected-row-delta", str(args.expected_row_delta)])
        if args.verify_sql:
            command.extend(["--verify-sql", args.verify_sql])

    result = run_command(command)
    payload = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "jsonl_line": line_no,
        "mutation_index": args.mutation_index,
        "mode": args.mode,
        "question": task.get("description", "").strip(),
        "prompt": prompt,
        "auto_verify": inferred,
        "candidate_sql": args.candidate_sql,
        "second_candidate_sql": args.second_candidate_sql,
        "command": command,
        "exit_code": result.returncode,
        "stdout": result.stdout,
        "stderr": result.stderr,
    }
    write_json(Path("tmp/agent_candidate_run.json"), payload)
    append_jsonl(Path(args.jsonl_log), payload)
    append_pretty_json_array(Path(args.log), payload)

    print(result.stdout)
    if result.stderr:
        print(result.stderr)
    print(f"Wrote agent run log: tmp/agent_candidate_run.json")
    print(f"Appended agent pretty history: {args.log}")
    print(f"Appended agent JSONL history: {args.jsonl_log}")
    raise SystemExit(result.returncode)


if __name__ == "__main__":
    main()
