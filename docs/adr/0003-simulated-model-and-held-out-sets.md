# 3. A simulated model in CI, and blind held-out sets

**Status:** accepted

**Context.** Evaluations that call a hosted model are slow, cost money, and change when the model does. But a
simulator written alongside the code can be built against.

**Decision.** CI and the committed reports use a deterministic simulated provider with fault modes (invalid
output, invented clauses and quotes, obeyed injections, flipped decisions, outage). The answer key is computed
by a second implementation from the simulator's raw parameters. Separately, blind held-out sets are written by
someone who has not seen the code, from a guide only, and their first runs are kept unedited.

**Consequences.** Safety properties (nothing taken wrongly, nothing ungrounded shown, injections never
decided by the model) are tested on every commit. Accuracy on unseen text is reported honestly by the
held-out first runs, which are lower than the simulated world's numbers. Bedrock and the Anthropic API are
exercised by request and response shape tests, and by running the held-out sets live as new sets.
