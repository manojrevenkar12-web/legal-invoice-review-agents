# 5. Classical NLP screens every line before a model reads any

**Status:** accepted

**Context.** Most lines on an invoice are unremarkable. Sending every line to a model costs money and latency
and gives the model more chances to be wrong.

**Decision.** Calibrated classifiers trained on reviewer history score every line for each judgement question;
NER resolves names against the firm's roster; an hours model flags unusual time. Only lines above the candidate
thresholds in `config/review.yaml` go to the model. The classifiers' scores are also the fallback when no model
is available, and a confident classifier can overrule a model decision (`classifier_override`).

**Consequences.** About one line in six reaches the model in the simulated world. Recall is bounded by the
screen: the held-out entries set shows the classifiers miss entries written differently from the training
history, which is why drift monitoring watches narrative style per firm.
