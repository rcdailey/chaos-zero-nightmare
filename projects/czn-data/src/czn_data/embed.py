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


def passage_text(post_title: str, heading: str | None, text: str) -> str:
    return f"{post_title}\n{heading or ''}\n{text[:PASSAGE_CHARS]}"


@cache
def _model():
    from fastembed import TextEmbedding

    return TextEmbedding(MODEL)


def passages(texts: Iterable[str]) -> list[list[float]]:
    return [vector.tolist() for vector in _model().passage_embed(list(texts))]


def query(text: str) -> list[float]:
    return next(iter(_model().query_embed(text))).tolist()
