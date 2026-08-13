#!/usr/bin/env python3
import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from load_task import build_sql, column_name, quote_identifier, select_task


def run_mysql(container, database, sql):
    cmd = ["docker", "exec", "-i", container, "mysql", "-uroot", "-prootpass"]
    if database:
        cmd.append(database)
    result = subprocess.run(
        cmd,
        input=sql,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    return result


def require_success(result, context):
    if result.returncode == 0:
        return
    sys.stderr.write(f"\n{context} failed\n")
    sys.stderr.write(result.stderr)
    raise SystemExit(result.returncode)


def table_context_sql(table_name):
    quoted_table = quote_identifier(table_name)
    return "\n".join(
        [
            "SHOW TABLES;",
            f"DESCRIBE {quoted_table};",
            f"SELECT COUNT(*) AS row_count FROM {quoted_table};",
            f"SELECT * FROM {quoted_table} LIMIT 5;",
        ]
    )


def write_log(path, lines):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_json(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def append_jsonl(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload) + "\n")


def append_pretty_json_array(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        runs = json.loads(path.read_text(encoding="utf-8"))
    else:
        runs = []
    runs.append(payload)
    write_json(path, runs)


def main():
    parser = argparse.ArgumentParser(description="Run DBBench linear/no-undo baseline on one mutation task.")
    parser.add_argument("--jsonl", required=True, help="Path to DBBench JSONL file.")
    parser.add_argument("--mutation-index", type=int, default=0, help="Nth INSERT/UPDATE/DELETE task to run.")
    parser.add_argument("--container", default="dbbench-mysql", help="MySQL Docker container name.")
    parser.add_argument("--database", default="dbbench", help="Database name to reset and use.")
    parser.add_argument("--action-sql", help="SQL action to execute directly in linear mode.")
    parser.add_argument(
        "--use-reference",
        action="store_true",
        help="Use DBBench's reference SQL as the action. Useful for testing the runner.",
    )
    parser.add_argument("--log", default="tmp/linear_run.md", help="Markdown log output path.")
    parser.add_argument("--json-log", default="tmp/linear_run.json", help="Structured JSON log output path.")
    parser.add_argument("--jsonl-log", default="logs/linear_runs.jsonl", help="Append-only JSONL run log path.")
    parser.add_argument(
        "--pretty-history",
        default="logs/linear_runs_pretty.json",
        help="Append-only pretty JSON array for human-readable run history.",
    )
    args = parser.parse_args()

    line_no, task = select_task(Path(args.jsonl), args.mutation_index)
    table = task["table"]
    table_name = table["table_name"]
    columns = [column_name(column) for column in table["table_info"]["columns"]]
    reference_sql = task.get("label", [""])[0]

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
        print("Reference SQL is hidden unless you pass --use-reference.")
        return

    reset_result = run_mysql(args.container, None, build_sql(task, args.database))
    require_success(reset_result, "Database reset")

    before = run_mysql(args.container, args.database, table_context_sql(table_name))
    require_success(before, "Before inspection")

    action_result = run_mysql(args.container, args.database, action_sql)

    after = run_mysql(args.container, args.database, table_context_sql(table_name))
    require_success(after, "After inspection")

    success = action_result.returncode == 0
    task_id = f"dev_mutation_{args.mutation_index}_line_{line_no}"
    json_payload = {
        "task_id": task_id,
        "jsonl_line": line_no,
        "question": task.get("description", "").strip(),
        "mode": "linear",
        "schema_summary": {
            "database": args.database,
            "table": table_name,
            "columns": columns,
            "initial_rows": len(table["table_info"].get("rows", [])),
        },
        "steps": [
            {
                "step": 1,
                "sql": action_sql.strip(),
                "exit_code": action_result.returncode,
                "stdout": action_result.stdout.strip(),
                "stderr": action_result.stderr.strip(),
            }
        ],
        "before_state": before.stdout.strip(),
        "after_state": after.stdout.strip(),
        "final_answer": "mutation executed" if success else "mutation failed",
        "success": success,
        "notes": "Linear mode executes directly against the live database. There is no checkpoint or restore.",
        "created_at": datetime.now(timezone.utc).isoformat(),
    }

    log_lines = [
        "# Linear Mode Run",
        "",
        f"- JSONL line: {line_no}",
        f"- Mutation index: {args.mutation_index}",
        f"- Type: {', '.join(task.get('type', []))}",
        f"- Table: `{table_name}`",
        "",
        "## Task",
        "",
        task.get("description", "").strip(),
        "",
        "## SQL Action Executed Directly",
        "",
        "```sql",
        action_sql.strip(),
        "```",
        "",
        "## Before",
        "",
        "```text",
        before.stdout.strip(),
        "```",
        "",
        "## Action Result",
        "",
        "```text",
        (action_result.stdout + action_result.stderr).strip(),
        "```",
        "",
        "## After",
        "",
        "```text",
        after.stdout.strip(),
        "```",
        "",
        "## Linear Mode Observation",
        "",
        "The SQL action was executed directly against the live database. If this action had been wrong, the session state would now be wrong too. There is no restore point in this baseline.",
    ]
    write_log(Path(args.log), log_lines)
    write_json(Path(args.json_log), json_payload)
    append_jsonl(Path(args.jsonl_log), json_payload)
    append_pretty_json_array(Path(args.pretty_history), json_payload)

    print(f"Task: {task.get('description', '').strip()}")
    print(f"Table: {table_name}")
    print(f"Executed SQL directly with no checkpoint: {action_sql.strip()}")
    print(f"Action exit code: {action_result.returncode}")
    print(f"Wrote log: {args.log}")
    print(f"Wrote structured JSON: {args.json_log}")
    print(f"Appended JSONL log: {args.jsonl_log}")
    print(f"Appended pretty JSON history: {args.pretty_history}")
    if action_result.returncode != 0:
        print("The action failed. That is still useful: linear mode has no automatic recovery.")


if __name__ == "__main__":
    main()
