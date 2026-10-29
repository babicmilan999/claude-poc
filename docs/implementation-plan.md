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

- [x] **Repo skeleton** (owner: orchestrator): folders per the repo layout in `docs/architecture.md`, `.env.example`, root `Makefile` with `test`, `test-agent`, `test-banking`, `test-frontend`, `lint-contracts`, `up`, `infra`, `models` (targets for apps that do not exist yet print "skipped").
  Done when: `make test` and `make lint-contracts` run without error.
- [x] **Banking API contract** (owner: orchestrator): `contracts/banking-api.yaml`, read-only endpoints behind every ops tool in section 6 and customer tool in section 3 (cards, authorisations, balance, card controls, fraud flags, scheduled payments, payment attempts, transactions, payees, mandates, direct debit collections, customers), plus `/simulation/incidents`. Reason codes as enums.
  Done when: lints clean; every tool in the architecture doc maps to an endpoint.
- [x] **Agent API contract** (owner: orchestrator): `contracts/agent-api.yaml`: conversations and messages (SSE), cases, investigations, events (SSE with `Last-Event-ID`), feedback, simulation, plus a schema per SSE event type from section 8.
  Done when: lints clean; every ops console and customer app screen in the doc maps to endpoints.
- [x] **Scenario format** (owner: orchestrator): `contracts/scenario.schema.json` for eval and simulation scenarios (incident type, complaint, persona hints, expected issue type, root cause, outcome), with two examples.
  Done when: the examples validate against the schema.
- [x] **CI runs** (owner: orchestrator): `.github/workflows/ci.yml` calls the make targets.
  Done when: CI is green on the Phase 0 PR.
  Note: green on the Phase 0 PR and on `main` after the merge.

## Phase 1 — Components against the contracts (parallel)

- [x] **Banking service** (owner: `banking-dev`, `banking-service/`): Spring Boot app with the domain (customers, accounts, cards, authorisations, scheduled payments, payees, mandates, direct debit collections, fraud flags) and the rules that produce declines and failures with the contract's reason codes; seed data for about 10 customers; read-only API per `contracts/banking-api.yaml`; simulation module behind `@Profile("simulation")` covering every scenario row in section 5; Flyway migrations in schema `banking`.
  Done when: `make test-banking` passes, including a test per simulation scenario and a test that responses match the contract.
  Note: 13 seeded customers (one section 5 problem each, built through the rules); 12 scenario tests; contract test over every GET endpoint. Not yet run against a real Postgres.
- [x] **Agent core** (owner: `agent-dev`, `agent-service/`, excluding `assistant/`, `investigation/`, `simulation/`, `evals/`): uv project, FastAPI app skeleton, settings, banking client from the contract, ops-scoped and customer-scoped tools, signal snapshot, triage (structured output), guardrails (input guard, output guard, masking), LLM client using aliases via LiteLLM, fake LLM in `tests/fakes.py`, knowledge base ingestion and hybrid search, DB models and migrations for the agent tables in section 9, Postgres job queue (`SKIP LOCKED`, `NOTIFY`), Langfuse tracing setup with masking.
  Done when: `make test-agent` passes with the banking API mocked from the contract; customer tools have a test proving `customer_id` cannot be supplied by the model; masking has tests.
  Note: Phase 2 packages plug in as `assistant/api.py`, `investigation/api.py`, `simulation/api.py` (router + optional lifespan). Postgres tests run with `TEST_DATABASE_URL` and `-m integration`. The fastembed model download is untested in the cloud sandbox.
- [x] **Front-ends** (owner: `frontend-dev`, `frontend/`): Angular workspace with `customer-app` (log-in-as picker, account view, chat with streamed answers, sources, 👍/👎, watch mode) and `ops-console` (case queue with Simulate buttons, case detail with timeline, diagnosis and review panel, conversations list, new case form), against a mock of `contracts/agent-api.yaml` including SSE.
  Done when: `make test-frontend` passes and both apps build.
  Note: Angular 21 (22 needs Node ≥ 22.22.3). Mock is the default; switch to the real API in each app's `environment.ts`. CI now also runs `npm run build`.
- [x] **Infra and knowledge base** (owner: `infra-dev`, `infra/`, `knowledge-base/`): Compose stack per section 11 (Postgres with pgvector, LiteLLM, self-hosted Langfuse, optional `ollama` profile), LiteLLM profiles `ollama`, `anthropic`, `mixed` with the five aliases, a Langfuse first-run setup script, `make models`; 10–15 knowledge base articles covering the topics in section 4.
  Done when: `docker compose config` validates every profile combination; every LiteLLM profile defines all five aliases; articles exist for every topic.
  Note: `infra/scripts/validate.py` runs `docker compose config` for all 6 combinations and checks aliases, pinned images and article coverage of every reason code. `make up` itself is Phase 3.

## Phase 2 — Agents (parallel, separate subfolders)

- [x] **Assistant graph** (owner: `agent-dev`, `agent-service/assistant/`): graph per section 4, handoff rules as code, case creation on handoff, conversation endpoints with SSE streaming.
  Done when: tests with the fake LLM cover each handoff rule, the output guard retry then handoff, and a resolved conversation.
  Note: handoff rules in `assistant/rules.py`; the answer streams only after the output guard passes; without a database the handoff goes to the investigation's in-memory store. The quiet-resolution and abandon sweep is not scheduled yet.
- [x] **Investigation graph** (owner: `agent-dev`, `agent-service/investigation/`): graph per section 6 (verify, triage, route, park, investigators with restricted tools and step budgets, evidence check, diagnose, escalate, `interrupt()` review, finalize), Postgres checkpointer, workers, investigation endpoints with event storage and SSE replay, reject-with-hint resume and Re-investigate.
  Done when: tests with the fake LLM and in-memory checkpointer cover each route, park, escalate, review accept, correct and reject-with-hint resume.
  Note: both graphs share one checkpointer (schema `langgraph`); rejects reset the retry budget; streamed tokens are masked across chunk boundaries; worker writes check job ownership.
- [x] **Simulation and evals** (owner: `agent-dev`, `agent-service/simulation/` and `agent-service/evals/`): customer simulator persona, Simulate customer and Simulate email endpoints, scenario YAML files for every row in section 5, scorers, Langfuse experiment runner, pytest gate with thresholds.
  Done when: scorers have unit tests; the runner runs end to end against the fake LLM and a mocked banking API.
  Note: 12 scenario files (one per section 5 row); `make eval` runs the gate (`pytest -m eval`, needs the stack); the gate fails on missing scores; dataset items carry no answer-revealing input. Thresholds are guesses until the first measured run (Phase 3).
- [x] **Front-ends on the real API** (owner: `frontend-dev`, `frontend/`): replace mocks with the real agent API behind a config switch; keep mocks for tests.
  Done when: `make test-frontend` passes; both apps build with the real API configured.
  Note: `npm run build:real` / `--configuration real`; Docker images default to the real API, base URL set at run time via `config.json`; CORS, no dev proxy; `/help/:slug` is a placeholder page.

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
- **Lifecycle details not in sections 5 and 9** (Agent API contract). The contract decides: a parked run ends `completed` with no diagnosis; a run still failing after its retries ends `failed` and the case goes to `needs_manual`; rejecting an escalation moves the case `escalated → investigating`; accepting an escalation means "handled manually" and resolves the case; Re-investigate on a `resolved` case is rejected. Confirm, then add the transitions to the case lifecycle diagram.
- **Field differences from section 9** (Agent API contract). `CONVERSATION.status` is `open`/`closed` (outcome separate); `rating` is `up`/`down`, not an int; `CASE` gains `summary` (handoff summary) and `simulated`. One diagnosis and one review per investigation are returned; earlier ones after a reject survive only in the event log.
- **Root-cause codes beyond section 5** (scenario schema). `RootCause` also has codes for banking reasons no scenario uses yet (`online_payments_disabled`, `card_lost_or_stolen`, `account_not_active`, `invalid_payee_details`, `no_mandate`, `payer_account_closed`), so a diagnosis can name them instead of `unknown`.
- **What the stream replays** (Agent API contract). Only investigation events are stored (section 9). Watch mode replays stored messages, then goes live (tokens are not stored, no `Last-Event-ID`); the case queue stream is live only and the console reloads `GET /cases` after a reconnect.
- **Customer session** (Agent API contract). "Log in as" is passed as an `X-Customer-Id` header on customer endpoints; the server injects it into customer-scoped tools. Dev-only, no real authentication. Conversation reads (`GET /conversations/{id}`, `/events`) are not customer-scoped, because watch mode needs them.
- **Code generation** (contracts). Both specs `$ref` the shared enums in `scenario.schema.json`. Generators with weak OpenAPI 3.1 support should use `make bundle-contracts` (single-file specs in `contracts/dist/`, git-ignored). Nullable enums are written as `oneOf: [$ref, null]`, which some generators wrap in an extra class.
- **Langfuse arrow from LiteLLM** (Infra). The section 1 diagram has LiteLLM → Langfuse ("token usage, cost"), but the rules keep LiteLLM's own Langfuse logging off; the agent service sends all traces. Remove the arrow?
- **Gateway service name** (Infra). Section 11 calls it `litellm-proxy`; Compose and the Makefile use `litellm`.
- **Langfuse v3 vs v4** (Infra). The design says v3; upstream is on v4. Compose pins v3 (3.225.15). MinIO is `cgr.dev/chainguard/minio:latest` (no versioned free image), as in Langfuse's own Compose file.
- **Knowledge base claims the banking rules do not define** (Infra). Article menu paths (Cards > Card controls, Payments > Scheduled, Payments > Direct Debits) assume screens the customer app does not have; customers changing their contactless limit; "skipped" payments; replacement cards arriving before expiry; fees as their own transaction.
- **Undeclared 400s** (Banking, Agent API contracts). Banking read endpoints return 400 problem+json for bad `limit`/`since`/`result`/`status`; the contract declares only 404. `sendMessage` and `rateMessage` declare no 400/422 (FastAPI returns 422 by default); `rateMessage` has no 401 although it needs `X-Customer-Id`. `/me/overview` returns 502 when the banking service fails; the contract declares only 200 and 401.
- **Direct Debit codes** (Banking API contract). There is no collection failure code for a *frozen* payer account; banking uses `PAYER_ACCOUNT_CLOSED`, and the article covers "closed or frozen". `NO_MANDATE` cannot happen as written (every collection has a `mandate_id`); banking uses a dormant mandate for it. Add `PAYER_ACCOUNT_FROZEN`?
- **Other banking interpretations** (Banking API contract). Payee account status `unknown` triggers `INVALID_PAYEE_DETAILS`; `since` on collections compares `due_date` (no creation time); approved card payments book at once, so "minus pending authorisations" in `available` is always zero; the simulation returns 400 for an unknown `customer_id` (404 means the profile is off) and for root causes outside section 5.
- **Daily payment limit not visible** (Banking API contract). Investigators see `DAILY_PAYMENT_LIMIT_EXCEEDED` but not the limit. Add `daily_payment_limit` to `Account`?
- **`CustomerSummary.simulated`** (Agent API vs Banking API). The banking `Customer` has no simulated flag, so `/customers` always returns `false` and simulated customers fill the log-in picker unmarked. Add `simulated` to the banking `Customer`?
- **Knowledge base article links** (Agent API contract). `Source.url` has no defined form and no endpoint serves articles. The agent service uses `KB_ARTICLE_BASE_URL` + slug (`/help/<slug>`), which neither app serves yet.
- **Unspecified stream behaviour** (Agent API contract). Whether a handoff also sends `message_created`; whether the server closes the watch stream when a conversation closes, or ever closes `/cases/events`; the `id` format of replayed watch events; whether the feedback 202 body already shows the new status (the console assumes it does).
- **Ops tools bound to the case's customer** (Agent core). The design says this only for customer tools; the agent core binds ops tools too (safer, easier for small models).
- **Agent tables beyond section 9** (Agent core). Taken from the contract: CASE gains `summary`, `handoff_evidence`, `simulated`, `customer_name`, `expected_outcome`; INVESTIGATION gains `steps_used`, `category_changed`, `evidence`, `escalation`, `error` and queue columns; MESSAGE gains `rating`; REVIEW links to the investigation (diagnosis optional, for escalations) with `corrected_issue_type`, `hint`, `notes`; evidence is JSON on rows, not a table.
- **Guard failure modes** (Agent core). If the guard model fails, the input guard lets the message through and the output guard blocks the answer. Confirm.
- **`GET /me/overview`, `/customers`, `/health`** (Agent core). They had no owner in the plan; the agent core implements them.
- **Assistant decisions to confirm** (Assistant graph). A handoff always queues an investigation (section 5 and the contract; types outside `AUTO_INVESTIGATE_TYPES` stop as `needs_manual`). The answer is streamed only after the output guard passes, word by word, not as model tokens. A bare greeting or "thanks" gets a canned reply without an LLM call. Watch mode only sees turns run in the same process (in-process hub).
- **Stream details decided in code** (Agent API contract). A handoff sends `message_created` (the notice), then `handoff`, then `conversation_status`; the watch stream closes when the conversation closes; replayed watch events have ids `replay-msg-{id}` / `replay-status`; chat events have no `id:`; the timeline closes at `awaiting_review`, `completed`, `failed` and `superseded`. Add these to the contract?
- **More undeclared statuses** (Agent API contract). A malformed conversation id gives 404; a blank message 400; `rateMessage` 401 without a known customer; `getInvestigationEvents` 400 for a bad `Last-Event-ID` or `after_id`; `GET /cases` 400 for an unknown `status`; Simulate 503 when a package is not running.
- **Raw customer text shown to ops** (Assistant graph). Messages and the case complaint are stored and returned unmasked to the ops console; only LLM and Langfuse paths are masked, which meets CLAUDE.md. Mask them for ops too?
- **Investigation lifecycle details** (Investigation graph). A rejected run's 202 shows `queued` (the contract says `running`) until a worker claims it. Accept and correct are stored at once and the paused thread is finalized in the API process, so Langfuse scores are lost if that process dies. A category change mid-run goes out as `status_changed` with the status unchanged. Park applies only on the first pass of an automatic run. An unusable diagnosis escalates (an extra diagnose → escalate edge). Verify is deterministic code.
- **Scoring against the answer key** (Investigation graph). Section 7 has the worker score the diagnosis against the expected root cause, which would mean the worker reading the answer key. Finalize scores only the review outcome; the simulation package and the evals score the diagnosis instead.
- **Handoff case API** (Investigation graph, Simulation). `NewCase` / `create_case` have no fields for category, summary, evidence, conversation id, `simulated` or the answer key, so the assistant and the simulator write handoff and simulated cases through `add_case` (no database) or straight into the `cases` table. Add `InvestigationService.create_handoff_case(...)`?
- **Connection pool** (Investigation graph). Both graphs now use the assistant's checkpointer pool (5 connections); with many workers it may need to be configurable. Old investigation checkpoints in schema `public` are left behind.
- **Eval datasets** (Simulation and evals). One Langfuse dataset with the mode per item, not separate assistant and copilot datasets. Datasets created before the review fix still hold items whose input names the root cause: archive them or use a new `EVAL_DATASET`. The experiment trace stores each item's expected output (Langfuse design); the eval mask redacts it in the evaluator's input.
- **Simulation lifecycle** (Simulation and evals). A satisfied simulated customer closes the conversation as `closed`/`resolved`, which the real app never does (👍 keeps it open). The simulator calls the assistant service directly, not `POST /conversations/{id}/messages`, so the API's checks are skipped. The simulation endpoints are always mounted and answer 404 problem+json when `SIMULATION_ENABLED` is off. Unknown incident `params` (e.g. `merchant_mcc` in the contract example) are ignored by the banking service.

## Progress notes

Add a short note per session: date, phase, what was done, what is left.

- **2026-10-28, Phase 0.** Done: repo skeleton (folders, root `Makefile`, `.env.example`), `contracts/banking-api.yaml`, `contracts/agent-api.yaml`, `contracts/scenario.schema.json` with two examples; `make test` (all skipped) and `make lint-contracts` pass. The reviewer found nothing blocking; the medium findings are fixed in the contracts or recorded above. Left: push the branch and open the Phase 0 PR (the session got GitHub 403), then confirm CI is green.
- **2026-10-28, Phase 1.** Done: banking service, agent core, front-ends, infra and knowledge base, each in its own PR (`claude/phase-1-banking`, `claude/phase-1-agent-core`, `claude/phase-1-frontend`, `claude/trusting-galileo-i21y2r`), plan updates in `claude/phase-1-plan` (merge it last). `make test`, `make lint-contracts` and `infra/scripts/validate.py` pass. Reviewer findings fixed: input guard false positives, `/me/overview` 401s, migrations at startup, per-alias temperature, over-masking, job ownership on retry, KB wording (pending payments, frozen accounts), mock article slugs, Dockerfiles, banking healthcheck, new settings in `.env.example`. Left: the open questions above; Phase 2.
- **2026-10-29, Phase 2.** Done: assistant graph, investigation graph, simulation and evals, front-ends on the real API, each in its own PR (`claude/phase-2-assistant`, `claude/phase-2-investigation`, `claude/phase-2-simulation` stacked on those two, `claude/phase-2-frontend`); plan, settings in `.env.example` and the front-ends' Compose settings in `claude/phase-2-plan` (merge it last). `make test` and `make lint-contracts` pass; each branch passes on its own. Reviewer findings fixed: rejects using up retries, masking across streamed chunks, worker ownership of events, one checkpointer, no-DB handoff reaching the investigation, early "didn't help", problem+json 404s, timeline stopping after a reject, `API_MODE` values, eval gate passing with no scores, root cause in Langfuse inputs. Left: the open questions above; Phase 3 on the developer's machine.
