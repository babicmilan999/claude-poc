# claude-poc — Payments Support AI

Two AI agents on one platform: an in-app customer assistant and an ops copilot that investigates payment issues. The design is in `docs/architecture.md`; follow it. The build is tracked in `docs/implementation-plan.md`.

## Layout

| Folder | What | Stack |
|---|---|---|
| `contracts/` | API specs and shared formats; the source of truth between apps | OpenAPI 3.1, YAML |
| `agent-service/` | FastAPI, LangGraph graphs, tools, workers, simulator, evals | Python 3.12, uv |
| `banking-service/` | Mock core banking: domain, rules, read-only API, simulation module | Java 21, Spring Boot 3, Maven wrapper |
| `frontend/` | Angular workspace: `customer-app` (:4300), `ops-console` (:4200) | Angular, npm |
| `knowledge-base/` | Markdown help articles for the assistant | Markdown |
| `infra/` | `docker-compose.yml`, LiteLLM profiles, Terraform (later) | Docker Compose |

## Commands

- `make test` runs every app's tests; `make test-agent`, `make test-banking`, `make test-frontend` run one.
- `make lint-contracts` validates the OpenAPI files.
- `make up` starts the full stack in Docker (not available in cloud sessions).

## Rules

- **Follow the design.** If something in `docs/architecture.md` or `contracts/` looks wrong, do not silently change it: note it under "Open questions" in `docs/implementation-plan.md`.
- **Contracts change only on purpose.** Apps code against `contracts/`. Changing a contract means updating the spec, every app that uses it, and noting it in the plan.
- **Tests must run without network services.** No Docker, Postgres, Ollama or API keys in unit tests. LLM calls use the fake LLM (`agent-service/tests/fakes.py`), the banking API is mocked from the contract, and Postgres-only tests are marked `@pytest.mark.integration` and skipped by default.
- **Tools are read-only.** Customer-scoped tools never take `customer_id` as a parameter; it is injected from the session.
- **Mask sensitive data** (card numbers, identifiers) before anything reaches an LLM or Langfuse.
- **Never expose simulation answers** (`expected_root_cause`, `expected_outcome`) to the agents, their tools or their prompts.
- **The code names only model aliases** (`triage`, `assistant`, `reasoning`, `simulator`, `judge`), never a provider or model.
- **No secrets in the repo.** Settings come from environment variables; document new ones in `.env.example`.
- Keep changes inside the folder your task owns.
