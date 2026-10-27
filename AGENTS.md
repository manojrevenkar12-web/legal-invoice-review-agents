# Working on this repository

## Fast feedback

* `pytest -q` (about 20 s once the session fixture has built the world). Service tests need PostgreSQL:
  `LIRA_TEST_DATABASE_URL=postgresql://lira:lira@localhost:5432/lira_test`. Infra tests need `.[infra]` and Node.
* `ruff check src tests infra`, `ruff format --check src tests`, `mypy`.
* `cd web && npm test` (Vitest); `npx playwright test` after `lira demo --seed-only`.
* `lira run` rebuilds everything and ends with the release gate; `lira eval && lira gate` after a change to
  rules, prompts, classifiers or the graph.

## Invariants

* **Models read, rules decide.** Money is computed only by `review/engine.py` from approved rules and recorded
  decisions. A model's output is used only after `adjudicate.verify_decision` or `rules.verify` accepts it.
* **Judgement findings are proposals.** Nothing a model said changes an amount until a person accepts it.
* **Truth isolation.** Only `lira.sim` and `lira.eval` read `data/truth` or `config/world.yaml`;
  `tests/test_isolation.py` checks imports.
* **Tenancy in PostgreSQL.** Tenant data is read and written only inside `tenant_tx`; retrieval always takes a
  tenant. Decisions and audit are append-only.
* **Names never reach a model.** Entries are pseudonymised before any model call; the router honours each
  tenant's allowed regions.
* **Settings live in `config/`**, not in code: thresholds, model chains, tenant policies, drift, release gate.
* **Prompts are versioned.** A changed prompt needs a new version, `lira prompts lock`, and a regression run
  (`lira eval`) before `lira prompts check` passes.

## Held-out sets

Never change code to pass a set in `heldout/`. Its first run in `reports/heldout/<set>/first_run.json` is not
rewritten. If you study a set's failures, write `regression` to `heldout/<set>/STATUS` and have someone who has
not seen the code write a new set from `heldout/WRITING_GUIDELINES.md` or `heldout/WRITING_ENTRIES.md` only.

## Where things are

`README.md` for the map, `docs/DESIGN.md` for the architecture, `docs/ADJUSTMENTS.md` for the arithmetic,
`docs/RUNBOOK.md` for alarms, `docs/adr/` for decisions, `evals/annotation/PROTOCOL.md` for human evaluation.
