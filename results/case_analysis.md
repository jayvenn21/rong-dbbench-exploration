# Step 10 Case Analysis

## Case 1: Checkpointing Helped

- Task: `dev_mutation_0_line_21`
- Question: Insert 'Madison High School' into 'School Location Table' with values 'San Diego' for 'Location', '1980' for 'Date moved', and 'nearby junior high school' for 'Currently at this location'.
- Accepted attempt: `2`

Evidence:

- Attempt 1: validation=False, restored=True, SQL=`INSERT INTO `School Location Table` (`School`, `Location`, `Date moved`, `Currently at this location`) VALUES ('Wrong High School', 'Nowhere', '1900', 'bad row')`
- Attempt 2: validation=True, restored=False, SQL=`INSERT INTO `School Location Table` (`School`, `Location`, `Date moved`, `Currently at this location`)       VALUES ('Madison High School', 'San Diego', '1980', 'nearby junior high school')`

Interpretation to write yourself:

> Checkpointing helped because the first mutation executed but failed semantic validation. The system restored the pre-mutation state and accepted a later candidate.

## Case 2: Checkpointing Did Not Help

- Task: `dev_mutation_0_line_21`
- Question: Insert 'Madison High School' into 'School Location Table' with values 'San Diego' for 'Location', '1980' for 'Date moved', and 'nearby junior high school' for 'Currently at this location'.

Evidence:

- Attempt 1: validation=False, restored=True, SQL=`INSERT INTO `School Location Table` (`School`, `Location`, `Date moved`, `Currently at this location`) VALUES ('Wrong High School', 'Nowhere', '1900', 'bad row')`
- Attempt 2: validation=False, restored=True, SQL=`INSERT INTO `School Location Table` (`School`, `Location`, `Date moved`, `Currently at this location`) VALUES ('Still Wrong High School', 'Elsewhere', '1901', 'bad retry')`

Interpretation to write yourself:

> Checkpointing preserved the database, but it did not solve the task because every proposed retry still violated the post-condition.

## Case 3: Overhead Not Worth It

Reference-SQL batch cases where both modes succeeded and checkpoint mode did not restore:

| task_id | type | table | linear_s | checkpoint_s | overhead_s |
| --- | --- | --- | ---: | ---: | ---: |
| dev_mutation_0_line_21 | INSERT | School Location Table | 0.0858 | 0.1693 | 0.0835 |
| dev_mutation_1_line_22 | INSERT | Football Standings | 0.0767 | 0.2162 | 0.1394 |
| dev_mutation_2_line_23 | INSERT | Golf Tournament Winners | 0.0773 | 0.1616 | 0.0843 |
| dev_mutation_3_line_24 | INSERT | Olympic Medals | 0.0716 | 0.1637 | 0.0921 |
| dev_mutation_4_line_25 | INSERT | Dallas Cowboys 1986 Season | 0.0656 | 0.1477 | 0.0822 |

Interpretation to write yourself:

> When the first SQL action is already correct, checkpointing mainly adds dump overhead. Its value appears when actions are uncertain or validation can reject unsafe mutations.
