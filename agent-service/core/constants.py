"""Constants shared across packages."""

# Size of the knowledge base embedding vectors (BAAI/bge-small-en-v1.5, the default
# EMBEDDING_MODEL). The kb_chunks.embedding column has this size; a model with another
# size needs a migration.
EMBEDDING_DIM = 384
