"""Measure the search ranking (``czn_data.search``) on questions with known answers.

``search_eval.toml`` lists questions and the passages that answer them. For each, the search
runs against the database's real passages, each kind ranked on its own as the search tool shows
them; a case is found when an expected passage is among its kind's first TOP. Recall is the share of cases found; MRR (mean reciprocal rank) averages 1/rank
of the first expected passage, 0 when it is past MAX_RANK. The daily run exports both, so a
change in the data that buries answers shows on the dashboard.
"""

import time
import tomllib
from dataclasses import dataclass
from importlib.resources import files

import psycopg
from opentelemetry.metrics import Meter

from . import digest, metrics
from .search import ALL_SOURCES, KIND_SOURCES, kind_of, rank_by_kind, search

TOP = 5
MAX_RANK = 20


@dataclass
class CaseResult:
    kind: str  # the search kind of the case's first expected passage
    query: str
    rank: int | None  # of the first expected passage, from 1; None when past MAX_RANK
    top: list[str]  # the first TOP passages, as "source: title > heading"


@dataclass
class EvalResult:
    cases: list[CaseResult]

    @property
    def recall(self) -> float:
        return sum(c.rank is not None and c.rank <= TOP for c in self.cases) / len(self.cases)

    @property
    def mrr(self) -> float:
        return sum(1 / c.rank for c in self.cases if c.rank is not None) / len(self.cases)


STEP = "search_eval"


def _cases_text() -> str:
    return files(__package__).joinpath("search_eval.toml").read_text()


def cases() -> list[dict]:
    return tomllib.loads(_cases_text())["case"]


def fingerprint(conn: psycopg.Connection) -> str:
    """Fingerprint of what the evaluation measures: the cases, the search code, and every
    passage's searchable text. A passage's embedding is left out: it follows from its text and
    the embedding code, which the fingerprint holds."""
    (passages,) = conn.execute(
        """
        SELECT md5(string_agg(
            concat_ws(chr(31), source, key, position, title, heading, text, published_at),
            chr(30) ORDER BY source, key, position))
        FROM passage
        """
    ).fetchone()
    return digest.item(digest.code(__name__), _cases_text(), passages)


def _matches(passage: dict, expected: dict) -> bool:
    def holds(field: str) -> bool:
        wanted = expected.get(field)
        return wanted is None or wanted.casefold() in (passage[field] or "").casefold()

    return passage["source"] == expected["source"] and holds("title") and holds("heading")


async def evaluate(database_url: str) -> EvalResult:
    results = []
    async with await psycopg.AsyncConnection.connect(database_url) as conn:
        for case in cases():
            # Searched as the search tool does by default: every kind, each ranked on its own
            # (rank 1 is the full text it shows, 2 to TOP its one-line matches).
            by_kind = rank_by_kind(await search(conn, ALL_SOURCES, case["query"]))
            ranks = [
                n
                for rows in by_kind.values()
                for n, passage in enumerate(rows[:MAX_RANK], 1)
                if any(_matches(passage, e) for e in case["expect"])
            ]
            rank = min(ranks, default=None)
            top = [
                f"{p['source']}: {p['title']}" + (f" > {p['heading']}" if p["heading"] else "")
                for rows in by_kind.values()
                for p in rows[:TOP]
            ]
            kind = kind_of(case["expect"][0]["source"])
            results.append(CaseResult(kind, case["query"], rank, top))
    return EvalResult(results)


def export_metrics(result: EvalResult) -> bool:
    """Send recall and MRR, overall and per search kind (see ``czn_data.metrics``); False if
    the export failed."""

    def record(meter: Meter) -> None:
        recall = meter.create_gauge("czn.search.recall", description=f"Share found in top {TOP}")
        mrr = meter.create_gauge("czn.search.mrr", description="Mean reciprocal rank")
        recall.set(result.recall)
        mrr.set(result.mrr)
        for kind in KIND_SOURCES:
            part = EvalResult([c for c in result.cases if c.kind == kind])
            if part.cases:
                recall.set(part.recall, {"kind": kind})
                mrr.set(part.mrr, {"kind": kind})
        meter.create_gauge("czn.search.last_run.timestamp", unit="s").set(time.time())

    return metrics.export("czn_data.search", record)
