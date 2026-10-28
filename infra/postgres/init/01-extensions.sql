-- Runs once, when the app Postgres volume is first created.
-- pgvector for the knowledge base. Schemas (banking, agents, kb, langgraph)
-- are created by each app's own migrations, so they own their objects.
CREATE EXTENSION IF NOT EXISTS vector;
