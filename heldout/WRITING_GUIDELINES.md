# Writing a held-out guideline set

You are writing a test that the guideline compiler has never seen. Write it the way a corporate legal
department writes outside counsel billing guidelines, not the way you imagine software would like to read
them. Do not open anything else in this repository: not the code, not the data, not other sets. This file is
all you need.

A set is a folder `heldout/<set-name>/` with two files.

## 1. `guidelines.md`: the client's billing guidelines

Invent a client (company name, industry, country) and write its outside counsel guidelines as a legal
operations team would publish them: a title, a short preamble, then sections with numbered clauses. Aim for
35 to 60 clauses.

Format, so the document can be split into citable clauses:

* Markdown. `#` for the title, `##` (and optionally `###`) for section headings.
* Each clause starts its own paragraph with its number. Any of these numbering styles is fine, but keep one
  style per document: `3.1 Text`, `§ 3.1 Text`, `Section 3.1 Text`, `3.1(b) Text`, or a sub-paragraph `(b) Text`
  directly after a numbered clause (it becomes `3.1(b)`).
* Unnumbered paragraphs are allowed (preambles, notes); they get ids like `2.p1` and are rarely rules.

Content:

* Include as many of the rule kinds below as are natural for your client, each stated once with its value.
  Use your own words and vary the phrasing the way real guidelines do ("six-minute units", "one-tenth of an
  hour", "0.1 hours"; "will not be paid", "is not reimbursable", "will be written off"). Write numbers the way
  the client would (digits, words, or both).
* Include at least 12 clauses that are not checkable rules: confidentiality, conflicts, staffing courtesy,
  reporting duties, data protection, diversity commitments, invoice addressing, and the like. Some should look
  similar to rules ("we expect counsel to staff matters efficiently") without stating anything checkable.
* Do not state the same rule twice, and do not put two rule kinds in one clause unless real guidelines would.

## 2. `answer_key.yaml`: what a legal-operations analyst would record

```yaml
client_name: Halden Maritime AS      # the name used in the document
tenant: halden                       # a short lowercase id
version: 1
effective_from: 2026-01-01
rules:
  - kind: billing_increment
    clause_id: "3.1"                 # exactly as numbered in the document, e.g. "3.1", "3.1(b)"
    hours: 0.1
  - ...
not_rules: ["1.1", "1.2", "8.3"]     # clause ids of the non-rule clauses you wrote on purpose
```

One entry per rule stated in the document. The kinds and their values:

| kind | values | meaning |
| --- | --- | --- |
| `rate_cap` | none | time may be billed only at the rates approved for each timekeeper |
| `billing_increment` | `hours` (e.g. 0.1, 0.25) | the unit time must be recorded in |
| `max_daily_hours` | `hours` | most hours one timekeeper may bill on one day |
| `travel_time` | `rate_factor` (0 = not billable, 0.5 = half rate) | how non-working travel time is paid |
| `internal_conference` | `policy`: `not_billable` or `one_attendee` | internal meetings between the firm's own timekeepers: not paid at all, or paid for one attendee only |
| `block_billing` | `action`: `reject` or `reduce`; `reduce_factor` when reduce (0.5 = half taken off) | entries combining several tasks under one time |
| `vague_description` | none | entries that do not say what was done |
| `clerical_work` | none | administrative work is not billable whoever does it |
| `nonbillable_role` | `roles`: list of role names as written, lowercase | roles whose time is not paid (e.g. "summer associate") |
| `nonreimbursable_expense` | `codes`: UTBMS expense codes | expense types not reimbursed |
| `expense_unit_cap` | `code`, `max_unit_cost` | a per-unit cap on one expense type |
| `submission_deadline` | `days` | invoices later than this many days after the billing period are refused |
| `max_attendees` | `event`: `deposition` or `hearing`; `max_timekeepers` | most timekeepers paid for attending one event |
| `research_cap` | `hours_per_month` | most legal research hours paid per matter per month |
| `budget` | none | billing above the approved budget needs prior approval |

UTBMS expense codes for `codes` and `code`: E101 copying, E102 outside printing, E103 word processing,
E104 facsimile, E105 telephone, E106 online research, E107 delivery/messengers, E108 postage, E109 local
travel, E110 out-of-town travel, E111 meals, E112 court fees, E113 subpoena fees, E115 deposition transcripts,
E116 trial transcripts, E119 experts, E123 other professionals, E124 other. If the document names an expense
in words only ("meals", "secretarial overtime"), record the code a legal-operations analyst would map it to,
and leave that rule out of the key if no code fits.

Write the key from the document as a careful analyst would, before and independently of any run. Do not
change either file after the set's first run; if you find a mistake afterwards, say so in a `NOTES.md` and the
set will be retired to a regression set.
