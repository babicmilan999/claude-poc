# Ops Copilot — Architecture

An AI copilot for operations/support agents. When a customer complaint arrives
("my card was declined", "my rent payment never went out", "my direct debit
failed"), the copilot automatically investigates the customer's banking data,
explains the root cause with evidence, and recommends the next action. By the
time an ops agent opens the case, the diagnosis is waiting for their review.

## Goals

- Diagnose the common issue types: **card declines**, **missed scheduled payments**, **failed direct debits**, with a fallback for anything else.
- **Investigate automatically** as soon as a case arrives, so the ops agent starts from a diagnosis instead of from scratch.
- Every diagnosis is grounded in tool evidence (no evidence → no conclusion).
- Measure diagnosis accuracy against a labelled dataset and track it over time.
- Human in the loop: the copilot proposes, the ops agent accepts, corrects or rejects.
- **Simulate complaints** on demand, so the system can be demoed and tested with realistic, fresh cases.
- **Local-first:** the whole system runs on a laptop with `docker compose up`. The only cost is LLM tokens, and switching to a local model makes it free.

## Tech stack

| Concern | Choice |
|---|---|
| Agent runtime | Python 3.12, FastAPI, LangGraph |
| LLM access | LiteLLM proxy → Anthropic API (Claude), or Ollama for a free local model |
| Observability & evals | Langfuse, self-hosted in Docker (traces, scores, datasets) |
| Banking domain service (mock core banking) | Java 21, Spring Boot 3 |
| Persistence | PostgreSQL (domain data, cases, investigations, job queue, LangGraph checkpoints) |
| Ops console | Angular |
| Runtime | Docker Compose (primary); AWS via Terraform as an optional later step |
| CI/CD | GitHub Actions (tests, offline eval gate, image build) |

### Cost

| Component | Cost |
|---|---|
| Everything in Docker Compose (services, Postgres, Langfuse, LiteLLM, Ollama) | Free |
| Anthropic API | Pay per token; a POC investigation is roughly cents |
| GitHub Actions | Free for public repos; monthly free minutes for private repos |
| AWS (optional, later) | Hourly charges for Fargate, RDS, load balancer, plus Bedrock tokens |

---

## 1. System context

```mermaid
flowchart LR
    ops(["Ops agent"])
    console["Ops Console<br/>Angular"]
    copilot["Copilot Service<br/>FastAPI + LangGraph"]
    banking["Banking Data Service<br/>Spring Boot, Java 21"]
    litellm["LLM Gateway<br/>LiteLLM"]
    anthropic[("Anthropic API<br/>Claude")]
    ollama[("Ollama<br/>local model, free")]
    langfuse["Langfuse<br/>self-hosted"]

    ops -->|"works the case queue,<br/>simulates complaints,<br/>reviews diagnoses"| console
    console -->|"REST + SSE stream"| copilot
    copilot -->|"read-only tool calls,<br/>simulate incident"| banking
    copilot -->|"OpenAI-compatible API"| litellm
    litellm -->|"default"| anthropic
    litellm -.->|"free option"| ollama
    copilot -.->|"traces, scores"| langfuse
    litellm -.->|"token usage, cost"| langfuse
```

## 2. Containers and data stores

```mermaid
flowchart TB
    subgraph client["Client"]
        console["Ops Console (Angular)"]
    end

    subgraph copilot_svc["Copilot Service (Python)"]
        api["FastAPI<br/>/cases, /cases/simulate,<br/>/investigations, /feedback"]
        workers["Investigation workers<br/>max N in parallel"]
        graph_rt["LangGraph runtime<br/>investigation graph"]
        tools["Tool layer<br/>typed clients for Banking API"]
        api -->|"enqueue"| jobs
        workers -->|"claim next job"| jobs
        workers --> graph_rt --> tools
    end

    subgraph banking_svc["Banking Data Service (Java 21 / Spring Boot)"]
        bapi["REST API<br/>customers, accounts, cards,<br/>authorisations, payments, direct debits"]
        sim["Simulation API<br/>creates a fresh incident<br/>(local profile only)"]
        rules["Decline-reason and<br/>payment-failure rules"]
        bapi --> rules
        sim --> rules
    end

    subgraph data["PostgreSQL"]
        bankdb[("banking schema<br/>seeded mock data")]
        copdb[("copilot schema<br/>cases, investigations,<br/>events, reviews")]
        jobs[("investigation queue<br/>rows with status = queued")]
        ckpt[("langgraph schema<br/>checkpoints")]
    end

    litellm["LiteLLM proxy<br/>routing, retries, fallbacks"]
    anthropic[("Anthropic API")]
    ollama[("Ollama")]
    langfuse["Langfuse"]

    console --> api
    api --> sim
    tools --> bapi
    bapi --> bankdb
    sim --> bankdb
    api --> copdb
    graph_rt --> ckpt
    graph_rt --> litellm
    litellm --> anthropic
    litellm -.-> ollama
    graph_rt -.-> langfuse
```

**Model routing via LiteLLM.** The copilot only ever asks for two model aliases; the LiteLLM config maps them to real models, so switching provider is a config change, not a code change.

| Alias | Used for | Default (Anthropic API) | Free profile (Ollama) |
|---|---|---|---|
| `copilot-fast` | triage, summaries | small Claude model (Haiku) | small local model, e.g. Llama 3.1 8B |
| `copilot-reasoning` | investigation loop, diagnosis | larger Claude model (Sonnet) | larger local model with tool calling, e.g. Qwen 2.5 14B |

The profile is chosen with an environment variable (for example `LLM_PROFILE=anthropic` or `LLM_PROFILE=ollama`). Expect lower diagnosis accuracy from small local models; they are good enough for building and testing the flow, and the eval loop (section 8) shows the difference in numbers.

## 3. Cases and automatic investigation

A **case** is a customer complaint waiting for an ops agent. An **investigation**
is one run of the copilot on a case. Every new case is investigated
automatically; the ops agent can re-run it (for example after rejecting a
diagnosis), which creates a new investigation on the same case.

```mermaid
flowchart LR
    sim_btn(["Simulate complaint<br/>button"]) --> sim_api["POST /cases/simulate"]
    sim_api -->|"pick scenario type"| incident["Banking service<br/>creates fresh incident data"]
    incident -->|"customer, complaint text,<br/>expected root cause"| create
    seed(["Seeded cases<br/>on first start"]) --> create
    form(["New case form<br/>(free-text complaint)"]) --> create

    create["Create case<br/>status: new"] --> enqueue["Enqueue investigation<br/>status: queued"]
    enqueue --> worker["Worker claims job<br/>(max N in parallel)"]
    worker --> run_graph["Run investigation graph"]
    run_graph -->|"issue type enabled"| ready["Diagnosis ready<br/>case: awaiting_review"]
    run_graph -->|"issue type not enabled<br/>for auto"| manual["Case: needs_manual<br/>agent clicks Investigate"]
    manual --> enqueue
```

### Case lifecycle

```mermaid
stateDiagram-v2
    [*] --> new: case created
    new --> investigating: worker picks up the job
    investigating --> awaiting_review: diagnosis ready
    investigating --> needs_manual: issue type not enabled for auto
    investigating --> escalated: no conclusion within budget
    needs_manual --> investigating: agent clicks Investigate
    awaiting_review --> resolved: accepted or corrected
    awaiting_review --> investigating: rejected with hint
    escalated --> resolved: handled manually
    resolved --> [*]
```

### How the automatic run works

- **Trigger.** Creating a case (from any source) inserts the case and a `queued` investigation in **one transaction**, then sends a Postgres `NOTIFY` to wake the workers. No separate message broker is needed for the POC.
- **Workers.** The copilot service runs a small pool of background workers (`MAX_PARALLEL_INVESTIGATIONS`, default 3; use 1 with Ollama). Each worker claims the oldest queued job with `SELECT … FOR UPDATE SKIP LOCKED`, so two workers never take the same job, and jobs survive a restart. If a worker dies mid-run, the job is retried and LangGraph resumes from the last checkpoint.
- **One active investigation per case.** Clicking Investigate while one is queued or running just opens the existing one.
- **Which issue types run automatically.** `AUTO_INVESTIGATE_TYPES=card_decline,missed_payment,failed_direct_debit`. The graph always runs the cheap triage step first; if the issue type is not in the list, it stops there and marks the case `needs_manual`, so tokens are not spent on cases the copilot is not built for.
- **Streaming still works.** Workers save every event (step, tool call, evidence, tokens of the final explanation) to the `investigation_event` table. When the ops agent opens a case, the console replays the saved events over SSE and then continues live if the run is still going.
- **Later.** If volume grows, the Postgres queue can be swapped for RabbitMQ or SQS without changing the graph.

### Simulate complaint

The **Simulate complaint** button in the case queue creates a realistic new case end to end:

1. The console calls `POST /cases/simulate` (optionally with a scenario type; random by default).
2. The copilot service calls the banking service's simulation API, for example `POST /simulation/incidents {"type": "card_decline"}`.
3. The banking service picks a customer and inserts **fresh data that causes the problem**, timestamped now: for example a frozen card plus a declined authorisation at a petrol station, or a direct debit collection that failed because the mandate was cancelled.
4. It returns the customer, a complaint written the way a customer would say it (several phrasings per scenario), and the **expected root cause**.
5. The copilot service creates the case with channel `simulated` and stores the expected root cause on it, **hidden from the copilot and the ops agent**.
6. The automatic investigation starts; the case appears in the queue and fills in live.

Because simulated cases carry the expected root cause, every one of them is scored automatically in Langfuse (`diagnosis_correct`), on top of the ops agent's review. The simulation API is only enabled in the local profile (`SIMULATION_ENABLED=true`) and is never part of the tools the copilot can call.

| Scenario type | Example root causes the simulator can create |
|---|---|
| Card decline | card frozen, insufficient funds, merchant category blocked, card expired, contactless limit, fraud rule |
| Missed payment | insufficient funds on the run date, payee account closed, daily payment limit exceeded |
| Failed direct debit | mandate cancelled, insufficient funds, account closed by originator |

### Console screens

1. **Case queue:** open cases with customer, complaint snippet, age, issue type and status (`investigating`, `awaiting_review`, `needs_manual`, …). Buttons: **Simulate complaint** (with an optional scenario-type picker) and **New case**.
2. **Case detail:** complaint and customer summary; the investigation timeline (live or replayed); the diagnosis with linked evidence; the review panel (accept, correct, reject with hint). Earlier investigations of the same case are listed below.
3. **New case form:** pick a customer, write any complaint, choose a channel. For trying edge cases by hand.

## 4. Investigation graph (LangGraph)

```mermaid
flowchart TD
    start([START]) --> intake["intake<br/>load case + customer profile,<br/>normalise complaint"]
    intake --> triage{"triage<br/>classify issue type"}

    triage -->|"auto run, type not enabled"| park["park<br/>case: needs_manual"]
    park --> parked([END])

    triage -->|card_decline| card["card decline<br/>investigator"]
    triage -->|missed_payment| pay["missed payment<br/>investigator"]
    triage -->|failed_direct_debit| dd["direct debit<br/>investigator"]
    triage -->|other / unclear| generic["generic<br/>investigator"]

    card --> evidence
    pay --> evidence
    dd --> evidence
    generic --> evidence

    evidence{"evidence check<br/>enough to conclude?"}
    evidence -->|"no, budget left"| triage
    evidence -->|yes| diagnose["diagnose<br/>structured output:<br/>root cause, evidence, confidence,<br/>recommended action"]
    evidence -->|"no, budget spent"| escalate["escalate<br/>summarise findings,<br/>flag for human"]

    diagnose --> review[/"human review<br/>interrupt()"/]
    escalate --> review
    review -->|accepted / corrected| finalize["finalize<br/>persist outcome,<br/>score in Langfuse"]
    review -->|"rejected with hint"| triage
    finalize --> done([END])
```

The `park` route only applies to automatic runs. When the ops agent clicks
Investigate on a `needs_manual` case, the run is marked as manual and triage
continues to the matching investigator (usually `generic`).

Each investigator is a ReAct-style subgraph with a **playbook prompt** and a
**restricted tool set** for its issue type:

```mermaid
flowchart LR
    subgraph investigator["Investigator subgraph (e.g. card decline)"]
        direction LR
        plan["agent<br/>LLM picks next tool"] -->|tool call| exec["tool node<br/>execute + record evidence"]
        exec --> plan
        plan -->|"no more calls"| out([return findings])
    end
```

| Issue type | Tools available |
|---|---|
| Card decline | `get_cards`, `get_card_authorisations`, `get_account_balance`, `get_card_controls`, `get_fraud_flags` |
| Missed payment | `get_scheduled_payments`, `get_payment_attempts`, `get_account_balance`, `get_transactions`, `get_payee` |
| Failed direct debit | `get_mandates`, `get_direct_debit_collections`, `get_account_balance`, `get_transactions` |
| Generic | read-only union of the above, smaller step budget |

**Graph state** (sketch):

```python
class InvestigationState(TypedDict):
    investigation_id: str
    case_id: str
    customer_id: str
    complaint: str
    trigger: Literal["auto", "manual"]
    issue_type: Literal["card_decline", "missed_payment", "failed_direct_debit", "other"]
    evidence: Annotated[list[Evidence], operator.add]   # every tool result cited
    steps_used: int
    diagnosis: Diagnosis | None                         # root_cause, confidence, evidence_ids, action
    reviewer_feedback: str | None
```

## 5. Investigation sequence (simulated complaint)

```mermaid
sequenceDiagram
    autonumber
    actor Ops as Ops agent
    participant UI as Ops Console
    participant API as Copilot API
    participant B as Banking Service
    participant W as Worker + LangGraph
    participant LLM as LiteLLM → Claude / Ollama
    participant LF as Langfuse

    Ops->>UI: Click "Simulate complaint"
    UI->>API: POST /cases/simulate
    API->>B: POST /simulation/incidents
    B->>B: insert frozen card + declined authorisation
    B-->>API: customer, complaint, expected root cause
    API->>API: create case + queued investigation (one transaction), NOTIFY
    API-->>UI: 201 case (status: new)
    W->>API: claim next queued job (SKIP LOCKED)
    W->>LLM: triage complaint
    LLM-->>W: issue_type = card_decline (enabled for auto)
    loop until evidence is sufficient or budget spent
        W->>LLM: next step? (playbook + evidence so far)
        LLM-->>W: tool call get_card_authorisations
        W->>B: GET /customers/{id}/cards/{cardId}/authorisations
        B-->>W: declined auth, reason = CARD_FROZEN
        W->>W: save event (step, evidence)
    end
    W->>LLM: diagnose (structured output) + explanation
    LLM-->>W: root cause, confidence 0.94, action
    W->>W: case → awaiting_review, interrupt()
    W-)LF: trace + score vs expected root cause
    Ops->>UI: Open case from queue
    UI->>API: GET /investigations/{id}/events (SSE)
    API-->>UI: replay saved events, then live
    Ops->>UI: Accept / correct root cause
    UI->>API: POST /investigations/{id}/feedback
    API->>W: resume thread with feedback
    W->>W: finalize, case → resolved
    W-)LF: score diagnosis_accepted = 1
```

## 6. Streaming to the console

- The console opens `GET /investigations/{id}/events` as Server-Sent Events.
- Events are our own small set of types (`step_started`, `classified`, `tool_call`, `evidence`, `token`, `diagnosis`, `awaiting_review`, `status_changed`), mapped from LangGraph's `updates`, `messages` and `custom` stream modes.
- Only the final plain-language explanation is streamed token by token; triage, tool calls and the structured diagnosis are sent as complete events.
- Every event is stored with an increasing id, so a refresh or reconnect (`Last-Event-ID`) replays what was missed and then continues live.
- The case queue receives `status_changed` events over a second SSE stream (`GET /cases/events`), so new simulated cases and status changes appear without refreshing.

## 7. Data model

```mermaid
erDiagram
    CUSTOMER ||--o{ ACCOUNT : owns
    ACCOUNT ||--o{ CARD : has
    CARD ||--o{ CARD_AUTHORISATION : "receives"
    ACCOUNT ||--o{ TRANSACTION : records
    ACCOUNT ||--o{ SCHEDULED_PAYMENT : has
    SCHEDULED_PAYMENT ||--o{ PAYMENT_ATTEMPT : "executes as"
    ACCOUNT ||--o{ DD_MANDATE : authorises
    DD_MANDATE ||--o{ DD_COLLECTION : "collected via"

    CUSTOMER ||--o{ CASE : "complains in"
    CASE ||--o{ INVESTIGATION : "investigated by"
    INVESTIGATION ||--o{ INVESTIGATION_EVENT : emits
    INVESTIGATION ||--o| DIAGNOSIS : produces
    DIAGNOSIS ||--o| REVIEW : "reviewed by"

    CUSTOMER {
        uuid id PK
        string name
        string status
    }
    ACCOUNT {
        uuid id PK
        uuid customer_id FK
        decimal balance
        decimal overdraft_limit
        string status
    }
    CARD {
        uuid id PK
        uuid account_id FK
        string status
        date expiry
        jsonb controls
    }
    CARD_AUTHORISATION {
        uuid id PK
        uuid card_id FK
        decimal amount
        string merchant_mcc
        string result
        string decline_reason
        timestamp created_at
    }
    SCHEDULED_PAYMENT {
        uuid id PK
        uuid account_id FK
        string payee_ref
        decimal amount
        date next_run
    }
    PAYMENT_ATTEMPT {
        uuid id PK
        uuid scheduled_payment_id FK
        string result
        string failure_reason
        timestamp attempted_at
    }
    DD_MANDATE {
        uuid id PK
        uuid account_id FK
        string originator
        string status
    }
    DD_COLLECTION {
        uuid id PK
        uuid mandate_id FK
        decimal amount
        string result
        string failure_code
    }
    TRANSACTION {
        uuid id PK
        uuid account_id FK
        decimal amount
        string type
        timestamp booked_at
    }
    CASE {
        uuid id PK
        uuid customer_id
        string channel
        string complaint
        string status
        string expected_root_cause
        timestamp created_at
    }
    INVESTIGATION {
        uuid id PK
        uuid case_id FK
        string trigger
        string status
        string issue_type
        int attempts
        string langfuse_trace_id
        timestamp queued_at
        timestamp started_at
        timestamp finished_at
    }
    INVESTIGATION_EVENT {
        bigint id PK
        uuid investigation_id FK
        string type
        jsonb payload
        timestamp created_at
    }
    DIAGNOSIS {
        uuid id PK
        uuid investigation_id FK
        string root_cause
        float confidence
        string recommended_action
    }
    REVIEW {
        uuid id PK
        uuid diagnosis_id FK
        string verdict
        string corrected_root_cause
    }
```

- `CASE.channel` is `chat`, `email`, `phone` or `simulated`. `expected_root_cause` is only set for simulated and seeded cases and is never shown to the copilot or the ops agent.
- `INVESTIGATION.status` is `queued`, `running`, `awaiting_review`, `completed` or `failed`; the `queued` rows are the job queue the workers claim from.
- `INVESTIGATION_EVENT` holds everything streamed to the console, so a case can be replayed after the fact.

The banking tables live in the Spring Boot service's schema and are seeded with
scenarios that each have one known root cause. The simulation API uses the same
scenario definitions to create fresh incidents, and the same scenarios make up
the evaluation dataset.

## 8. Evaluation loop

```mermaid
flowchart LR
    seed["Seeded scenarios<br/>(customer + complaint +<br/>expected root cause)"] --> ds[("Langfuse dataset")]
    ds --> run["Eval run<br/>local: make eval<br/>CI: GitHub Actions"]
    run --> inv_graph["Investigation graph"]
    inv_graph --> judge["Scorers<br/>exact root-cause match,<br/>LLM-as-judge on explanation,<br/>steps + cost"]
    judge --> scores[("Langfuse scores")]
    scores --> gate{"accuracy ≥ baseline?"}
    gate -->|yes| merge["allow merge"]
    gate -->|no| fail["fail the PR check"]

    simulated["Simulated cases<br/>(scored automatically)"] -.-> scores
    reviews["Ops reviews<br/>(accept / correct)"] -.->|"corrected cases become<br/>new dataset items"| ds
```

**Headline metrics:**
- **Diagnosis accuracy** = diagnoses whose root cause matches the expected one (simulated and eval cases) or was accepted unchanged by the ops agent ÷ all scored diagnoses.
- **Time to diagnosis** = case created → diagnosis ready (automatic run), and case created → review (end to end).

The same dataset can be run against each LiteLLM profile, so Claude and the
local Ollama models can be compared side by side in Langfuse.

In CI, the eval job starts the Compose stack and uses the Anthropic API (the
key is a GitHub Actions secret). Ollama models are usually too slow and large for
free CI runners, so the Ollama profile is evaluated locally.

## 9. Local runtime (Docker Compose)

```mermaid
flowchart TB
    browser(["Browser<br/>localhost:4200"])

    subgraph compose["docker compose"]
        subgraph app["Application"]
            console["ops-console<br/>Angular dev server :4200"]
            copilot["copilot-service<br/>FastAPI + workers :8000"]
            banking["banking-service<br/>Spring Boot :8080"]
            pg[("postgres<br/>banking, copilot,<br/>langgraph schemas")]
        end

        subgraph llm["LLM gateway"]
            litellm["litellm-proxy :4000"]
            ollama["ollama :11434<br/>profile: ollama"]
        end

        subgraph obs["Langfuse (self-hosted)"]
            lf_web["langfuse-web :3000"]
            lf_worker["langfuse-worker"]
            lf_pg[("langfuse-postgres")]
            lf_ch[("clickhouse")]
            lf_redis[("redis")]
            lf_minio[("minio<br/>S3-compatible storage")]
        end
    end

    anthropic[("Anthropic API")]

    browser --> console
    console --> copilot
    copilot --> banking
    copilot --> pg
    banking --> pg
    copilot --> litellm
    litellm --> anthropic
    litellm -.-> ollama
    copilot -.-> lf_web
    lf_web --> lf_pg
    lf_web --> lf_ch
    lf_worker --> lf_ch
    lf_worker --> lf_redis
    lf_worker --> lf_minio
```

- `docker compose up` starts everything except Ollama. `docker compose --profile ollama up` adds Ollama, and `LLM_PROFILE=ollama` points LiteLLM at it.
- The only secret is `ANTHROPIC_API_KEY`, read from a git-ignored `.env` file. Not needed with the Ollama profile.
- Self-hosted Langfuse brings its own Postgres, ClickHouse, Redis and MinIO; keep them separate from the app's Postgres so either can be reset independently.
- On a Mac, Ollama runs much faster natively (using the GPU) than inside Docker. In that case skip the `ollama` container and point LiteLLM at `host.docker.internal:11434`.
- The workers run inside the `copilot-service` container for the POC. They can later move to their own container (same image, different start command) to scale separately from the API.

## 10. Optional later: AWS deployment

Not needed for the POC. If the app is later hosted on AWS, the same container
images move to ECS, and only the LiteLLM config changes (Anthropic API → Bedrock,
or keep the Anthropic API).

```mermaid
flowchart TB
    subgraph gh["GitHub"]
        repo["claude-poc repo"] --> actions["GitHub Actions<br/>test → eval → build → deploy"]
    end

    subgraph aws["AWS (Terraform)"]
        ecr[("ECR")]
        alb["Application Load Balancer"]
        subgraph ecs["ECS Fargate cluster"]
            s_copilot["copilot-service"]
            s_banking["banking-service"]
            s_litellm["litellm-proxy"]
        end
        rds[("RDS PostgreSQL")]
        s3[("S3<br/>Ops Console static site")]
        secrets["Secrets Manager"]
        bedrock[("Bedrock<br/>or Anthropic API")]
    end

    langfuse["Langfuse<br/>(cloud or self-hosted)"]

    actions -->|push images| ecr
    actions -->|"terraform apply / ECS deploy"| ecs
    ecr --> ecs
    alb --> s_copilot
    s_copilot --> s_banking
    s_copilot --> s_litellm --> bedrock
    s_copilot --> rds
    s_banking --> rds
    s_copilot -.-> langfuse
    secrets -.-> ecs
```

## Proposed repo layout

```
claude-poc/
├── docs/architecture.md
├── copilot-service/      # Python, FastAPI, LangGraph, workers
├── banking-service/      # Java 21, Spring Boot (incl. simulation API)
├── ops-console/          # Angular
├── infra/
│   ├── docker-compose.yml
│   ├── litellm/
│   │   ├── anthropic.yaml   # default profile
│   │   └── ollama.yaml      # free local profile
│   └── terraform/           # optional, later
├── .env.example          # ANTHROPIC_API_KEY, LLM_PROFILE, AUTO_INVESTIGATE_TYPES, Langfuse keys
└── .github/workflows/
```
