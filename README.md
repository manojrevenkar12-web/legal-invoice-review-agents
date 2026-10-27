# Legal invoice review agents

An invoice reviewer for corporate legal departments. Law firms send LEDES 1998B invoices; the system reads
each client's outside counsel billing guidelines, applies them line by line, and gives the in-house reviewer a
short queue of decisions with the clause, the evidence and an explanation the firm can read. Firms can appeal
an adjustment and get an answer that quotes their appeal back to them.

The design rule is **models read, rules decide**:

* A language model turns guideline prose into typed rules, but every rule carries a verbatim quote that is
  checked mechanically, a pattern extractor cross-checks the values, and legal operations approves the rule
  set before it is used.
* A deterministic engine applies the rules that are arithmetic (rates, increments, daily caps, roles,
  expenses, research caps, attendees). The money it takes is reproducible to the cent.
* Classical NLP (calibrated classifiers, NER against the firm's roster, topic and hours models) screens every
  line. Only lines where a judgement call is plausible go to the model, which must quote the entry; a
  verifier throws away any decision whose quote or clause does not check out.
* Judgement findings (block billing, vague descriptions, clerical work, internal conferences) are proposals.
  A person accepts or rejects them, and nothing a model said is ever applied without that step.

## What is in the repository

| Part | Where | What it does |
| --- | --- | --- |
| LEDES and UTBMS | `src/lira/ledes.py`, `utbms.py` | 1998B parser and writer (24 fields, `[]` line ends), format checks with line-level issues |
| Guideline compiler | `src/lira/guidelines/` | clause parser for the common numbering styles, typed `RuleSet`, quote and value verification, pattern cross-check, version diff |
| Rule engine | `src/lira/review/engine.py` | settles every line from approved rules; hour caps wait while related lines have open questions |
| Classical NLP | `src/lira/nlp/` | spaCy NER resolved against the roster and pseudonymised before any model call; calibrated logistic regression for the judgement questions, task-code and appeal-intent classifiers, NMF topics, robust z-scores for hours |
| Review graph | `src/lira/agents/review_graph.py` | LangGraph: validate, analyse, apply rules, plan, fan out batches to the line reviewer (`Send`), adjudicate, settle, human review (`interrupt`), settle again |
| Appeal graph | `src/lira/agents/appeal_graph.py` | classify intent, close or answer, or assess the facts with a recommendation that must quote the appeal |
| Retrieval and Q&A | `src/lira/retrieval/` | hybrid BM25 + dense with reciprocal rank fusion, mandatory tenant filter, version in force on a date, answers checked for citations and numbers or replaced by an abstention |
| Model access | `src/lira/llm/` | Bedrock Converse with forced tool use and Guardrails, Anthropic API, and a simulated provider with fault modes; router with per-tenant data residency, retries, circuit breaker, one repair round and a per-invoice budget; versioned prompts locked by sha256 |
| Guard | `src/lira/guard/screen.py` | prompt-injection and PII screen on every entry; Bedrock Guardrails when deployed |
| Drift | `src/lira/monitor/drift.py` | PSI with KS / chi-square gating on narrative style per firm and month, out-of-sample reference window |
| Service | `src/lira/service/` | FastAPI over PostgreSQL with forced row-level security per tenant, append-only decisions and audit, hashed API keys and roles, a `SKIP LOCKED` job queue |
| AWS | `src/lira/aws/`, `infra/` | S3 → EventBridge → Lambda intake, SQS worker, SageMaker training / registry / canary, Knowledge Base sync; CDK stacks with cdk-nag |
| Console | `web/` | React reviewer workstation: queue, invoice ledger with findings, guideline versions and diffs, Q&A, evaluation, blind annotation, firm view |
| Evaluation | `src/lira/eval/`, `reports/` | golden, fault and outage runs, compilation, retrieval, drift, appeals, LLM-as-judge against annotators, Bedrock Evaluations export, release gate |

## Run it

Python 3.12, PostgreSQL 16 for the service tests, Node 20 for the console.

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"            # add ",infra" for the CDK tests
pip install https://github.com/explosion/spacy-models/releases/download/en_core_web_md-3.8.0/en_core_web_md-3.8.0-py3-none-any.whl

lira run            # simulate the world, train, compile and approve guidelines, evaluate, gate
pytest -q           # unit, pipeline and (with LIRA_TEST_DATABASE_URL) service tests

lira review data/world/invoices/kestrel/2026-10/HAR-2610-0220.ledes --accept-all
lira ask --tenant kestrel --on 2026-09-01 "Is travel time billable?"
```

The service and console:

```bash
export LIRA_DATABASE_URL=postgresql://lira:lira@localhost:5432/lira
lira demo --seed-only && lira serve --port 8080
cd web && npm ci && npm run dev      # http://localhost:5173, sign in with demo-reviewer-kestrel
```

Everything runs offline with the simulated model. Set `LIRA_MODEL=live` with AWS credentials for Bedrock (and
`ANTHROPIC_API_KEY` for the direct API fallback) to use real models; `config/models.yaml` decides which model each task and tenant may use.

## Results

`lira eval` writes `reports/eval/report.json` and the generated `reports/eval/SUMMARY.md`; `lira gate`
fails the build when a number misses `config/release.yaml`. On the simulated model and 60 live invoices:

| | |
| --- | --- |
| Money taken automatically | 118,651.96, of which taken wrongly 0.00 |
| Savings captured if the reviewer accepts only correct proposals | 96.3% |
| Lines sent to the model | 15.7%, about $0.002 per invoice |
| Judgement findings, precision / recall | block billing 87.9 / 94.4, vague 85.7 / 91.3, clerical 100 / 88.9, internal conference 100 / 90 |
| Entries with instructions to an AI | 7 of 7 flagged, 0 decided by the model |
| Faulty model (invalid output, invented clauses and quotes, obeyed injections) | 0 ungrounded findings shown, 0 money taken wrongly |
| Model down for the whole run | 0 invoices left unreviewed |
| Guideline compilation | 59 of 59 rules, 0 wrong values; invented rules accepted 0 |
| Retrieval | recall@5 86.6%, cross-tenant hits 0; unsupported answers shown 0 |
| Injected narrative drift | detected in both months it was injected |

These numbers come from a simulated world whose answer key is computed independently of the engine
(`src/lira/sim/truth.py`). The blind held-out sets in `heldout/` were written by agents that never saw the
code, and their first runs are kept in `reports/heldout/` unedited; they are the better estimate of how the
system does on text it was not built against.

## Limits

* The model in every committed number is simulated. It is deterministic and has realistic fault modes, but it
  is not Claude; the Bedrock and Anthropic paths are tested against the real request and response shapes
  (botocore `Stubber` validates them against the service model), not against live endpoints.
* The world is simulated: three clients, five firms, two years of invoices. Real guidelines and narratives are
  messier than any generator.
* The CDK stacks synthesise and pass cdk-nag; they have not been deployed to an AWS account.
* Round 1 of human evaluation was graded by two independent AI annotators following the protocol; round 2
  needs people.

## Further reading

* `docs/DESIGN.md`: the architecture and why it is shaped this way
* `docs/ADJUSTMENTS.md`: the settlement arithmetic
* `docs/RUNBOOK.md`: alarms and what to do about them
* `docs/adr/`: decisions
* `evals/annotation/PROTOCOL.md`: how the judge is kept honest
* `heldout/`: how blind sets are written and why their first runs are never rewritten
