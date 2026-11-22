"""Meaning-based vectors for post sections and search queries, so a query finds a section that
says the same thing in other words.

The model (BAAI/bge-small-en-v1.5, 67 MB, CPU) runs locally through fastembed. fastembed keeps
it in ``FASTEMBED_CACHE_PATH``; the image downloads it at build time and sets
``HF_HUB_OFFLINE`` so a running container never fetches it. Vectors are unit length, so a dot
product is their cosine similarity.

Only a section's start is embedded (``passage_text``): on the real posts, whole sections
diluted the topic, and the heading plus the first lines ranked the wanted sections far higher.
"""

from collections.abc import Iterable
from functools import cache

MODEL = "BAAI/bge-small-en-v1.5"
PASSAGE_CHARS = 300
# fastembed's default of 256 texts per batch peaks near 1.2 GiB; 32 peaks near 370 MiB (about
# 270 MiB of that is the model).
PASSAGE_BATCH_SIZE = 32


def passage_text(post_title: str, heading: str | None, text: str) -> str:
    return f"{post_title}\n{heading or ''}\n{text[:PASSAGE_CHARS]}"


@cache
def _model():
    from fastembed import TextEmbedding

    # See czn_data.identity for why the memory arena is off.
    return TextEmbedding(MODEL, enable_cpu_mem_arena=False)


def passages(texts: Iterable[str]) -> list[list[float]]:
    vectors = _model().passage_embed(list(texts), batch_size=PASSAGE_BATCH_SIZE)
    return [vector.tolist() for vector in vectors]


def query(text: str) -> list[float]:
    return next(iter(_model().query_embed(text))).tolist()
