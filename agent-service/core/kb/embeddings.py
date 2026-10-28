"""Embedding models for the knowledge base.

`FastEmbedEmbedder` runs a small local model in-process (fastembed; downloaded on first
use, no API). `HashingEmbedder` is deterministic and needs nothing: unit tests use it.
"""

import asyncio
import hashlib
import math
import re
from typing import Any, Protocol

from core.constants import EMBEDDING_DIM

_TOKEN_RE = re.compile(r"[a-z0-9]+")


class Embedder(Protocol):
    dim: int

    async def embed_documents(self, texts: list[str]) -> list[list[float]]: ...

    async def embed_query(self, text: str) -> list[float]: ...


class FastEmbedEmbedder:
    def __init__(self, model_name: str, dim: int = EMBEDDING_DIM) -> None:
        self.model_name = model_name
        self.dim = dim
        self._model: Any = None

    def _load(self) -> Any:
        if self._model is None:
            from fastembed import TextEmbedding

            self._model = TextEmbedding(model_name=self.model_name)
        return self._model

    def _embed(self, texts: list[str], query: bool) -> list[list[float]]:
        model = self._load()
        vectors = model.query_embed(texts) if query else model.passage_embed(texts)
        out = [list(map(float, v)) for v in vectors]
        if out and len(out[0]) != self.dim:
            raise ValueError(f"embedding model {self.model_name} returns {len(out[0])} dimensions, expected {self.dim}")
        return out

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return await asyncio.to_thread(self._embed, texts, False)

    async def embed_query(self, text: str) -> list[float]:
        return (await asyncio.to_thread(self._embed, [text], True))[0]


class HashingEmbedder:
    """Bag-of-words hashed into a fixed-size, L2-normalised vector."""

    def __init__(self, dim: int = EMBEDDING_DIM) -> None:
        self.dim = dim

    def _vector(self, text: str) -> list[float]:
        vec = [0.0] * self.dim
        for token in _TOKEN_RE.findall(text.lower()):
            digest = hashlib.blake2b(token.encode(), digest_size=8).digest()
            index = int.from_bytes(digest[:4], "little") % self.dim
            sign = 1.0 if digest[4] % 2 == 0 else -1.0
            vec[index] += sign
        norm = math.sqrt(sum(v * v for v in vec)) or 1.0
        return [v / norm for v in vec]

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [self._vector(t) for t in texts]

    async def embed_query(self, text: str) -> list[float]:
        return self._vector(text)
