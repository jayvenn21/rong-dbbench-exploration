# Rong DBBench Exploration

Small harness for comparing linear and checkpointed exploration on DBBench modification tasks.

## Step 3: Load One Task Database

Generate SQL for the first mutation task in the DBBench dev set:

```bash
python3 scripts/load_task.py \
  --jsonl /Users/jayanth/Projects/AgentBench/data/dbbench/dev.jsonl \
  --mutation-index 0 \
  --database dbbench \
  --out tmp/task.sql \
  --summary tmp/task_summary.md
```

Load it into the local MySQL container:

```bash
docker cp tmp/task.sql dbbench-mysql:/tmp/task.sql
docker exec dbbench-mysql sh -c 'mysql -uroot -prootpass < /tmp/task.sql'
```

Then inspect manually:

```bash
docker exec -it dbbench-mysql mysql -uroot -prootpass dbbench
```

Inside MySQL:

```sql
SHOW TABLES;
DESCRIBE `School Location Table`;
SELECT * FROM `School Location Table` LIMIT 5;
```

The point of this step is schema awareness: before an agent mutates state, you need to understand the table, columns, row identity, and what would count as a correct change.

## Step 4: Linear Mode Baseline

Linear mode resets one DBBench task, executes one SQL action directly, and logs the before/after state. There is no checkpoint and no undo.

First, run it without an action so you can read the task and schema:

```bash
python3 scripts/run_linear.py \
  --jsonl /Users/jayanth/Projects/AgentBench/data/dbbench/dev.jsonl \
  --mutation-index 0
```

Then run with a chosen SQL action:

```bash
python3 scripts/run_linear.py \
  --jsonl /Users/jayanth/Projects/AgentBench/data/dbbench/dev.jsonl \
  --mutation-index 0 \
  --action-sql "INSERT INTO \`School Location Table\` (\`School\`, \`Location\`, \`Date moved\`, \`Currently at this location\`) VALUES ('Madison High School', 'San Diego', '1980', 'nearby junior high school')"
```

For testing the runner only, you can use DBBench's reference SQL:

```bash
python3 scripts/run_linear.py \
  --jsonl /Users/jayanth/Projects/AgentBench/data/dbbench/dev.jsonl \
  --mutation-index 0 \
  --use-reference
```

The run log is written to `tmp/linear_run.md`.

## Step 5: Logs

Every linear run now writes two kinds of logs:

- `tmp/linear_run.md`: readable Markdown for quick inspection
- `tmp/linear_run.json`: pretty structured log for the latest run
- `logs/linear_runs.jsonl`: append-only machine-friendly history of all linear runs
- `logs/linear_runs_pretty.json`: append-only human-readable history of all linear runs

The structured log captures:

```text
task_id
question
mode
schema_summary
steps[].sql
steps[].stdout/stderr
before_state
after_state
final_answer
success
notes
```

This is the evidence layer for the writeup. The code records what happened; your job is to explain why it matters.

## Step 6: Checkpoint Mode

Checkpoint mode creates a database dump before executing state-changing SQL.

Preview the task without executing SQL:

```bash
python3 scripts/run_checkpoint.py \
  --jsonl /Users/jayanth/Projects/AgentBench/data/dbbench/dev.jsonl \
  --mutation-index 0
```

Run with DBBench's reference SQL and keep the mutated state:

```bash
python3 scripts/run_checkpoint.py \
  --jsonl /Users/jayanth/Projects/AgentBench/data/dbbench/dev.jsonl \
  --mutation-index 0 \
  --use-reference
```

Run with DBBench's reference SQL, inspect the mutation, then automatically restore:

```bash
python3 scripts/run_checkpoint.py \
  --jsonl /Users/jayanth/Projects/AgentBench/data/dbbench/dev.jsonl \
  --mutation-index 0 \
  --use-reference \
  --restore
```

Run with validation rules:

```bash
python3 scripts/run_checkpoint.py \
  --jsonl /Users/jayanth/Projects/AgentBench/data/dbbench/dev.jsonl \
  --mutation-index 0 \
  --use-reference \
  --expected-affected-rows 1 \
  --expected-row-delta 1 \
  --verify-sql "SELECT * FROM \`School Location Table\` WHERE \`School\` = 'Madison High School' AND \`Location\` = 'San Diego'" \
  --restore-on-fail
```

The validation rule is the important research choice. Checkpointing gives the agent a restore point, but validation decides whether the mutation should be accepted or rolled back.

Checkpoint logs are written to:

- `tmp/checkpoint_run.md`
- `tmp/checkpoint_run.json`
- `logs/checkpoint_runs.jsonl`
- `logs/checkpoint_runs_pretty.json`

## Step 8: Retry / Alternative Exploration

Exploration mode tries candidate SQL actions one at a time. Before each candidate, it creates a checkpoint. If validation fails, it restores the checkpoint and tries the next candidate.

Preview the task:

```bash
python3 scripts/run_explore.py \
  --jsonl /Users/jayanth/Projects/AgentBench/data/dbbench/dev.jsonl \
  --mutation-index 0
```

Run the demo where the first candidate is intentionally wrong and the second candidate is the DBBench reference SQL:

```bash
python3 scripts/run_explore.py \
  --jsonl /Users/jayanth/Projects/AgentBench/data/dbbench/dev.jsonl \
  --mutation-index 0 \
  --first-bad-then-reference \
  --expected-affected-rows 1 \
  --expected-row-delta 1 \
  --verify-sql "SELECT * FROM \`School Location Table\` WHERE \`School\` = 'Madison High School' AND \`Location\` = 'San Diego'"
```

This simulates the loop:

```text
try SQL -> inspect -> validation fails -> restore -> revise SQL -> try again
```

Exploration logs are written to:

- `tmp/explore_run.md`
- `tmp/explore_run.json`
- `logs/explore_runs.jsonl`
- `logs/explore_runs_pretty.json`

## Step 9: Batch Comparison

Run linear and checkpoint mode on the same mutation tasks:

```bash
python3 scripts/run_batch.py \
  --jsonl /Users/jayanth/Projects/AgentBench/data/dbbench/dev.jsonl \
  --limit 5
```

Outputs:

- `results/batch_summary.csv`
- `results/batch_summary.json`
- `results/batch_summary.md`

This compares systems behavior:

```text
task_id
linear_success
checkpoint_success
num_checkpoints
num_restores
runtime_linear
runtime_checkpoint
failure_reason
```

The first batch uses DBBench's reference SQL so the comparison isolates runtime/checkpoint overhead. Later experiments can replace the reference SQL with generated agent actions.

## Step 10: Case Analysis

Summarize evidence from the logs:

```bash
python3 scripts/analyze_cases.py
```

Outputs:

- `results/case_analysis.md`
- `results/case_analysis.json`

The script looks for:

- a case where checkpointing helped by restoring a failed attempt and accepting a later candidate
- a case where checkpointing did not help because every retry failed validation
- cases where checkpointing only added overhead because the first SQL was already correct

The script gathers evidence. Your writeup should explain the lesson from each case.

## Step 11: Deliverable

A paper-style LaTeX draft is in:

```text
deliverable/main.tex
```

Compile it from the project root:

```bash
pdflatex -output-directory deliverable deliverable/main.tex
```

## Minimal SQL-Agent Interface

Build a prompt for one DBBench mutation task:

```bash
python3 scripts/build_agent_prompt.py \
  --jsonl /Users/jayanth/Projects/AgentBench/data/dbbench/dev.jsonl \
  --mutation-index 0
```

Paste the prompt into an LLM or write the SQL yourself, then run the candidate through checkpoint mode:

```bash
python3 scripts/run_agent_candidate.py \
  --jsonl /Users/jayanth/Projects/AgentBench/data/dbbench/dev.jsonl \
  --mutation-index 0 \
  --mode checkpoint \
  --candidate-sql "INSERT INTO \`School Location Table\` (\`School\`, \`Location\`, \`Date moved\`, \`Currently at this location\`) VALUES ('Madison High School', 'San Diego', '1980', 'nearby junior high school');" \
  --expected-affected-rows 1 \
  --expected-row-delta 1 \
  --verify-sql "SELECT * FROM \`School Location Table\` WHERE \`School\` = 'Madison High School' AND \`Location\` = 'San Diego'"
```

This keeps the agent boundary explicit: the prompt and candidate SQL are logged separately from execution, validation, and checkpointing.

## Autonomous OpenAI SQL Agent

Create a virtual environment and install the OpenAI SDK:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

Set your API key in the shell. Do not commit it:

```bash
export OPENAI_API_KEY="your_key_here"
```

Dry-run the prompt without calling the API:

```bash
python3 scripts/run_autonomous_agent.py \
  --jsonl /Users/jayanth/Projects/AgentBench/data/dbbench/dev.jsonl \
  --mutation-index 0 \
  --dry-run
```

Run autonomous checkpointed exploration:

```bash
python3 scripts/run_autonomous_agent.py \
  --jsonl /Users/jayanth/Projects/AgentBench/data/dbbench/dev.jsonl \
  --mutation-index 0 \
  --model gpt-5-mini \
  --max-attempts 2 \
  --auto-verify
```

If validation fails, the runner restores the checkpoint and asks the model for a revised SQL action using the validation feedback.

Auto-verification currently supports simple DBBench `INSERT ... VALUES ...` reference SQL by deriving an exact-row post-condition query from the table, columns, and values.
