# Human evaluation protocol

The LLM-as-judge grades proposed findings so that prompt and model changes can be compared without a
reviewer reading every one. A judge is only worth trusting where people agree with each other and the judge
agrees with them, so every round of judge evaluation starts with people.

## A round

1. **Sample once, then pin.** `lira eval` draws a stratified sample of proposed findings (by finding kind and
   firm) into `round-N/items.jsonl` the first time it runs. The file is never regenerated; a new sample is a new
   round.
2. **Grade blind.** Each annotator receives the items in their own shuffled order, without the finding's
   source (rule, classifier or model), its confidence, the judge's verdict or the other annotator's labels. The
   console's *Annotate* page and `GET /v1/annotation/items` serve exactly these fields.
3. **Rubric.** Each criterion is `pass` or `fail`:

   | Criterion | Pass when |
   | --- | --- |
   | supported | the cited clause states the rule the finding applies |
   | correct | a careful reviewer applying that clause would make the same decision about this entry |
   | specific | the explanation refers to this entry (its words, hours or date), not entries in general |
   | professional | the explanation is courteous and factual, fit to send to the law firm |
   | overall | pass only if supported and correct both pass |

   A short free-text note is optional and is never shown to the other annotator.
4. **Agreement first.** Cohen's kappa between the two annotators, per criterion, is the ceiling for the judge.
   Items where they disagree go to an adjudicator (`round-N/adjudicated.jsonl`); until then they are left out
   of the judge comparison rather than guessed.
5. **Judge against people.** The judge grades the same items. Its kappa against the settled labels is
   reported per criterion next to the annotators' kappa (`reports/eval/SUMMARY.md`). The judge replaces
   people only on a criterion where its kappa is close to the people's; elsewhere it is a triage aid.
6. **Bias check.** The judge grades every item twice, once with an explanation padded with irrelevant
   courteous text. A verdict that changes with padding counts as a flip (reported as `length_padding_flips`).

## Round 1

Round 1 (`round-1/`) has 60 lines but 59 distinct items: the sampler drew one finding twice because the
engine recorded non-billable travel twice on a line (fixed, with a test; the sampler now also skips repeated
findings). The pinned file is left as it is and the agreement counts each item once. Its two annotators were independent AI agents standing in for people:
each was given only this protocol and its own shuffled, blinded copy of the items, and neither saw the code,
the answer key, the judge's verdicts or the other's labels. That is enough to exercise the protocol and the
arithmetic; it is not a substitute for two legal-spend reviewers, and round 2 should be graded by people
through the console.

Round 1 result (`reports/eval/SUMMARY.md`): the two annotators agreed on every criterion of every item; both
failed the same two findings on *correct*. With so few failures, perfect agreement is weak evidence about the
rubric. The simulated judge agreed with them on *correct* for 76% of items, but its kappa was −0.06: it fails
findings the annotators pass and misses the two they fail. Under rule 5 the judge is not trusted on
*correct* or *overall*; it stays a triage aid until a real model judge is measured on round 2.
