# Runbook

## dead-letters

**Alarm:** invoices that failed review three times reached the dead-letter queue.

1. Read the job's last error in `jobs.last_error` (or the worker's log in CloudWatch, filtered by invoice
   number). The trace id in the log links to the Langfuse / X-Ray trace.
2. *Format or reference data* (unknown matter, timekeeper missing from the roster): fix the reference data or
   ask the firm to resubmit; the invoice was never partly reviewed.
3. *No approved guidelines in force on the invoice date*: legal operations must approve a version
   (`/v1/guidelines`). Re-queue with `POST /v1/invoices/{number}/review`.
4. *Model or budget*: an outage alone never dead-letters (the invoice is reviewed without the model). A budget
   error means the invoice is unusually long; raise `max_cost_usd_per_invoice` for that tenant in
   `config/models.yaml` through a reviewed change, or re-queue after reducing batch size.
5. Re-drive from the queue only after the cause is fixed; a re-run settles from the stored decisions and does
   not duplicate them.

## drift

**Alarm:** a firm's narrative style moved away from the reference window (PSI above `config/drift.yaml` and a
significant KS / chi-square test on at least one alert feature).

1. Open the drift report (`lira drift`, or the evaluation page) and see which features moved: share of upper
   case, words per entry, tasks per entry, out-of-vocabulary rate.
2. *A new billing system or template at the firm* (upper case, abbreviations): the classifiers' calibrated
   probabilities are less trustworthy for that firm. Lower its candidate thresholds so more lines reach the
   model, and sample 50 lines for labelling.
3. *New vocabulary* (a new practice area, appellate work): check retrieval and task-code accuracy on a sample;
   if they hold, record the review and close.
4. Retrain with the new labels (`lira train`, or the SageMaker pipeline), compare on the held-out sets and
   the golden run, and promote through the canary. Do not retrain on unlabelled drifted data.

## model errors or latency

The router opens a breaker after `breaker_failures` failures and moves to the next model the tenant allows. If
all are open, invoices are still reviewed and marked degraded; proposals come from the classifiers only.
Check Bedrock service health and quotas for the region, and the Guardrail's version. Nothing needs re-running
afterwards, but degraded invoices can be re-reviewed for better proposals.

## a reviewer reports a wrong adjustment

1. Find the finding and its decision history (`audit`, `decisions`). Automatic findings cite a rule and an
   approved guideline version; check the version and the rule's quote.
2. If the rule is wrong, legal operations uploads a corrected version; invoices after its effective date use
   it. Past invoices are adjusted by a new decision, never by editing the old one.
3. Add the case to `tests/` and, if it came from a held-out set, mark that set as a regression set.
