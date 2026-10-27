# Design

## The problem

A corporate legal department receives hundreds of invoices a month from outside counsel. Each client has its
own billing guidelines (rates, increments, travel, staffing, expenses, what counts as block billing), and
reviewers spend their time on two very different kinds of work: arithmetic that a machine should do exactly,
and judgement on narrative entries where reviewers themselves disagree. Published work on LLM invoice review
(Whitehouse et al., *Better Bill GPT*, 2025) found that reviewers agree well on whether to dispute a line but
only fairly on why. That shapes the whole system: the arithmetic must be exact and the judgement must be
explained, proposed and decided by a person.

## Flow of one invoice

```
LEDES file ─▶ parse_validate ─▶ analyze ─▶ apply_rules ─▶ plan ─┬─▶ review_batch ×N ─▶ adjudicate ─▶ settle ─▶ human_review ─▶ settle
 (S3/API)     format issues     NER,        engine:         which  │   line reviewer:      verifier:      exact      interrupt:      with the
              reject early      classifiers,  auto findings   lines  │   forced tool call,   quotes, clause, money      accept /        decisions
                                hours z,      and proposals   need a │   may search the      one reason per             reject
                                injections                    model  │   guidelines          line
                                                                     └─ model down: lines stay as classifier proposals
```

* **parse_validate** rejects files that are not valid LEDES 1998B, with line-level issues the firm can fix.
* **analyze** runs the classical NLP on every line: names resolved against the firm's roster and
  pseudonymised (`PERSON_n`) before any text leaves the process, calibrated probabilities for the judgement
  questions, a task-code check, an hours model by task, activity and timekeeper class, event keys for
  attendance caps, and the injection screen.
* **apply_rules** runs the engine on the approved `RuleSet` in force on the invoice date. Arithmetic rules are
  applied; text-dependent rules (travel, attendees) and judgement rules are proposed. Daily and research caps
  wait while a related line has an open question, so the engine never removes hours that a reviewer's
  decision would have changed.
* **plan** sends only plausible lines to the model (`config/review.yaml: candidates`), in batches.
* **review_batch** is a tool-using agent: it may call `search_guidelines` up to `max_agent_steps` times and
  must finish with `record_findings`. Every decision quotes the entry.
* **adjudicate** keeps a model decision only if its quote is in the entry, its clause exists and states the
  rule, and it does not contradict a confident classifier (`classifier_override`). One reason per line.
* **settle** is pure arithmetic over findings and decisions (`docs/ADJUSTMENTS.md`). It runs before and after
  the human step, and again when an appeal is decided.

The graph is checkpointed (PostgreSQL in the service), so an invoice waiting for a reviewer survives restarts
and the reviewer's decisions resume the same thread.

## Guidelines

Guideline documents arrive as Markdown or text exported from Word or PDF. The clause parser keeps the client's
numbering (`3.1`, `§ 4.1`, `4.1(b)`, `(c)`) so every rule cites something a person can find. The compiler asks
the model for typed rules with clause ids and verbatim quotes; `verify` checks that each quote is in its
clause and that each value is stated in the quote; a pattern extractor proposes the same rules independently
and disagreements are shown to legal operations. Nothing is used until a person approves the version.
Versions have effective dates; an invoice is reviewed under the version in force on its date, and the diff
between versions is shown in plain words.

## Models

`config/models.yaml` lists the models, which tasks may use which models in which order, and which regions each
tenant's data may go to. Kestrel's data stays in the EU (`eu.` inference profiles on Bedrock); Corvane allows a
vendor API as a last resort. The router retries with backoff, opens a breaker on a failing model, repairs
invalid output once, and stops before a call that would exceed the invoice's budget. If every model is
unavailable, the invoice is still reviewed: the engine's findings stand and the classifier's candidates become
proposals marked as such.

Prompts are versioned YAML files. `prompts.lock` pins each prompt's sha256, and a changed prompt must have a
new version and a regression run recorded in `reports/prompts/history.jsonl` before `lira prompts check`
passes.

## Tenancy and data

Every tenant table has `FORCE ROW LEVEL SECURITY` with a policy on `current_setting('app.tenant')`, set per
transaction. Decisions and audit are append-only (triggers refuse updates and deletes). Firms see the outcome,
the clause and the explanation of each finding, never the evidence, the model's reasoning or other firms'
invoices. Names are pseudonymised before model calls, and Bedrock Guardrails screens prompts for attacks and
personal data when deployed.

## AWS

* **Foundation**: VPC with private subnets and endpoints, KMS key, S3 intake bucket (EventBridge
  notifications), Aurora PostgreSQL with IAM authentication, SQS queue with a dead-letter queue.
* **AI**: OpenSearch Serverless vector collection and index, a Bedrock Knowledge Base over approved guideline
  versions (no chunking: clauses are already the chunks), a Guardrail, SageMaker model package group.
* **Service**: ECS Fargate API behind an internal ALB and a worker on the queue, the intake Lambda, alarms on
  dead letters, drift and latency, a CloudWatch dashboard, and a GitHub OIDC deploy role.

The classical models can be trained in SageMaker, registered with metrics, approved, and deployed blue/green
with a canary; `aws/sagemaker.py` builds those requests and the endpoint scorer that replaces local scoring.

## Evaluation

The simulator writes invoices from firms with known habits and an answer key computed by a second,
independent implementation of the guidelines. The evaluation measures money (what was taken, what was taken
wrongly, what share of the possible savings was captured), the reviewer's queue, judgement precision and
recall, behaviour under a faulty model and with no model at all, compilation, retrieval and Q&A, drift
detection, appeals, and the judge against annotators. The release gate reads only the report.

Simulation can be fooled by building against the simulator. The held-out sets exist for that reason: they are
written by someone who has not seen the code, from a guide only, and their first runs are kept unedited.
