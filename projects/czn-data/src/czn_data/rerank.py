"""Relevance scores for (query, passage) pairs from a cross-encoder, which reads the query and
the passage together and so ranks far better than comparing two separate embeddings; it is too
slow to score every passage, so the search tools use it to reorder their candidates.

The model (Xenova/ms-marco-MiniLM-L-6-v2, 80 MB, CPU) runs locally through fastembed, which
keeps it in ``FASTEMBED_CACHE_PATH`` like the embedding model (see ``czn_data.embed``).
"""

from functools import cache

MODEL = "Xenova/ms-marco-MiniLM-L-6-v2"
# Characters of a passage the model reads: its 512-token window, with room for the query.
PASSAGE_CHARS = 1_500
# Pairs scored at once. fastembed's default of 64 full-length pairs peaked near 3.4 GB of
# memory, over the pods' limits; 4 peaks near 600 MB with both search models loaded, no slower.
BATCH_SIZE = 4


@cache
def _model():
    from fastembed.rerank.cross_encoder import TextCrossEncoder

    # See czn_data.identity for why the memory arena is off.
    return TextCrossEncoder(MODEL, enable_cpu_mem_arena=False)


def scores(query: str, passages: list[str]) -> list[float]:
    """Each passage's relevance to query; higher is more relevant, on no fixed scale."""
    if not passages:
        return []
    texts = [p[:PASSAGE_CHARS] for p in passages]
    return [float(s) for s in _model().rerank(query, texts, batch_size=BATCH_SIZE)]
