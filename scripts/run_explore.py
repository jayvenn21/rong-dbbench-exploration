#!/usr/bin/env python3
import argparse
from datetime import datetime, timezone
from pathlib import Path

from load_task import build_sql, column_name, select_task
from run_checkpoint import (
    create_checkpoint,
    execute_action,
    parse_affected_rows,
    restore_checkpoint,
    row_count,
    validate_action,
    verify_query_has_rows,
)
from run_linear import (
    append_jsonl,
    append_pretty_json_array,
    require_success,
    run_mysql,
    table_context_sql,
    write_json,
    write_log,
)


def validation_with_post_condition(action_result, affected_rows, before_count, after_count, args):
    checks = validate_action(action_result, affected_rows, before_count, after_count, args)
    verify_result = None
    if args.verify_sql:
        verify_result, verify_passed = verify_query_has_rows(args.container, args.database, args.verify_sql)
        checks.append(
            {
                "name": "post_condition_query",
                "passed": verify_passed,
                "expected": "at least one returned row",
                "actual": verify_result.stdout.strip(),
                "sql": args.verify_sql,
            }
        )
    return checks, all(check["passed"] for check in checks)


def main():
    parser = argparse.ArgumentParser(description="Run checkpointed exploration with retry candidates.")
    parser.add_argument("--jsonl", required=True, help="Path to DBBench JSONL file.")
    parser.add_argument("--mutation-index", type=int, default=0, help="Nth INSERT/UPDATE/DELETE task to run.")
    parser.add_argument("--container", default="dbbench-mysql", help="MySQL Docker container name.")
    parser.add_argument("--database", default="dbbench", help="Database name to reset and use.")
    parser.add_argument(
        "--action-sql",
        action="append",
        default=[],
        help="Candidate SQL action. Pass multiple times to simulate retries.",
    )
    parser.add_argument(
        "--first-bad-then-reference",
        action="store_true",
        help="Demo mode: try an intentionally wrong INSERT first, then DBBench's reference SQL.",
    )
    parser.add_argument("--expected-affected-rows", type=int, help="Validation: expected ROW_COUNT().")
    parser.add_argument("--expected-row-delta", type=int, help="Validation: expected table row-count change.")
    parser.add_argument("--verify-sql", help="Validation: SQL query that must return at least one data row.")
    parser.add_argument("--checkpoint-path", default="/tmp/dbbench_checkpoint.sql", help="Checkpoint path inside container.")
    parser.add_argument("--log", default="tmp/explore_run.md", help="Markdown log output path.")
    parser.add_argument("--json-log", default="tmp/explore_run.json", help="Structured JSON log output path.")
    parser.add_argument("--jsonl-log", default="logs/explore_runs.jsonl", help="Append-only JSONL log path.")
    parser.add_argument(
        "--pretty-history",
        default="logs/explore_runs_pretty.json",
        help="Append-only pretty JSON array for human-readable run history.",
    )
    args = parser.parse_args()

    line_no, task = select_task(Path(args.jsonl), args.mutation_index)
    table = task["table"]
    table_name = table["table_name"]
    columns = [column_name(column) for column in table["table_info"]["columns"]]
    reference_sql = task.get("label", [""])[0]

    candidates = list(args.action_sql)
    if args.first_bad_then_reference:
        candidates = [
            "INSERT INTO `School Location Table` (`School`, `Location`, `Date moved`, `Currently at this location`) "
            "VALUES ('Wrong High School', 'Nowhere', '1900', 'bad row')",
            reference_sql,
        ]

    if not candidates:
        print("No candidate SQL actions provided.")
        print("Read the task/schema below, decide candidate actions, then rerun with --action-sql '...'.")
        print()
        print(f"Task: {task.get('description', '').strip()}")
        print(f"Table: {table_name}")
        print("Columns:", ", ".join(columns))
        print()
        print("Exploration mode will checkpoint before each candidate, restore failed attempts, and stop on the first validated action.")
        return

    reset_result = run_mysql(args.container, None, build_sql(task, args.database))
    require_success(reset_result, "Database reset")

    attempts = []
    accepted_attempt = None
    before_all = run_mysql(args.container, args.database, table_context_sql(table_name))
    require_success(before_all, "Initial inspection")

    for index, candidate_sql in enumerate(candidates, start=1):
        checkpoint_result = create_checkpoint(args.container, args.database, args.checkpoint_path)
        require_success(checkpoint_result, f"Checkpoint creation for attempt {index}")

        before_count_result, before_count = row_count(args.container, args.database, table_name)
        require_success(before_count_result, f"Before row count for attempt {index}")

        action_result = execute_action(args.container, args.database, candidate_sql)
        affected_rows = parse_affected_rows(action_result)

        after_count_result, after_count = row_count(args.container, args.database, table_name)
        require_success(after_count_result, f"After row count for attempt {index}")

        checks, passed = validation_with_post_condition(
            action_result,
            affected_rows,
            before_count,
            after_count,
            args,
        )
        after_mutation = run_mysql(args.container, args.database, table_context_sql(table_name))
        require_success(after_mutation, f"After mutation inspection for attempt {index}")

        restored = False
        restore_result = None
        after_restore = None
        if not passed:
            restore_result = restore_checkpoint(args.container, args.database, args.checkpoint_path)
            require_success(restore_result, f"Restore for attempt {index}")
            after_restore = run_mysql(args.container, args.database, table_context_sql(table_name))
            require_success(after_restore, f"After restore inspection for attempt {index}")
            restored = True
        else:
            accepted_attempt = index

        attempts.append(
            {
                "attempt": index,
                "sql": candidate_sql.strip(),
                "exit_code": action_result.returncode,
                "affected_rows": affected_rows,
                "before_row_count": before_count,
                "after_row_count": after_count,
                "validation": {"passed": passed, "checks": checks},
                "restored": restored,
                "after_mutation_state": after_mutation.stdout.strip(),
                "after_restore_state": None if after_restore is None else after_restore.stdout.strip(),
                "restore_stderr": None if restore_result is None else restore_result.stderr.strip(),
            }
        )

        if passed:
            break

    final_state = run_mysql(args.container, args.database, table_context_sql(table_name))
    require_success(final_state, "Final inspection")

    payload = {
        "task_id": f"dev_mutation_{args.mutation_index}_line_{line_no}",
        "jsonl_line": line_no,
        "question": task.get("description", "").strip(),
        "mode": "checkpoint_exploration",
        "schema_summary": {
            "database": args.database,
            "table": table_name,
            "columns": columns,
            "initial_rows": len(table["table_info"].get("rows", [])),
        },
        "attempts": attempts,
        "accepted_attempt": accepted_attempt,
        "success": accepted_attempt is not None,
        "final_state": final_state.stdout.strip(),
        "notes": "Exploration mode retries candidate SQL actions. Failed validation triggers restore before the next candidate runs.",
        "created_at": datetime.now(timezone.utc).isoformat(),
    }

    log_lines = [
        "# Checkpoint Exploration Run",
        "",
        f"- JSONL line: {line_no}",
        f"- Mutation index: {args.mutation_index}",
        f"- Table: `{table_name}`",
        f"- Candidates tried: `{len(attempts)}`",
        f"- Accepted attempt: `{accepted_attempt}`",
        "",
        "## Task",
        "",
        task.get("description", "").strip(),
        "",
        "## Attempts",
    ]

    for attempt in attempts:
        log_lines.extend(
            [
                "",
                f"### Attempt {attempt['attempt']}",
                "",
                "```sql",
                attempt["sql"],
                "```",
                "",
                "```text",
                f"exit_code={attempt['exit_code']}",
                f"affected_rows={attempt['affected_rows']}",
                f"before_row_count={attempt['before_row_count']}",
                f"after_row_count={attempt['after_row_count']}",
                f"validation_passed={attempt['validation']['passed']}",
                f"restored={attempt['restored']}",
                "```",
                "",
                "Validation checks:",
                "",
            ]
        )
        for check in attempt["validation"]["checks"]:
            log_lines.append(
                f"- {check['name']}: expected={check['expected']} actual={check['actual']} passed={check['passed']}"
            )

    log_lines.extend(
        [
            "",
            "## Observation",
            "",
            "Exploration is more than an undo button: the failed attempt is inspected, rolled back, and followed by a revised candidate action.",
        ]
    )

    write_log(Path(args.log), log_lines)
    write_json(Path(args.json_log), payload)
    append_jsonl(Path(args.jsonl_log), payload)
    append_pretty_json_array(Path(args.pretty_history), payload)

    print(f"Task: {task.get('description', '').strip()}")
    print(f"Table: {table_name}")
    print(f"Attempts tried: {len(attempts)}")
    print(f"Accepted attempt: {accepted_attempt}")
    print(f"Success: {accepted_attempt is not None}")
    for attempt in attempts:
        print(
            f"Attempt {attempt['attempt']}: validation={attempt['validation']['passed']} "
            f"restored={attempt['restored']} sql={attempt['sql']}"
        )
    print(f"Wrote log: {args.log}")
    print(f"Wrote structured JSON: {args.json_log}")
    print(f"Appended JSONL log: {args.jsonl_log}")
    print(f"Appended pretty JSON history: {args.pretty_history}")


if __name__ == "__main__":
    main()
