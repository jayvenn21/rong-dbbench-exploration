#!/usr/bin/env python3
import argparse
from datetime import datetime, timezone
from pathlib import Path

from auto_verify import infer_verifier
from load_task import build_sql, column_name, quote_identifier, select_task
from run_linear import (
    append_jsonl,
    append_pretty_json_array,
    require_success,
    run_mysql,
    table_context_sql,
    write_json,
    write_log,
)


STATE_CHANGING_PREFIXES = ("INSERT", "UPDATE", "DELETE", "CREATE", "DROP", "ALTER", "TRUNCATE")


def is_state_changing(sql):
    stripped = sql.lstrip().upper()
    return stripped.startswith(STATE_CHANGING_PREFIXES)


def run_container_shell(container, command):
    import subprocess

    return subprocess.run(
        ["docker", "exec", container, "sh", "-c", command],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )


def create_checkpoint(container, database, checkpoint_path):
    command = f"mysqldump -uroot -prootpass {database} > {checkpoint_path}"
    return run_container_shell(container, command)


def restore_checkpoint(container, database, checkpoint_path):
    command = f"mysql -uroot -prootpass {database} < {checkpoint_path}"
    return run_container_shell(container, command)


def scalar_query(container, database, sql):
    result = run_mysql(container, database, sql)
    if result.returncode != 0:
        return result, None
    lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    if len(lines) < 2:
        return result, None
    return result, lines[-1]


def row_count(container, database, table_name):
    sql = f"SELECT COUNT(*) AS row_count FROM {quote_identifier(table_name)};"
    result, value = scalar_query(container, database, sql)
    if value is None:
        return result, None
    return result, int(value)


def execute_action(container, database, action_sql):
    sql = action_sql.rstrip().rstrip(";") + ";\nSELECT ROW_COUNT() AS affected_rows;"
    return run_mysql(container, database, sql)


def parse_affected_rows(result):
    lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    if len(lines) < 2:
        return None
    try:
        return int(lines[-1])
    except ValueError:
        return None


def verify_query_has_rows(container, database, verify_sql):
    result = run_mysql(container, database, verify_sql)
    if result.returncode != 0:
        return result, False
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    return result, len(lines) > 1


def validate_action(action_result, affected_rows, before_count, after_count, args):
    checks = []

    checks.append(
        {
            "name": "sql_exit_code",
            "passed": action_result.returncode == 0,
            "expected": 0,
            "actual": action_result.returncode,
        }
    )

    if args.expected_affected_rows is not None:
        checks.append(
            {
                "name": "affected_rows",
                "passed": affected_rows == args.expected_affected_rows,
                "expected": args.expected_affected_rows,
                "actual": affected_rows,
            }
        )

    if args.expected_row_delta is not None:
        actual_delta = None if before_count is None or after_count is None else after_count - before_count
        checks.append(
            {
                "name": "row_count_delta",
                "passed": actual_delta == args.expected_row_delta,
                "expected": args.expected_row_delta,
                "actual": actual_delta,
            }
        )

    return checks


def main():
    parser = argparse.ArgumentParser(description="Run DBBench checkpointed mutation mode on one task.")
    parser.add_argument("--jsonl", required=True, help="Path to DBBench JSONL file.")
    parser.add_argument("--mutation-index", type=int, default=0, help="Nth INSERT/UPDATE/DELETE task to run.")
    parser.add_argument("--container", default="dbbench-mysql", help="MySQL Docker container name.")
    parser.add_argument("--database", default="dbbench", help="Database name to reset and use.")
    parser.add_argument("--action-sql", help="SQL action to execute after checkpointing.")
    parser.add_argument("--use-reference", action="store_true", help="Use DBBench's reference SQL as the action.")
    parser.add_argument("--restore", action="store_true", help="Restore the checkpoint after executing the action.")
    parser.add_argument(
        "--restore-on-fail",
        action="store_true",
        help="Restore the checkpoint only if validation fails.",
    )
    parser.add_argument("--expected-affected-rows", type=int, help="Validation: expected ROW_COUNT() after mutation.")
    parser.add_argument("--expected-row-delta", type=int, help="Validation: expected table row-count change.")
    parser.add_argument(
        "--auto-verify",
        action="store_true",
        help="Infer a post-condition validator from DBBench reference SQL when possible.",
    )
    parser.add_argument(
        "--verify-sql",
        help="Validation: SQL query that must return at least one data row after mutation.",
    )
    parser.add_argument("--checkpoint-path", default="/tmp/dbbench_checkpoint.sql", help="Checkpoint path inside container.")
    parser.add_argument("--log", default="tmp/checkpoint_run.md", help="Markdown log output path.")
    parser.add_argument("--json-log", default="tmp/checkpoint_run.json", help="Structured JSON log output path.")
    parser.add_argument("--jsonl-log", default="logs/checkpoint_runs.jsonl", help="Append-only JSONL log path.")
    parser.add_argument(
        "--pretty-history",
        default="logs/checkpoint_runs_pretty.json",
        help="Append-only pretty JSON array for human-readable run history.",
    )
    args = parser.parse_args()

    line_no, task = select_task(Path(args.jsonl), args.mutation_index)
    table = task["table"]
    table_name = table["table_name"]
    columns = [column_name(column) for column in table["table_info"]["columns"]]
    reference_sql = task.get("label", [""])[0]

    inferred = None
    if args.auto_verify:
        inferred = infer_verifier(reference_sql)
        if inferred is None:
            raise SystemExit("Could not infer an automatic verifier for this task.")
        if args.verify_sql is None:
            args.verify_sql = inferred["verify_sql"]
        if args.expected_affected_rows is None:
            args.expected_affected_rows = inferred["expected_affected_rows"]
        if args.expected_row_delta is None:
            args.expected_row_delta = inferred["expected_row_delta"]

    if args.use_reference:
        action_sql = reference_sql
    elif args.action_sql:
        action_sql = args.action_sql
    else:
        print("No SQL action provided.")
        print("Read the task/schema below, decide an action, then rerun with --action-sql '...'.")
        print()
        print(f"Task: {task.get('description', '').strip()}")
        print(f"Table: {table_name}")
        print("Columns:", ", ".join(columns))
        print()
        print("Checkpoint mode will create a mysqldump before executing state-changing SQL.")
        return

    reset_result = run_mysql(args.container, None, build_sql(task, args.database))
    require_success(reset_result, "Database reset")

    before = run_mysql(args.container, args.database, table_context_sql(table_name))
    require_success(before, "Before inspection")
    before_count_result, before_count = row_count(args.container, args.database, table_name)
    require_success(before_count_result, "Before row count")

    checkpoint_result = create_checkpoint(args.container, args.database, args.checkpoint_path)
    require_success(checkpoint_result, "Checkpoint creation")

    action_result = execute_action(args.container, args.database, action_sql)
    affected_rows = parse_affected_rows(action_result)

    after_mutation = run_mysql(args.container, args.database, table_context_sql(table_name))
    require_success(after_mutation, "After mutation inspection")
    after_count_result, after_count = row_count(args.container, args.database, table_name)
    require_success(after_count_result, "After row count")

    validation_checks = validate_action(action_result, affected_rows, before_count, after_count, args)
    verify_result = None
    if args.verify_sql:
        verify_result, verify_passed = verify_query_has_rows(args.container, args.database, args.verify_sql)
        validation_checks.append(
            {
                "name": "post_condition_query",
                "passed": verify_passed,
                "expected": "at least one returned row",
                "actual": verify_result.stdout.strip(),
                "sql": args.verify_sql,
            }
        )

    validation_passed = all(check["passed"] for check in validation_checks)

    restore_result = None
    after_restore = None
    should_restore = args.restore or (args.restore_on_fail and not validation_passed)
    if should_restore:
        restore_result = restore_checkpoint(args.container, args.database, args.checkpoint_path)
        require_success(restore_result, "Checkpoint restore")
        after_restore = run_mysql(args.container, args.database, table_context_sql(table_name))
        require_success(after_restore, "After restore inspection")

    success = action_result.returncode == 0
    task_id = f"dev_mutation_{args.mutation_index}_line_{line_no}"
    json_payload = {
        "task_id": task_id,
        "jsonl_line": line_no,
        "question": task.get("description", "").strip(),
        "mode": "checkpoint",
        "schema_summary": {
            "database": args.database,
            "table": table_name,
            "columns": columns,
            "initial_rows": len(table["table_info"].get("rows", [])),
        },
        "checkpoint": {
            "method": "mysqldump",
            "path_inside_container": args.checkpoint_path,
            "exit_code": checkpoint_result.returncode,
            "stderr": checkpoint_result.stderr.strip(),
        },
        "auto_verify": inferred,
        "steps": [
            {
                "step": 1,
                "sql": action_sql.strip(),
                "state_changing": is_state_changing(action_sql),
                "exit_code": action_result.returncode,
                "affected_rows": affected_rows,
                "stdout": action_result.stdout.strip(),
                "stderr": action_result.stderr.strip(),
            }
        ],
        "validation": {
            "passed": validation_passed,
            "checks": validation_checks,
        },
        "before_state": before.stdout.strip(),
        "after_mutation_state": after_mutation.stdout.strip(),
        "restored": should_restore,
        "restore": None
        if restore_result is None
        else {
            "exit_code": restore_result.returncode,
            "stderr": restore_result.stderr.strip(),
            "after_restore_state": after_restore.stdout.strip(),
        },
        "final_answer": "mutation executed with checkpoint available" if success else "mutation failed with checkpoint available",
        "success": success,
        "notes": "Checkpoint mode creates a recoverable boundary before mutating SQL. Restore is explicit so the user can inspect whether the mutation should be accepted or rolled back.",
        "created_at": datetime.now(timezone.utc).isoformat(),
    }

    log_lines = [
        "# Checkpoint Mode Run",
        "",
        f"- JSONL line: {line_no}",
        f"- Mutation index: {args.mutation_index}",
        f"- Type: {', '.join(task.get('type', []))}",
        f"- Table: `{table_name}`",
        f"- Checkpoint: `{args.checkpoint_path}` inside `{args.container}`",
        f"- Validation passed: `{validation_passed}`",
        f"- Restored after mutation: `{should_restore}`",
        "",
        "## Task",
        "",
        task.get("description", "").strip(),
        "",
        "## SQL Action",
        "",
        "```sql",
        action_sql.strip(),
        "```",
        "",
        "## Before Checkpoint",
        "",
        "```text",
        before.stdout.strip(),
        "```",
        "",
        "## Checkpoint Result",
        "",
        "```text",
        (checkpoint_result.stdout + checkpoint_result.stderr).strip(),
        "```",
        "",
        "## After Mutation",
        "",
        "```text",
        after_mutation.stdout.strip(),
        "```",
        "",
        "## Validation",
        "",
        "```text",
        "\n".join(
            f"{check['name']}: expected={check['expected']} actual={check['actual']} passed={check['passed']}"
            for check in validation_checks
        ),
        "```",
    ]

    if restore_result is not None:
        log_lines.extend(
            [
                "",
                "## Restore Result",
                "",
                "```text",
                (restore_result.stdout + restore_result.stderr).strip(),
                "```",
                "",
                "## After Restore",
                "",
                "```text",
                after_restore.stdout.strip(),
                "```",
            ]
        )

    log_lines.extend(
        [
            "",
            "## Checkpoint Mode Observation",
            "",
            "The mutation was executed after a database checkpoint was created. If the mutation is bad, the checkpoint can restore the database to its pre-mutation state.",
        ]
    )

    write_log(Path(args.log), log_lines)
    write_json(Path(args.json_log), json_payload)
    append_jsonl(Path(args.jsonl_log), json_payload)
    append_pretty_json_array(Path(args.pretty_history), json_payload)

    print(f"Task: {task.get('description', '').strip()}")
    print(f"Table: {table_name}")
    print(f"Checkpoint created at {args.checkpoint_path} inside {args.container}")
    print(f"Executed SQL after checkpoint: {action_sql.strip()}")
    print(f"Action exit code: {action_result.returncode}")
    print(f"Affected rows: {affected_rows}")
    print(f"Validation passed: {validation_passed}")
    print(f"Restored after mutation: {should_restore}")
    print(f"Wrote log: {args.log}")
    print(f"Wrote structured JSON: {args.json_log}")
    print(f"Appended JSONL log: {args.jsonl_log}")
    print(f"Appended pretty JSON history: {args.pretty_history}")


if __name__ == "__main__":
    main()
