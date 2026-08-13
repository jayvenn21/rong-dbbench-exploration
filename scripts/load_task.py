#!/usr/bin/env python3
import argparse
import json
from pathlib import Path


MUTATION_TYPES = {"INSERT", "UPDATE", "DELETE"}


def quote_identifier(value):
    return "`" + value.replace("`", "``") + "`"


def quote_literal(value):
    if value is None:
        return "NULL"
    text = str(value)
    return "'" + text.replace("\\", "\\\\").replace("'", "''") + "'"


def column_name(column):
    if isinstance(column, dict):
        return column["name"]
    return str(column)


def column_type(column):
    # DBBench table values are scraped from natural tables and often contain
    # loose values, e.g. YEAR columns with notes or out-of-range years. TEXT
    # keeps the starter loader robust while preserving the visible schema.
    return "TEXT"


def mutation_tasks(jsonl_path):
    with open(jsonl_path, "r", encoding="utf-8") as handle:
        for line_no, line in enumerate(handle, start=1):
            task = json.loads(line)
            if set(task.get("type", [])) & MUTATION_TYPES:
                yield line_no, task


def select_task(jsonl_path, mutation_index):
    for idx, (line_no, task) in enumerate(mutation_tasks(jsonl_path)):
        if idx == mutation_index:
            return line_no, task
    raise SystemExit(f"No mutation task found at mutation index {mutation_index}")


def normalize_rows(rows, column_count):
    normalized = []
    for row in rows:
        values = list(row)
        if len(values) < column_count:
            values.extend([None] * (column_count - len(values)))
        normalized.append(values[:column_count])
    return normalized


def build_sql(task, database_name):
    table = task["table"]
    table_name = table["table_name"]
    table_info = table["table_info"]
    raw_columns = table_info["columns"]
    columns = [column_name(column) for column in raw_columns]
    rows = normalize_rows(table_info.get("rows", []), len(columns))

    statements = [
        f"DROP DATABASE IF EXISTS {quote_identifier(database_name)};",
        f"CREATE DATABASE {quote_identifier(database_name)};",
        f"USE {quote_identifier(database_name)};",
        "",
        f"CREATE TABLE {quote_identifier(table_name)} (",
    ]

    column_defs = [
        f"  {quote_identifier(column_name(column))} {column_type(column)}"
        for column in raw_columns
    ]
    statements.append(",\n".join(column_defs))
    statements.append(");")

    if rows:
        quoted_columns = ", ".join(quote_identifier(column) for column in columns)
        statements.append("")
        statements.append(f"INSERT INTO {quote_identifier(table_name)} ({quoted_columns}) VALUES")
        row_sql = []
        for row in rows:
            row_sql.append("  (" + ", ".join(quote_literal(value) for value in row) + ")")
        statements.append(",\n".join(row_sql) + ";")

    statements.append("")
    return "\n".join(statements)


def write_summary(task, line_no, mutation_index, summary_path, database_name):
    table = task["table"]
    table_name = table["table_name"]
    columns = [column_name(column) for column in table["table_info"]["columns"]]
    rows = table["table_info"].get("rows", [])

    lines = [
        "# Loaded DBBench Task",
        "",
        f"- JSONL line: {line_no}",
        f"- Mutation index: {mutation_index}",
        f"- Type: {', '.join(task.get('type', []))}",
        f"- Database: `{database_name}`",
        f"- Table: `{table_name}`",
        f"- Columns: {', '.join(f'`{column}`' for column in columns)}",
        f"- Initial rows: {len(rows)}",
        "",
        "## Task",
        "",
        task.get("description", "").strip(),
        "",
        "## Reference SQL",
        "",
        "```sql",
        *task.get("label", []),
        "```",
        "",
        "## Manual Inspection",
        "",
        "```sql",
        "SHOW TABLES;",
        f"DESCRIBE `{table_name}`;",
        f"SELECT * FROM `{table_name}` LIMIT 5;",
        "```",
    ]
    summary_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description="Load one DBBench mutation task into MySQL-compatible SQL.")
    parser.add_argument("--jsonl", required=True, help="Path to DBBench JSONL file.")
    parser.add_argument("--mutation-index", type=int, default=0, help="Nth INSERT/UPDATE/DELETE task to load.")
    parser.add_argument("--database", default="dbbench", help="Database name to create.")
    parser.add_argument("--out", required=True, help="SQL output path.")
    parser.add_argument("--summary", required=True, help="Markdown summary output path.")
    args = parser.parse_args()

    jsonl_path = Path(args.jsonl)
    out_path = Path(args.out)
    summary_path = Path(args.summary)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.parent.mkdir(parents=True, exist_ok=True)

    line_no, task = select_task(jsonl_path, args.mutation_index)
    out_path.write_text(build_sql(task, args.database), encoding="utf-8")
    write_summary(task, line_no, args.mutation_index, summary_path, args.database)

    table_name = task["table"]["table_name"]
    print(f"Selected DBBench mutation task {args.mutation_index} from line {line_no}")
    print(f"Type: {', '.join(task.get('type', []))}")
    print(f"Table: {table_name}")
    print(f"Wrote SQL: {out_path}")
    print(f"Wrote summary: {summary_path}")
    print()
    print("After loading into MySQL, manually inspect with:")
    print("  SHOW TABLES;")
    print(f"  DESCRIBE `{table_name}`;")
    print(f"  SELECT * FROM `{table_name}` LIMIT 5;")


if __name__ == "__main__":
    main()
