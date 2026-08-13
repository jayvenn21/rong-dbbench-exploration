#!/usr/bin/env python3
import argparse
import csv
import io
import re
from pathlib import Path

from load_task import quote_identifier, quote_literal, select_task


INSERT_RE = re.compile(
    r"^\s*INSERT\s+INTO\s+`(?P<table>(?:``|[^`])+)`\s*"
    r"\((?P<columns>.*?)\)\s*VALUES\s*\((?P<values>.*)\)\s*;?\s*$",
    re.IGNORECASE | re.DOTALL,
)


def parse_backtick_identifiers(text):
    return [match.replace("``", "`") for match in re.findall(r"`((?:``|[^`])*)`", text)]


def parse_sql_values(text):
    reader = csv.reader(
        io.StringIO(text),
        delimiter=",",
        quotechar="'",
        doublequote=True,
        skipinitialspace=True,
    )
    values = next(reader)
    return [None if value.upper() == "NULL" else value for value in values]


def infer_insert_verifier(sql):
    match = INSERT_RE.match(sql)
    if not match:
        return None

    table = match.group("table").replace("``", "`")
    columns = parse_backtick_identifiers(match.group("columns"))
    values = parse_sql_values(match.group("values"))
    if len(columns) != len(values):
        return None

    predicates = []
    for column, value in zip(columns, values):
        if value is None:
            predicates.append(f"{quote_identifier(column)} IS NULL")
        else:
            predicates.append(f"{quote_identifier(column)} = {quote_literal(value)}")

    return {
        "kind": "insert_exact_row",
        "verify_sql": f"SELECT * FROM {quote_identifier(table)} WHERE " + " AND ".join(predicates) + ";",
        "expected_affected_rows": 1,
        "expected_row_delta": 1,
    }


def infer_verifier(sql):
    stripped = sql.lstrip().upper()
    if stripped.startswith("INSERT"):
        return infer_insert_verifier(sql)
    return None


def main():
    parser = argparse.ArgumentParser(description="Infer a simple validation query from DBBench reference SQL.")
    parser.add_argument("--jsonl", required=True)
    parser.add_argument("--mutation-index", type=int, default=0)
    args = parser.parse_args()

    _, task = select_task(Path(args.jsonl), args.mutation_index)
    reference_sql = task.get("label", [""])[0]
    inferred = infer_verifier(reference_sql)
    if inferred is None:
        raise SystemExit("Could not infer verifier for this task.")
    print(inferred["verify_sql"])
    print(f"expected_affected_rows={inferred['expected_affected_rows']}")
    print(f"expected_row_delta={inferred['expected_row_delta']}")


if __name__ == "__main__":
    main()
