# Contracts

The source of truth between the apps. Apps code against these files; changing one means
updating the spec, every app that uses it, and noting it in `docs/implementation-plan.md`.

| File | What | Used by |
|---|---|---|
| `banking-api.yaml` | Read-only banking API behind the tools and the signal snapshot, plus `POST /simulation/incidents` | banking-service (implements), agent-service (calls) |
| `agent-api.yaml` | Conversations, cases, investigations, simulation, and the SSE event schemas | agent-service (implements), frontend (calls) |
| `scenario.schema.json` | Eval and simulation scenario format; also defines the shared enums `IssueType`, `RootCause`, `ExpectedOutcome` that both OpenAPI files `$ref` | agent-service (simulation, evals), banking-service (simulation) |
| `examples/scenarios/` | Example scenarios that must validate against the schema | |

`make lint-contracts` lints both OpenAPI files with Redocly (`redocly.yaml`) and validates
the examples with ajv (JSON Schema 2020-12).
