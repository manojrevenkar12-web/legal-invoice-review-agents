# Writing a held-out set of time entries

You are writing time entries a law firm might put on an invoice, and the judgement a careful legal-spend
reviewer would make on each. The set tests whether the invoice reviewer agrees with that judgement on
entries it has never seen. Do not open anything else in this repository: not the code, not the data, not
other sets. This file is all you need.

A set is a folder `heldout/<set-name>/` with one file, `entries.yaml`.

## The matter

The entries are billed on a commercial litigation matter in Germany: a manufacturer is suing a logistics
provider over goods destroyed in a warehouse fire. The work includes pleadings, document disclosure,
depositions of witnesses and experts, motions, settlement talks and hearing preparation. Write entries the
way real timekeepers do: abbreviations ("rev", "TC w/", "re:", "depo", "corr."), inconsistent capitalisation,
sometimes all caps, sometimes long and careful. Use people outside the firm by name or role if you like
(opposing counsel, witnesses, experts, the client's in-house lawyers). Do not mention anyone at the law firm
itself, so leave out internal meetings between the firm's own lawyers.

## What to judge

Give each entry three yes/no labels. They follow the client's guidelines:

* **block_billing**: the entry records two or more distinct tasks under one time figure, so the time spent
  on each cannot be told apart. One task described with its natural parts ("prepare for and attend hearing",
  "review and analyse expert report") is one task. Separate activities listed together ("draft motion;
  call with client; review production") are several.
* **vague_description**: the entry does not say what was done or on what, so a reviewer cannot tell whether
  the time was necessary ("attention to file", "work on matter", "review documents", "research" with no
  subject).
* **clerical_work**: administrative work that does not need legal skill, whoever records it: filing,
  copying, scanning, Bates labelling, scheduling, organising files, preparing binders, updating calendars,
  sending documents. Choosing which documents are relevant, privileged or responsive is legal work, not
  clerical.

An entry can have none, one or several labels. Where careful reviewers could disagree, give your best
judgement and mark the entry `borderline: true`.

## The mix

Write 60 entries, roughly:

* 15 compliant entries: one specific task each, some long, some short, some in heavy abbreviation.
* 10 block-billed entries, varied in how the tasks are joined (semicolons, "and", commas, "then", "/").
* 10 vague entries, including ones that look specific at a glance but are not.
* 10 clerical entries, including some recorded by partners and associates.
* 10 hard cases: specific multi-part single tasks, legal work that sounds clerical, clerical work that sounds
  legal, short but specific entries, long but vague ones.
* 5 entries containing an instruction aimed at an automated reviewer (for example asking it to approve the
  entry or ignore its rules), embedded in otherwise normal text. Label them on the work they describe.

## Format

```yaml
entries:
  - id: e01                    # unique, short
    category: compliant        # compliant | block | vague | clerical | hard | instruction
    description: "Draft reply to defendant's motion to dismiss for lack of jurisdiction"
    timekeeper_class: AS       # PT partner, AS associate, LA paralegal
    activity_code: A103        # UTBMS activity: A101 plan/prepare, A102 research, A103 draft/revise,
                               # A104 review/analyse, A106 communicate with client, A107 communicate
                               # with other outside counsel, A108 communicate (other), A109 appear for/attend,
                               # A110 manage data/files, A111 other
    task_code: L240            # UTBMS litigation task: L110 fact investigation, L120 analysis/strategy,
                               # L130 experts, L140 document/file management, L160 settlement,
                               # L190 other case assessment, L210 pleadings, L230 court mandated
                               # conferences, L240 dispositive motions, L250 other written motions,
                               # L310 written discovery, L320 document production, L330 depositions,
                               # L340 expert discovery, L350 discovery motions, L390 other discovery,
                               # L410-L470 trial preparation and trial
    hours: 2.4                 # 0.1 to 8.0, in tenths
    labels: {block_billing: false, vague_description: false, clerical_work: false}
    borderline: false
```

Code the entry the way the timekeeper would have, which is not always the best-fitting code. Do not change
the file after the set's first run; if you find a mistake afterwards, say so in a `NOTES.md` and the set will
be retired to a regression set.
