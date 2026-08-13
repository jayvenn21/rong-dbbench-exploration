#!/usr/bin/env python3
import argparse
import csv
import json
import time
from datetime import datetime, timezone
from pathlib import Path

from load_task import build_sql, column_name, mutation_tasks
from run_checkpoint import (
    create_checkpoint,
    execute_action,
    parse_affected_rows,
    restore_checkpoint,
    row_count,
    validate_action,
)
from run_linear import require_success, run_mysql, table_context_sql, write_json, write_log


def expected_delta(task):
    task_types = set(task.get("type", []))
    if "INSERT" in task_types:
        return 1
    if "UPDATE" in task_types:
        return 0
    if "DELETE" in task_types:
        return -1
    return None


def run_linear_case(container, database, task, table_name, action_sql):
    reset_result = run_mysql(container, None, build_sql(task, database))
    require_success(reset_result, "Linear database reset")

    before_count_result, before_count = row_count(container, database, table_name)
    require_success(before_count_result, "Linear before row count")

    start = time.perf_counter()
    action_result = execute_action(container, database, action_sql)
    runtime = time.perf_counter() - start

    after_count_result, after_count = row_count(container, database, table_name)
    require_success(after_count_result, "Linear after row count")

    affected_rows = parse_affected_rows(action_result)
    delta = None if before_count is None or after_count is None else after_count - before_count
    success = action_result.returncode == 0

    return {
        "success": success,
        "runtime_seconds": runtime,
        "affected_rows": affected_rows,
        "before_rows": before_count,
        "after_rows": after_count,
        "row_delta": delta,
        "failure_reason": "" if success else action_result.stderr.strip(),
    }


def run_checkpoint_case(container, database, task, table_name, action_sql, checkpoint_path):
    reset_result = run_mysql(container, None, build_sql(task, database))
    require_success(reset_result, "Checkpoint database reset")

    before_count_result, before_count = row_count(container, database, table_name)
    require_success(before_count_result, "Checkpoint before row count")

    start = time.perf_counter()
    checkpoint_result = create_checkpoint(container, database, checkpoint_path)
    checkpoint_runtime = time.perf_counter() - start
    require_success(checkpoint_result, "Checkpoint creation")

    action_start = time.perf_counter()
    action_result = execute_action(container, database, action_sql)
    action_runtime = time.perf_counter() - action_start

    after_count_result, after_count = row_count(container, database, table_name)
    require_success(after_count_result, "Checkpoint after row count")

    affected_rows = parse_affected_rows(action_result)

    class ValidationArgs:
        expected_affected_rows = None
        expected_row_delta = expected_delta(task)

    checks = validate_action(action_result, affected_rows, before_count, after_count, ValidationArgs)
    validation_passed = all(check["passed"] for check in checks)

    restores = 0
    restore_runtime = 0.0
    if not validation_passed:
        restore_start = time.perf_counter()
        restore_result = restore_checkpoint(container, database, checkpoint_path)
        restore_runtime = time.perf_counter() - restore_start
        require_success(restore_result, "Checkpoint restore")
        restores = 1

    runtime = checkpoint_runtime + action_runtime + restore_runtime
    delta = None if before_count is None or after_count is None else after_count - before_count
    failure_reason = ""
    if action_result.returncode != 0:
        failure_reason = action_result.stderr.strip()
    elif not validation_passed:
        failure_reason = "validation_failed"

    return {
        "success": validation_passed,
        "runtime_seconds": runtime,
        "checkpoint_runtime_seconds": checkpoint_runtime,
        "action_runtime_seconds": action_runtime,
        "restore_runtime_seconds": restore_runtime,
        "num_checkpoints": 1,
        "num_restores": restores,
        "affected_rows": affected_rows,
        "before_rows": before_count,
        "after_rows": after_count,
        "row_delta": delta,
        "validation_checks": checks,
        "failure_reason": failure_reason,
    }


def collect_tasks(jsonl_path, limit):
    selected = []
    for mutation_index, (line_no, task) in enumerate(mutation_tasks(jsonl_path)):
        selected.append((mutation_index, line_no, task))
        if len(selected) >= limit:
            break
    return selected


def write_csv(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = [
        "task_id",
        "jsonl_line",
        "type",
        "table",
        "linear_success",
        "checkpoint_success",
        "num_checkpoints",
        "num_restores",
        "runtime_linear",
        "runtime_checkpoint",
        "failure_reason",
    ]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in fields})


def write_markdown(path, rows):
    lines = [
        "# Batch Comparison",
        "",
        f"- Created at: {datetime.now(timezone.utc).isoformat()}",
        f"- Tasks: {len(rows)}",
        "",
        "| task_id | type | linear | checkpoint | checkpoints | restores | linear_s | checkpoint_s | failure |",
        "| --- | --- | --- | --- | ---: | ---: | ---: | ---: | --- |",
    ]
    for row in rows:
        lines.append(
            "| {task_id} | {type} | {linear_success} | {checkpoint_success} | {num_checkpoints} | {num_restores} | {runtime_linear:.4f} | {runtime_checkpoint:.4f} | {failure_reason} |".format(
                **row
            )
        )
    write_log(path, lines)


def main():
    parser = argparse.ArgumentParser(description="Compare linear and checkpoint modes across DBBench mutation tasks.")
    parser.add_argument("--jsonl", required=True, help="Path to DBBench JSONL file.")
    parser.add_argument("--limit", type=int, default=5, help="Number of mutation tasks to run.")
    parser.add_argument("--container", default="dbbench-mysql", help="MySQL Docker container name.")
    parser.add_argument("--database", default="dbbench", help="Database name to reset and use.")
    parser.add_argument("--checkpoint-path", default="/tmp/dbbench_checkpoint.sql", help="Checkpoint path inside container.")
    parser.add_argument("--csv", default="results/batch_summary.csv", help="CSV summary output path.")
    parser.add_argument("--json", default="results/batch_summary.json", help="JSON summary output path.")
    parser.add_argument("--md", default="results/batch_summary.md", help="Markdown summary output path.")
    args = parser.parse_args()

    rows = []
    details = []
    for mutation_index, line_no, task in collect_tasks(Path(args.jsonl), args.limit):
        table = task["table"]
        table_name = table["table_name"]
        columns = [column_name(column) for column in table["table_info"]["columns"]]
        task_type = ",".join(task.get("type", []))
        action_sql = task.get("label", [""])[0]
        task_id = f"dev_mutation_{mutation_index}_line_{line_no}"

        print(f"Running {task_id}: {task_type} on {table_name}")
        linear = run_linear_case(args.container, args.database, task, table_name, action_sql)
        checkpoint = run_checkpoint_case(args.container, args.database, task, table_name, action_sql, args.checkpoint_path)

        row = {
            "task_id": task_id,
            "jsonl_line": line_no,
            "type": task_type,
            "table": table_name,
            "linear_success": linear["success"],
            "checkpoint_success": checkpoint["success"],
            "num_checkpoints": checkpoint["num_checkpoints"],
            "num_restores": checkpoint["num_restores"],
            "runtime_linear": linear["runtime_seconds"],
            "runtime_checkpoint": checkpoint["runtime_seconds"],
            "failure_reason": checkpoint["failure_reason"] or linear["failure_reason"],
        }
        rows.append(row)
        details.append(
            {
                "task_id": task_id,
                "jsonl_line": line_no,
                "question": task.get("description", "").strip(),
                "type": task_type,
                "schema_summary": {
                    "database": args.database,
                    "table": table_name,
                    "columns": columns,
                    "initial_rows": len(table["table_info"].get("rows", [])),
                },
                "reference_sql": action_sql,
                "linear": linear,
                "checkpoint": checkpoint,
            }
        )

    write_csv(Path(args.csv), rows)
    write_json(Path(args.json), details)
    write_markdown(Path(args.md), rows)

    print()
    print(f"Wrote CSV: {args.csv}")
    print(f"Wrote JSON: {args.json}")
    print(f"Wrote Markdown: {args.md}")


if __name__ == "__main__":
    main()
