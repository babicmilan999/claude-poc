"""In-app customer assistant (architecture section 4).

- `graph`: the LangGraph assistant graph, one run per customer message
  (`thread_id = conversation_id`).
- `rules`: the handoff rules, as code.
- `store`: conversations, messages and handoff cases (Postgres, or in memory for tests).
- `service`: one conversation turn, ratings, and the event hub for SSE and watch mode.
- `api`: the conversation endpoints (`contracts/agent-api.yaml`, tags `customer-chat`
  and `shared-conversations`), picked up by `core.app`.
"""
