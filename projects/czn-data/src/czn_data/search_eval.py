"""Measure the search ranking (``czn_data.search``) on questions with known answers.

``search_eval.toml`` lists questions and the passages that answer them. For each, the search
runs against the database's real passages; a case is found when an expected passage is among
the first TOP. Recall is the share of cases found; MRR (mean reciprocal rank) averages 1/rank
of the first expected passage, 0 when it is past MAX_RANK. The daily run exports both, so a
change in the data that buries answers shows on the dashboard.
"""

import time
import tomllib
from dataclasses import dataclass
from importlib.resources import files

import psycopg
from opentelemetry.metrics import Meter

from . import metrics
from .search import TOOL_SOURCES, search

TOP = 5
MAX_RANK = 20


@dataclass
class CaseResult:
    tool: str
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


def cases() -> list[dict]:
    return tomllib.loads(files(__package__).joinpath("search_eval.toml").read_text())["case"]


def _matches(passage: dict, expected: dict) -> bool:
    def holds(field: str) -> bool:
        wanted = expected.get(field)
        return wanted is None or wanted.casefold() in (passage[field] or "").casefold()

    return passage["source"] == expected["source"] and holds("title") and holds("heading")


async def evaluate(database_url: str) -> EvalResult:
    results = []
    async with await psycopg.AsyncConnection.connect(database_url) as conn:
        for case in cases():
            ranked = await search(conn, TOOL_SOURCES[case["tool"]], case["query"])
            rank = next(
                (
                    n
                    for n, passage in enumerate(ranked[:MAX_RANK], 1)
                    if any(_matches(passage, e) for e in case["expect"])
                ),
                None,
            )
            top = [
                f"{p['source']}: {p['title']}" + (f" > {p['heading']}" if p["heading"] else "")
                for p in ranked[:TOP]
            ]
            results.append(CaseResult(case["tool"], case["query"], rank, top))
    return EvalResult(results)


def export_metrics(result: EvalResult) -> bool:
    """Send recall and MRR, overall and per tool (see ``czn_data.metrics``); False if the
    export failed."""

    def record(meter: Meter) -> None:
        recall = meter.create_gauge("czn.search.recall", description=f"Share found in top {TOP}")
        mrr = meter.create_gauge("czn.search.mrr", description="Mean reciprocal rank")
        recall.set(result.recall)
        mrr.set(result.mrr)
        for tool in TOOL_SOURCES:
            part = EvalResult([c for c in result.cases if c.tool == tool])
            if part.cases:
                recall.set(part.recall, {"tool": tool})
                mrr.set(part.mrr, {"tool": tool})
        meter.create_gauge("czn.search.last_run.timestamp", unit="s").set(time.time())

    return metrics.export("czn_data.search", record)
