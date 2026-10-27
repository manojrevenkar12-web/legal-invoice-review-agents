# 4. Data residency is a routing decision

**Status:** accepted

**Context.** Some clients require that their data stays in the EU; others accept a US region or a vendor API as
a fallback. Fallback logic that ignores this would move data during an outage, exactly when nobody is looking.

**Decision.** `config/models.yaml` lists, per tenant, the region groups its data may go to. The router builds
each task's fallback chain and drops models outside the tenant's groups before any call. Bedrock calls use the
`eu.` cross-region inference profiles for EU tenants. Names are pseudonymised before any model call.

**Consequences.** An outage of every EU model degrades an EU tenant's review to classifiers and rules instead of
moving its data. The policy is tested in `tests/test_llm.py`.
