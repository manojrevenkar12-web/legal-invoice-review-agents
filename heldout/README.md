# Blind held-out sets

The simulated world is useful for measuring money and safety, but a system can be built against its own
simulator. These sets are the check on that: each was written by an agent that was given one guide from this
folder and nothing else (no code, no data, no other set), run once, and its first run kept in
`reports/heldout/<set>/first_run.json` with the sha256 of the set's files.

| Set | Written from | What it measures |
| --- | --- | --- |
| `guidelines-lindeveld` | `WRITING_GUIDELINES.md` | compiling a Dutch energy utility's guidelines (49 clauses, 17 rules, 30 deliberate non-rule clauses) |
| `entries-litigation-1` | `WRITING_ENTRIES.md` | the full review of 60 time entries against the writer's block-billing, vague and clerical labels |

Rules:

* `lira heldout run <set>` writes `first_run.json` the first time and `latest.json` afterwards. It refuses to
  run a set whose files changed after the first run.
* Nobody changes code to pass a set. Studying a set's failures in order to change code turns it into a
  regression set: write `regression` to its `STATUS`, keep its first run, and have someone who has not seen
  the code write a new set from the guide.
* The first runs are quoted, not re-run, in `reports/eval/SUMMARY.md`.

## What the first runs say

Both first runs were made with the simulated model, whose guideline reading is the pattern extractor and
whose line reading is the classifiers plus fixed heuristics. They are much lower than the simulated world's
numbers, which is the point of having them:

* Guidelines: 8 of 17 rules found, none invented, none taken from the 30 non-rule clauses, one rule with an
  incomplete list of expense codes. The verifier and cross-check keep precision; recall on wording the
  extractor has not seen needs a real model, and every missed rule is visible to legal operations as a clause
  without a rule before approval.
* Entries: no compliant entry was touched (15 of 15) and every finding made was one the writer agreed with
  (precision 100% on all three questions), but recall was 58% for block billing, 43% for vague descriptions
  and 23% for clerical work; 3 of 5 embedded instructions were flagged.

Both sets stay blind. The next step is to run them once with Bedrock (`LIRA_MODEL=live`) as a second recorded
first run under a new set name, so the comparison between the simulated reader and a real one is on the same
text.
