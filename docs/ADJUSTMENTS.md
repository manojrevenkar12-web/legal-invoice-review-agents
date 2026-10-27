# Settlement arithmetic

The engine (`src/lira/review/engine.py`) and the answer key (`src/lira/sim/truth.py`) implement this
independently; `tests/test_engine.py` checks they agree on every simulated invoice.

All amounts are `Decimal`, rounded half up to the cent only when a line's amount is settled.

## Fee lines

1. **Rejected lines are paid nothing.** A line is rejected when any of these applies: the timekeeper is not
   authorised on the matter, the role is not billable, the task code is not authorised, travel time is not
   billable, the timekeeper is above the attendee cap for the event (the most senior attendees keep their
   time), or a reviewer accepted a judgement rejection (vague, clerical, block billing where the rule rejects,
   internal conference).
2. **Hours.** Hours are floored to the billing increment. Research hours above the monthly cap on the matter,
   then hours above the daily cap for the timekeeper, are removed from the latest lines first. Rejected lines
   do not count towards either cap. While a related line has an open question, these caps are proposed
   rather than applied.
3. **Rate.** The rate is capped at the approved rate on the line's date, times the travel factor for travel.
4. **Block billing reduced.** An accepted block-billing reduction takes its share off what remains.

## Expense lines

Non-reimbursable expense codes are rejected. Expenses with a per-unit cap are reduced to the cap times the
units.

## Findings and the line adjustment

Each finding records the amount its own step removed, so the findings on a line add up to the line's
adjustment: settled amount minus billed total. An invoice-level finding (late submission, budget) adjusts
nothing by itself; it routes the invoice to a person.

## Reversals

A reviewer's later decision (for example reversing a finding on appeal) does not edit the earlier one: a new
decision is appended and the invoice is settled again from all decisions in order.
