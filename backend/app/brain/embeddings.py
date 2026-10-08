"""Local text embeddings with fastembed (ARCHITECTURE.md §2: BAAI/bge-small-en-v1.5, 384-dim, CPU)."""

import asyncio
from functools import lru_cache

from fastembed import TextEmbedding

from app.core.config import BACKEND_DIR, get_settings

# Kept under backend/ (git-ignored) rather than the OS temp dir, which Windows may clean.
CACHE_DIR = BACKEND_DIR / ".cache" / "fastembed"


@lru_cache
def _model() -> TextEmbedding:
    return TextEmbedding(model_name=get_settings().embedding_model, cache_dir=str(CACHE_DIR))


def embed_texts(texts: list[str]) -> list[list[float]]:
    vectors = [v.tolist() for v in _model().embed(texts)]
    dim = get_settings().embedding_dim
    if vectors and len(vectors[0]) != dim:
        raise RuntimeError(f"{get_settings().embedding_model} returned {len(vectors[0])}-dim vectors, EMBEDDING_DIM={dim}")
    return vectors


async def aembed_texts(texts: list[str]) -> list[list[float]]:
    """CPU-bound; run off the event loop."""
    return await asyncio.to_thread(embed_texts, texts)