# Implementation Plan

The build, phase by phase. The orchestrator (`build-phase` skill) works on the first phase with open tasks, ticks the boxes as tasks finish, and stops at the end of each phase for review. Every task has an owner (a subagent and the folder it may edit) and a **done when** that can be checked by running something.

Status: `[ ]` open · `[x]` done · `[~]` partly done (see note).

## Decisions made for the build

- **Rejecting a diagnosis** with a hint resumes the **same** investigation (same LangGraph thread, feedback in state). A separate **Re-investigate** button creates a **new** investigation on the same case. Only one active investigation per case.
- **Banking service build:** Maven wrapper (`./mvnw`).
- **Contracts** live in `contracts/` and are written before any app code (Phase 0).
- **Default LLM profile** is `ollama`; all tests use the fake LLM.

---

## Phase 0 — Foundations (sequential, orchestrator)

No parallel work: everything later depends on this.

- [ ] **Repo skeleton** (owner: orchestrator): folders per the repo layout in `docs/architecture.md`, `.env.example`, root `Makefile` with `test`, `test-agent`, `test-banking`, `test-frontend`, `lint-contracts`, `up`, `infra`, `models` (targets for apps that do not exist yet print "skipped").
  Done when: `make test` and `make lint-contracts` run without error.
- [ ] **Banking API contract** (owner: orchestrator): `contracts/banking-api.yaml`, read-only endpoints behind every ops tool in section 6 and customer tool in section 3 (cards, authorisations, balance, card controls, fraud flags, scheduled payments, payment attempts, transactions, payees, mandates, direct debit collections, customers), plus `/simulation/incidents`. Reason codes as enums.
  Done when: lints clean; every tool in the architecture doc maps to an endpoint.
- [ ] **Agent API contract** (owner: orchestrator): `contracts/agent-api.yaml`: conversations and messages (SSE), cases, investigations, events (SSE with `Last-Event-ID`), feedback, simulation, plus a schema per SSE event type from section 8.
  Done when: lints clean; every ops console and customer app screen in the doc maps to endpoints.
- [ ] **Scenario format** (owner: orchestrator): `contracts/scenario.schema.json` for eval and simulation scenarios (incident type, complaint, persona hints, expected issue type, root cause, outcome), with two examples.
  Done when: the examples validate against the schema.
- [ ] **CI runs** (owner: orchestrator): `.github/workflows/ci.yml` calls the make targets.
  Done when: CI is green on the Phase 0 PR.

## Phase 1 — Components against the contracts (parallel)

- [ ] **Banking service** (owner: `banking-dev`, `banking-service/`): Spring Boot app with the domain (customers, accounts, cards, authorisations, scheduled payments, payees, mandates, direct debit collections, fraud flags) and the rules that produce declines and failures with the contract's reason codes; seed data for about 10 customers; read-only API per `contracts/banking-api.yaml`; simulation module behind `@Profile("simulation")` covering every scenario row in section 5; Flyway migrations in schema `banking`.
  Done when: `make test-banking` passes, including a test per simulation scenario and a test that responses match the contract.
- [ ] **Agent core** (owner: `agent-dev`, `agent-service/`, excluding `assistant/`, `investigation/`, `simulation/`, `evals/`): uv project, FastAPI app skeleton, settings, banking client from the contract, ops-scoped and customer-scoped tools, signal snapshot, triage (structured output), guardrails (input guard, output guard, masking), LLM client using aliases via LiteLLM, fake LLM in `tests/fakes.py`, knowledge base ingestion and hybrid search, DB models and migrations for the agent tables in section 9, Postgres job queue (`SKIP LOCKED`, `NOTIFY`), Langfuse tracing setup with masking.
  Done when: `make test-agent` passes with the banking API mocked from the contract; customer tools have a test proving `customer_id` cannot be supplied by the model; masking has tests.
- [ ] **Front-ends** (owner: `frontend-dev`, `frontend/`): Angular workspace with `customer-app` (log-in-as picker, account view, chat with streamed answers, sources, 👍/👎, watch mode) and `ops-console` (case queue with Simulate buttons, case detail with timeline, diagnosis and review panel, conversations list, new case form), against a mock of `contracts/agent-api.yaml` including SSE.
  Done when: `make test-frontend` passes and both apps build.
- [ ] **Infra and knowledge base** (owner: `infra-dev`, `infra/`, `knowledge-base/`): Compose stack per section 11 (Postgres with pgvector, LiteLLM, self-hosted Langfuse, optional `ollama` profile), LiteLLM profiles `ollama`, `anthropic`, `mixed` with the five aliases, a Langfuse first-run setup script, `make models`; 10–15 knowledge base articles covering the topics in section 4.
  Done when: `docker compose config` validates every profile combination; every LiteLLM profile defines all five aliases; articles exist for every topic.

## Phase 2 — Agents (parallel, separate subfolders)

- [ ] **Assistant graph** (owner: `agent-dev`, `agent-service/assistant/`): graph per section 4, handoff rules as code, case creation on handoff, conversation endpoints with SSE streaming.
  Done when: tests with the fake LLM cover each handoff rule, the output guard retry then handoff, and a resolved conversation.
- [ ] **Investigation graph** (owner: `agent-dev`, `agent-service/investigation/`): graph per section 6 (verify, triage, route, park, investigators with restricted tools and step budgets, evidence check, diagnose, escalate, `interrupt()` review, finalize), Postgres checkpointer, workers, investigation endpoints with event storage and SSE replay, reject-with-hint resume and Re-investigate.
  Done when: tests with the fake LLM and in-memory checkpointer cover each route, park, escalate, review accept, correct and reject-with-hint resume.
- [ ] **Simulation and evals** (owner: `agent-dev`, `agent-service/simulation/` and `agent-service/evals/`): customer simulator persona, Simulate customer and Simulate email endpoints, scenario YAML files for every row in section 5, scorers, Langfuse experiment runner, pytest gate with thresholds.
  Done when: scorers have unit tests; the runner runs end to end against the fake LLM and a mocked banking API.
- [ ] **Front-ends on the real API** (owner: `frontend-dev`, `frontend/`): replace mocks with the real agent API behind a config switch; keep mocks for tests.
  Done when: `make test-frontend` passes; both apps build with the real API configured.

## Phase 3 — Integration (on the developer's machine, not in the cloud)

Needs Docker and Ollama, so it runs locally (or later in GitHub Actions).

- [ ] `make up` starts the whole stack; Langfuse setup done; models pulled.
- [ ] Simulate customer works end to end for each scenario type, visible in both apps and in Langfuse.
- [ ] First eval run on `ollama`; results recorded here.
- [ ] Later, with API credits: eval runs on `anthropic` and `mixed`, comparison table filled in `docs/architecture.md` section 10, CI eval gate enabled.

---

## Open questions

Add questions found during the build here, with the task they came from.

- **Re-investigate while awaiting review** (Agent API contract). Section 5 allows one active investigation per case, and section 9 lists investigation statuses `queued, running, awaiting_review, completed, failed`. What happens to an investigation paused at `interrupt()` when Re-investigate starts a new one is not specified. The contract adds status `superseded` for it and rejects Re-investigate (409) while one is `queued` or `running`. Confirm, then add `superseded` to section 9.
- **Extra investigation SSE events** (Agent API contract). Section 8 lists `step_started, classified, tool_call, evidence, token, diagnosis, awaiting_review`. The contract adds `escalated` (escalate has no diagnosis to show), `review_recorded` and `status_changed` (so the timeline shows the review and the end of a run). Confirm, then add them to section 8.
- **"Account closed by the originator"** (Banking API contract). The failed direct debit `needs_human` row in section 5 is ambiguous. The contract reads it as the originator's side closing the account the collection belongs to (root cause `account_closed_by_originator`, failure code `ACCOUNT_CLOSED_BY_ORIGINATOR`). Confirm the meaning, so banking-dev can write the rule.
- **👍 / 👎 versus "did this help?"** (Agent API contract). Section 4 has both a 👍 / 👎 per answer and the customer confirming an answer helped. The contract treats them as one signal: `up` resolves the conversation, `down` counts as "that didn't help" for the repeated-failure rule.
- **Customer session** (Agent API contract). "Log in as" is passed as an `X-Customer-Id` header on customer endpoints; the server injects it into customer-scoped tools. Dev-only, no real authentication.

## Progress notes

Add a short note per session: date, phase, what was done, what is left.
