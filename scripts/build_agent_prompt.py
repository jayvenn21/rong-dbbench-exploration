#!/usr/bin/env python3
import argparse
import json
from pathlib import Path

from load_task import column_name, select_task


def format_rows(columns, rows, limit):
    lines = []
    for row in rows[:limit]:
        pairs = []
        for column, value in zip(columns, row):
            pairs.append(f"{column}={value!r}")
        lines.append("- " + ", ".join(pairs))
    return "\n".join(lines)


def build_prompt(task, sample_rows):
    table = task["table"]
    table_name = table["table_name"]
    table_info = table["table_info"]
    columns = [column_name(column) for column in table_info["columns"]]
    rows = table_info.get("rows", [])

    return f"""You are a SQL agent operating on a MySQL database.

Task:
{task.get("description", "").strip()}

Database context:
- There is one relevant table: `{table_name}`
- Columns: {", ".join(f"`{column}`" for column in columns)}

Sample rows:
{format_rows(columns, rows, sample_rows)}

Instructions:
- Produce exactly one MySQL state-changing SQL statement.
- Use only the table and columns shown above.
- Prefer targeted mutations over broad mutations.
- Do not include explanation.
- Do not wrap the SQL in Markdown.
- End the SQL statement with a semicolon.
"""


def main():
    parser = argparse.ArgumentParser(description="Build a SQL-agent prompt for one DBBench mutation task.")
    parser.add_argument("--jsonl", required=True, help="Path to DBBench JSONL file.")
    parser.add_argument("--mutation-index", type=int, default=0, help="Nth INSERT/UPDATE/DELETE task.")
    parser.add_argument("--sample-rows", type=int, default=5, help="Number of sample rows to include.")
    parser.add_argument("--out", default="tmp/agent_prompt.txt", help="Prompt output path.")
    parser.add_argument("--metadata", default="tmp/agent_prompt_metadata.json", help="Metadata output path.")
    args = parser.parse_args()

    line_no, task = select_task(Path(args.jsonl), args.mutation_index)
    prompt = build_prompt(task, args.sample_rows)

    out_path = Path(args.out)
    metadata_path = Path(args.metadata)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    metadata_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(prompt, encoding="utf-8")
    metadata_path.write_text(
        json.dumps(
            {
                "jsonl_line": line_no,
                "mutation_index": args.mutation_index,
                "type": task.get("type", []),
                "table": task["table"]["table_name"],
                "question": task.get("description", "").strip(),
                "prompt_path": str(out_path),
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    print(prompt)
    print(f"\nWrote prompt: {out_path}")
    print(f"Wrote metadata: {metadata_path}")


if __name__ == "__main__":
    main()
