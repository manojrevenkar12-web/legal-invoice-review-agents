# 1. Models read, rules decide

**Status:** accepted

**Context.** An invoice adjustment is a payment decision a firm can dispute. Reviewers agree on whether to
dispute a line much more than on why, and a model that both reads and decides makes errors that are hard to
explain and impossible to reproduce.

**Decision.** Models do two things: turn guideline prose into typed rules (with verbatim quotes, verified
mechanically and approved by legal operations), and answer judgement questions about narrative entries (with
a quote from the entry, verified before use). All money is computed by the deterministic engine from approved
rules and recorded decisions. Judgement findings are proposals until a person accepts them.

**Consequences.** Automatic adjustments are reproducible to the cent and auditable to a clause. A faulty or
absent model reduces what is found, never what is taken wrongly (the fault and outage runs measure this). The
cost is a reviewer step for every judgement finding; the queue length is measured and gated.
