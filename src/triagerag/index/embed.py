from functools import lru_cache

import numpy as np
from sentence_transformers import SentenceTransformer

MODEL_NAME = "BAAI/bge-base-en-v1.5"
MAX_TOKENS = 512
# bge expects this prefix on queries only, never on documents
QUERY_PREFIX = "Represent this sentence for searching relevant passages: "


@lru_cache(maxsize=1)
def model() -> SentenceTransformer:
    return SentenceTransformer(MODEL_NAME)


def embed_query(text: str) -> np.ndarray:
    return model().encode(QUERY_PREFIX + text, normalize_embeddings=True)


def embed_documents(texts: list[str], batch_size: int = 32, show_progress: bool = True) -> np.ndarray:
    return model().encode(texts, batch_size=batch_size,
                          normalize_embeddings=True, show_progress_bar=show_progress)